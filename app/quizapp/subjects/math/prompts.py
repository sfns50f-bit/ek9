"""数学の LLM へのプロンプト。LLM は文章（問題文・解説・ヒント）だけを書き、数値は書き換えない。"""

from __future__ import annotations

from typing import Any

GEN_SYSTEM = """あなたは日本の高校数学の問題集の編集者です。
数値と正解はすでに Python で計算済みです。あなたの仕事は、それを使って問題文・ヒント・解説の文章を書くことだけです。

厳守すること:
- 「問題文に入れる式・値」は、1つ残らず一字一句そのまま書き写す（$…$ で示したものは $…$ の中に）。数値・符号・文字を変えない。書き換えや計算のやり直しをしない
- 問題に必要な条件は、与えられたものだけを使う。条件を足したり省いたりしない
- 正解をあなたが計算し直す必要はない。与えられた正解と解き方の要点に沿って解説を書く
- 解説の最後の行は、必ず「答え: $<正解>$」と、与えられた正解の LaTeX をそのまま書く
- ヒントは答えを書かずに、最初の一歩（使う公式や考え方）だけを1〜2文で示す
- 解説は「解き方の要点」の各段階を順に、途中式も省かずに書く。言葉の説明（上側・下側、正・負、増加・減少など）は、その直後の式と合わせる
- 「解き方の要点」「与えられた正解」「指示」など、あなたに渡された指示や要点そのものには触れない（生徒が読む文章として書く）
- ヒントは、この問題を解く方法に合ったものにする（内積の問題で余弦定理を勧めない、など）
- 用語は教科書のとおりに使う：点と傾きの式（点斜式）、2 点を通る直線の式、底の変換公式、分母の有理化、方程式 f'(x)=0（「微分方程式」とは呼ばない）

書き方:
- 文章は自然な日本語（中国語の字や言い回しを混ぜない）。問題文は「〜を求めよ。」のような問いかけで終える
- 問題文は短く、条件と問いだけを書く（「〜を求める問題について、」「考えよう。さて、」のような前置きや、同じことの言い直しを書かない）
- 数式だけを LaTeX で $…$ に入れる。数式は必ず $…$ か $$…$$ で囲む（\\int や \\vec を $ の外に書かない）。$…$ の中に日本語を入れない（「$3$ 回」「$x$ 軸」のように数式の外に書く）
- 複数行の式は $$\\begin{aligned} … \\end{aligned}$$ と書く（\\begin{align} は使わない）
- 角度の度は ^\\circ と書く（例: $120^\\circ$）。° や ^o、\\text{deg} などは使わない
- バックスラッシュは 1 本で書く（\\frac、\\sqrt、\\sin）。改行は本当に改行し、「\\n」と書かない
- KaTeX で表示できる命令だけを使う（\\root ではなく \\sqrt など）"""

GEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "問題文"},
        "hint": {"type": "string", "description": "ヒント"},
        "explanation": {"type": "string", "description": "解説（最後の行は「答え: $…$」）"},
    },
    "required": ["question", "hint", "explanation"],
}


def _has_japanese(text: str) -> bool:
    return any("\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff" for ch in text)


def gen_user(task: str, givens: list[str], answer_latex: str, steps: list[str], fmt: str) -> str:
    # 日本語の入った値（「10人」「3回」など）は $ の外に書かせる（$…$ の中の日本語は表示が崩れる）
    lines = [f"- {g}" if _has_japanese(g) else f"- ${g}$" for g in givens]
    step_lines = [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    if fmt == "choice":
        form = "選択式（選択肢は別に表示するので、問題文には選択肢を書かない。「次のうちから選べ」などとも書かない）"
    else:
        form = "答えを入力する形式（問題文には選択肢を書かない）"
    return "\n".join([
        f"問題の内容: {task}",
        f"出題の形式: {form}",
        "",
        "問題文に入れる式・値（$…$ のものは $…$ の中に、そうでないものは文章の中に、そのまま書き写すこと）:",
        *lines,
        "",
        f"正解: ${answer_latex}$",
        "",
        "解き方の要点（Python で計算済み。解説はこれに沿って書く）:",
        *step_lines,
        "",
        "問題文（question）、ヒント（hint）、解説（explanation）を、決められた出力の形で書いてください。",
    ])


def fix_user(original: str, previous: dict[str, Any], problem: str) -> str:
    """文章の確認で落ちたときに、指摘した点だけを直させる。前回の出力は同じ見出しの形で見せる
    （JSON で見せるとバックスラッシュが二重になり、それを書き写してしまうため）。"""
    shown = [f"[[{k}]]\n{previous.get(k, '')}" for k in ("question", "hint", "explanation")]
    return "\n".join([
        original,
        "",
        "あなたの前回の出力:",
        *shown,
        "",
        f"直してほしい点: {problem}",
        "上の指示をもう一度守り、この点を直した問題文（question）、ヒント（hint）、解説（explanation）を、"
        "決められた出力の形で書き直してください。式・値は $…$ の中に一字一句そのまま書き写し、数値は変えないでください。",
    ])


SOLVE_SYSTEM = """あなたは数学が得意な高校生です。与えられた問題を解いてください。
途中の考え方を work に短く書き、最終的な答えだけを answer に書きます。
answer は指定された答え方に従い、数式は Python / SymPy で読める形で書きます（分数は a/b、平方根は sqrt(n)、円周率は pi）。
問題の式を書き写すのではなく、計算した結果の値を書きます（sin や log、binomial などの関数を答えに残さない）。
単位や「x =」などの説明は付けません（答え方に書かれている場合を除く）。answer には答えだけを 1 行で書きます。"""

SOLVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "work": {"type": "string", "description": "考え方（短く）"},
        "answer": {"type": "string", "description": "最終的な答えだけ（1 行）"},
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


REVIEW_SYSTEM = """あなたは日本の高校数学の問題集の校閲者です。問題文・正解・解説を読み、次の点に明らかな誤りがないかだけを確かめてください。
1. 三角形の辺の名前の対応（a = BC, b = CA, c = AB）が、問題文と解説で正しく使われているか
2. 解説の途中式が 1 行ずつ正しいか（最終的な答えだけでなく、途中の計算も）
3. 解説の言葉による説明（上側・下側、増加・減少、正・負、最大・最小など）が式と矛盾していないか
4. 解説の各行の言葉（「正なので」「上側にある」など）と、その直後の式が矛盾していないか
5. ヒントがこの問題の解き方に合っているか（内積の問題で余弦定理を勧めていないか、など）
6. 問題文が自然な日本語で、問題として成り立っているか（同じことの言い直しや、意味の通らない文がないか）

正解は Python で計算済みで正しいものとします（正解を疑わない）。
誤りとするのは、値・式・論理・用語が違うものだけです。教科書でよく使う書き方（$\\log_2 (x-4)(x-6)$ のような
真数の括弧の省略、分数の書き方の違い、記号の選び方など）や、言い回しの好みは誤りとしません。
明らかな誤りがあるときだけ「問題あり」とし、どこがどう違うかを短く書きます。"""

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "description": "「問題なし」または「問題あり」のどちらか"},
        "issues": {"type": "string", "description": "誤りの箇所と理由（なければ「なし」）"},
    },
    "required": ["verdict"],
}


def review_user(question: str, answer_latex: str, explanation: str) -> str:
    return "\n".join(["問題文:", question, "", f"正解: ${answer_latex}$", "", "解説:", explanation])
