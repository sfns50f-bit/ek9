# CLAUDE.md（引き継ぎ用）

- 仕様書：[docs/spec.md](docs/spec.md)（アプリ全体の方針と決定事項。各フェーズの依頼文と食い違う場合は**依頼文を優先**する）
- 導入・運用手順：[README.md](README.md)（開発者は git に不慣れ。手順書のコマンドは省略しない）

## 構成

| コンテナ | 中身 | ネットワーク | メモリ上限 |
|---|---|---|---|
| web | FastAPI の画面（`app/quizapp/web.py`）。**LLM を呼ばない** | default（:8000 公開）＋ sandbox | 256MB |
| worker | 作成依頼の処理・検証（`worker.py`）、Ollama の状態確認、毎日のバックアップ（`backup.py`） | default（Ollama へ）＋ sandbox | 256MB |
| sandbox | 答えの文字列の読み取りと sympy の同値判定（`sandbox/sandbox_service`）。LLM のコードは実行しない | sandbox（`internal: true`）のみ。データのボリュームなし、root 以外、read_only | 512MB（1 回の計算 384MB・10〜20 秒） |

- 動かす場所：NAS の Raspberry Pi 4（arm64、OMV・Open WebUI と共用。8080 と 3030 は使用中、8000 は空き）。将来 x86 のミニPC へ移す（`python:3.12-slim` をその場でビルドするだけ）
- データ：HDD 上の `DATA_PATH`（例 `/srv/dev-disk-by-uuid-df337903-…/ai-quiz`）を `/data` にバインド。`quiz.db`（SQLite、WAL ＋ synchronous=FULL）と `backups/`（14 世代）
- LLM：デスクトップPC（Windows、GPU 8GB）の Ollama を LAN 経由（`OLLAMA_BASE_URL`）。普段は電源が切れている

### コードの地図（app/quizapp）

| ファイル | 役割 |
|---|---|
| `subjects/base.py` | **科目の窓口**（`Subject`：units / generate / verify / grade）。中心部分はここだけを使う |
| `subjects/__init__.py` | 科目の登録（`REGISTRY`）。科目を足すときはここに 1 行 |
| `subjects/math/` | 数学。`catalog.py`（単元・小単元・間違いの種類。sympy なし＝web 用）、`templates.py`（20 テンプレート。sympy）、`prompts.py`、`__init__.py`（MathSubject） |
| `worker.py` | 依頼の処理。**作成フェーズ（全問の文章）→ 検証フェーズ（まとめて）** の順。Ollama につながらなければ `waiting` にして再試行 |
| `stock.py` | ストック数、解き始めのセットの組み方（間違えた問題を 30% まで混ぜる）、/status の集計 |
| `textcheck.py` | 問題文にテンプレートの式・値が、解説に正解が「そのまま」入っているかの確認（LaTeX の揺れだけ吸収） |
| `runtime.py` | /status から変えるモデル・検証回数（kv 表。空なら .env） |
| `datadir.py` | 目印ファイル `.ai-quiz-data` がなければ何も書かずに終了（HDD マウント前の起動対策） |
| `db.py` | テーブル：users / passages（英語・公共用に用意済み）/ jobs / problems / study_sessions / attempts / kv |

## 守るルール

1. **子供が操作する画面（web）では LLM を一切呼ばない**。web は Ollama のクライアントを import しない（`tests/test_web.py::test_web_never_uses_the_llm`）。Ollama の状態は worker が kv に書いたものを表示するだけ
2. **検証を通った問題だけをストック（status=ready）に入れる**。検証役には正解も選択肢も見せずに問題文だけで解かせ、設定回数すべて一致したものだけ合格
3. **科目を足しても中心部分（worker / web / stock / db）を触らない**。`subjects/<科目>/` を作り `Subject` を実装して `REGISTRY` に登録する。本文（英文・資料）は `passages` 表、出典は `problems.source` と `passages.source_*`
4. 数学：数値・正解・誤答は Python（sympy）で作る。LLM は問題文・解説・ヒントだけ。誤答には `catalog.MISTAKES` の印を付ける。新しいテンプレートを足したら `tests/test_math_templates.py` の `CHECKS` に**別の計算方法での確認**を必ず足す
5. 問題文にテンプレートの式・値がそのまま入っているか、解説に正解が入っているかを機械的に確認する（`textcheck`）
6. sandbox：`internal: true`、メモリ・CPU・時間の上限、root 以外、データをマウントしない。**全コンテナにメモリ上限**を付ける
7. 停電対策：目印ファイルの確認を外さない。データのバインドは short syntax（`- ${DATA_PATH}:/data`）のままにする（long syntax だと HDD 未マウント時にコンテナが作れず、Docker の自動再起動が効かない）
8. 回答記録（attempts）の項目は消さない：日時・かかった時間（サーバー時計）・何回目か・ヒント/解説を見たか・選んだ選択肢と間違いの種類・端末・利用者・科目/単元/小単元/形式
9. **フェーズの最後（プルリクエストを作る前）に一度だけ、実装を見ていない別のサブエージェントに、docs/spec.md・そのフェーズの依頼文・完了の確認項目と照らしたレビューをさせる**。指摘があれば直してから、指摘の内容と対応を開発者に報告する。途中の区切りごとの確認は自動テスト（`python -m pytest`）で行う
10. フェーズの終わりに CLAUDE.md と README を更新する。README のコマンドは省略しない
11. コミットとプッシュは指定のブランチへ。プルリクエストは開発者の依頼があってから作る

## フェーズ1で決めたこと（依頼文の追加分を含む）

- 数学の範囲：子供は高校2年。数学II（三角関数・指数対数・微分積分・式と証明）中心に 12、数学I 4、数学A 2、数学B（数列）1、数学C（ベクトル）1 の計 20 小単元
- 端末：スマホ＝選択式だけ、キーボード＝キーボード入力＋選択式
- 解く画面：20 問のセットでも 1 問ずつ表示し、答えるたびに正誤と解説を出す。かかった時間は 1 問ごとに記録
- 作成依頼と解き始めの両方で、範囲を「科目全体／単元／小単元（テンプレート）」から選べる
- 選択式の誤答に間違いの種類の印を付け、回答記録に選んだ選択肢と印を残す（後のフェーズの得意・苦手分析用）
- 検証に使うモデルと回数を /status から変えられる。検証で弾かれた割合（検証役のモデルごと）と理由も /status に出す
- 1 回の依頼分は、先に全問の文章を作り、その後まとめて検証する（GPU 8GB でのモデルの入れ替えを減らす）
- 問題文にテンプレートの数値がそのまま入っているかを機械的に確認（解説の正解確認と同じ方法）
- 既定のモデル：作成 `gemma4:12b-it-qat`、検証 `gemma4:e4b` × 2 回。手元の候補：gemma4:12b-it-qat、gemma4:e4b、gemma4:26b-a4b-it-qat、qwen2.5:7b
- 「この問題おかしい？」が押された問題は、集計から外し、出題もしない（/status から戻せる）
- 間違えた問題の再出題はセットの 30% まで（`RETRY_PERCENT`）。ストック不足時はある分だけで始めて知らせる
- 「残りが少ない」は科目・端末ごとの未出題が 40 問未満（`LOW_STOCK`）
- 難易度の選択はなし（テンプレートごとに決まる）。利用者は既定の 1 人（id=1）。切り替え画面は後のフェーズ
- Pi への clone はデプロイキー（読み取り専用）で `git@github-ai-quiz:sfns50f-bit/ek9.git`
- 「わからない（解説を見る）」で答えた場合は `gave_up` と `explanation_viewed` を立てる。答えた後は必ず解説を表示するので、それは「見た」に数えない
- 検証のテスト：/status から「正解を +1 ずらした問題」を流せる（ストックに入らない）。全 20 テンプレートで弾かれることを自動テストで確認済み

## 進み具合

- [x] フェーズ1「新しい土台＋数学」（実装・自動テスト・Docker での動作確認まで済み。開発者の Pi での導入確認は README の手順で行う）
- [ ] フェーズ2 英語（共通テストレベルの読解・語彙）
- [ ] フェーズ3 公共（資料を取得して作る。worker からインターネットへ出る。sandbox は出さない）
- [ ] フェーズ4 作り方を増やす（キーワード指定、子供からのリクエスト、類題、時事問題。同じ jobs の順番待ちで処理）
- [ ] フェーズ5 学習機能（集計、間隔をあけた再出題、テストモードなど）
- [ ] フェーズ6 その他の科目（プログラミングは最後）

## 次にやること

1. 開発者が Pi に導入し、実際の Ollama で作成・検証を試す。/status の「検証の成績」で、e4b が正しい問題を弾きすぎていないか確認する（弾きすぎなら検証役を 12b にする）。実際の gemma4 が問題文に式をそのまま書き写せず `text_givens` で落ちることが多ければ、`subjects/math/prompts.py` を調整する
2. フェーズ2（英語）：`subjects/english/` を作り、本文を `passages` に入れる「本文 → 設問 → 本文だけで解けるか検証」の流れを実装する。英語の小単元（読解・語彙・スペル）を `units()` に、採点（選択式／英単語の完全一致）を `grade()` に。解く画面は `problem.passage` があれば本文を表示する（play.html に入口だけ用意済み。英文と設問を行き来しやすくする工夫はフェーズ2で）
3. 仕様書（docs/spec.md）は開発者が次のフェーズで更新版に差し替える

## テスト

```
python -m pytest
```

- `tests/test_math_templates.py`：20 テンプレートの正解を別の方法で確認、誤答の重複なし、キーボード入力での採点
- `tests/test_worker.py`：作成→検証の順番、Ollama 停止時の待機と再開、正解をずらした問題が弾かれる、/status の設定の反映
- `tests/test_web.py`：LLM なしで解ける、回答記録の項目、再出題の混ぜ方、報告、キーボード入力の欄の属性
- `tests/test_storage.py`：目印ファイルがなければ起動しない、SQLite の設定、再起動後もデータが残る、バックアップ
- テストの LLM（`tests/conftest.py::OracleLLM`）は、作成役は式をそのまま書き写し、検証役は seed からテンプレートを作り直して本当の正解を答える
