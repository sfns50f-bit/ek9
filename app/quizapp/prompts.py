"""LLM に渡すプロンプトと JSON スキーマ。"""

from __future__ import annotations

from .topics import Topic, difficulty_prompt

KINDS = ("value", "set", "tuple", "matrix", "interval", "antiderivative")

# kind: (説明, answer の書き方の例, verify_code で answer に入れるもの)
KIND_GUIDE: dict[str, tuple[str, str, str]] = {
    "value": ("1つの数または式", "3/2, sqrt(3)/2, 2*x**2 - 1, pi/6", "数や式"),
    "set": ("複数の値（順不同）。方程式の解をすべて答える場合など", "[1, -3]", "値のリスト（solve の結果をそのまま入れてよい）"),
    "tuple": ("順序のある組。座標や連立方程式の解など。答える順番を問題文に書く", "[2, -1]", "問題文の順に値を並べたリスト"),
    "matrix": ("行列", "[[1, 2], [3, 4]]", "Matrix"),
    "interval": (
        "不等式の解（変数の範囲）",
        "1 < x < 3 や x < 1, 3 < x（カンマは「または」）。解がなければ 解なし、すべての実数なら すべての実数",
        "solve_univariate_inequality(不等式, x, relational=False) などで求めた集合",
    ),
    "antiderivative": ("不定積分（積分定数 C は answer に書かない）", "x**3/3 + sin(x)", "integrate(f, x) の結果"),
}

KIND_LABELS = {
    "value": "数・式",
    "set": "複数の値（順不同）",
    "tuple": "順序のある組",
    "matrix": "行列",
    "interval": "範囲（不等式の解）",
    "antiderivative": "不定積分",
}


def _kind_table() -> str:
    return "\n".join(
        f"- {kind}: {desc}。answer の例: {example} ／ verify_code の answer: {code}"
        for kind, (desc, example, code) in KIND_GUIDE.items()
    )


GEN_SYSTEM = f"""あなたは日本の高校・大学で長年数学を教えてきた教師です。学習者が自分で解いて答え合わせできる計算問題を1問作ります。

## 問題のルール
- 答えがただ1通りに定まる問題にする。証明問題・図を見ないと解けない問題・答えが文章になる問題は作らない。
- 問題文だけで解けるように、必要な条件をすべて書く。何を答えればよいか（求めるもの・答えの形）を問題文の最後にはっきり書く。
- 答えがきれいな値（整数・分数・根号・π など）になるように数値を選ぶ。
- 数式は LaTeX で書き、$...$ で囲む（例: $x^2 - 3x + 2 = 0$）。
- 角度は度数法なら「°」を付け、弧度法なら π を使う。

## 出力する項目
- question: 問題文
- explanation: 解き方と途中式を含む解説（学習者向け、日本語）
- answer_kind: 答えの形式（下の一覧から選ぶ）
- answer: 最終的な答え。LaTeX ではなく SymPy で読める書き方にする（例: 3/2, sqrt(3)/2, 2*x**2 - 1, pi/6, log(3)）
- verify_code: 答えを検算する Python (SymPy) のコード

## answer_kind の一覧
{_kind_table()}

## verify_code の書き方
- 問題文の条件をそのまま SymPy の式にして、計算で答えを求める。答えの値を直接書き込んではいけない。
- 最後に変数 answer に答えを代入する（入れるものは上の一覧のとおり）。
- 近似値（float や math モジュール）ではなく厳密な値（Rational, sqrt, pi など）で計算する。
- x, y, z, t, a, b, n, k は定義済みの記号として使ってよい。print は不要。
"""

GEN_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "explanation": {"type": "string"},
        "answer_kind": {"type": "string", "enum": list(KINDS)},
        "answer": {"type": "string"},
        "verify_code": {"type": "string"},
    },
    "required": ["question", "explanation", "answer_kind", "answer", "verify_code"],
}


def gen_user(topic: Topic, difficulty: int, subtopic: str, recent: list[str]) -> str:
    lines = [
        "次の条件で問題を1問作ってください。",
        f"- 科目: {topic.course}（{topic.level}向け）",
        f"- 単元: {topic.unit}",
        f"- 扱う内容: {subtopic}",
        f"- 難易度: {difficulty_prompt(topic, difficulty)}",
    ]
    if recent:
        lines.append("")
        lines.append("最近作った問題（これらとは数値や問い方が違う問題にしてください）:")
        lines.extend(f"- {q[:120]}" for q in recent)
    return "\n".join(lines)


SOLVE_SYSTEM = """あなたは数学が得意な学生です。与えられた問題を解いて答えを出します。

## 出力する項目
- work: 解き方の要点と途中式（簡潔に）
- answer: 最終的な答え。LaTeX ではなく SymPy で読める書き方にする（例: 3/2, sqrt(3)/2, 2*x**2 - 1）
- code: 問題文の条件を Python (SymPy) の式にして答えを計算し、最後に変数 answer に代入するコード。近似値ではなく厳密な値で計算する。x, y, z, t, a, b, n, k は定義済みの記号として使ってよい。
"""

SOLVE_SCHEMA = {
    "type": "object",
    "properties": {
        "work": {"type": "string"},
        "answer": {"type": "string"},
        "code": {"type": "string"},
    },
    "required": ["work", "answer", "code"],
}


def solve_user(question: str, kind: str) -> str:
    desc, example, code = KIND_GUIDE[kind]
    return (
        f"## 問題\n{question}\n\n"
        f"## 答えの形式\n{desc}。answer の書き方の例: {example}\n"
        f"code では answer に{code}を代入してください。"
    )


REVIEW_SYSTEM = """あなたは数学の試験問題を校閲する専門家です。問題文・想定解答・解説を読み、学習者に出してよい問題かを厳しく確認します。

確認すること:
- solvable: 問題文の情報だけで解けるか（条件の不足や矛盾がないか）
- unique: 答えが1通りに定まり、指定された形式で答えられるか
- answer_matches: 想定解答が、問題文で問われているものに正しく対応しているか
- explanation_ok: 解説に計算や論理の誤りがないか
- level_ok: 対象学年と難易度に合っているか

まず issues に気づいた問題点を具体的に書き（なければ空のリスト）、そのうえで各項目を true / false で答えてください。
"""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {"type": "array", "items": {"type": "string"}},
        "solvable": {"type": "boolean"},
        "unique": {"type": "boolean"},
        "answer_matches": {"type": "boolean"},
        "explanation_ok": {"type": "boolean"},
        "level_ok": {"type": "boolean"},
    },
    "required": ["issues", "solvable", "unique", "answer_matches", "explanation_ok", "level_ok"],
}

REVIEW_FLAGS = {
    "solvable": "条件不足・矛盾",
    "unique": "答えが1通りに定まらない",
    "answer_matches": "想定解答が問いに対応していない",
    "explanation_ok": "解説の誤り",
}


def review_user(topic: Topic, difficulty: int, question: str, kind: str, answer_latex: str,
                explanation: str) -> str:
    return (
        f"## 対象\n{topic.course}（{topic.level}向け）・{topic.unit}・難易度: {difficulty_prompt(topic, difficulty)}\n\n"
        f"## 問題\n{question}\n\n"
        f"## 答えの形式\n{KIND_GUIDE[kind][0]}\n\n"
        f"## 想定解答（Python で検算済み）\n${answer_latex}$\n\n"
        f"## 解説\n{explanation}"
    )
