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


def normalize(text: str) -> str:
    s = unicodedata.normalize("NFKC", str(text))
    for src, dst in _REPLACE:
        s = s.replace(src, dst)
    for token in _REMOVE:
        s = s.replace(token, "")
    s = re.sub(r"\s+", "", s)
    s = s.replace("{", "").replace("}", "")
    return s


def contains(text: str, snippet: str) -> bool:
    return normalize(snippet) in normalize(text)


def missing(text: str, snippets: list[str]) -> list[str]:
    """text に入っていない snippet の一覧。"""
    return [s for s in snippets if not contains(text, s)]
