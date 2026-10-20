"""LLM が書いた文章の LaTeX の表記をそろえる（normalize）・崩れを見つける（problems）。

sympy を使わない（web からも使う：問題検証モードで、前に作った問題の表記をそろえて点検する）。

normalize で直すもの（意味を変えずに機械的に直せるもの）:
  - 度の記号の揺れ：数字のあとの ^\\top・^\\rho・^\\times・^°・^\\text{o}・\\u00b0 など → ^\\circ（数式の中）／°（数式の外）
  - 英字の前の二重のバックスラッシュ \\\\sin → \\sin
  - 文字どおりの「\\n」→ 改行、「\\u00b0」などの \\uXXXX → その文字
  - JSON の読み取りで制御文字になったもの（改ページ＋rac → \\frac など）
problems で見つけるもの（直せないので、作成役に直させるか不合格にする）:
  - 制御文字、$ の数が奇数、\\$、数式の中の改行・日本語、{} の対応、KaTeX にない命令
  - 中国語の簡体字（日本語の文字コード cp932 にない漢字）
  - 選択式なのに「示せ」「説明せよ」、問題文の文末が「求める。」
"""

from __future__ import annotations

import re

# --- そろえる -------------------------------------------------------------------

_CTRL_LETTER = {"\x08": "b", "\x0c": "f", "\t": "t", "\r": "r", "\x07": "a", "\x0b": "v"}
_CTRL_LETTER_RE = re.compile(r"[\x07\x08\x0b\x0c\t\r](?=[A-Za-z])")
# 文字どおりの \n（LaTeX の \neq・\nabla・\not・\nu などは除く）
_LITERAL_NL_RE = re.compile(r"\\n(?!(?:e|eq|eg|abla|ot[a-z]*|u|i|leq|geq|mid|ewline|parallel|subseteq)(?![A-Za-z]))")
_UNICODE_ESC_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
_DOUBLE_BS_RE = re.compile(r"\\\\(?=[A-Za-z])")
# 数字のあとの ^ に続く「度」のつもりの書き方（^\circ そのものも含めてそろえる）
_DEG_BODY = (r"(?:\\circ|\{\s*\\circ\s*\}|\\textcirc|\\degree"
             r"|\\(?:text|mathrm)\s*\{\s*(?:\\?circ|deg|o|°|\\u00b0)\s*\}"
             r"|\\(?:top|rho|theta|times|bullet|triangle|ngle|circle)(?![A-Za-z])"
             r"|\\[a-z]{1,3}\s*\{[^{}]*circ[^{}]*\}|\{[^{}]*(?:circ|°|deg)[^{}]*\}|[°º˚∘]|o(?![A-Za-z])|\{\s*o\s*\})")
_DEG_IN_MATH_RE = re.compile(r"(\d)\s*(?:\^\s*" + _DEG_BODY + r"|[°º˚∘])")
_DEG_OUT_MATH_RE = re.compile(r"(\d)\s*\^\s*" + _DEG_BODY)
_DOLLAR_SPLIT_RE = re.compile(r"(?<!\\)\$")


def _fix_math(seg: str) -> str:
    return _DEG_IN_MATH_RE.sub(r"\1^\\circ", seg)


def normalize(text: str) -> str:
    """意味を変えずに直せる表記の崩れを直す。"""
    s = str(text or "")
    s = _CTRL_LETTER_RE.sub(lambda m: "\\" + _CTRL_LETTER[m.group()], s)
    s = _UNICODE_ESC_RE.sub(lambda m: chr(int(m.group(1), 16)), s)
    s = _DOUBLE_BS_RE.sub(r"\\", s)
    parts = _DOLLAR_SPLIT_RE.split(s)
    if len(parts) % 2 == 1:  # $ の対応がとれているときだけ、数式の中と外を分けて直す
        for i in range(len(parts)):
            parts[i] = _fix_math(parts[i]) if i % 2 else _DEG_OUT_MATH_RE.sub(r"\1°", parts[i])
        s = "$".join(parts)
    s = re.sub(r"\\ngle(?![A-Za-z])", r"\\angle", s)  # \angle が \n＋gle になったもの
    s = _LITERAL_NL_RE.sub("\n", s)
    return s


# --- 見つける -------------------------------------------------------------------

# KaTeX で使える命令のうち、高校数学の問題集で使いそうなもの。これ以外は「表示できない（かもしれない）命令」
KATEX_COMMANDS = set("""
frac dfrac tfrac cfrac sqrt sin cos tan sec csc cot arcsin arccos arctan log ln lg exp lim max min sup inf det
pi theta alpha beta gamma delta epsilon varepsilon zeta eta iota kappa lambda mu nu xi rho sigma tau upsilon phi
varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Phi Psi Omega
left right bigl bigr Bigl Bigr big Big bigg Bigg middle
cdot times div pm mp le ge leq geq leqq geqq leqslant geqslant neq ne lt gt approx fallingdotseq equiv sim simeq
cong propto infty int iint oint sum prod to rightarrow leftarrow Rightarrow Leftarrow leftrightarrow
Leftrightarrow iff implies longrightarrow mapsto therefore because circ degree angle triangle perp parallel mid
nmid overrightarrow overleftarrow vec overline underline widehat hat bar dot ddot tilde text textbf textit mathrm
mathbf mathit mathbb mathcal boxed quad qquad ldots cdots dots vdots ddots binom dbinom tbinom choose begin end
hline colon cap cup in notin ni subset subseteq supset supseteq subsetneq emptyset varnothing setminus forall
exists neg land lor lnot displaystyle textstyle scriptstyle operatorname not star ast bullet prime backslash
overbrace underbrace stackrel overset underset xrightarrow square blacksquare checkmark nabla partial ell
lfloor rfloor lceil rceil langle rangle vert Vert lbrace rbrace
""".split())
_CMD_RE = re.compile(r"\\([A-Za-z]+)")
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_TEXT_ARG_RE = re.compile(r"\\(?:text|textbf|textit|mathrm|operatorname)\s*\{[^{}]*\}")
_PROVE_RE = re.compile(r"示せ|証明せよ|説明せよ|過程(?:も|を)")


def _simplified_chinese(text: str) -> list[str]:
    """日本語の文字コード（cp932）にない漢字。中国語の簡体字の混入の目安。"""
    out = []
    for ch in dict.fromkeys(_CJK_RE.findall(text)):
        if "\u4e00" <= ch <= "\u9fff":
            try:
                ch.encode("cp932")
            except UnicodeEncodeError:
                out.append(ch)
    return out


def problems(text: str, *, field: str = "", choice: bool = False) -> list[str]:
    """表記の崩れの一覧（なければ空）。field は「問題文」「解説」など、指摘の文に使う。"""
    s = str(text or "")
    name = field or "文章"
    out: list[str] = []
    ctrl = sorted({f"\\x{ord(c):02x}" for c in s if ord(c) < 0x20 and c != "\n"})
    if ctrl:
        out.append(f"{name}に制御文字（{'・'.join(ctrl)}）が入っている")
    if "\\$" in s:
        out.append(f"{name}に「\\$」がある（数式は $…$ で囲む）")
    parts = _DOLLAR_SPLIT_RE.split(s)
    if len(parts) % 2 == 0:
        out.append(f"{name}の $ の数が奇数で、数式の始まりと終わりが対応していない")
        parts = parts[:-1]
    bad_cmds: list[str] = []
    for i in range(1, len(parts), 2):
        seg = parts[i]
        if "\n" in seg:
            out.append(f"{name}の数式 ${seg.strip()[:30]}…$ の中で改行している")
        if _CJK_RE.search(_TEXT_ARG_RE.sub("", seg)):
            out.append(f"{name}の数式 ${seg.strip()[:30]}$ の中に日本語がある（日本語は $ の外に書く）")
        if seg.count("{") != seg.count("}"):
            out.append(f"{name}の数式 ${seg.strip()[:30]}$ の {{ }} の数が合わない")
        bad_cmds += [c for c in _CMD_RE.findall(seg) if c not in KATEX_COMMANDS]
    if bad_cmds:
        shown = "、".join(dict.fromkeys("\\" + c for c in bad_cmds))
        out.append(f"{name}に KaTeX で表示できない命令がある（{shown}）")
    zh = _simplified_chinese(s)
    if zh:
        out.append(f"{name}に中国語の字（{''.join(zh)}）が入っている")
    if choice and _PROVE_RE.search(s):
        out.append(f"選択式なのに{name}に「示せ」「説明せよ」「過程」などがある")
    return out


def question_problems(question: str, *, choice: bool) -> list[str]:
    out = problems(question, field="問題文", choice=choice)
    tail = re.sub(r"[\s。．.]+$", "", question.strip())
    if tail.endswith(("求める", "答える", "計算する")):
        out.append("問題文の文末が「求める。」になっている（「求めよ。」のような問いかけで終える）")
    return out
