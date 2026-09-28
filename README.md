# AI問題集（ek9）

自宅のPCで動くローカルLLM（Ollama）が数学の問題を作り、**Python で検算し、別の解答者が解き直して答えが一致した問題だけ**を出題するWebアプリです。
家庭内LANのスマホやタブレットから使えます。解いた問題は、正誤と当時の回答つきで見返し・再挑戦できます。

- 対象：高校1年〜大学（数学I・A・II・B・C・III、大学数学）
- 答えは数式で入力し、SymPy で自動採点（`1/√2` と `√2/2`、`0.5` と `1/2` などは同じ答えとして扱う）
- 在庫方式：最近解いた単元の問題を空き時間に作り置きしておき、すぐ出題

> 現在は段階1（数学・検証パイプライン・履歴・Docker）です。時事問題や他教科は今後追加します（末尾の「今後の予定」）。

## しくみ

```
[スマホ / ノートPC] ──家庭LAN──▶ Windows のデスクトップPC
                                   ├─ Docker Desktop
                                   │   ├─ web      画面（:8000）
                                   │   ├─ worker   作問と検証
                                   │   └─ sandbox  Python の実行と採点（ネット遮断・時間/メモリ制限）
                                   └─ Ollama（Windows に直接インストール、Radeon の GPU を使う）
```

Docker Desktop のコンテナからは AMD の GPU を使えないため、Ollama だけは Windows に直接インストールします。

### 出題前の検証

1問ごとに次の順で確認し、どれか1つでも落ちたら作り直します（最大 `MAX_ATTEMPTS` 回）。

| 段階 | 内容 |
|---|---|
| 1. 作問 | 問題文・解説・答え・**検算用の Python コード**を JSON で出力させる |
| 2. Python検算 | 検算コードを隔離環境で実行し、結果が作問時の答えと一致するか確認する |
| 3. 独立解答 | 答えを見せずに問題文だけを渡して解かせる（Python でも解かせる）。答えが一致するか確認する |
| 4. 審査 | 条件不足・答えが複数・問いと答えのずれ・解説の誤りがないかを LLM が確認する |

画面に表示する正解は、Python で計算した値です。各問題の「検証記録」から、実行したコードや解答役の答えを確認できます。
それでも誤りが残ることはあるので、おかしな問題は画面の「問題の誤りを報告する」から報告してください。報告した問題は復習リストから外れます。

## セットアップ（Windows）

### 1. Ollama を入れてモデルを取得する

1. AMD Software（Adrenalin）のドライバーを最新にする
2. https://ollama.com から Windows 版をインストールする
3. PowerShell でモデルを取得して動作を確認する

   ```powershell
   ollama pull gemma4:e4b
   ollama run gemma4:e4b "1+1は？"
   ollama ps
   ```

   `ollama ps` の PROCESSOR が `100% GPU` なら GPU で動いています。

### 2. Docker Desktop を入れる

https://www.docker.com/products/docker-desktop/ からインストールします（WSL 2 を使う設定のまま）。

### 3. アプリを起動する

```powershell
git clone https://github.com/sfns50f-bit/ek9.git
cd ek9
copy .env.example .env    # 設定を変えるときだけ編集
docker compose up -d --build
```

PC のブラウザで http://localhost:8000 を開き、右上の「状態」で Ollama・モデル・サンドボックス・ワーカーがすべて OK になっていることを確認します。

### 4. スマホから使えるようにする

1. PowerShell で `ipconfig` を実行し、「IPv4 アドレス」（例: `192.168.1.23`）を確認する
2. 管理者の PowerShell でポート 8000 を家庭内ネットワークにだけ開ける

   ```powershell
   New-NetFirewallRule -DisplayName "AI問題集" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
   ```

   Windows の「設定 → ネットワークとインターネット」で、接続中のネットワークが「プライベート」になっていることも確認してください。
3. スマホで `http://192.168.1.23:8000` を開く（ホーム画面に追加すると便利です）

PC がスリープすると使えなくなります。使う時間帯は「電源とスリープ」でスリープしないようにしてください。
なお、数式の表示（KaTeX）はインターネットから読み込むため、見る側の端末はインターネットにつながっている必要があります。

## 使い方

- **出題**：ホームで科目・単元・難易度を選んで「出題する」。在庫があればすぐ、なければ作問から始まります（数分〜十数分）。
- **解答**：答えを入力すると、下にどう読み取ったかが数式で表示されます。記号ボタン（√ π ^ など）も使えます。
- **復習**：「復習」には最後に間違えた問題が並びます。「再挑戦」で同じ問題をもう一度解けます。
- **状態**：モデルの有無、作業中の内容、在庫、検証で破棄された問題とその理由を確認できます。ゲームなどで GPU を使うときは、ここで在庫の自動補充を一時停止できます。

### 答えの書き方

| 答えの形式 | 入力例 |
|---|---|
| 数・式 | `3/2`　`-sqrt(3)/2`　`2√3`　`2x^2+1`　`pi/6`　`log(3)`　`e^2`　`3+4i` |
| 複数の値（順不同） | `1, -3`　`x = (1±√5)/2` |
| 順序のある組 | `(2, -1)`　`x=2, y=-1` |
| 行列 | `1, 2; 3, 4`（行を `;` で区切る） |
| 範囲（不等式の解） | `1<x<3`　`x<=-1, 2<x`（カンマは「または」）　`解なし`　`すべての実数` |
| 不定積分 | `x^3/3 + sin(x)`（`+ C` はあってもなくてもよい） |

全角の数字や記号（`＋`、`≦`、`π`、`√` など）もそのまま使えます。
`0.7071` のような小数の近似値は不正解になり、「厳密な値で答えてください」と表示されます。

## モデルの選び方（VRAM 8GB / RX 7600 の場合）

| 設定 | 特徴 |
|---|---|
| `OLLAMA_MODEL=gemma4:e4b`（既定） | GPU に全部載るので速い。まずはこれで試す |
| `GEN_MODEL=gemma4:12b` を追加 | 作問だけ大きいモデルにする。VRAM に入りきらない分は CPU で動くので遅くなるが、問題の質は上がりやすい |
| 26B クラス | 大半が CPU で動くことになり、かなり遅い |

gemma4 は既定で考えてから答える（thinking）ため、1問あたり数分かかることがあります。
在庫の作り置き（`POOL_TARGET`）があるので、よく使う単元はすぐに出題されます。
「状態」ページの「最近の不合格」が多すぎるときは、モデルを大きくするか、難易度を下げてみてください。

## 設定（.env）

| 項目 | 既定値 | 説明 |
|---|---|---|
| `WEB_PORT` | `8000` | 画面のポート |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama の場所 |
| `OLLAMA_MODEL` | `gemma4:e4b` | 使うモデル |
| `GEN_MODEL` / `SOLVE_MODEL` / `REVIEW_MODEL` | （空） | 作問・解答・審査ごとにモデルを変えるとき |
| `OLLAMA_NUM_CTX` | `8192` | コンテキスト長 |
| `OLLAMA_NUM_PREDICT` | `6144` | 1回の最大出力トークン数（thinking を含む） |
| `OLLAMA_KEEP_ALIVE` | `15m` | 使い終わったモデルを VRAM から降ろすまでの時間 |
| `OLLAMA_THINK` | （空） | 空ならモデルの既定。gemma4 で `false` にすると JSON 出力の指定が効かなくなる不具合があります |
| `MAX_ATTEMPTS` | `4` | 検証に落ちたときに作り直す回数 |
| `REVIEW_ENABLED` | `true` | 審査の段階を行うか |
| `POOL_TARGET` | `2` | 単元・難易度ごとに作り置きする問題数 |
| `REFILL_ENABLED` | `true` | 在庫の自動補充 |
| `REFILL_DAYS` | `14` | 何日以内に使った単元を補充するか |

変更したら `docker compose up -d` で反映されます。

## 困ったとき

- **「Ollama に接続できません」**：Ollama が起動しているか（タスクトレイのアイコン）確認します。それでもだめなら、Windows の環境変数に `OLLAMA_HOST=0.0.0.0` を追加して Ollama を再起動してください（この場合、ポート 11434 をファイアウォールで外部に開けないでください）。
- **「モデルが見つかりません」**：`ollama pull <モデル名>` を実行します。
- **遅い**：`ollama ps` で GPU を使っているか確認します。CPU になっている場合は AMD のドライバーと Ollama を更新してください。
- **ログを見る**：`docker compose logs -f worker`

## データのバックアップと更新

問題と解答の記録は Docker のボリューム `quiz-data`（SQLite）に保存されます。`docker compose down` では消えません（`down -v` は消えます）。

```powershell
# バックアップ（カレントフォルダに quiz-backup.db ができる）
docker compose exec web python -c "import sqlite3; sqlite3.connect('/data/quiz.db').backup(sqlite3.connect('/data/backup.db'))"
docker compose cp web:/data/backup.db ./quiz-backup.db

# アプリの更新
git pull
docker compose up -d --build
```

## セキュリティ

- 画面にログインはありません。**家庭内LANだけで使い、ルーターのポート開放などでインターネットに公開しないでください。**
- LLM が書いた Python は `sandbox` コンテナで実行します。このコンテナはインターネットにもホストにも出られないネットワークにつながり、読み取り専用・権限なし・メモリ 1GB・1回あたり最大 20 秒に制限されています。

## 開発

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r app/requirements.txt -r sandbox/requirements.txt pytest
pytest
```

| パス | 内容 |
|---|---|
| `app/quizapp/pipeline.py` | 作問 → 検算 → 独立解答 → 審査 |
| `app/quizapp/prompts.py` | プロンプトと JSON スキーマ |
| `app/quizapp/worker.py` | 依頼の処理と在庫補充 |
| `app/quizapp/web.py`, `templates/`, `static/` | 画面 |
| `app/quizapp/topics.py` | 出題範囲（単元と内容） |
| `sandbox/sandbox_service/mathcheck.py` | 答えの読み取り・比較（SymPy） |
| `sandbox/sandbox_service/server.py` | 隔離実行の HTTP API |

## 今後の予定

- 段階2：他教科（物理・化学・英語・歴史など）の選択式問題
- 段階3：時事問題（RSS / SearXNG でニュースを集め、記事本文だけを根拠に作問）
- 段階4：複数ユーザー、苦手分析、間隔をあけた復習
