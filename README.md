# AI問題集（ai-quiz）

高校生向けの勉強用Webアプリです。デスクトップPCのローカルLLM（Ollama）が作った問題のうち、**検証に合格した問題だけ**をストックに入れて出題します。家庭内LANのスマホから使えます。

- 今できること（フェーズ1）：数学（数学IIを中心に、数学I・Aの復習、数学B・Cを少し。20種類の小単元。レベルは高校基礎・共通テスト）
- 問題の形式：スマホ向けの選択式／キーボード入力（数値・数式。sympy で同値判定）
- 解いた問題は、正誤・当時の回答・かかった時間つきで見返し、再挑戦できます
- デスクトップPCの電源が切れていても、ストックの問題を解く・見返す・採点するのは全部できます

仕様書は [docs/spec.md](docs/spec.md)、開発の引き継ぎは [CLAUDE.md](CLAUDE.md) にあります。

## しくみ

```
[スマホ / PC] ──家庭内LAN──▶ Raspberry Pi 4（NAS。常時稼働）
                               ├─ web      画面（:8000）。LLM は呼ばない
                               ├─ worker   作成依頼の処理と検証、毎日のバックアップ ──LAN──▶ デスクトップPCの Ollama
                               ├─ sandbox  答えの読み取りと sympy の同値判定（外部ネットワークなし）
                               └─ データ   NAS の HDD 上のフォルダ（quiz.db とバックアップ）
```

### 問題の作り方と検証（数学）

1. **Python（sympy）が数値・正解・誤答を作る**。誤答は「符号ミス」「公式の取り違え」などよくある間違いを実際に計算して作り、それぞれに間違いの種類の印を付けます
2. 誤答が正解とも互いとも一致しないことを sympy で確認します（見た目が同じものも重複とみなします）
3. **LLM は問題文・ヒント・解説の文章だけを書きます**
4. 問題文にテンプレートの数値・式がそのまま入っているか、解説に正解が入っているかを機械的に確認します
5. **検証役の LLM に、正解も選択肢も見せずに問題文だけで解かせ**、答えを sympy で正解と突き合わせます。決めた回数（初期値 2 回）すべて一致した問題だけがストックに入ります

1 回の依頼分は「先に全問の文章を作る → まとめて検証する」の順に処理します（GPU 8GB でモデルの入れ替えを減らすため）。
デスクトップPCが止まっているときは、依頼は「Ollama を待っています」のまま残り、つながったら続きから進みます。

---

## 導入（Raspberry Pi に入れる）

以下のコマンドは、Pi に SSH でログインして 1 行ずつ実行します。`$` は入力しません。
**HDD のパスなど、この README に書いてある値はこの家の環境に合わせてあります。**

### 0. 事前に確認すること

- デスクトップPC の Ollama に、使うモデルがダウンロード済みであること（Windows の PowerShell で）

  ```powershell
  ollama list
  ```

  `gemma4:12b-it-qat` と `gemma4:e4b` がなければ取得します。

  ```powershell
  ollama pull gemma4:12b-it-qat
  ollama pull gemma4:e4b
  ```

- Pi の Open WebUI からデスクトップの Ollama が使えていること（LAN からの接続設定が済んでいる確認になります）
- 以前 Windows の Docker Desktop で動かしていた試運転版は不要です。止めたいときは、Windows の PowerShell で試運転版のフォルダに移動して `docker compose down` を実行します

### 1. Pi にログインする

PC のターミナル（Windows なら PowerShell）から：

```
ssh <Piのユーザー名>@<PiのIPアドレス>
```

### 2. git と Docker が使えるか確認する

```
docker compose version
```

`Docker Compose version v2.xx` と出れば OK です。`permission denied` と出たら、自分のユーザーを docker グループに入れて、ログインし直します。

```
sudo usermod -aG docker $USER
exit
```

（`exit` のあと、もう一度 `ssh <Piのユーザー名>@<PiのIPアドレス>` でログインし、`docker compose version` をやり直します）

次に git を確かめます。

```
git --version
```

`git version 2.xx` と出れば入っています。「command not found」と出たら入れます。

```
sudo apt update
sudo apt install -y git
```

### 3. GitHub から取り出すための鍵（デプロイキー）を作る

このリポジトリは非公開なので、Pi が GitHub から読み出すための鍵を作って登録します。
デプロイキーは「このリポジトリだけ」「読み取りだけ」に使える鍵です。

1. Pi で鍵を作る（質問は出ません）

   ```
   mkdir -p ~/.ssh
   chmod 700 ~/.ssh
   ssh-keygen -t ed25519 -C "ai-quiz-pi" -f ~/.ssh/ai_quiz_deploy -N ""
   ```

2. 公開鍵を表示して、出てきた 1 行（`ssh-ed25519 AAAA… ai-quiz-pi`）をまるごとコピーする

   ```
   cat ~/.ssh/ai_quiz_deploy.pub
   ```

3. ブラウザで GitHub を開き、リポジトリ `sfns50f-bit/ek9` の **Settings → Deploy keys → Add deploy key** を押す
   - Title：`raspberry-pi`
   - Key：さっきコピーした 1 行を貼り付ける
   - **Allow write access のチェックは付けない**（読み取りだけにする）
   - **Add key** を押す

4. Pi で、この鍵を GitHub に使う設定を書く

   ```
   nano ~/.ssh/config
   ```

   開いた画面に次の 5 行を貼り付け、`Ctrl+O` → `Enter` で保存、`Ctrl+X` で閉じます。

   ```
   Host github-ai-quiz
       HostName github.com
       User git
       IdentityFile ~/.ssh/ai_quiz_deploy
       IdentitiesOnly yes
   ```

   ```
   chmod 600 ~/.ssh/config
   ```

5. つながるか確かめる

   ```
   ssh -T github-ai-quiz
   ```

   初回は `Are you sure you want to continue connecting (yes/no/[fingerprint])?` と聞かれるので `yes` と入力します。
   `Hi sfns50f-bit/ek9! You've successfully authenticated, but GitHub does not provide shell access.` と出れば成功です。

### 4. リポジトリを取り出す（git clone）

```
cd ~
git clone git@github-ai-quiz:sfns50f-bit/ek9.git ai-quiz
cd ~/ai-quiz
ls
```

`docker-compose.yml` や `README.md` が見えれば成功です。以後の操作はすべて `~/ai-quiz` フォルダで行います。

### 5. データの場所を用意する（HDD 上）

1. HDD がマウントされているか確かめる

   ```
   findmnt /srv/dev-disk-by-uuid-df337903-0195-4c8c-8237-49c1f271a27c
   ```

   `TARGET` と `SOURCE`（`/dev/sda1` など）が 1 行表示されれば OK です。何も出なければ HDD がマウントされていないので、OpenMediaVault の画面で確認してください。

2. データのフォルダとバックアップのフォルダを作る

   ```
   sudo mkdir -p /srv/dev-disk-by-uuid-df337903-0195-4c8c-8237-49c1f271a27c/ai-quiz/backups
   ```

3. 目印ファイルを置く（**これがないとアプリは起動しません**。停電後に HDD より先にアプリが起動して、空のデータで動き出すのを防ぐためです）

   ```
   sudo touch /srv/dev-disk-by-uuid-df337903-0195-4c8c-8237-49c1f271a27c/ai-quiz/.ai-quiz-data
   ```

4. アプリ（コンテナの中の利用者番号 10001）が書き込めるようにする

   ```
   sudo chown -R 10001:10001 /srv/dev-disk-by-uuid-df337903-0195-4c8c-8237-49c1f271a27c/ai-quiz
   ls -la /srv/dev-disk-by-uuid-df337903-0195-4c8c-8237-49c1f271a27c/ai-quiz
   ```

   `.ai-quiz-data` と `backups` が見えれば OK です。

### 6. 設定ファイル（.env）を作る

```
cd ~/ai-quiz
cp .env.example .env
nano .env
```

`OLLAMA_BASE_URL=http://192.168.x.x:11434` の `192.168.x.x` を、デスクトップPC の固定 IP アドレスに書き換えます（Open WebUI に設定してある Ollama の URL と同じです）。
`DATA_PATH` は上で作ったフォルダになっているので、そのままで構いません。
`Ctrl+O` → `Enter` で保存、`Ctrl+X` で閉じます。`.env` は git で管理しないファイルなので、更新を取り込んでも消えません。

### 7. メモリの上限が効くか確かめる

Pi は OpenMediaVault・Open WebUI と共用なので、このアプリのコンテナにはメモリの上限（web 256MB、worker 256MB、sandbox 512MB）を付けています。

```
docker info 2>&1 | grep -i "memory limit"
```

何も出なければ OK です。`WARNING: No memory limit support` と出たら、上限が効いていません。次の手順で有効にします。

```
sudo nano /boot/firmware/cmdline.txt
```

（このファイルがなければ `sudo nano /boot/cmdline.txt`）
1 行だけのファイルなので、**行の最後に半角スペースを 1 つ空けて** `cgroup_enable=memory cgroup_memory=1` を書き足し（改行しない）、保存して Pi を再起動します。

```
sudo reboot
```

### 8. 起動する

```
cd ~/ai-quiz
docker compose up -d --build
```

初回は部品のダウンロードとビルドに 10〜20 分ほどかかります。終わったら状態を確認します。

```
docker compose ps
```

`ai-quiz-web-1`・`ai-quiz-worker-1`・`ai-quiz-sandbox-1` の 3 つが `Up`（web と sandbox は `healthy`）になれば起動しています。

```
docker stats --no-stream
```

で `MEM USAGE / LIMIT` の LIMIT が 256MiB / 512MiB になっていれば、メモリの上限も効いています。

ブラウザ（スマホでも）で `http://<PiのIPアドレス>:8000` を開きます。
右上の「状態」で、Ollama・worker・sandbox が ○ になっていることを確認してください（デスクトップPC の電源が切れていると Ollama は × ですが、問題を解くのには影響しません）。
スマホではホーム画面に追加しておくと便利です。**ルーターでポート 8000 を開放しないでください**（家庭内だけで使います）。

---

## 使い方

1. **問題を作る**（「作る」）：科目・範囲・レベル・形式・問題数を選んで「作成を依頼する」。順番待ちに入り、デスクトップPC の Ollama につながると順に作成・検証されます。進み具合は同じ画面に出ます
   - **範囲**：「範囲：…」を押すと単元の一覧が開きます。単元にチェックを付けると、その単元を全部選びます（複数選べます）。単元の下の「小単元」を開くと、小単元ごとにも選べます。何も選ばなければ科目全体です。単元・小単元の横の数は、まだ解いていないストックの数です
   - **レベル**：中学／高校基礎／共通テスト／入試標準。選ばなければすべて。今の数学のテンプレートは高校基礎と共通テストだけです（中学・入試標準は今後追加）
   - **モデル**：「モデル：作成 …（変える）」を開くと、この依頼で使う作成役と検証役（回数・別系統も）を選べます。初期値は「状態 → モデルと思考モード」の設定です。依頼したときのモデルで最後まで処理します（後で設定を変えても、その依頼は変わりません）
2. **解く**（ホーム）：科目・範囲・レベル・端末（スマホ／キーボード）・問題数（初期値 20 問）を選んで「はじめる」。範囲とレベルの選び方は「作る」と同じです。1 問ずつ表示され、答えるたびに正誤と解説が出ます。前に間違えた問題も少し混ざります。選んだ範囲の小単元からなるべく均等に選び、同じ小単元の問題が続かないように並べます
   - スマホ：選択式だけが出ます
   - キーボード：キーボード入力と選択式が出ます
3. **見返す**（「履歴」）：解いた問題の一覧。問題を開くと、当時の回答と「再挑戦する」ボタンがあります。まだ解いていない問題は（正解が見えてしまうので）開けません
4. **この問題おかしい？**：問題の下にあるボタンから報告できます。報告された問題は出題されず、記録の集計からも外れます。「状態」の画面に一覧が出るので、確認して問題なければ戻せます
5. **作成の取り消し**：「作る」または「状態」の依頼の「取り消す」で、処理中の依頼も数秒以内に止まります（「取り消し中」→「取り消し済み」）。デスクトップの GPU での生成も止まります。それまでに検証に合格した問題はストックに残ります
6. **状態**（「状態」）：上の切り替えで 5 つのページに分かれています
   - **まとめ**：Ollama・worker・sandbox の状態、ストック残数（少ないと赤くなります。ホームにも出ます）、最近の依頼、バックアップ、「おかしい？」と報告された問題
   - **モデルと思考モード**：作成・検証に使うモデルと検証役に解かせる回数の初期値（「作る」画面の初期値になります）。正しい問題まで弾かれすぎる（「検証役の答えが正解と一致しない」が多い）ときは、検証役を `gemma4:12b-it-qat` に変えてみてください
     - 思考モード：モデルごとに「自動／切る／切らない」を選べます。選んだ設定はモデルごとに保存されます。「自動」は qwen3 系の思考を切り、gemma4 はそのままにします。一覧には Ollama に今あるモデルだけが出ます（消したモデルの設定も残っているので、入れ直せば同じ設定が効きます）
     - 検証のテスト：わざと正解をずらした問題を流して、検証役との突き合わせで弾かれることを確かめられます（ストックには入りません）。「期待どおり検証で弾かれました」と出れば成功です。「判定できませんでした」と出たときは、もう一度流してください
   - **検証の成績**：期間（24 時間・7 日・30 日・すべて）と「この依頼番号以降だけ」で絞り込めます（直す前の古いデータを外すときに使います）。作成役のモデルごとの合格率と不合格の内訳、検証役のモデルごとの弾いた割合、**依頼ごとの比較**（作成役・検証役、合格／作成、1 問あたりの作成時間・検証時間、モデルの読み込み時間、不合格の内訳）、その期間の不合格の一覧（作成役・検証役のモデル名、問題文、生の出力）
   - **LLM の呼び出し**：最近の LLM の呼び出しごとの時間の内訳（遅いときの原因を見分けるため）
   - **問題検証モード**：下の 7
7. **問題検証モード**（「状態」→「問題検証モード」。親向け）：ストックの問題を正解つきで確かめる画面です（この版を Pi に入れる手順は「更新を取り込む」と同じです）
   - 科目・単元・レベル・作成役のモデル・状態（ストック／削除した問題／検証で不合格）で絞り込めます
   - 問題ごとに、選択肢（誤答には間違いの種類）・正解・ヒント・解説・作成役と検証役のモデルが出ます
   - **削除する**：その問題は今後出題しません（回答の記録は残ります）。「削除した問題」で絞り込むと「ストックに戻す」で戻せます
   - **Markdown で表示／保存**：絞り込んだ問題を、Claude などのチャットにそのまま貼れる形で書き出します。冒頭に「高校生向けの問題として、正確さ・分かりやすさ・難易度の適切さを評価してください。」が付きます
   - パスワード：初期はパスワードなしで入れます。子供に見られたくなければ、下の「問題検証モードにパスワードをかける」の手順で設定してください（ほかの画面にはかかりません）

### 問題検証モードにパスワードをかける

1. Pi で設定ファイルを開く

   ```
   cd ~/ai-quiz
   nano .env
   ```

2. 次の 1 行を探して、`=` の後ろにパスワードを書く（行がなければ最後に足す）

   ```
   REVIEW_PASSWORD=ここにパスワード
   ```

   `Ctrl + O` → `Enter` で保存、`Ctrl + X` で閉じます。パスワードには英数字だけを使ってください（`$` や `#` は docker compose が特別な意味に読みます）

3. 設定を反映する

   ```
   docker compose up -d
   ```

パスワードを外すときは、`REVIEW_PASSWORD=` の後ろを空にして、もう一度 `docker compose up -d` を実行します。一度入ると、その端末では 30 日間パスワードを聞かれません（「問題検証モードを出る」で出られます）。

---

## 起動・停止

いずれも `cd ~/ai-quiz` してから実行します。

| したいこと | コマンド |
|---|---|
| 状態を見る | `docker compose ps` |
| 止める | `docker compose stop` |
| 止めたものを動かす | `docker compose start` |
| 作り直して起動（設定を変えたとき） | `docker compose up -d` |
| 完全に止めてコンテナを消す（データは消えない） | `docker compose down` |
| ログを見る | `docker compose logs --tail 50 web` （`web` を `worker` や `sandbox` に変えると、それぞれのログ） |

Pi を再起動すると、アプリも自動で起動します（`restart: unless-stopped`）。`docker compose stop` で止めたときは自動では起動しません。

### 停電のあと

停電から復帰したとき、HDD のマウントより先にアプリが起動することがあります。そのときは目印ファイルが見えないので、アプリは何も書かずに終了し、Docker が少し後に再起動します。HDD がマウントされれば自動で正常に動き出します。
しばらくしても動かないときは、ログに理由が出ています。

```
cd ~/ai-quiz
docker compose logs --tail 20 web
```

`目印ファイル .ai-quiz-data がありません` と出ていたら、「5. データの場所を用意する」の 1 で HDD がマウントされているか確認してください。

データベース（SQLite）は、書き込み途中で電源が切れても壊れにくい設定（WAL ＋ synchronous=FULL）にしています。

---

## 更新を取り込む

開発（Claude Code）は GitHub 上のブランチで行い、**プルリクエスト**として届きます。

1. ブラウザで GitHub のリポジトリ `sfns50f-bit/ek9` を開き、**Pull requests** タブから該当のプルリクエストを開く
2. 内容を確認して、緑の **Merge pull request** → **Confirm merge** を押す（main ブランチに取り込まれます）
3. Pi で取り込んで作り直す

   ```
   cd ~/ai-quiz
   git status
   ```

   `nothing to commit, working tree clean` と出ることを確認します（`.env` は表示されません）。

   ```
   git pull
   docker compose up -d --build
   docker compose ps
   ```

4. 古いイメージを消して、SD カードの空きを保つ（更新のたびに古いものがたまるため）

   ```
   docker image prune -f
   ```

`git status` でファイルが変更済み（`modified:`）と出たときは、Pi の上で直接ファイルを変えてしまっています。元に戻してよければ `git checkout -- <ファイル名>` を実行してから `git pull` します。分からなければ、そのまま開発者（Claude Code）に `git status` の結果を見せてください。

---

## バックアップ

- worker が **1 日 1 回**、`/srv/dev-disk-by-uuid-df337903-0195-4c8c-8237-49c1f271a27c/ai-quiz/backups/` に `quiz-年月日-時分秒.db`（日本時間）を作り、新しい **14 個**を残します（`.env` の `BACKUP_KEEP` / `BACKUP_INTERVAL_HOURS` で変更可）
- 最新のバックアップの時刻は「状態」画面で確認できます

いますぐ取るとき：

```
cd ~/ai-quiz
docker compose exec worker python -m quizapp.backup
```

一覧を見るとき：

```
ls -l /srv/dev-disk-by-uuid-df337903-0195-4c8c-8237-49c1f271a27c/ai-quiz/backups
```

バックアップは本体と同じ HDD にあるので、HDD そのものの故障には備えられません。ときどき別の場所（PC など）へコピーしておくと安心です。例：PC の PowerShell から

```powershell
scp <Piのユーザー名>@<PiのIPアドレス>:/srv/dev-disk-by-uuid-df337903-0195-4c8c-8237-49c1f271a27c/ai-quiz/backups/quiz-20261018-030000.db .
```

（ファイル名は一覧で確かめたものに置き換えます）

## 復元（バックアップから戻す）

例として `quiz-20261018-030000.db` に戻す手順です。ファイル名は一覧で確かめたものに置き換えてください。

1. アプリを止める

   ```
   cd ~/ai-quiz
   docker compose down
   ```

2. 今のデータベースを、関連するファイルごと `old` フォルダへ移して残す（念のため）

   ```
   sudo mkdir -p /srv/dev-disk-by-uuid-df337903-0195-4c8c-8237-49c1f271a27c/ai-quiz/old
   sudo mv /srv/dev-disk-by-uuid-df337903-0195-4c8c-8237-49c1f271a27c/ai-quiz/quiz.db* /srv/dev-disk-by-uuid-df337903-0195-4c8c-8237-49c1f271a27c/ai-quiz/old/
   ls -la /srv/dev-disk-by-uuid-df337903-0195-4c8c-8237-49c1f271a27c/ai-quiz/old
   ```

   `quiz.db`（あれば `quiz.db-wal` と `quiz.db-shm` も）が `old` に移っていれば OK です。

3. バックアップをコピーして、アプリが書き込めるようにする

   ```
   sudo cp /srv/dev-disk-by-uuid-df337903-0195-4c8c-8237-49c1f271a27c/ai-quiz/backups/quiz-20261018-030000.db /srv/dev-disk-by-uuid-df337903-0195-4c8c-8237-49c1f271a27c/ai-quiz/quiz.db
   sudo chown 10001:10001 /srv/dev-disk-by-uuid-df337903-0195-4c8c-8237-49c1f271a27c/ai-quiz/quiz.db
   ```

4. 起動する

   ```
   docker compose up -d
   docker compose ps
   ```

問題なく動いたら、`old` フォルダは消して構いません（`sudo rm -r /srv/dev-disk-by-uuid-df337903-0195-4c8c-8237-49c1f271a27c/ai-quiz/old`）。

---

## 困ったとき

| 症状 | 確かめること |
|---|---|
| 「状態」で Ollama が × | デスクトップPC の電源と Ollama が起動しているか。`.env` の `OLLAMA_BASE_URL` が正しいか（変えたら `docker compose up -d`） |
| 依頼が「Ollama を待っています」のまま | 上と同じ。Ollama につながれば自動で進みます |
| 依頼が「失敗」「一部完了」 | 「状態」→「検証の成績」の依頼ごとの比較と不合格の一覧で理由を見る。検証役が弾きすぎなら、モデルや回数を変える |
| 依頼が「失敗」で「モデル … が Ollama に見つかりません」 | 選んだモデルがデスクトップPC の Ollama に入っていない。名前を確かめるか、`ollama pull <モデル名>` で入れてから依頼し直す |
| ストックが少ないと出る | 「作る」から作成を依頼する |
| キーボード入力で「sandbox につながりません」 | `docker compose ps` で sandbox が動いているか。止まっていれば `docker compose up -d` |
| 画面が開かない | `docker compose ps` と `docker compose logs --tail 50 web` |

## 設定（.env）

| 項目 | 既定値 | 説明 |
|---|---|---|
| `DATA_PATH` | （必須） | データの場所（HDD 上のフォルダ） |
| `WEB_PORT` | `8000` | 画面のポート |
| `OLLAMA_BASE_URL` | （必須） | デスクトップPC の Ollama の URL |
| `GEN_MODEL` | `gemma4:12b-it-qat` | 作成に使うモデルの初期値（「状態 → モデルと思考モード」の設定が優先。依頼ごとに「作る」画面でも選べる） |
| `VERIFY_MODEL` / `VERIFY_COUNT` | `gemma4:e4b` / `2` | 検証役のモデルと解かせる回数（同上） |
| `VERIFY_MODEL_2` | （空） | 別系統の検証役（1 回だけ追加で解かせる。同上） |
| `REVIEW_PASSWORD` | （空） | 問題検証モードのパスワード。空ならパスワードなし |
| `MAX_ATTEMPTS` | `3` | 1 問あたり何回まで作り直すか |
| `OLLAMA_RETRY_SECONDS` | `60` | Ollama につながらないときの再試行の間隔（秒） |
| `BACKUP_KEEP` / `BACKUP_INTERVAL_HOURS` | `14` / `24` | バックアップの世代数と間隔 |
| `LOW_STOCK` | `40` | 科目・端末ごとのストックがこれ未満で「少ない」と表示 |
| `RETRY_PERCENT` | `30` | 解き始めのセットに混ぜる「間違えた問題」の上限（%） |

## 開発者向け

テストの実行（Python 3.12 以上）：

```
python -m venv .venv
.venv/bin/pip install -r app/requirements.txt pytest
.venv/bin/python -m pytest
```

将来ミニPC（x86）へ移すときは、同じ手順（`git clone` → データのフォルダを移す → `.env` の `DATA_PATH` を直す → `docker compose up -d --build`）で動きます。イメージはその場でビルドするので、arm64 / x86 の違いは気にしなくて構いません。
