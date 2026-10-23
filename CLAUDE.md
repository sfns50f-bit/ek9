# CLAUDE.md（引き継ぎ用）

- 仕様書：[docs/spec.md](docs/spec.md)（アプリ全体の方針と決定事項。各フェーズの依頼文と食い違う場合は**依頼文を優先**する）
- 導入・運用手順：[README.md](README.md)（開発者は git に不慣れ。手順書のコマンドは省略しない）
- 引き継ぎメモ：[docs/handover.md](docs/handover.md)（これまでの経緯・現状・残課題の要約。新しいセッションはまずここから）
- **数学の作り方の詳細**：[docs/math.md](docs/math.md)（テンプレート、文章の確認、表記の直しと点検、答えの読み取り、プロンプト、実機とレビューの記録）。数学の問題を直すときはそこだけ見ればよい。このファイルには科目によらない決めごとを書く
- **英語の作り方の詳細**：[docs/english.md](docs/english.md)（小単元、読解の作り方と根拠の確認、本文なしの確認、単語リスト、英語だけの検証のルール、画面）

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
| `subjects/base.py` | **科目の窓口**（`Subject`：units / generate / verify / review / grade / text_issues / normalize_text、`math_text`）。中心部分はここだけを使う |
| `subjects/__init__.py` | 科目の登録（`REGISTRY`）。科目を足すときはここに 1 行 |
| `subjects/math/` | 数学（詳細は [docs/math.md](docs/math.md)）。`catalog.py`（単元・間違いの種類と、`templates.py` の `@template(...)` を **ast で読む** `template_info()`。sympy なし＝web 用）、`templates.py`（20 テンプレート。sympy。単元・小単元名・レベル・関連する単元は `@template` に書く）、`prompts.py`、`__init__.py`（MathSubject） |
| `subjects/english/` | 英語（詳細は [docs/english.md](docs/english.md)）。`catalog.py`（単元・小単元・誤答の種類・読解の題材）、`words.py`（単語リスト 1232 語）、`check.py`（英文の点検）、`prompts.py`、`__init__.py`（EnglishSubject） |
| `worker.py` | 依頼の処理。**作成フェーズ（全問の文章）→ 検証フェーズ（まとめて）** の順。Ollama につながらなければ `waiting` にして再試行。**順番待ちは 2 つ**（`lane`：ollama＝どれかが Ollama のモデル、api＝全部 API のモデル）を別スレッドで同時に処理 |
| `stock.py` | ストック数、解き始めのセットの組み方（間違えた問題を 30% まで混ぜる。小単元を均等に・続けない。同じ本文の設問はまとめて続けて出す）、/status の集計（期間・依頼番号での絞り込み、作成役ごと、依頼ごとの比較） |
| `textcheck.py` | **数学向け**。問題文にテンプレートの式・値が、解説に正解が「そのまま」入っているかの確認（LaTeX の揺れだけ吸収） |
| `latexfix.py` | **数学向け**（LaTeX）。LLM が書いた文章の表記をそろえる（`normalize`：度の記号の揺れ・二重のバックスラッシュ `\\sin`・文字どおりの `\n` など）・崩れを見つける（`problems`：KaTeX にない命令・$ の対応・数式の中の日本語・中国語の字など）。sympy なし（web の問題検証モードでも使う） |
| `runtime.py` | /status から変えるモデル・検証回数（kv 表。空なら .env）＝作る画面の初期値、依頼ごとのモデル（`plan_for_job` / `apply_plan`）、モデル名の「提供元/」の読み方（`provider_of`）、モデルごとの思考モード |
| `review.py` | 問題検証モード：パスワード（cookie）、絞り込み、品質チェック用の Markdown の書き出し、表記の点検と直し（中身は科目の `text_issues` / `normalize_text`） |
| `llm.py` | Ollama のクライアント。ストリーミングで受け取り、取り消し（`should_abort`）で接続を切る。**出力は JSON スキーマを使わず `[[項目]]` の見出しの平文**（`tagged_instructions` / `parse_tagged`。JSON で返ってきたときも読む）、呼び出しごとの時間の記録。`LLMRouter`（モデル名の「提供元/」で API に振り分ける。今は Ollama だけ） |
| `llm_anthropic.py` | Anthropic（Claude）の API のクライアント（`AnthropicClient`。worker だけが読む）。ストリーミング、取り消し、プロンプトキャッシュ、effort、断られたときの fallbacks、呼び出しごとの料金（`cost_usd`）、上限の確認（`budget_check`） |
| `datadir.py` | 目印ファイル `.ai-quiz-data` がなければ何も書かずに終了（HDD マウント前の起動対策） |
| `db.py` | テーブル：users / passages（英語の本文。公共の資料も入れる）/ jobs / problems / study_sessions / attempts / kv |

## 守るルール

1. **子供が操作する画面（web）では LLM を一切呼ばない**。web のプロセスでは Ollama のクライアント（`quizapp.llm`）も sympy も読み込まれない（`tests/test_web.py::test_web_process_does_not_load_the_llm_client`。科目の部品も web から読まれるので、`llm` を import するのは関数の中だけにする）。Ollama の状態は worker が kv に書いたものを表示するだけ
2. **検証を通った問題だけをストック（status=ready）に入れる**。検証役には正解も選択肢も見せずに問題文だけで解かせ、設定回数すべて一致したものだけ合格。**英語の選択式だけは例外**：読解の答えは文章で、選択肢なしでは機械的に突き合わせられないので、選択肢は見せ（正解は見せない。順番は毎回入れ替える）、記号で答えさせる（開発者と決めた英語の作成ルール。docs/english.md の 4 章）
3. **科目を足しても中心部分（worker / web / stock / db）を触らない**。`subjects/<科目>/` を作り `Subject` を実装して `REGISTRY` に登録する。本文（英文・資料）は `Draft.passage`（dict）に入れれば worker が `passages` 表に保存して問題と結び付ける。1 つの本文に複数の設問をつけるときは、`generate()` が同じ dict を持つ `Draft` のリストを返す（`tests/test_worker.py::test_passages_from_a_subject_are_saved_and_shared`）。検証役は `problem.passage` を読める。出典は `problems.source` と `passages.source_*`。検証のテスト（正解をずらす）に対応する科目は `test_format` を設定し、`ctx.is_test` のときに正解をずらす。**本文つきの科目のためにフェーズ2で中心部分に足したもの**（開発者と合意。英語に特有の処理は入れていない）：同じ本文の設問をセットでまとめて続けて出す（`stock.build_session`）、同じ本文の設問のうち作成の段階で使えないものを `Draft.rejected` で返すと worker が不合格として残す、`$` を数式に使わない科目の `Subject.math_text = False`（画面に `nomath` の class）、問題検証モードの点検と直しは `Subject.text_issues` / `normalize_text`
4. 検証役や子供に答えが見えないようにする：キーボード入力の答え方の例（`input_hint`）や検証役のプロンプトに具体的な値を書かない。問題文・ヒントに正解が書かれていたら弾く（数学は `answer_leak`、英語はヒントに正解の選択肢・語があれば直させる）。数学の採点・検証では「sin(5π/6)」のように式を書き写しただけの答えを受け付けない（`mathcheck._check_functions`）
5. 数学：数値・正解・誤答は Python（sympy）で作る。LLM は問題文・解説・ヒントだけ。誤答には `catalog.MISTAKES` の印を付ける。新しいテンプレートを足したら `tests/test_math_templates.py` の `CHECKS` に**別の計算方法での確認**を必ず足す（手順は docs/math.md の 9 章）
6. 正解の裏付けを機械的に確認する。数学は問題文にテンプレートの式・値がそのまま、解説に正解が入っているか（`textcheck`）、英語の読解は正解の根拠の文が本文にそのまま入っているか（`english/check.contains`）
7. sandbox：`internal: true`、メモリ・CPU・時間の上限、root 以外、データをマウントしない。**全コンテナにメモリ上限**を付ける
8. 停電対策：目印ファイルの確認を外さない。データのバインドは short syntax（`- ${DATA_PATH}:/data`）のままにする（long syntax だと HDD 未マウント時にコンテナが作れず、Docker の自動再起動が効かない）
9. 回答記録（attempts）の項目は消さない：日時・かかった時間（サーバー時計）・何回目か・ヒント/解説を見たか・選んだ選択肢と間違いの種類・端末・利用者・科目/単元/小単元/形式
10. **フェーズの最後（プルリクエストを作る前）に一度だけ、実装を見ていない別のサブエージェントに、docs/spec.md・そのフェーズの依頼文・完了の確認項目と照らしたレビューをさせる**。指摘があれば直してから、指摘の内容と対応を開発者に報告する。途中の区切りごとの確認は自動テスト（`python -m pytest`）で行う
11. フェーズの終わりに CLAUDE.md と README を更新する。README のコマンドは省略しない
12. コミットとプッシュは指定のブランチへ。プルリクエストは開発者の依頼があってから作る
13. **科目ごとの詳細は科目ごとのファイルに書く**（`docs/math.md`、英語は `docs/english.md` など）。このファイルには科目によらない決めごとだけを書き、科目のファイルへリンクする（仕様書の「開発の進め方」）


## 科目によらない決めごと

フェーズ1（数学）の間に決めたことのうち、ほかの科目にも関わるもの。数学だけの決めごと（テンプレート・`textcheck`・`latexfix`・答えの読み取り・プロンプトなど）は [docs/math.md](docs/math.md)。いつ・なぜ決めたかは [docs/handover.md](docs/handover.md) の経緯と git の履歴。

### 作成と検証の流れ

- 1 回の依頼分は、先に全問の文章を作り、その後まとめて検証する（GPU 8GB でのモデルの入れ替えを減らす）。作成依頼は小単元を順番に回す
- 検証役には正解も選択肢も見せず、問題文（と本文）だけで解かせる。設定回数すべて一致したものだけ合格（英語の選択式だけは選択肢を見せる。ルール 2）
- 別系統の検証役は「使わない」を推奨（実機で正しい問題まで弾きすぎた）
- 解説の見直し（`Subject.review`）：検証役が全回一致した問題だけ、検証役のモデルに 1 回見させ、「問題あり」なら不合格。依頼ごとに切れる（`jobs.explanation_review`。初期は「する」）。見るところは科目ごとのプロンプトで決める。時間は検証の時間に数える
- 文章の確認で落ちたら、作り直しの回数を使わずに指摘した点だけを直させる（数学は `FIX_ROUNDS = 1`。科目の中の仕組み）
- 同じ問題（テンプレートと `params_key`）がすでにストックにあれば、合格してもストックに入れない（`duplicate`）。duplicate は「検証の成績」の不合格の一覧に出さず、合格率では合格に数える
- 不合格の問題は消さずに残す（1 件あたり数 KB）。不合格の理由は科目が `all_reject_codes` で名前を付ける。検証役は「答えが読み取れなかった（solver_unreadable）」と「数値が違う（solver_mismatch）」を分ける
- 検証・作成の生の出力を記録し（検証の記録の `raw`）、/status の「最近の不合格」に作成役・検証役のモデル名、問題文、生の出力を並べる。作成役に送った文章（システムの指示も）は作成の記録に残す
- 検証のテスト：/status から「正解をずらした問題」を流せる（ストックに入らない）。**検証役との突き合わせで弾かれたときだけ成功**。手前で落ちたら 3 回まで作り直し、届かなければ「判定できませんでした」。対応する科目は `test_format` を設定し、`ctx.is_test` のときに正解をずらす。テストのときは LLM に本当の正解を含む要点を渡さない（数学の細かいところは docs/math.md の 8 章）

### LLM の出力

- **JSON スキーマ（Ollama の format）は使わない**。作成・検証・見直しのすべての呼び出しで、`[[question]]` `[[hint]]` `[[explanation]]`（検証は `[[work]]` `[[answer]]`）の見出しで区切った平文で書かせる（`llm.tagged_instructions` / `parse_tagged`）。スキーマの `properties` に `description` を書けばその名前で出力させる
  - 理由：JSON の文字列の中ではバックスラッシュの後ろに `" \ / b f n r t u` しか書けないよう生成が縛られ、LaTeX の `\circ` `\angle` が `\top`・`\theta`・`\rho`・`ngle`・`°` などに押し曲げられていた（第 1 回のレビューの原因）。英語でも記号や引用符が崩れうるので、同じ形を使う
  - JSON で返ってきたときも読む（1 本のバックスラッシュの修復、制御文字になった `\frac` などを戻す `llm._fix_controls`）。見出しの揺れ（`**[[question]]**`、`## [[ Hint ]]:`）、同じ見出しが 2 回（後のものを使う）、`<think>…</think>`（開きタグのないものも）、```json の囲みを外す
  - 直しを頼むときに前回の出力を見せるのも同じ見出しの形（JSON で見せると二重のバックスラッシュを書き写す）
- 出力の上限で切れたら「出力の上限で途中で切れました」。読めなければ 1 回だけ呼び直してから不合格（作り直しの回数は使わない）
- **思考モード**：モデルごとに /status で「自動／切る／切らない」を選べ、そのモデルに保存される（kv の `think_settings`）。自動は qwen3 系（qwen3.5 も）を切り、gemma4 などはモデルの既定のまま（gemma4 は think=false で format が効かなくなる不具合があった）。`.env` の `OLLAMA_THINK` を書けば自動の代わりにその値
- LLM の呼び出しごとに時間の内訳（全体・モデルの読み込み・出力トークン数・速さ・料金）を kv の `llm_calls` に新しい 40 件まで残し、/status の「LLM の呼び出し」に出す（`OllamaClient._record`、`Worker._save_llm_calls`）。種類は作成・直し・検証・見直し

### モデルと依頼

- 既定のモデル：作成 `gemma4:12b-it-qat`、検証 `gemma4:e4b` × 2 回。手元の候補：gemma4:12b-it-qat、gemma4:e4b、gemma4:26b-a4b-it-qat、qwen2.5:7b、qwen3.5、bonsai-2
- **依頼ごとのモデル**：「作る」画面で作成役・検証役・回数・別系統の検証役・解説の見直しを選べる（候補は Ollama にあるモデル。名前を打ち込むこともできる）。初期値は「状態 → モデルと思考モード」の設定。選んだものを `jobs.gen_model / verify_model / verify_count / verify_model_2` に保存し、worker はそれで最後まで処理する（`runtime.plan_for_job`）。モデルの記録がない前の版の依頼は、取りかかった時点の設定を書き込んでから処理する
- 選んだモデルが見つからない（Ollama の 404、API の 400・401・403・404・402）：その依頼は「失敗」（`LLMModelMissing`）。待ち続けない。検証待ちの下書きは捨てて合格数を数え直す
- Ollama につながらない・5xx・API の混雑（429/5xx/overloaded）・sandbox の停止：`LLMUnavailable` で依頼を `waiting` にして、待ち時間が過ぎてから再開（作り直しの回数を使わない）
- **取り消し**：処理中の依頼は「取り消し中」→ worker が数秒以内に止めて「取り消し済み」。まだ始まっていない依頼はすぐ「取り消し済み」。LLM への問い合わせはストリーミングで受け、約 1 秒ごとに取り消しを確かめて接続を切る（Ollama はソケットの shutdown で生成をやめる）。問題と問題の間・作成と検証の間でも確かめる。取り消す前に合格した問題はストックに残し、検証待ちの下書きは捨てる。API のモデルが考えている間はイベントが少ないので、取り消しが少し遅れることがある
- **時間の比べ方**：依頼ごとに、作成役・検証役の eval の合計と load の合計を分けて `jobs` に記録。「状態 → 検証の成績」の依頼ごとの比較で、作成の 1 問あたり＝作成の eval ÷ 作った下書きの数、検証の 1 問あたり＝検証の eval ÷ 検証にかけた下書きの数、読み込みは依頼全体の合計を別に出す。合格率＝合格（後で削除したものも含む）÷ 作成（検証待ちの下書きは除く）。前の版の依頼のモデルは作った問題の記録から出す
- **成績の期間**：「検証の成績」は期間（24 時間・7 日・30 日・すべて）と「この依頼番号以降」で絞れる。検証のテストの依頼は合計に入れない。作成役のモデルごとの合格率と不合格の内訳も出す

### API（Anthropic）での作成（開発者と決めたこと）

- ふだんはローカル LLM。たくさん急いで作るときだけ API。動作確認済み（Haiku で 20 問 0.03 ドル）
- モデル名の頭に「提供元/」を付ける（`anthropic/claude-haiku-5-5`）と `llm.LLMRouter` がその提供元のクライアントへ送る。作成役・検証役・見直しのどれにも使える。提供元は今は Anthropic だけ
- **提供元を変える・足す手順**：(1) `llm_anthropic.py` と同じ形のクライアント（`chat_json(model, system, user, schema, *, temperature)`・`list_models()`・`pop_calls()`・`pending_cost()` と属性 `label` / `should_abort` / `think_for` / `budget_check`。出力は見出しの平文、`_record` と同じ形で時間・料金を記録、取り消しは `should_abort` を見て止める）を作る、(2) `worker.make_llm` で `LLMRouter` に登録、(3) API キーは `.env` → `docker-compose.yml` の **worker の `environment` だけ**（`x-app-env` は web と共用なので使わない。worker に `environment:` を書くときは `<<: *app-env` で共通の分も引き継ぐ）→ `config.Settings`（web には渡さない）
- **API の依頼と Ollama の依頼は別の順番待ち（別スレッド）で同時に進む**（`Worker.lane`。作成役・検証役のどれかが Ollama のモデルなら Ollama 側）。Ollama につながらないときも API だけの依頼は進む（`Worker.next_job_id`）。kv の読み書き（呼び出しの記録・利用額）は `_KV_LOCK` で 1 つずつ。Ollama の状態の確認とバックアップは Ollama 側だけ。API の順番待ちも、待ち時間が過ぎるまで次に手をつけない（`run_once` の最初で `unavailable_until` を見る。料金のかかる呼び出しを繰り返さないため）
- **上限**：月 5 ドル（`.env` の `API_MONTHLY_LIMIT_USD`。「状態 → モデルと思考モード」で変えられ、画面の値が優先。0 は API を止める）と 1 回の依頼の上限（`API_JOB_LIMIT_USD`。0 は上限なし）。API を呼ぶ前に今月の合計（kv の `api_spend`、日本時間の月ごと）と依頼の合計（`jobs.api_cost_usd`）、まだ集計していない分（`pending_cost`）を確かめ、上限に達していれば `LLMBudgetExceeded` → その依頼は「失敗」。1 回の呼び出しの分だけ少し超えることはある。inf・nan は受け付けない。Anthropic のコンソールでも上限を設定してもらう
- **料金**：応答の usage（入力・キャッシュの書き込み 1.25 倍・読み込み 0.1 倍・出力）に料金表（`ANTHROPIC_PRICES`、ドル／100 万トークン）を掛けて呼び出しごとに `cost_usd` を記録。料金表にないモデルは表の一番高い料金で見積もる。取り消した呼び出しは途中までの usage、fallback のときは usage.iterations（モデルごと）で数える
- **安くする工夫**：システムの指示（毎回同じ）にプロンプトキャッシュの印（512 トークン以上の前置きでしか効かないので、作成役で効き、指示の短い検証役・見直しではほとんど効かない）、考える量は `ANTHROPIC_EFFORT=low`、出力は見出しの平文。temperature は今のモデルでは送れないので送らない。`ANTHROPIC_MAX_TOKENS` も docker-compose.yml で渡す
- **断られたとき**（refusal）：Opus 5.5・Sonnet 5.5 では fallbacks（`server-side-fallback-2026-07-01`、`fallbacks: "default"`）を付け、最後の fallback より後の文章だけを使う。それでも断られたら出力が読めない扱い
- web は anthropic のパッケージを読み込まない（テストで確認）。SDK は `anthropic==1.17.0`（1.x）
- **しなかったこと**：Batch API（料金が半額、結果は非同期）。今は 1 問ずつ「作成 → 確認 → 直し」と順に呼ぶので、まとめて投げる形に作り替える必要がある

### 画面

- 端末：スマホ＝選択式だけ、キーボード＝キーボード入力＋選択式。キーボードの端末の「出せる問題」は形式の合計で、内訳（キーボード入力 n・選択式 m）を併記する
- 解く画面：20 問のセットでも 1 問ずつ表示し、答えるたびに正誤と解説を出す。かかった時間は 1 問ごとに記録（読み込み直しでリセットしない）
- 「わからない（解説を見る）」で答えた場合は `gave_up` と `explanation_viewed` を立てる。答えた後は必ず解説を表示するので、それは「見た」に数えない
- 選択式の誤答に間違いの種類の印を付け、回答記録に選んだ選択肢と印を残す（後のフェーズの得意・苦手分析用）。二重送信で回答記録が 2 件にならないよう (session_id, problem_id) に一意制約
- **範囲の選び方**：解き始めと作成依頼で、単元をチェックで複数選べる。単元の「小単元」を開けば小単元ごとにも選べる。何も選ばなければ科目全体。範囲は `"unit:…"` と `"tmpl:…"` をカンマでつないだ文字列（和集合。`Subject.templates_for` / `scope_valid` / `base.split_scope` / `join_scope`）。選んだ単元に入る小単元は省いて保存する。単元・小単元ごとのストック数（端末・形式に合わせて）を同じ所に出す
- **レベル**：`base.LEVELS`（jhs 中学／basic 高校基礎／common 共通テスト／exam 入試標準）。小単元ごとに決め、問題（`problems.level`）にも写す。作成依頼（`jobs.levels`）と解き始め（`study_sessions.levels`）でチェックで選ぶ（選ばなければすべて）。難易度の選択はほかにない。前の版の問題の空のレベル・関連する単元は worker の起動時（`recover`）に書き込む。絞り込みは小単元で行う
- **関連する単元**：主な単元（`problems.unit`）とは別に `problems.related_units`（カンマ区切り）。今は記録だけ
- **セットの組み方**（`stock.build_session`）：間違えた問題の再出題はセットの 30% まで（`RETRY_PERCENT`）。未出題・再出題とも小単元ごとに 1 問ずつ順番に取り（`spread_pick`）、「直前と違う小単元のうち残りが最も多いもの」から並べて同じ小単元が続かないようにする（`spread_order`）。ストック不足時はある分だけで始めて知らせる（1 問目でも）。**同じ本文の設問は 1 つの塊**として選び、続けて設問の順に出す（塊は設問の数で数え、残りより大きい塊は前の設問から残りの数だけ。同じ小単元が続かない並べ方は塊と塊の間で行う）
- **本文のある設問**：スマホでは「本文」「設問」のタブで切り替え、幅 960px 以上では左右に並べる（`play.html` の `.reading`、`app.js` の `setupReading`）。答えた後と見返しの画面では「本文を見る」で開ける
- **`$` を数式に使わない科目**（英語）：`Subject.math_text = False` の科目の要素には `nomath` の class（`web._nomath`）が付き、`renderMath` が飛ばす。キーボード入力の数式のプレビューも出さない
- 「残りが少ない」は科目・端末ごとの未出題が 40 問未満（`LOW_STOCK`）
- 「この問題おかしい？」が押された問題は、集計から外し、出題もしない（/status から戻せる）
- 利用者は既定の 1 人（id=1）。切り替え画面は後のフェーズ。時刻は日本時間で表示
- **/status をページに分けた**：まとめ（/status）／モデルと思考モード（/status/models）／検証の成績（/status/quality）／LLM の呼び出し（/status/llm）／問題検証モード（/review）。思考モードの一覧は Ollama に今あるモデルだけ（保存した設定は消さない）。worker の生存判定は処理中なら LLM_TIMEOUT 分延ばす。worker は変わったときと 1 分ごとだけ DB に書く
- **/problems/<番号> の穴**：まだ解いていないストックの問題は 404。見られるのは「自分が解いた問題」「出題されない問題（不合格（duplicate 以外）・削除・検証のテスト・報告済み）」と、問題検証モードの一覧からのリンク（`?review=1`。パスワードがあればログインも要る。ログインせずに開くとログインの画面へ）。検証待ちの下書きは見せない

### 問題検証モード（/review）

- 正解・選択肢（誤答の間違いの種類）・ヒント・解説・作成役と検証役のモデルの一覧。絞り込みは科目・単元・レベル・作成役・状態（ストック／表記の点検で引っかかる問題／削除／不合格）・作った日（日本時間の日。から・まで）・管理番号（から・まで）
- 削除は `status = "deleted"`（出題しない。回答の記録は残す。「ストックに戻す」で戻せる）。番号を指定して削除・まとめて削除もできる
- Markdown の書き出し（`/review/export.md`、最大 500 問）は冒頭に「高校生向けの問題として、正確さ・分かりやすさ・難易度の適切さを評価してください。」。各問に作成日時と管理番号
- 「前に作った問題の手入れ」：表記をまとめて直す（直す前の文章を各問題の記録に残す）と「表記の点検で引っかかる問題」。中身は科目ごと（`Subject.normalize_text` / `text_issues`。数学は `latexfix`＝docs/math.md の 1 章・5 章、英語は `english/check.py`＝docs/english.md の 5 章）
- フォームを入れ子にしない（ブラウザでボタンが働かなくなった。テストで確かめる）
- パスワードは `.env` の `REVIEW_PASSWORD` を書いたときだけ（cookie にはパスワードの HMAC を入れる。30 日）。ほかの画面にはかけない。見送り：パスワードを変えれば全員ログアウト、ログインの回数制限はなし（家庭内 LAN）

### データベース・運用

- 既存のデータベースには、起動時に足りない列を足す（`db._add_missing_columns`。列を足すだけの変更はこれで済む）
- Pi への clone はデプロイキー（読み取り専用）で `git@github-ai-quiz:sfns50f-bit/ek9.git`
- sandbox の同時実行は 1。2 つの順番待ちと子供の採点で共有している（込むと sandbox の待ちが長くなり、その依頼は「待っています」になる）
- worker のメモリは anthropic の SDK と順番待ち 2 つ分で約 127MB（上限 256MB の内）。英語の部品（単語リストを含む）で増えたのは 1MB 未満
- README：docker グループ、イメージの掃除、復元で wal も一緒に退避、「表記をまとめて直す」の前にバックアップ、API の手順に `--build`

## 進み具合

- [x] フェーズ1「新しい土台＋数学」（PR #2〜#9。実機で運用中。API での作成も動作確認済み：Haiku で 20 問 0.03 ドル。開発者の判断で「使用上の問題なし」としてフェーズ2へ）
- [ ] フェーズ2 英語（共通テストレベルの読解・語彙）：実装済み（読解 3・語彙 2・スペル 1 の小単元、本文なしの確認、検証に選択肢を見せる英語のルール、本文のタブ表示、同じ本文の設問をまとめて出す）。開発者が実機で試してから詳細を詰める
- [ ] フェーズ3 公共（資料を取得して作る。worker からインターネットへ出る。sandbox は出さない）
- [ ] フェーズ4 作り方を増やす（キーワード指定、子供からのリクエスト、類題、時事問題。同じ jobs の順番待ちで処理）
- [ ] フェーズ5 学習機能（集計、間隔をあけた再出題、テストモードなど）
- [ ] フェーズ6 その他の科目（プログラミングは最後）

## 次にやること

1. フェーズ2（英語）の実機での確認：開発者が数問ずつ作り（読解・語彙・スペル。ローカル・API とも）、「検証の成績」の不合格の内訳（英文の点検・本文なしで解けた・見直し）と、問題検証モードの書き出しで英文と設問の質を見る。そのうえで詳細を詰める（docs/english.md の 6 章の「気をつけること」が候補：本文なしの確認の弾きすぎ、誤答の種類、題材の数）
2. 仕様書（docs/spec.md）はフェーズ1で決めたことを反映済み（2026-10-23）。原本は開発者が手で合わせる。次のフェーズで更新版が来たら差し替える
3. 数学で残っていること（急がない）は [docs/math.md](docs/math.md) の 12 章と、[docs/handover.md](docs/handover.md) の残課題
4. 開発者が実機でローカル・API とも作り、問題検証モードで「作った日」「管理番号」で絞って書き出して点検してもらう。「検証の成績」の不合格の内訳で弾きすぎを見る（見直しで弾きすぎなら切る、検証役 e4b で弾きすぎなら 12b にする）

## テスト

```
python -m pytest
```

- `tests/test_math_templates.py`：20 テンプレートの正解を別の方法で確認、誤答の重複なし、キーボード入力での採点（数学のテストの詳細は docs/math.md の 10 章）
- `tests/test_worker.py`：作成→検証の順番、Ollama 停止時の待機と再開、正解をずらした問題が弾かれる、/status の設定の反映、依頼に記録したモデルで処理、レベル・関連する単元の記録、本文の保存と共有、API だけの依頼は Ollama が止まっていても進む、API と Ollama の順番待ちの分け方・利用額の記録・月と依頼の上限、弧度法の答え、取り消し（`test_cancel_*`）
- `tests/test_web.py`：LLM なしで解ける（web のプロセスで llm・sympy・anthropic を読み込まない）、回答記録の項目、再出題の混ぜ方、報告、キーボード入力の欄の属性、依頼ごとのモデル・複数の単元・レベル、小単元を続けない並べ方、/problems の穴、問題検証モード（パスワード・削除・書き出し・入れ子のフォームなし）、/status のページ分け
- `tests/test_storage.py`：目印ファイルがなければ起動しない、SQLite の設定、再起動後もデータが残る、バックアップ、古いデータベースへの列の追加
- `tests/test_llm.py`：Ollama のクライアント（偽の HTTP サーバー）。ストリーミング、取り消しで数秒以内に接続を切る、出力の上限、JSON スキーマを送らず見出しの平文で受け取る、JSON の修復。Anthropic のクライアント（偽の API サーバー。本物と同じストリーミングの形）：キャッシュの印・effort・fallbacks・料金・上限・取り消し
- `tests/test_latexfix.py`：数学の表記の直しと点検（docs/math.md の 5 章）
- `tests/test_english.py`：英語の作成・検証・採点・出題・画面（docs/english.md の 7 章）。テスト用の LLM は `EnglishOracle`
- テストの LLM（`tests/conftest.py::OracleLLM`）は、作成役は式をそのまま書き写し、検証役は seed からテンプレートを作り直して本当の正解を答える
