"""数学の LLM へのプロンプト。LLM は文章（問題文・解説・ヒント）だけを書き、数値は書き換えない。"""

from __future__ import annotations

from typing import Any

GEN_SYSTEM = """あなたは日本の高校数学の問題集の編集者です。
数値と正解はすでに Python で計算済みです。あなたの仕事は、それを使って問題文・ヒント・解説の文章を書くことだけです。

厳守すること:
- 「問題文に入れる式・値」は、1つ残らず $…$ の中に一字一句そのまま書き写す。数値・符号・文字を変えない。書き換えや計算のやり直しをしない
- 問題に必要な条件は、与えられたものだけを使う。条件を足したり省いたりしない
- 正解をあなたが計算し直す必要はない。与えられた正解と解き方の要点に沿って解説を書く
- 解説の最後の行は、必ず「答え: $<正解>$」と、与えられた正解の LaTeX をそのまま書く
- ヒントは答えを書かずに、最初の一歩（使う公式や考え方）だけを1〜2文で示す
- 数式は LaTeX で $…$ に入れる。JSON の文字列なのでバックスラッシュは \\\\ と二重に書く（例: \\\\frac{1}{2}）
- 出力は指定された JSON だけにする"""

GEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "hint": {"type": "string"},
        "explanation": {"type": "string"},
    },
    "required": ["question", "hint", "explanation"],
}


def gen_user(task: str, givens: list[str], answer_latex: str, steps: list[str], fmt: str) -> str:
    lines = [f"- ${g}$" for g in givens]
    step_lines = [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    if fmt == "choice":
        form = "選択式（選択肢は別に表示するので、問題文には選択肢を書かない。「次のうちから選べ」などとも書かない）"
    else:
        form = "答えを入力する形式（問題文には選択肢を書かない）"
    return "\n".join([
        f"問題の内容: {task}",
        f"出題の形式: {form}",
        "",
        "問題文に入れる式・値（すべて $…$ の中にそのまま書き写すこと）:",
        *lines,
        "",
        f"正解: ${answer_latex}$",
        "",
        "解き方の要点（Python で計算済み。解説はこれに沿って書く）:",
        *step_lines,
        "",
        "問題文（question）、ヒント（hint）、解説（explanation）を JSON で出力してください。",
    ])


SOLVE_SYSTEM = """あなたは数学が得意な高校生です。与えられた問題を解いてください。
途中の考え方を work に短く書き、最終的な答えだけを answer に書きます。
answer は指定された答え方に従い、数式は Python / SymPy で読める形で書きます（分数は a/b、平方根は sqrt(n)、円周率は pi）。
問題の式を書き写すのではなく、計算した結果の値を書きます（sin や log、binomial などの関数を答えに残さない）。
単位や「x =」などの説明は付けません（答え方に書かれている場合を除く）。出力は指定された JSON だけにしてください。"""

SOLVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "work": {"type": "string"},
        "answer": {"type": "string"},
    },
    "required": ["work", "answer"],
}

ANSWER_FORMS = {
    "value": "1つの数または式",
    "tuple": "座標などの組を (a, b) の形で",
    "set": "答えをすべてカンマ区切りで",
    "interval": "x の範囲を不等式で（a<x<b の形。「または」はカンマで区切る。≦ は <=）",
}


def solve_user(question: str, kind: str, input_hint: str) -> str:
    return "\n".join([
        "問題:",
        question,
        "",
        f"答え方: {ANSWER_FORMS.get(kind, '1つの数または式')}。{input_hint}",
    ])
