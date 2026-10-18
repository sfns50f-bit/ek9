"""LLM が書いた文章に、決められた式・値がそのまま入っているかを機械的に確かめる。

問題文にはテンプレートが作った数値・式が、解説には正解が入っていなければならない。
LaTeX の書き方の揺れ（空白、{}、\\left/\\right、\\dfrac など）だけを吸収して、
部分文字列として含まれるかで判定する。数値を書き換えていれば一致しない。
"""

from __future__ import annotations

import re
import unicodedata

_REMOVE = [
    r"\displaystyle", r"\textstyle", r"\left", r"\right", r"\,", r"\;", r"\:", r"\!", r"\ ", "$",
]
_REPLACE = [
    (r"\dfrac", r"\frac"), (r"\tfrac", r"\frac"), (r"\leq", r"\le"), (r"\geq", r"\ge"),
    (r"\leqq", r"\le"), (r"\geqq", r"\ge"), ("≦", r"\le"), ("≤", r"\le"), ("≧", r"\ge"), ("≥", r"\ge"),
    ("°", r"^\circ"), ("−", "-"), ("–", "-"), ("π", r"\pi"), ("×", r"\times"), (r"\cdot", r"\times"),
]
# \leqq → \le を \leq → \le より先に処理しないと "\leqq" が "\leq" + "q" になるので、長い方から置換する
_REPLACE.sort(key=lambda kv: -len(kv[0]))


# 数字のあとの度の記号の書き方の揺れ（30^\circ, 30\circ, 30^o, 30º, 30˚, 30∘, 30度 など）
_DEGREE_RE = re.compile(r"(\d)\s*(?:\^\s*\{?\s*(?:\\circ|o|∘)\s*\}?|\\circ|\\degree|[°º˚∘]|度)")


def normalize(text: str) -> str:
    s = _DEGREE_RE.sub(r"\1°", str(text))  # NFKC で º が o に変わる前に揃える
    s = unicodedata.normalize("NFKC", s)
    s = _DEGREE_RE.sub(r"\1°", s)
    s = s.replace("∠", r"\angle ")
    for src, dst in _REPLACE:
        s = s.replace(src, dst)
    for token in _REMOVE:
        s = s.replace(token, "")
    s = re.sub(r"\s+", "", s)
    s = s.replace("{", "").replace("}", "")
    return s


def contains(text: str, snippet: str) -> bool:
    """snippet が text に入っているか。数字の途中での一致（"12人" の中の "2人" など）は数えない。"""
    t, n = normalize(text), normalize(snippet)
    if not n:
        return True
    start = 0
    while (i := t.find(n, start)) >= 0:
        before = t[i - 1] if i > 0 else ""
        after = t[i + len(n)] if i + len(n) < len(t) else ""
        if not (n[0].isdigit() and before.isdigit()) and not (n[-1].isdigit() and after.isdigit()):
            return True
        start = i + 1
    return False


Snippet = str | list[str] | tuple[str, ...]


def first(snippet: Snippet) -> str:
    """書き方の候補があるときは、最初の書き方（LLM に見せる書き方）。"""
    return snippet if isinstance(snippet, str) else snippet[0]


def missing(text: str, snippets: list[Snippet]) -> list[str]:
    """text に入っていない snippet の一覧。候補のリストはどれか 1 つ入っていればよい。"""
    out = []
    for s in snippets:
        options = [s] if isinstance(s, str) else list(s)
        if not any(contains(text, o) for o in options):
            out.append(first(s))
    return out


_CTRL_MAP = {"\x08": "b", "\x0c": "f", "\t": "t", "\r": "r", "\n": "n"}
_CTRL_RE = re.compile(r"[\x08\x0c\t\r\n](?=[A-Za-z])")


def repair_latex(text: str) -> str:
    r"""JSON の "\frac" が改ページ文字 + "rac" になる、といった崩れを数式の中だけ直す。"""
    out: list[str] = []
    in_math, delim = False, ""
    for part in re.split(r"(\$\$|\$)", text):
        if part in ("$", "$$"):
            if not in_math:
                in_math, delim = True, part
            elif part == delim:
                in_math = False
            out.append(part)
        elif in_math:
            out.append(_CTRL_RE.sub(lambda m: "\\" + _CTRL_MAP[m.group()], part))
        else:
            out.append(part)
    return "".join(out)
