"""LLM が書いた文章の LaTeX の表記をそろえる（normalize）・崩れを見つける（problems）。

sympy を使わない（web からも使う：問題検証モードで、前に作った問題の表記をそろえて点検する）。

normalize で直すもの（意味を変えずに機械的に直せるもの）:
  - 度の記号の揺れ：数字のあとの ^\\top・^\\textcirc・^°・^\\text{o}・\\u00b0 など → ^\\circ（数式の中）／°（数式の外）。
    ^\\theta・^\\rho・^\\times・^o は本当の指数のこともあるので、角度の話（角・∠・° など）のときだけ
  - 英字の前の二重のバックスラッシュ \\\\sin → \\sin（後ろが KaTeX の命令のときだけ。\\\\ の改行は残す）
  - 文字どおりの「\\n」→ 改行（後ろが英字でないときだけ。\\nearrow・\\neq などは残す）、「\\u00b0」などの \\uXXXX → その文字
  - JSON の読み取りで制御文字になったもの（改ページ＋rac → \\frac など）
problems で見つけるもの（直せないので、作成役に直させるか不合格にする）:
  - 制御文字・¶、$ の対応、\\$、数式の中の改行・日本語、{} の対応、\\frac の引数の数、
    KaTeX にない命令（アプリに同梱の katex.min.js から命令の一覧を作る）、バックスラッシュの抜け（rac{・egin{ など）
  - 中国語の簡体字（日本語の文字コード cp932 にない漢字）
  - 選択式なのに「示せ」「説明せよ」、問題文の文末が「求める。」
"""

from __future__ import annotations

import functools
import os
import re
from typing import Callable

# --- KaTeX の命令の一覧 ----------------------------------------------------------

_KATEX_JS = os.path.join(os.path.dirname(__file__), "static", "katex", "katex.min.js")
# 1 文字の命令のうち KaTeX で使えるもの（katex.min.js から拾うと \x のような関係ない文字列も混ざるので、1 文字は別に持つ）
_ONE_LETTER = {"i", "j", "o", "O", "l", "L", "S", "P", "H", "u", "v", "c", "d", "b", "r", "t"}
_FALLBACK = set("""
frac dfrac tfrac sqrt sin cos tan log ln exp lim max min pi theta alpha beta gamma delta left right cdot times div
pm mp le ge leq geq leqq geqq neq ne approx infty int sum to rightarrow Rightarrow circ degree angle triangle
overrightarrow vec overline text mathrm boxed quad qquad ldots cdots binom begin end therefore because
""".split())


@functools.cache
def katex_commands() -> frozenset[str]:
    """アプリに同梱している KaTeX が知っている命令の名前（\\ なし）。"""
    try:
        with open(_KATEX_JS, encoding="utf-8") as f:
            names = set(re.findall(r'"\\\\([A-Za-z]+)"', f.read()))
    except OSError:
        names = set(_FALLBACK)
    return frozenset({n for n in names if len(n) >= 2} | _ONE_LETTER)


# --- 数式の範囲 -----------------------------------------------------------------

# $$…$$・\[…\]・\(…\)・$…$（画面の app.js と同じ区切り）。\$ は区切りではない
_MATH_RE = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|\\\((.+?)\\\)|(?<!\\)\$(.+?)(?<!\\)\$", re.S)


def _body(m: re.Match[str]) -> str:
    return next(g for g in m.groups() if g is not None)


def _math_segments(text: str) -> list[str]:
    return [_body(m) for m in _MATH_RE.finditer(text)]


def _prose(text: str) -> str:
    return _MATH_RE.sub(" ", text)


def _map_math(text: str, in_math: Callable[[str], str], out_math: Callable[[str], str]) -> str:
    parts, pos = [], 0
    for m in _MATH_RE.finditer(text):
        parts.append(out_math(text[pos:m.start()]))
        body = _body(m)
        start = m.start(next(i for i, g in enumerate(m.groups(), 1) if g is not None))
        parts.append(text[m.start():start] + in_math(body) + text[start + len(body):m.end()])
        pos = m.end()
    parts.append(out_math(text[pos:]))
    return "".join(parts)


# --- そろえる -------------------------------------------------------------------

_CTRL_LETTER = {"\x08": "b", "\x0c": "f", "\t": "t", "\r": "r", "\x07": "a", "\x0b": "v"}
_CTRL_LETTER_RE = re.compile(r"[\x07\x08\x0b\x0c\t\r](?=[A-Za-z])")
_LITERAL_NL_RE = re.compile(r"\\n(?![A-Za-z])")  # 後ろが英字なら \nearrow・\neq などの命令
_UNICODE_ESC_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
_DOUBLE_BS_RE = re.compile(r"\\\\([A-Za-z]+)")
# 数字のあとの ^ に続く「度」のつもりの書き方。いつでも度と読めるもの
_DEG_SURE = (r"\\circ|\{\s*\\circ\s*\}|\\textcirc|\\degree|\\(?:text|mathrm)\s*\{\s*\\?(?:circ|deg|°|o)\s*\}"
             r"|\\(?:top|bullet|triangle|ngle|circle)(?![A-Za-z])|\\[a-z]{1,3}\s*\{[^{}]*circ[^{}]*\}"
             r"|\{[^{}]*(?:circ|°|deg)[^{}]*\}|[°º˚∘]")
# 本当の指数のこともあるもの（2^θ、x^o など）。角度の話のときだけ度とみなす
_DEG_MAYBE = r"\\(?:theta|rho|times)(?![A-Za-z])|o(?![A-Za-z])|\{\s*o\s*\}"
_ANGLE_CONTEXT_RE = re.compile(r"角|∠|\\angle|°|\\circ|度|三角形|△|弧度")
_DEG = {flag: re.compile(r"(\d)\s*\^\s*(?:" + _DEG_SURE + ("|" + _DEG_MAYBE if flag else "") + ")")
        for flag in (False, True)}
_BARE_DEG_RE = re.compile(r"(\d)\s*[°º˚∘]")


# 中国語の簡体字のうち、日本語の字に置き換えても意味が変わらないもの（数学の文章で出てきたもの）
_ZH_TO_JA = str.maketrans({
    "计": "計", "边": "辺", "长": "長", "时": "時", "题": "題", "问": "問", "间": "間", "设": "設", "则": "則",
    "线": "線", "积": "積", "两": "両", "选": "選", "项": "項", "说": "説", "对": "対", "应": "応", "关": "関",
    "实": "実", "现": "現", "习": "習", "简": "簡", "单": "単", "种": "種", "图": "図", "数": "数",
})
# $ で囲まれていない数式の環境。画面の KaTeX は $$…$$ などの中しか表示しないので、$$\begin{aligned}…$$ に直す
_BARE_ENV_RE = re.compile(r"\\begin\{(align\*?|aligned|equation\*?|eqnarray\*?|gather\*?|gathered|cases)\}"
                          r"(.*?)\\end\{\1\}", re.S)
_ENV_RENAME = {"align": "aligned", "align*": "aligned", "equation": "aligned", "equation*": "aligned",
               "eqnarray": "aligned", "eqnarray*": "aligned", "gather": "gathered", "gather*": "gathered"}
_ANSWER_LINE_RE = re.compile(r"^(答え[ \t]*[:：][ \t]*)([^$\n]+?)[ \t]*$", re.M)
_CJK_ANY_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


def _fix_environments(s: str) -> str:
    def inside(body: str) -> str:  # 数式の中の align など → aligned（KaTeX は文中では align を表示できない）
        return re.sub(r"\\(begin|end)\{(align\*?|equation\*?|eqnarray\*?|gather\*?)\}",
                      lambda m: f"\\{m.group(1)}{{{_ENV_RENAME[m.group(2)]}}}", body)

    def outside(t: str) -> str:
        return _BARE_ENV_RE.sub(lambda m: "$$\\begin{" + _ENV_RENAME.get(m.group(1), m.group(1)) + "}" + m.group(2)
                                + "\\end{" + _ENV_RENAME.get(m.group(1), m.group(1)) + "}$$", t)

    return _map_math(s, inside, outside)


def _wrap_answer_line(s: str) -> str:
    """「答え: 13/2」のように $ のない答えの行を「答え: $13/2$」にする（日本語が入っていないときだけ）。"""
    # 日本語・% （KaTeX ではコメントの始まり）・Markdown の * が入っているときは囲まない
    return _ANSWER_LINE_RE.sub(lambda m: m.group(0) if _CJK_ANY_RE.search(m.group(2)) or re.search(r"[%*]", m.group(2))
                               else f"{m.group(1)}${m.group(2)}$", s)


def normalize(text: str) -> str:
    """意味を変えずに直せる表記の崩れを直す。"""
    s = str(text or "").translate(_ZH_TO_JA)
    s = _CTRL_LETTER_RE.sub(lambda m: "\\" + _CTRL_LETTER[m.group()], s)
    s = _UNICODE_ESC_RE.sub(lambda m: chr(int(m.group(1), 16)), s)
    s = _wrap_answer_line(s)  # 先に $ で囲む（そのあとの度の記号の直しが数式の中として効くように）
    cmds = katex_commands()
    s = _DOUBLE_BS_RE.sub(lambda m: "\\" + m.group(1) if m.group(1) in cmds else m.group(0), s)
    deg = _DEG[bool(_ANGLE_CONTEXT_RE.search(s))]
    s = _map_math(s, lambda b: _BARE_DEG_RE.sub(r"\1^\\circ", deg.sub(r"\1^\\circ", b)),
                  lambda t: deg.sub(r"\1°", t))
    s = re.sub(r"\\ngle(?![A-Za-z])", r"\\angle", s)  # \angle が \n＋gle になったもの
    s = _LITERAL_NL_RE.sub("\n", s)
    s = _fix_environments(s)
    return s


# --- 見つける -------------------------------------------------------------------

_CMD_RE = re.compile(r"\\([A-Za-z]+)")
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_TEXT_ARG_RE = re.compile(r"\\(?:text|textbf|textit|textrm|mathrm|operatorname|mbox)\s*\{[^{}]*\}")
_PROVE_RE = re.compile(r"示せ|証明せよ|説明せよ|過程(?:も|を)")
# 作成役への指示や要点そのものに触れた文（「与えられた解き方の要点に従うと」など）
_META_RE = re.compile(r"解き方の要点|要点に(?:従|沿)|与えられた(?:解き方|正解|指示|要点)|指示に従|プロンプト|Python|テンプレート")
# 誤った用語 → 正しい用語
_WRONG_TERMS = [
    (re.compile(r"2\s*点(?:形|間)の式|二点(?:形|間)の式"), "「点と傾きの式（点斜式）」または「2 点を通る直線の式」"),
    (re.compile(r"微分方程式"), "「方程式」（f'(x)=0 は微分方程式ではない）"),
    (re.compile(r"無理数を払"), "「分母を有理化する」"),
    (re.compile(r"二乗差の積分"), "正しい公式の名前（例: 1/6 公式）"),
]
# 「(x + 0)」「- 0」のような不要な項
# 「(x + 0)」のような因数だけを見る（「8 - 0 = 8」「y - 0 = 2(x-1)」のような計算の途中は正しいので見ない）
_ZERO_TERM_RE = re.compile(r"(?:\\left)?\(\s*[a-z]\s*[+-]\s*0(?![\d.])\s*(?:\\right)?\)")
# 制御文字になって消えたバックスラッシュの名残（\frac → rac{、\begin → egin{、\times → imes など）
_RESIDUE_RE = re.compile(r"(?<![\\A-Za-z])(rac\s*\{|egin\s*\{|imes(?![A-Za-z])|heta(?![A-Za-z])|qrt\s*\{)")
_FRAC_RE = re.compile(r"\\(?:d|t|c)?frac(?![A-Za-z])|\\binom(?![A-Za-z])")


def _arg_count(seg: str, pos: int) -> int:
    """pos から始まる引数（{…}、命令、1 文字）の数を 2 つまで数える。"""
    n = 0
    while n < 2:
        while pos < len(seg) and seg[pos] == " ":
            pos += 1
        if pos >= len(seg) or seg[pos] in "}^_=+-)]":
            break
        if seg[pos] == "{":
            depth, end = 0, -1
            for j in range(pos, len(seg)):
                depth += {"{": 1, "}": -1}.get(seg[j], 0)
                if depth == 0:
                    end = j
                    break
            if end < 0:
                break
            pos = end + 1
        elif seg[pos] == "\\":
            m = _CMD_RE.match(seg, pos)
            pos = m.end() if m else pos + 2
        else:
            pos += 1
        n += 1
    return n


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
    if "¶" in s:
        out.append(f"{name}に「¶」が入っている")
    if "\\$" in s:
        out.append(f"{name}に「\\$」がある（数式は $…$ で囲む）")
    if "$" in _prose(s).replace("\\$", ""):
        out.append(f"{name}の $ の数が合わず、数式の始まりと終わりが対応していない")
    outside_cmds = [c for c in _CMD_RE.findall(_prose(s)) if c not in ("n",)]
    if outside_cmds:
        shown = "、".join(dict.fromkeys("\\" + c for c in outside_cmds))
        out.append(f"{name}で数式の外に LaTeX の命令（{shown}）がある（数式は $…$ で囲む）")
    if _META_RE.search(s):
        out.append(f"{name}が作成の指示や「解き方の要点」そのものに触れている（指示には触れずに書く）")
    for pattern, right in _WRONG_TERMS:
        if pattern.search(s):
            out.append(f"{name}の用語「{pattern.search(s).group(0)}」が誤り（{right}）")
    cmds = katex_commands()
    bad_cmds: list[str] = []
    for seg in _math_segments(s):
        if not re.search(r"[0-9A-Za-z\\]", seg):
            out.append(f"{name}に中身のない数式（${seg}$）がある")
        if _ZERO_TERM_RE.search(seg):
            out.append(f"{name}の数式 ${seg.strip()[:30]}$ に「+ 0」のような不要な項がある")
        short = seg.strip().replace("\n", " ")[:30]
        if "\n" in seg.strip() and "\\begin" not in seg and "\\\\" not in seg:
            out.append(f"{name}の数式 ${short}…$ の中で改行している")
        if _CJK_RE.search(_TEXT_ARG_RE.sub("", seg)):
            out.append(f"{name}の数式 ${short}$ の中に日本語がある（日本語は $ の外に書く）")
        if seg.count("{") != seg.count("}"):
            out.append(f"{name}の数式 ${short}$ の {{ }} の数が合わない")
        elif any(_arg_count(seg, m.end()) < 2 for m in _FRAC_RE.finditer(seg)):
            out.append(f"{name}の数式 ${short}$ の \\frac（または \\binom）の引数が 2 つない")
        if _RESIDUE_RE.search(seg):
            out.append(f"{name}の数式 ${short}$ で命令のバックスラッシュが抜けている（rac{{・egin{{・imes など）")
        bad_cmds += [c for c in _CMD_RE.findall(seg) if c not in cmds]
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
