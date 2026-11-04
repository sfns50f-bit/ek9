"""公共の LLM へのプロンプト。

本文（資料）は e-Gov・Wikipedia から取ってきた文章をそのまま使う（LLM は書かない）。作成役は設問・選択肢・
ヒント・解説だけを書く。出力は [[見出し]] で区切った平文（llm.tagged_instructions / parse_tagged）。
"""

from __future__ import annotations

from typing import Any

from .. import choicejudge

QUESTION_SYSTEM = """あなたは日本の高校の「公共」（大学入学共通テストの「公共」）の問題を作る編集者です。
与えられた資料（法令の条文、または百科事典の記事の一部）を読んで答える 4 択の設問を作ります。資料は書き換えません。

設問:
- 正解は資料の内容だけから 1 つに決まるようにする。生徒は資料を読みながら解く
- 「資料の内容に合うもの」「〜について資料から読み取れること」「〜の場合にあてはまるもの」など、合うものを 1 つ選ばせる形にする。「誤っているもの」「適当でないもの」を選ばせる設問は作らない
- 条文の要件（誰が・何を・どんな場合に・どれだけ）や、用語の意味・しくみの違いを問う。資料の言葉を探すだけで終わらず、具体的な場面にあてはめる設問もよい
- 意見が分かれるテーマ（憲法改正、安全保障、原子力、死刑など）では、どちらが正しいかを決めつけない。制度のしくみや、資料に書かれた立場・主張の中身を問う。生徒の意見・賛否・価値判断を問わない
- 正解の選択肢は、資料の言葉をそのまま写さず言い換える。正解のどの部分も資料で裏付けられるようにする（資料にない要素を足さない）
- 誤答は、資料と食い違う内容（主体・条件・数・時期の取り違えなど）にする。**現実に正しい内容を誤答にしない**（資料に書かれていなくても、事実として正しい選択肢は正解が 2 つになる）
- 4 つの選択肢の長さをそろえる。「すべて正しい」のような選択肢は作らない。すでにある設問と同じことは問わない
- 選択肢は A〜D の 4 つ。誤答それぞれに、間違いの種類を次の記号で付ける:
  contradicts（資料と食い違う）、not_stated（資料に書かれておらず、事実としても誤り）、partial（一部だけ合っている）、detail（主体・時期・数などの取り違え）
- Evidence には、正解の根拠になる資料の 1 文を、資料から一字一句そのまま写す（言い換えない・つなげない・省略しない）

ヒントと解説:
- ヒントは答えを書かずに、資料のどこを読めばよいか（第◯条、◯◯の部分など）を 1〜2 文で示す
- 解説は、まず正解がなぜ正しいかを、根拠になる資料の部分とともに書く。そのあとで、ほかの選択肢がなぜ誤りかを書く
- 資料の内容と食い違うことを書かない。資料にない知識を足すときは、確かな事実だけを短く書く
- 選択肢は画面で並べ替えるので、ヒントと解説では選択肢を A・B などの記号で呼ばない（「『〜』という選択肢は」のように中身で呼ぶ）
- 資料は「資料」と呼ぶ。自然な日本語で書く（中国語の字や言い回しを混ぜない）
- 生徒が読む文章なので、作成の指示やこのアプリの言葉（「与えられた」「指示」「指定された」など）は書かない

各設問の欄は、次の形で 1 行ずつ書く（Explanation だけは複数行でよい）:
Question: <設問>
A: <選択肢>
B: <選択肢>
C: <選択肢>
D: <選択肢>
Answer: <正解の記号 1 文字>
Wrong: <誤答の記号>=<種類>, <誤答の記号>=<種類>, <誤答の記号>=<種類>
Evidence: <資料から写した根拠の 1 文>
Hint: <ヒント>
Explanation: <解説>"""


def question_schema(n_questions: int) -> dict[str, Any]:
    props = {f"q{i}": {"type": "string",
                       "description": f"設問 {i}（Question・A〜D・Answer・Wrong・Evidence・Hint・Explanation の各行）"}
             for i in range(1, n_questions + 1)}
    return {"type": "object", "properties": props, "required": list(props)}


def question_user(title: str, body: str, n_questions: int, focus: str, existing: list[str]) -> str:
    lines = [f"資料の題名: {title}", "資料:", body, "",
             f"作る設問の数: {n_questions}（[[q1]] から [[q{n_questions}]] まで。それぞれ資料の別の部分を問う）",
             f"この単元で問いたいこと: {focus}"]
    if existing:
        lines += ["", "この資料にすでにある設問（同じことを問わない）:", *[f"- {q}" for q in existing]]
    return "\n".join(lines)


def explanation_user(body: str, q: dict[str, Any]) -> str:
    letters = "ABCD"
    options = "\n".join(f"{letters[i]}. {t}" for i, t in enumerate(q["options"]))
    return "\n".join([
        "資料:", body, "", "設問: " + q["question"], options, "正解: " + letters[q["answer"]],
        "正解の根拠の文: " + q["evidence"], "",
        "この設問の解説だけを書いてください（2〜4 文）。資料のどこからなぜ正解になるかと、"
        "ほかの選択肢が違う理由を書く。選択肢は A・B などの記号ではなく、中身で呼ぶ。",
    ])


EXPLANATION_SCHEMA: dict[str, Any] = {
    "type": "object", "properties": {"explanation": {"type": "string", "description": "解説"}},
    "required": ["explanation"],
}

fix_user = choicejudge.fix_user
JUDGMENTS_SCHEMA = choicejudge.JUDGMENTS_SCHEMA
REVIEW_SCHEMA = choicejudge.REVIEW_SCHEMA

# --- 検証：選択肢ごとの判定（正解は見せない。選択肢の順番は毎回入れ替える） ------------------------

CHOICE_LABELS = {"裏付けあり": "", "資料と食い違う": "contradicts", "資料に書かれていない": "not_stated",
                 "一部だけ合う": "partial", "細部の取り違え": "detail"}

CHOICE_VERIFY_SYSTEM = """あなたは日本の高校の「公共」の先生です。資料を読んで答える問題の選択肢を、資料と照らして 1 つずつ判定します。
選択肢ごとに、次のどれか 1 つを選んでください。
- 裏付けあり：選択肢のどの部分も資料に書かれている（言い換えは可）。資料にない条件・理由・限定（「必ず」「すべて」「〜だけ」など）が混ざっていれば「裏付けあり」にしない
- 資料と食い違う：資料の内容と反対・矛盾する
- 資料に書かれていない：資料から分からない内容
- 一部だけ合う：前半は資料どおりだが、後半など一部が資料と違う・書かれていない
- 細部の取り違え：主体・時期・数・条件などの細かい点だけが資料と違う

判定に使った資料の文を、資料からそのまま（1 文。言い換えずに）引用する。「資料に書かれていない」のときは引用しなくてよい。
1 行に 1 つ、「番号: 判定 — 引用: "資料の文" — 理由（短く）」の形で、すべての選択肢を書く。"""


def choice_judge_user(title: str, passage: str, question: str, choices: list[str]) -> str:
    return "\n".join([f"資料の題名: {title}", "資料:", passage, "", "設問:", question, "", "選択肢:",
                      *[f"{i}: {c}" for i, c in enumerate(choices, 1)]])


# 事実としての判定（公共だけ。資料に書かれていない誤答が、現実には正しいことがある＝正解が 2 つ）
FACT_LABELS = {"正しい": True, "誤り": False, "分からない": None}

FACT_SYSTEM = """あなたは日本の高校の「公共」の先生です。設問の選択肢が、事実として正しいかどうかを、
あなたの知識で 1 つずつ判定します（資料は見せません）。いまの日本の法律・制度・用語の意味に照らして判定する。
- 正しい：事実として正しい（設問への答えとして成り立つ）
- 誤り：事実として誤っている
- 分からない：確かなことが言えない
自信がないものは「分からない」にする。1 行に 1 つ、「番号: 判定 — 理由（短く）」の形で、すべての選択肢を書く。"""


def fact_user(question: str, choices: list[str]) -> str:
    return "\n".join(["設問:", question, "", "選択肢:", *[f"{i}: {c}" for i, c in enumerate(choices, 1)]])


# --- 見直し（見直し役。検証で全回一致した問題だけ） ----------------------------------------

REVIEW_SYSTEM = """あなたは日本の高校の「公共」の先生です。生徒に出す前に、資料を読んで答える問題の解説を点検します。
次の点を 1 つずつ確かめてください。
1. 解説・ヒントの内容が資料と合っているか。条文の主体・条件・数（誰が・何を・どんな場合に）を取り違えていないか
2. 解説が正解と食い違っていないか。正解の理由を資料の根拠とともに説明しているか
3. ヒントが正解の場所を正しく示しているか（答えそのものを書いていないか）
4. 解説が、資料にない誤った知識や、古い制度を今のもののように書いていないか
5. 意見が分かれるテーマで、どれかの立場を正しいと決めつけていないか（設問・選択肢・解説とも）
6. 設問・選択肢・解説が自然な日本語か
細かい言い回しの好みや、もっと良い書き方がある、というだけのものは誤りとしない。

verdict の 1 行目には「問題なし」か「問題あり」のどちらかだけを書く。"""


def review_user(title: str, passage: str, question: str, choices: list[tuple[str, bool]], explanation: str,
                hint: str) -> str:
    return "\n".join([f"資料の題名: {title}", "資料:", passage, "", "問題:", question, "", "選択肢:",
                      *[f"- {t}{'（正解）' if ok else ''}" for t, ok in choices], "",
                      "ヒント:", hint, "", "解説:", explanation])
