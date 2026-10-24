"""英語の LLM へのプロンプト。

読解：作成役が本文と設問（選択肢・正解・根拠の文・解説）を書く。正解の根拠の文が本文にあるかは Python で確かめる。
語彙・スペル：正解の語と誤答の語は Python が単語リストから選ぶ。作成役は例文・訳・ヒント・解説だけを書く。
出力は数学と同じく JSON を使わず、[[見出し]] で区切った平文（llm.tagged_instructions / parse_tagged）。
"""

from __future__ import annotations

from typing import Any

# --- 読解の作成（2 段階：本文作成役が本文を書く → 作成役が確定した本文に設問を書く） ---------------

PASSAGE_SYSTEM = """あなたは日本の大学入学共通テスト（英語リーディング）の問題を作る編集者です。
日本の高校生が読む英文の本文を書きます（設問は別の人が作ります）。

- 指定された種類・題材・語数で、自然で正しい英語を書く。共通テストに近い難しさ（高校で習う語彙と文法）にする
- 本文は自分で考えた内容にする。実在の人物・会社・商品の名前や、事実かどうか分からない数字・出来事を本当のことのように書かない（登場人物や店の名前は架空のものにする）
- 時系列・因果関係が筋の通った内容にし、前後で矛盾させない。題名や本文で使う言葉（misunderstanding など）は、内容と合うものにする
- 架空の URL・メールアドレスは example.org などの例示用のドメインを使う（www.example.org/events、info@example.org など）
- 段落の間は空行を 1 つ入れる。見出しや箇条書きは、お知らせ・広告のときだけ使ってよい
- 設問・解説・日本語訳は書かない"""

PASSAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "本文の題名（英語）"},
        "passage": {"type": "string", "description": "本文（英語）"},
    },
    "required": ["title", "passage"],
}


def passage_user(genre: str, topic: str, words: tuple[int, int]) -> str:
    return "\n".join([
        f"本文の種類: {genre}",
        f"題材: {topic}",
        f"本文の長さ: {words[0]}〜{words[1]} 語",
    ])


PASSAGE_REVIEW_SYSTEM = """あなたは日本の高校の英語の先生です。生徒に読ませる前に、英語の本文を点検します。
次の点に誤りがないかを確かめてください。
1. 時系列が筋が通っているか（出来事の順番、「〜した後で」「毎週」などの時の表し方が食い違っていないか）
2. 因果関係・人物の行動に無理がないか。前後で矛盾していないか
3. 題名や本文の言葉が内容と合っているか（misunderstanding と書いてあるのに誤解の話になっていない、など）
4. 英文が自然で、文法やつづりの誤りがないか
細かい言い回しの好みや、もっと良い書き方がある、というだけのものは誤りとしない。

verdict の 1 行目には「問題なし」か「問題あり」のどちらかだけを書く。"""


def passage_review_user(passage: str) -> str:
    return "本文:\n" + passage


QUESTION_SYSTEM = """あなたは日本の大学入学共通テスト（英語リーディング）の問題を作る編集者です。
与えられた英語の本文の内容を問う 4 択の設問を作ります。本文は書き換えません。

設問（英語）:
- 正解は本文の内容だけから 1 つに決まるようにする。ほかの 3 つの選択肢は、本文を読めばはっきり誤りと分かるものにする
- 本文を読まなくても常識で答えられる設問は作らない。すでにある設問と同じことは問わない
- 正解の選択肢は、本文の言葉をそのまま写さず、言い換えて書く。正解の選択肢のどの部分も本文で裏付けられるようにする（本文にない要素を足さない）
- 特に because・so・more・less・only・all・always・never など、理由・比較・限定を表す言葉は、本文に同じ内容が書かれているときだけ正解の選択肢に入れる
- 理由・目的を問う設問（Why …? / What was the purpose …? など）では、根拠の文も理由・目的を述べた文にする（結果を述べただけの文は使わない）。本文に理由・目的が書かれていなければ、その設問は作らない
- 正解だけが長くならないよう、4 つの選択肢の長さをそろえる
- 本文で性別が分からない人物（特に語り手）は、設問・選択肢で the writer などと呼び、he / she を使わない
- 設問の中の代名詞（his / her / their）は、指す人物が 1 人に決まるときだけ使う
- 「NOT」を使う設問や、「All of the above」のような選択肢は作らない
- 選択肢は A〜D の 4 つ。誤答それぞれに、間違いの種類を次の記号で付ける:
  not_stated（本文に書かれていない）、contradicts（本文と食い違う）、partial（一部だけ合っている）、detail（数・日時・人などの取り違え）
- Evidence には、正解の根拠になる本文の 1 文を、本文から一字一句そのまま写す（言い換えない・つなげない）

ヒントと解説（日本語）:
- ヒントは答えを書かずに、本文のどこを読めばよいか（第◯段落、◯◯の部分など）を 1〜2 文で示す
- 解説は、まず正解がなぜ正しいかを、根拠になる本文の部分とその意味（和訳）とともに書く。そのあとで、ほかの選択肢がなぜ誤りかを書く（誤答の説明から始めない）。誰が誰に何をしたかを本文のとおりに書く
- ヒントは解説の 1 文目と同じことを書かない。根拠の文をそのまま書かない
- 人名は英語の表記のまま書く（Mr. Hale など。カタカナにしない）
- 選択肢は画面で並べ替えるので、解説とヒントでは選択肢を A・B などの記号で呼ばない（「『〜』という選択肢は」のように中身で呼ぶ）
- 自然な日本語で書く（中国語の字や言い回しを混ぜない）
- 生徒が読む文章なので、作成の指示やこのアプリの言葉（「誤答の語」「与えられた」「指示」「使う単語」「指定された」など）は書かない

各設問の欄は、次の形で 1 行ずつ書く（Explanation だけは複数行でよい）:
Question: <設問の英文>
A: <選択肢>
B: <選択肢>
C: <選択肢>
D: <選択肢>
Answer: <正解の記号 1 文字>
Wrong: <誤答の記号>=<種類>, <誤答の記号>=<種類>, <誤答の記号>=<種類>
Evidence: <本文から写した根拠の 1 文>
Hint: <ヒント（日本語）>
Explanation: <解説（日本語）>"""


def question_schema(n_questions: int) -> dict[str, Any]:
    props = {f"q{i}": {"type": "string",
                       "description": f"設問 {i}（Question・A〜D・Answer・Wrong・Evidence・Hint・Explanation の各行）"}
             for i in range(1, n_questions + 1)}
    return {"type": "object", "properties": props, "required": list(props)}


def question_user(passage: str, n_questions: int, style: str, existing: list[str]) -> str:
    lines = ["本文:", passage, "",
             f"作る設問の数: {n_questions}（[[q1]] から [[q{n_questions}]] まで。それぞれ本文の別の部分を問う）",
             f"設問の作り方: {style}"]
    if existing:
        lines += ["", "この本文にすでにある設問（同じことを問わない）:", *[f"- {q}" for q in existing]]
    return "\n".join(lines)


# --- 語彙・スペルの作成 -------------------------------------------------------

# 生徒が読む文章に書かせない言葉（アプリの内部の言葉。第 4 回のレビュー A-2）。check.internal_words でも弾く
NO_INTERNAL = "生徒が読む文章なので、作成の指示やこのアプリの言葉（「誤答の語」「与えられた」「指示」「使う単語」「指定された」など）は書かない"

_VOCAB_COMMON = """あなたは日本の高校生向けの英語の単語問題を作る編集者です。
指定された英単語を使った英語の例文と、その和訳・ヒント・解説を書きます。正解の語は決まっています。

例文（英語）:
- 指定された単語を、指定された意味で、指定された形のまま（語尾を変えずに）ちょうど 1 回だけ使う
- 8〜20 語の自然で正しい 1 文にする。高校生に分かる語彙で書く
- 単独で読んで、理由と結果・前後の内容が常識的につながる文にする（無理のある状況や、つじつまの合わない理由を書かない）
- 文脈から、その単語の意味がはっきり決まるようにする

和訳・ヒント・解説（日本語）:
- 和訳は例文全体の自然な日本語訳
- 品詞に触れるときは、指定された品詞のとおりに説明する（副詞を形容詞と説明しない、など）
- 自然な日本語で書く（中国語の字や言い回しを混ぜない）
- """ + NO_INTERNAL

_VOCAB_PARTS = {
    "en-vocab-blank": """

この問題は、例文の中の指定された単語を空所にして、4 つの語から選ばせる問題です。
- 選択肢には、指定された単語のほかに「選択肢に並ぶ別の語」が並ぶ。その語は例文に使わず、どれを空所に入れても意味が通らない文にする
- 指定された単語の直前に a や an を置かない（空所にしたとき答えの手がかりになるため）
- ヒントは答えの単語もその意味も書かずに、文のどこに注目すればよいかを 1 文で示す
- 解説は、文の意味から考えてなぜその単語が入るかを 2〜3 文で書く。ほかの選択肢の語に触れるときは、その語のつづり（英語）をそのまま書く""",
    "en-vocab-meaning": """

この問題は、例文の中の指定された単語を [ ] で示し、その意味を 4 つの日本語から選ばせる問題です。例文に空所はない。
- ヒントは答えの意味を書かずに、文のどこに注目すればよいかを 1 文で示す
- 解説は、文の意味から考えてなぜその単語がその意味になるかを 2〜3 文で書く
- ヒント・解説で「空所」「入る」「入れる」という言葉を使わない（空所はない）""",
    "en-spelling": """

この問題は、日本語の意味と例文（指定された単語の所は最初の文字と文字数だけを見せる）から、単語のつづりを書かせる問題です。
- ヒントは答えの単語を書かずに、文のどこに注目すればよいかを 1 文で示す
- 解説は、文の意味から考えてなぜその単語が入るかを 2〜3 文で書く（ほかの語には触れない）""",
}


def vocab_system(template: str) -> str:
    """語彙・スペルの作成役への指示。小単元ごとに、その問題の形に要ることだけを書く
    （スペル・意味の問題に「ほかの語」の話を書くと、作成役が解説に書き写してしまった。第 4 回のレビュー A-2）。"""
    return _VOCAB_COMMON + _VOCAB_PARTS.get(template, "")


VOCAB_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sentence": {"type": "string", "description": "例文（英語の 1 文）"},
        "translation": {"type": "string", "description": "例文の和訳"},
        "hint": {"type": "string", "description": "ヒント（日本語）"},
        "explanation": {"type": "string", "description": "解説（日本語）"},
    },
    "required": ["sentence", "translation", "hint", "explanation"],
}


def vocab_user(word: str, pos: str, meaning: str, others: list[str]) -> str:
    lines = [f"使う単語: {word}（品詞: {pos}。意味: {meaning}）"]
    if others:
        lines.append("選択肢に並ぶ別の語（例文に使わない。空所に入れると意味が通らない文にする）: " + ", ".join(others))
    return "\n".join(lines)


# --- 直し ---------------------------------------------------------------------

def fix_user(original: str, previous: dict[str, Any], issues: str, keys: list[str]) -> str:
    """前回の出力を同じ見出しの形で見せて、指摘した点だけを直させる。"""
    shown = "\n".join(f"[[{k}]]\n{str(previous.get(k) or '').strip()}" for k in keys)
    return "\n".join([
        original, "", "前回の出力:", shown, "",
        "直してほしい点: " + issues,
        "指摘した点だけを直し、ほかはできるだけそのままにして、同じ見出しの形ですべて書き直してください。",
    ])


# --- 本文なしの確認（作成役のモデルで、作成のときに） ---------------------------

BLIND_SYSTEM = """英語の 4 択の設問に答えます。ただし、設問の元になった本文は見せません。
一般常識だけで正解が確実に分かるときだけ、その記号（A〜D）を答えてください。
本文を読まないと分からないときは X と答えてください。当て推量はしないでください。"""

BLIND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string", "description": "記号 A〜D のどれか 1 文字、または X"}},
    "required": ["answer"],
}


# --- 検証 ---------------------------------------------------------------------

SOLVE_SYSTEM = """あなたは英語がよくできる受験生です。英語の問題を解いてください。
本文があるときは、本文に書かれていることだけを根拠にして答えます。"""

SOLVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "work": {"type": "string", "description": "考え方（根拠になる本文の部分など。短くてよい）"},
        "answer": {"type": "string", "description": "答え（選択式なら記号 1 文字だけ、つづりの問題なら英単語 1 語だけ）"},
    },
    "required": ["work", "answer"],
}


def solve_user(question: str, choices: list[str], passage: str = "", title: str = "") -> str:
    parts = []
    if passage:
        parts += ["本文:", (title + "\n\n" if title else "") + passage, ""]
    parts += ["問題:", question, ""]
    if choices:
        parts += ["選択肢:", *[f"{'ABCDEFGH'[i]}: {c}" for i, c in enumerate(choices)], "",
                  "答え方: 最も適切な選択肢の記号を 1 文字だけ answer に書く"]
    else:
        parts += ["答え方: 空所に入る英単語を 1 語だけ、小文字で answer に書く"]
    return "\n".join(parts)


# --- 解説の見直し -------------------------------------------------------------

REVIEW_SYSTEM = """あなたは日本の高校の英語の先生です。生徒に出す前に、英語の問題を点検します。
次の点に誤りがないかを確かめてください。
1. 正解が本文（または例文）の内容から 1 つに決まるか。ほかの選択肢も正解になりうる場合は誤り
2. 英文（本文・設問・選択肢・例文）が自然で、文法やつづりの誤りがないか
3. 解説・ヒントが本文と正解に合っているか（本文にないことを書いていないか、和訳が大きく違っていないか）
4. 解説が自然な日本語か
細かい言い回しの好みや、もっと良い書き方がある、というだけのものは誤りとしない。

verdict の 1 行目には「問題なし」か「問題あり」のどちらかだけを書く。"""

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "description": "「問題なし」または「問題あり」"},
        "issues": {"type": "string", "description": "問題があればその内容（なければ「なし」）"},
    },
    "required": ["verdict"],
}


def review_user(question: str, choices: list[tuple[str, bool]], answer: str, explanation: str, hint: str,
                passage: str = "") -> str:
    parts = []
    if passage:
        parts += ["本文:", passage, ""]
    parts += ["問題:", question, ""]
    if choices:
        parts += ["選択肢:", *[f"- {t}{'（正解）' if ok else ''}" for t, ok in choices], ""]
    else:
        parts += [f"正解: {answer}", ""]
    parts += ["ヒント:", hint, "", "解説:", explanation]
    return "\n".join(parts)
