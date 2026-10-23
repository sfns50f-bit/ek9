"""英語の LLM へのプロンプト。

読解：作成役が本文と設問（選択肢・正解・根拠の文・解説）を書く。正解の根拠の文が本文にあるかは Python で確かめる。
語彙・スペル：正解の語と誤答の語は Python が単語リストから選ぶ。作成役は例文・訳・ヒント・解説だけを書く。
出力は数学と同じく JSON を使わず、[[見出し]] で区切った平文（llm.tagged_instructions / parse_tagged）。
"""

from __future__ import annotations

from typing import Any

# --- 読解の作成 ---------------------------------------------------------------

READING_SYSTEM = """あなたは日本の大学入学共通テスト（英語リーディング）の問題を作る編集者です。
日本の高校生が読む英文と、その内容を問う 4 択の設問を作ります。

本文（英語）:
- 指定された種類・題材・語数で、自然で正しい英語を書く。共通テストに近い難しさ（高校で習う語彙と文法）にする
- 本文は自分で考えた内容にする。実在の人物・会社・商品の名前や、事実かどうか分からない数字・出来事を本当のことのように書かない（登場人物や店の名前は架空のものにする）
- 段落の間は空行を 1 つ入れる。見出しや箇条書きは、お知らせ・広告のときだけ使ってよい

設問（英語）:
- 正解は本文の内容だけから 1 つに決まるようにする。ほかの 3 つの選択肢は、本文を読めばはっきり誤りと分かるものにする
- 本文を読まなくても常識で答えられる設問は作らない
- 正解の選択肢は、本文の言葉をそのまま写さず、言い換えて書く。正解だけが長くならないよう、4 つの選択肢の長さをそろえる
- 「NOT」を使う設問や、「All of the above」のような選択肢は作らない
- 選択肢は A〜D の 4 つ。誤答それぞれに、間違いの種類を次の記号で付ける:
  not_stated（本文に書かれていない）、contradicts（本文と食い違う）、partial（一部だけ合っている）、detail（数・日時・人などの取り違え）
- Evidence には、正解の根拠になる本文の 1 文を、本文から一字一句そのまま写す（言い換えない・つなげない）

ヒントと解説（日本語）:
- ヒントは答えを書かずに、本文のどこを読めばよいか（第◯段落、◯◯の部分など）を 1〜2 文で示す
- 解説は、根拠になる本文の部分の意味（和訳）と、正解がなぜ正しいか、ほかの選択肢がなぜ誤りかを書く
- 選択肢は画面で並べ替えるので、解説とヒントでは選択肢を A・B などの記号で呼ばない（「『〜』という選択肢は」のように中身で呼ぶ）
- 自然な日本語で書く（中国語の字や言い回しを混ぜない）。作成の指示そのものには触れない

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


def reading_schema(n_questions: int) -> dict[str, Any]:
    props: dict[str, Any] = {
        "title": {"type": "string", "description": "本文の題名（英語）"},
        "passage": {"type": "string", "description": "本文（英語）"},
    }
    for i in range(1, n_questions + 1):
        props[f"q{i}"] = {"type": "string",
                          "description": f"設問 {i}（Question・A〜D・Answer・Wrong・Evidence・Hint・Explanation の各行）"}
    return {"type": "object", "properties": props, "required": list(props)}


def reading_user(genre: str, topic: str, words: tuple[int, int], n_questions: int, style: str) -> str:
    return "\n".join([
        f"本文の種類: {genre}",
        f"題材: {topic}",
        f"本文の長さ: {words[0]}〜{words[1]} 語",
        f"設問の数: {n_questions}（[[q1]] から [[q{n_questions}]] まで。それぞれ本文の別の部分を問う）",
        f"設問の作り方: {style}",
    ])


# --- 語彙・スペルの作成 -------------------------------------------------------

VOCAB_SYSTEM = """あなたは日本の高校生向けの英語の単語問題を作る編集者です。
指定された英単語を使った英語の例文と、その和訳・ヒント・解説を書きます。正解の語と誤答の語は決まっています。

例文（英語）:
- 指定された単語を、指定された意味で、指定された形のまま（語尾を変えずに）ちょうど 1 回だけ使う
- 8〜20 語の自然で正しい 1 文にする。高校生に分かる語彙で書く
- 文脈から、その単語の意味がはっきり決まるようにする。「入らない語」として示した語を入れても意味が通らない文にする
- 「入らない語」は例文に使わない。指定された単語の直前に a や an を置かない（空所にしたとき答えの手がかりになるため）

和訳・ヒント・解説（日本語）:
- 和訳は例文全体の自然な日本語訳
- ヒントは答えの単語を書かずに、文のどこに注目すればよいかを 1 文で示す
- 解説は、文の意味から考えてなぜ指定された単語が入るか（意味が合うか）を 2〜3 文で書く。「入らない語」の意味にも触れてよい
- 自然な日本語で書く（中国語の字や言い回しを混ぜない）。作成の指示そのものには触れない"""

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
    lines = [f"使う単語: {word}（{pos}。意味: {meaning}）"]
    if others:
        lines.append("入らない語（例文に使わず、入れても意味が通らないようにする）: " + ", ".join(others))
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
