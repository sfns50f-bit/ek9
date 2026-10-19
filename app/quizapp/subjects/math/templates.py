"""数学のテンプレート。数値・正解・誤答を Python（sympy）で作る。

1 つのテンプレートは乱数から Instance を作る関数で、@template("小単元ID", unit=..., label=..., level=...) で
登録する。@template の引数は sympy を読まずに catalog.template_info() が読む（web 用）ので、文字列・タプルの
そのままの値で書く。
LLM はここで作った数値と正解を受け取って、問題文・解説・ヒントの文章だけを書く。

Instance の中身:
  task        何を求める問題か（LLM への説明）
  givens      問題文にそのまま入れる式・値（LaTeX）。入っていなければ不合格にする
  answer      正解（sympy）。kind は sandbox の mathcheck の答えの形式
  candidates  誤答の候補と「間違いの種類」。よくある間違いを実際に計算して作る。
              正解や他の候補と一致するものは、sandbox で同値判定して取り除く
  steps       解き方の要点（Python で計算済みの途中の値）。LLM が解説を書くときに使う
  fmt         答え・選択肢の LaTeX の書き方
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable

import sympy as sp

from .catalog import TemplateInfo

x = sp.Symbol("x")
L = sp.latex


@dataclass
class Cand:
    value: Any
    mistake: str


@dataclass
class Instance:
    task: str
    givens: list[Any]  # 文字列、または同じ式の書き方の候補のリスト（最初のものを LLM に見せる）
    answer: Any
    candidates: list[Cand]
    steps: list[str]
    params: dict[str, Any]
    kind: str = "value"
    var: str = "x"
    input_hint: str = "計算した値を入力"
    fmt: Callable[[Any], str] = L
    # 解説に入っていなければならない正解の書き方（省略時は fmt(answer)）
    checks: Callable[[Any], list[str]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def answer_checks(self, value: Any = None) -> list[str]:
        value = self.answer if value is None else value
        return self.checks(value) if self.checks else [self.fmt(value)]


# キーボード入力の答え方。例に具体的な値を書くと、正解と同じ値になって答えが見えてしまう
# ことがあるので、文字（a/b など）で書く（tests/test_math_templates.py で確認）
HINT_NUM = "計算した値を入力（分数は a/b、√ は sqrt(n) または √n、負の数は -a）"
HINT_INT = "整数で入力（負の数は -a）"

TEMPLATES: dict[str, Callable[[random.Random], Instance]] = {}
INFO: dict[str, TemplateInfo] = {}


def template(tid: str, *, unit: str, label: str, level: str, formats: tuple[str, ...] = ("choice", "input"),
             related: tuple[str, ...] = ()) -> Callable[[Callable[[random.Random], Instance]],
                                                        Callable[[random.Random], Instance]]:
    """テンプレートを登録する。unit は catalog.UNITS の単元 ID、level は LEVELS のキー。"""
    def deco(fn: Callable[[random.Random], Instance]) -> Callable[[random.Random], Instance]:
        TEMPLATES[tid] = fn
        INFO[tid] = TemplateInfo(tid, unit, label, level, formats, related)
        return fn
    return deco


# ---------------------------------------------------------------------------
# 書き方の補助
# ---------------------------------------------------------------------------

def poly(expr: Any) -> str:
    return L(sp.expand(expr), order="lex")


def signed(v: Any) -> str:
    """+ 3 / - 3 のように符号つきで書く（式の途中に入れる用）。"""
    v = sp.sympify(v)
    return f"- {L(-v)}" if v.could_extract_minus_sign() else f"+ {L(v)}"


def tex_tuple(t: Any) -> str:
    return r"\left(" + ", ".join(L(v) for v in t) + r"\right)"


def tex_interval(s: sp.Set, var: str = "x") -> str:
    pieces = list(s.args) if isinstance(s, sp.Union) else [s]
    out = []
    for p in pieces:
        lop = "<" if p.left_open else r"\leqq"
        hip = "<" if p.right_open else r"\leqq"
        if p.start == -sp.oo:
            out.append(f"{var} {hip} {L(p.end)}")
        elif p.end == sp.oo:
            out.append(f"{L(p.start)} {lop} {var}")
        else:
            out.append(f"{L(p.start)} {lop} {var} {hip} {L(p.end)}")
    return r",\ ".join(out)


def interval_checks(s: sp.Set) -> list[str]:
    return tex_interval(s).split(r",\ ")


def tex_set_eq(values: Any, var: str = "x") -> str:
    return ", ".join(f"{var} = {L(v)}" for v in values)


def deg(v: Any) -> str:
    return f"{L(v)}^\\circ"


def coef_term(c: Any, body: str) -> str:
    """係数つきの項（1 は省略、-1 は - だけ）。"""
    c = sp.sympify(c)
    if c == 1:
        return body
    if c == -1:
        return f"-{body}"
    return f"{L(c)}{body}"


def triangle_givens(b: int, c: int, A: int) -> list[Any]:
    """三角形 ABC の 2 辺とその間の角。∠BAC や「CA = 3」のような書き方も同じ値として認める。

    （値の書き換えは数字で確かめる。辺や角の取り違えは検証役が解いて気づく）
    """
    return [
        [f"b = {b}", f"CA = {b}", f"AC = {b}"],
        [f"c = {c}", f"AB = {c}", f"BA = {c}"],
        [f"A = {A}^\\circ", f"{A}^\\circ",
         # 度の記号を書かない・弧度法で書く場合も、値が変わっていなければ認める
         *(f"{name} = {v}" for name in ("A", "\\angle A", "\\angle BAC", "\\angle CAB")
           for v in (str(A), L(sp.pi * A / 180)))],
    ]


def frac_tex(v: Any) -> str:
    return L(sp.nsimplify(v))


# ---------------------------------------------------------------------------
# 数学I 2次関数
# ---------------------------------------------------------------------------

@template("m1-quad-vertex", unit="m1-quad", label="放物線の頂点", level="basic")
def quad_vertex(rng: random.Random) -> Instance:
    a = rng.choice([2, 3, -1, -2, -3])
    p = rng.choice([-4, -3, -2, -1, 1, 2, 3, 4])
    q = rng.choice([-7, -5, -4, -3, -2, -1, 1, 2, 3, 5, 6])
    f = sp.expand(a * (x - p) ** 2 + q)
    b, c = f.coeff(x, 1), f.coeff(x, 0)
    ans = sp.Tuple(p, q)
    wrong_p = -b / 2  # a でくくらずに平方完成した
    return Instance(
        task="2次関数のグラフ（放物線）の頂点の座標を求める問題",
        givens=[f"y = {poly(f)}"],
        answer=ans,
        kind="tuple",
        candidates=[
            Cand(sp.Tuple(-p, q), "sign"),
            Cand(sp.Tuple(p, -q), "sign"),
            Cand(sp.Tuple(wrong_p, c - b ** 2 / 4), "coef"),
            Cand(sp.Tuple(p, c), "swap"),
            Cand(sp.Tuple(-p, -q), "sign"),
        ],
        steps=[
            f"平方完成すると y = {L(a)}\\left(x {signed(-p)}\\right)^2 {signed(q)}",
            f"頂点の座標は {tex_tuple(ans)}",
        ],
        params={"a": a, "p": p, "q": q},
        fmt=tex_tuple,
        input_hint="頂点の座標を (x座標, y座標) の形で入力（2つの数はカンマで区切る）",
    )


@template("m1-quad-ineq", unit="m1-quad", label="2次不等式", level="basic")
def quad_ineq(rng: random.Random) -> Instance:
    while True:
        al, be = sorted(rng.sample(range(-6, 9), 2))
        if al + be != 0 and al * be != 0:
            break
    f = sp.expand((x - al) * (x - be))
    less = rng.random() < 0.5
    closed = rng.random() < 0.4  # ≦・≧ のときは境目を含む
    rel = ("\\leqq" if less else "\\geqq") if closed else ("<" if less else ">")

    def between(a: Any, b: Any, inside: bool, with_ends: bool) -> sp.Set:
        if inside:
            return sp.Interval(a, b) if with_ends else sp.Interval.open(a, b)
        if with_ends:
            return sp.Union(sp.Interval(-sp.oo, a), sp.Interval(b, sp.oo))
        return sp.Union(sp.Interval.open(-sp.oo, a), sp.Interval.open(b, sp.oo))

    ans = between(al, be, less, closed)
    return Instance(
        task="2次不等式を解く問題",
        givens=[f"{poly(f)} {rel} 0"],
        answer=ans,
        kind="interval",
        candidates=[
            Cand(between(al, be, not less, closed), "ineq"),
            Cand(between(-be, -al, less, closed), "sign"),
            Cand(between(al, be, less, not closed), "boundary"),
            Cand(between(-be, -al, not less, closed), "sign"),
        ],
        steps=[
            f"左辺を因数分解すると \\left(x {signed(-al)}\\right)\\left(x {signed(-be)}\\right) {rel} 0",
            f"{L(al)} と {L(be)} が境目で、解は {tex_interval(ans)}",
        ],
        params={"al": al, "be": be, "rel": rel},
        fmt=tex_interval,
        checks=interval_checks,
        input_hint="x の範囲を不等式で入力（a<x<b の形。「または」はカンマで区切って x<a, b<x のように。≦ は <=）",
    )


# ---------------------------------------------------------------------------
# 数学I 図形と計量
# ---------------------------------------------------------------------------

_COS = {30: sp.sqrt(3) / 2, 45: sp.sqrt(2) / 2, 60: sp.Rational(1, 2), 120: -sp.Rational(1, 2),
        135: -sp.sqrt(2) / 2, 150: -sp.sqrt(3) / 2}
_SIN = {30: sp.Rational(1, 2), 45: sp.sqrt(2) / 2, 60: sp.sqrt(3) / 2, 120: sp.sqrt(3) / 2,
        135: sp.sqrt(2) / 2, 150: sp.Rational(1, 2)}


@template("m1-trig-cosine", unit="m1-trig", label="余弦定理で辺の長さ", level="basic")
def trig_cosine(rng: random.Random) -> Instance:
    A = rng.choice([60, 120])
    b, c = rng.sample(range(2, 13), 2)
    cos = _COS[A]
    sq = b ** 2 + c ** 2 - 2 * b * c * cos
    ans = sp.sqrt(sq)
    return Instance(
        task="三角形 ABC で、2辺とその間の角から残りの辺 a = BC の長さを余弦定理で求める問題",
        givens=triangle_givens(b, c, A),
        answer=ans,
        candidates=[
            Cand(sp.sqrt(b ** 2 + c ** 2 + 2 * b * c * cos), "sign"),
            Cand(sp.sqrt(b ** 2 + c ** 2 - b * c * cos), "coef"),
            Cand(sq, "power"),
            Cand(sp.sqrt(b ** 2 + c ** 2), "partial"),
        ],
        steps=[
            "余弦定理 a^2 = b^2 + c^2 - 2bc\\cos A",
            f"\\cos {A}^\\circ = {L(cos)} なので a^2 = {L(b**2)} + {L(c**2)} - 2 \\cdot {b} \\cdot {c} \\cdot "
            f"\\left({L(cos)}\\right) = {L(sq)}",
            f"a > 0 より a = {L(ans)}",
        ],
        params={"A": A, "b": b, "c": c},
        input_hint=HINT_NUM,
    )


@template("m1-trig-area", unit="m1-trig", label="三角形の面積", level="basic")
def trig_area(rng: random.Random) -> Instance:
    A = rng.choice([30, 45, 60, 120, 135, 150])
    b, c = rng.sample(range(2, 13), 2)
    sin = _SIN[A]
    ans = sp.Rational(1, 2) * b * c * sin
    return Instance(
        task="三角形 ABC で、2辺 b = CA, c = AB とその間の角 A から面積 S を求める問題",
        givens=triangle_givens(b, c, A),
        answer=ans,
        candidates=[
            Cand(b * c * sin, "coef"),
            Cand(sp.Rational(1, 2) * b * c * sp.Abs(_COS[A]), "formula"),
            Cand(sp.Rational(1, 2) * b * c, "partial"),
            Cand(sp.Rational(1, 4) * b * c * sin, "coef"),
        ],
        steps=[
            "面積の公式 S = \\frac{1}{2} bc \\sin A",
            f"\\sin {A}^\\circ = {L(sin)} なので S = \\frac{{1}}{{2}} \\cdot {b} \\cdot {c} \\cdot {L(sin)} = {L(ans)}",
        ],
        params={"A": A, "b": b, "c": c},
        input_hint=HINT_NUM,
    )


# ---------------------------------------------------------------------------
# 数学A 場合の数と確率
# ---------------------------------------------------------------------------

@template("ma-case-comb", unit="ma-case", label="組合せ", level="basic")
def case_comb(rng: random.Random) -> Instance:
    n = rng.randint(5, 15)
    r = rng.randint(2, min(5, n - 2))
    ans = sp.binomial(n, r)
    unit, what = rng.choice([("人", ("人の中から", "人の委員を選ぶ選び方")), ("冊", ("冊の異なる本から", "冊を選ぶ選び方")),
                             ("個", ("個の異なる玉から", "個を取り出す取り出し方"))])
    return Instance(
        task=f"{n}{what[0]}{r}{what[1]}が何通りあるかを求める問題（選ぶ順番は区別しない）",
        givens=[f"{n}{unit}", f"{r}{unit}"],
        answer=ans,
        candidates=[
            Cand(sp.ff(n, r), "formula"),
            Cand(sp.Integer(n) ** r, "formula"),
            Cand(sp.Integer(n * r), "calc"),
            Cand(sp.binomial(n, r - 1), "calc"),
        ],
        steps=[
            f"順番を区別しないので組合せ {{}}_{{{n}}}\\mathrm{{C}}_{{{r}}} を使う",
            f"{{}}_{{{n}}}\\mathrm{{C}}_{{{r}}} = \\frac{{{L(sp.ff(n, r))}}}{{{L(sp.factorial(r))}}} = {L(ans)}",
        ],
        params={"n": n, "r": r, "what": what[1]},
        input_hint="何通りかを整数で入力",
    )


# 反復試行の 1 回の試行：(試行の言い方, 起きること, 確率, 問題文に入れる値)
_TRIALS = [("1個のさいころを投げる", "1の目が出る", sp.Rational(1, 6), []),
           ("1個のさいころを投げる", "3の倍数の目が出る", sp.Rational(1, 3), []),
           ("1個のさいころを投げる", "偶数の目が出る", sp.Rational(1, 2), []),
           ("1個のさいころを投げる", "5以上の目が出る", sp.Rational(1, 3), []),
           ("1個のさいころを投げる", "2以下の目が出る", sp.Rational(1, 3), []),
           ("1個のさいころを投げる", "6の約数の目が出る", sp.Rational(2, 3), []),
           ("1枚の硬貨を投げる", "表が出る", sp.Rational(1, 2), []),
           ("赤玉2個と白玉3個の入った袋から玉を1個取り出して色を見てから戻す", "赤玉が出る", sp.Rational(2, 5),
            ["赤玉2個", "白玉3個"]),
           ("赤玉1個と白玉3個の入った袋から玉を1個取り出して色を見てから戻す", "赤玉が出る", sp.Rational(1, 4),
            ["赤玉1個", "白玉3個"])]


@template("ma-case-repeat", unit="ma-case", label="反復試行の確率", level="common")
def case_repeat(rng: random.Random) -> Instance:
    trial, event, p, extra = rng.choice(_TRIALS)
    n = rng.randint(3, 6)
    k = rng.randint(1, n - 1) if rng.random() < 0.8 else n  # ときどき「n 回とも」
    q = 1 - p
    ans = sp.binomial(n, k) * p ** k * q ** (n - k)
    return Instance(
        task=f"「{trial}」ことを {n} 回くり返すとき、{event}のがちょうど {k} 回である確率を求める問題",
        givens=[f"{n}回", f"{k}回", *extra],
        answer=ans,
        candidates=[
            Cand(p ** k * q ** (n - k) if k < n else p ** (n - 1), "partial"),
            Cand(sp.binomial(n, k) * p ** (n - k) * q ** k, "swap"),
            Cand(sp.binomial(n, k) * p ** k, "partial"),
            Cand(p ** k if k < n else n * p, "partial"),
            Cand(sp.Rational(k, n) * p, "formula"),
        ],
        steps=[
            f"1回で{event}確率は {L(p)}、そうでない確率は {L(q)}",
            f"反復試行の確率 {{}}_{{{n}}}\\mathrm{{C}}_{{{k}}} \\left({L(p)}\\right)^{{{k}}} "
            f"\\left({L(q)}\\right)^{{{n - k}}} = {L(ans)}",
        ],
        params={"trial": trial, "event": event, "p": str(p), "n": n, "k": k},
        input_hint="確率を分数 a/b の形で入力",
    )


# ---------------------------------------------------------------------------
# 数学II 式と証明・複素数と方程式
# ---------------------------------------------------------------------------

@template("m2-poly-vieta", unit="m2-poly", label="解と係数の関係", level="common", related=('m1-quad',))
def poly_vieta(rng: random.Random) -> Instance:
    while True:  # 重解にならないようにする（「2つの解」と書けるように）
        p = rng.choice([-5, -4, -3, -2, -1, 1, 2, 3, 4, 5])
        q = rng.choice([-6, -5, -3, -2, -1, 1, 2, 3, 4, 6, 7])
        if p * p != 4 * q:
            break
    eq = f"{poly(x**2 + p*x + q)} = 0"
    s, t = -p, q  # α+β, αβ
    if rng.random() < 0.6:
        target = r"\alpha^2 + \beta^2"
        ans = sp.Integer(s ** 2 - 2 * t)
        cands = [Cand(sp.Integer(s ** 2 + 2 * t), "sign"), Cand(sp.Integer(s ** 2), "partial"),
                 Cand(sp.Integer(t ** 2 - 2 * s), "swap"), Cand(sp.Integer(s ** 2 - t), "coef")]
        steps = [f"解と係数の関係より \\alpha + \\beta = {L(s)}, \\alpha\\beta = {L(t)}",
                 f"\\alpha^2 + \\beta^2 = (\\alpha + \\beta)^2 - 2\\alpha\\beta = {L(ans)}"]
    else:
        target = r"\frac{1}{\alpha} + \frac{1}{\beta}"
        ans = sp.Rational(s, t)
        cands = [Cand(sp.Rational(-s, t), "sign"), Cand(sp.Rational(t, s), "reciprocal"),
                 Cand(sp.Rational(-t, s), "reciprocal"), Cand(sp.Integer(s * t), "calc")]
        steps = [f"解と係数の関係より \\alpha + \\beta = {L(s)}, \\alpha\\beta = {L(t)}",
                 f"\\frac{{1}}{{\\alpha}} + \\frac{{1}}{{\\beta}} = \\frac{{\\alpha + \\beta}}{{\\alpha\\beta}} = {L(ans)}"]
    return Instance(
        task=f"2次方程式の2つの解を α, β とするとき、式 {target} の値を求める問題",
        givens=[eq, target],
        answer=ans,
        candidates=cands,
        steps=steps,
        params={"p": p, "q": q, "target": target},
        input_hint=HINT_NUM,
    )


@template("m2-poly-remainder", unit="m2-poly", label="剰余の定理", level="basic")
def poly_remainder(rng: random.Random) -> Instance:
    a, b, c = (rng.choice([-4, -3, -2, -1, 1, 2, 3, 4]) for _ in range(3))
    k = rng.choice([-3, -2, 2, 3])
    P = x ** 3 + a * x ** 2 + b * x + c
    ans = P.subs(x, k)
    return Instance(
        task="整式 P(x) を1次式で割ったときの余りを、剰余の定理で求める問題",
        givens=[f"P(x) = {poly(P)}", poly(x - k)],
        answer=ans,
        candidates=[
            Cand(P.subs(x, -k), "sign"),
            Cand(sp.Integer(c), "formula"),
            Cand(P.subs(x, 1), "calc"),
            Cand(sp.diff(P, x).subs(x, k), "formula"),
        ],
        steps=[
            f"剰余の定理より、P(x) を {poly(x - k)} で割った余りは P({k})",
            f"P({k}) = {L(ans)}",
        ],
        params={"a": a, "b": b, "c": c, "k": k},
        input_hint=HINT_INT,
    )


# ---------------------------------------------------------------------------
# 数学II 三角関数
# ---------------------------------------------------------------------------

_TRIG = {"sin": sp.sin, "cos": sp.cos, "tan": sp.tan}


@template("m2-trig-value", unit="m2-trig", label="弧度法と三角関数の値", level="basic")
def trig_value(rng: random.Random) -> Instance:
    d = rng.choice([6, 4, 3])
    m = rng.choice([k for k in range(1, 2 * d) if math.gcd(k, d) == 1 and (2 * k) % d != 0])
    if rng.random() < 0.3:
        m = -m  # 負の角
    name = rng.choice(["sin", "cos", "tan"])
    theta = sp.pi * m / d
    ans = _TRIG[name](theta)
    other = "cos" if name == "sin" else "sin"
    neg = "-" if m < 0 else ""
    num = r"\pi" if abs(m) == 1 else f"{abs(m)}\\pi"
    frac = f"{neg}\\frac{{{num}}}{{{d}}}"
    # 括弧つきや「π を分数の外に出す」書き方も、同じ式として認める
    pi_out = f"{neg}\\frac{{{abs(m)}}}{{{d}}}\\pi"
    if m > 0:
        expr = f"\\{name} {frac}"
        expr_alts = [expr, f"\\{name}\\left({frac}\\right)", f"\\{name}\\left({pi_out}\\right)"]
        if m != 1:
            expr_alts.append(f"\\{name} {pi_out}")
    else:  # 負の角は括弧をつけて書く
        expr = f"\\{name}\\left({frac}\\right)"
        expr_alts = [expr, f"\\{name}\\left({pi_out}\\right)", f"\\{name} ({frac})"]
    cands = [Cand(-ans, "sign"), Cand(_TRIG[other](theta), "swap"), Cand(-_TRIG[other](theta), "swap"),
             Cand(sp.tan(theta), "formula"), Cand(1 / ans, "reciprocal")]
    if name == "tan":
        cands = [Cand(-ans, "sign"), Cand(1 / ans, "reciprocal"), Cand(-1 / ans, "reciprocal"),
                 Cand(sp.sin(theta), "formula"), Cand(sp.cos(theta), "formula")]
    return Instance(
        task="弧度法で表された角の三角関数の値を求める問題",
        givens=[expr_alts],
        answer=ans,
        candidates=cands,
        steps=[
            f"{frac} は {L(sp.Rational(m * 180, d))}^\\circ にあたる",
            f"単位円で考えると {expr} = {L(ans)}",
        ],
        params={"name": name, "m": m, "d": d},
        input_hint=HINT_NUM,
    )


_TRIPLES = [(3, 4, 5), (5, 12, 13), (8, 15, 17), (7, 24, 25), (20, 21, 29), (12, 35, 37)]


@template("m2-trig-addition", unit="m2-trig", label="加法定理", level="common", related=('m1-trig',))
def trig_addition(rng: random.Random) -> Instance:
    (a1, b1, c1), (a2, b2, c2) = rng.sample(_TRIPLES, 2)
    if rng.random() < 0.5:
        a1, b1 = b1, a1
    sa, ca = sp.Rational(a1, c1), sp.Rational(b1, c1)  # α は鋭角
    obtuse = rng.random() < 0.5
    cb = sp.Rational(-b2 if obtuse else b2, c2)  # β は鋭角または鈍角
    sb = sp.Rational(a2, c2)
    beta_range = r"\frac{\pi}{2} < \beta < \pi" if obtuse else r"0 < \beta < \frac{\pi}{2}"
    kind = rng.choice(["sin+", "cos+", "sin-"])
    if kind == "sin+":
        target, ans = r"\sin(\alpha + \beta)", sa * cb + ca * sb
        cands = [Cand(sa * cb - ca * sb, "formula"), Cand(ca * cb - sa * sb, "swap"),
                 Cand(-(sa * cb + ca * sb), "sign"), Cand(sa * -cb + ca * sb, "domain")]
        formula = r"\sin(\alpha + \beta) = \sin\alpha\cos\beta + \cos\alpha\sin\beta"
    elif kind == "cos+":
        target, ans = r"\cos(\alpha + \beta)", ca * cb - sa * sb
        cands = [Cand(ca * cb + sa * sb, "formula"), Cand(sa * cb + ca * sb, "swap"),
                 Cand(-ca * cb - sa * sb, "sign"), Cand(ca * -cb - sa * sb, "domain")]
        formula = r"\cos(\alpha + \beta) = \cos\alpha\cos\beta - \sin\alpha\sin\beta"
    else:
        target, ans = r"\sin(\alpha - \beta)", sa * cb - ca * sb
        cands = [Cand(sa * cb + ca * sb, "formula"), Cand(ca * cb + sa * sb, "swap"),
                 Cand(-sa * cb - ca * sb, "sign"), Cand(sa * -cb - ca * sb, "domain")]
        formula = r"\sin(\alpha - \beta) = \sin\alpha\cos\beta - \cos\alpha\sin\beta"
    return Instance(
        task=f"三角関数の値と角の範囲から、加法定理で {target} の値を求める問題",
        givens=[f"\\sin\\alpha = {L(sa)}", f"\\cos\\beta = {L(cb)}",
                r"0 < \alpha < \frac{\pi}{2}", beta_range, target],
        answer=ans,
        candidates=cands,
        steps=[
            f"\\cos\\alpha > 0 より \\cos\\alpha = \\sqrt{{1 - \\sin^2\\alpha}} = {L(ca)}",
            f"\\sin\\beta > 0 より \\sin\\beta = {L(sb)}",
            f"加法定理 {formula}",
            f"代入すると {target} = {L(ans)}",
        ],
        params={"sa": str(sa), "cb": str(cb), "kind": kind},
        input_hint="値を分数 a/b の形で入力（負なら -a/b）",
    )


@template("m2-trig-double", unit="m2-trig", label="2倍角の公式", level="common", related=('m1-trig',))
def trig_double(rng: random.Random) -> Instance:
    a, b, c = rng.choice(_TRIPLES)
    if rng.random() < 0.5:
        a, b = b, a
    s = sp.Rational(a, c)
    obtuse = rng.random() < 0.5
    co = sp.Rational(-b if obtuse else b, c)
    t = s / co
    rng_tex = r"\frac{\pi}{2} < \theta < \pi" if obtuse else r"0 < \theta < \frac{\pi}{2}"
    given_cos = rng.random() < 0.4  # sin θ ではなく cos θ の値を与える
    given = f"\\cos\\theta = {L(co)}" if given_cos else f"\\sin\\theta = {L(s)}"
    first = f"角の範囲から \\sin\\theta = {L(s)}" if given_cos else f"角の範囲から \\cos\\theta = {L(co)}"
    which = rng.choice(["sin", "cos", "cos", "tan"] if given_cos else ["sin", "sin", "cos", "tan"])
    if which == "sin":
        target, ans = r"\sin 2\theta", 2 * s * co
        cands = [Cand(-2 * s * co, "domain"), Cand(2 * s, "partial"), Cand(s * co, "coef"),
                 Cand(1 - 2 * s ** 2, "swap")]
        formula = r"\sin 2\theta = 2\sin\theta\cos\theta"
    elif which == "cos":
        target, ans = r"\cos 2\theta", 1 - 2 * s ** 2
        if given_cos:
            cands = [Cand(1 - 2 * co ** 2, "sign"), Cand(co ** 2 - 1, "formula"), Cand(2 * co - 1, "power"),
                     Cand(2 * s * co, "swap")]
            formula = r"\cos 2\theta = 2\cos^2\theta - 1"
        else:
            cands = [Cand(2 * s ** 2 - 1, "sign"), Cand(1 - s ** 2, "formula"), Cand(1 - 2 * s, "power"),
                     Cand(2 * s * co, "swap")]
            formula = r"\cos 2\theta = 1 - 2\sin^2\theta"
    else:
        target, ans = r"\tan 2\theta", 2 * t / (1 - t ** 2)
        cands = [Cand(-ans, "sign"), Cand(2 * t, "partial"), Cand(t / (1 - t ** 2), "coef"),
                 Cand(2 * t / (1 + t ** 2), "formula")]
        formula = r"\tan 2\theta = \frac{2\tan\theta}{1 - \tan^2\theta}"
        first += f"、\\tan\\theta = {L(t)}"
    return Instance(
        task=f"三角関数の値と角の範囲から、2倍角の公式で {target} の値を求める問題",
        givens=[given, rng_tex, target],
        answer=ans,
        candidates=cands,
        steps=[
            first,
            f"2倍角の公式 {formula}",
            f"{target} = {L(ans)}",
        ],
        params={"s": str(s), "obtuse": obtuse, "target": target, "given": "cos" if given_cos else "sin"},
        input_hint="値を分数 a/b の形で入力（負なら -a/b）",
    )


# 合成する sin θ・cos θ の係数（sin の係数は正、cos の係数は正負どちらも）
_SYNTH_COEFS = [1, 2, 3, 4, 5, 12, sp.sqrt(2), sp.sqrt(3), 2 * sp.sqrt(3), 3 * sp.sqrt(3)]
_SYNTH = [(a, s * b) for a in _SYNTH_COEFS for b in _SYNTH_COEFS for s in (1, -1)]


@template("m2-trig-synth", unit="m2-trig", label="三角関数の合成と最大・最小", level="common")
def trig_synth(rng: random.Random) -> Instance:
    a, b = rng.choice(_SYNTH)
    a, b = sp.sympify(a), sp.sympify(b)
    r = sp.sqrt(a ** 2 + b ** 2)
    want_max = rng.random() < 0.6
    ans = r if want_max else -r
    word = "最大値" if want_max else "最小値"
    sign_b = "-" if b.could_extract_minus_sign() else "+"
    sin_t, cos_t = r"\sin\theta", r"\cos\theta"
    expr = f"y = {coef_term(a, sin_t)} {sign_b} {coef_term(abs(b), cos_t)}"
    return Instance(
        task=f"三角関数の合成を使って、関数の{word}を求める問題（θ の範囲は 0 ≦ θ < 2π）",
        givens=[expr, r"0 \leqq \theta < 2\pi", word],
        answer=ans,
        candidates=[
            Cand(-ans, "swap"),
            Cand(a + b if want_max else -(a + b), "formula"),
            Cand(a ** 2 + b ** 2 if want_max else -(a ** 2 + b ** 2), "power"),
            Cand(abs(a) + abs(b) if want_max else -(abs(a) + abs(b)), "formula"),
        ],
        steps=[
            f"合成すると y = {L(r)}\\sin(\\theta + \\alpha) の形になる（\\sqrt{{a^2 + b^2}} = {L(r)}）",
            f"-1 \\leqq \\sin(\\theta + \\alpha) \\leqq 1 より、{word}は {L(ans)}",
        ],
        params={"a": str(a), "b": str(b), "max": want_max},
        input_hint=HINT_NUM,
    )


# ---------------------------------------------------------------------------
# 数学II 指数関数・対数関数
# ---------------------------------------------------------------------------

@template("m2-explog-base", unit="m2-explog", label="対数の値（底の変換）", level="basic")
def explog_base(rng: random.Random) -> Instance:
    c = rng.choice([2, 3, 5, 7])
    while True:
        m, n = rng.randint(2, 6), rng.randint(1, 7)
        if m != n and c ** n <= 1000 and c ** m <= 1000:
            break
    a, b = c ** m, c ** n
    inv = rng.random() < 0.3  # 真数が分数（1/b）のときは負の値になる
    ans = sp.Rational(-n if inv else n, m)
    arg = f"\\frac{{1}}{{{b}}}" if inv else str(b)
    return Instance(
        task="対数の値を、底の変換公式を使って求める問題",
        givens=[[f"\\log_{{{a}}} {arg}", f"\\log_{{{a}}} \\left({arg}\\right)", f"\\log_{{{a}}}{{{arg}}}"]],
        answer=ans,
        candidates=[
            Cand(sp.Rational(m, -n if inv else n), "reciprocal"),
            Cand(-ans, "sign") if inv else Cand(sp.Rational(b, a), "formula"),
            Cand(sp.Integer(n - m), "calc"),
            Cand(sp.Integer(n * m), "calc"),
        ],
        steps=[
            f"{a} = {c}^{{{m}}}, {arg} = {c}^{{{-n if inv else n}}}",
            f"\\log_{{{a}}} {arg} = \\frac{{\\log_{{{c}}} {arg}}}{{\\log_{{{c}}} {a}}} = "
            f"\\frac{{{-n if inv else n}}}{{{m}}}" + ("" if ans.q == m else f" = {L(ans)}"),
        ],
        params={"c": c, "m": m, "n": n, "inv": inv},
        input_hint=HINT_NUM,
    )


@template("m2-explog-equation", unit="m2-explog", label="対数方程式", level="common", related=('m1-quad',))
def explog_equation(rng: random.Random) -> Instance:
    options = []
    for c in (2, 3, 5):
        for p in range(-5, 6):
            for d in range(1, 7):
                q = p + d
                for r in range(q + 1, q + 30):
                    prod = (r - p) * (r - q)
                    k = round(math.log(prod, c)) if prod > 1 else 0
                    if k >= 1 and c ** k == prod and p != 0 and q != 0:
                        options.append((c, p, q, r, k))
    c, p, q, r, k = rng.choice(options)
    r2 = p + q - r
    eq = (f"\\log_{{{c}}} \\left(x {signed(-p)}\\right) + \\log_{{{c}}} \\left(x {signed(-q)}\\right) = {k}")
    half = sp.Rational(c ** k + p + q, 2)
    fmt = lambda v: tex_set_eq(v)  # noqa: E731
    return Instance(
        task="対数方程式を解く問題（真数条件に注意する）",
        givens=[eq],
        answer=sp.FiniteSet(r),
        kind="set",
        candidates=[
            Cand(sp.FiniteSet(r, r2), "domain"),
            Cand(sp.FiniteSet(r2), "domain"),
            Cand(sp.FiniteSet(half), "formula"),
            Cand(sp.FiniteSet(r + 1), "calc"),
        ],
        steps=[
            f"真数条件より x > {max(p, q)}",
            f"左辺をまとめると \\log_{{{c}}} \\left(x {signed(-p)}\\right)\\left(x {signed(-q)}\\right) = {k} "
            f"より \\left(x {signed(-p)}\\right)\\left(x {signed(-q)}\\right) = {c ** k}",
            f"これを解くと x = {r}, {r2}",
            f"真数条件を満たすのは x = {r}",
        ],
        params={"c": c, "p": p, "q": q, "r": r, "k": k},
        fmt=fmt,
        input_hint="x の値を入力（答えが2つ以上あるときはカンマで区切る）",
    )


# ---------------------------------------------------------------------------
# 数学II 微分法
# ---------------------------------------------------------------------------

@template("m2-diff-tangent", unit="m2-diff", label="接線の方程式", level="common")
def diff_tangent(rng: random.Random) -> Instance:
    if rng.random() < 0.5:
        f = x ** 3 + rng.choice([-3, -2, -1, 1, 2]) * x ** 2 + rng.choice([-4, -2, -1, 1, 3]) * x \
            + rng.choice([-2, -1, 1, 2])
    else:
        f = rng.choice([1, 2, -1]) * x ** 2 + rng.choice([-4, -3, -1, 1, 2]) * x + rng.choice([-3, -1, 2, 4])
    t = rng.choice([-2, -1, 1, 2])
    ft, m = f.subs(x, t), sp.diff(f, x).subs(x, t)
    ans = sp.expand(m * (x - t) + ft)
    fmt = lambda v: f"y = {poly(v)}"  # noqa: E731
    return Instance(
        task="曲線上の点における接線の方程式を求める問題",
        givens=[f"y = {poly(f)}", tex_tuple((t, ft))],
        answer=ans,
        candidates=[
            Cand(sp.expand(m * x + ft), "formula"),
            Cand(sp.expand(m * (x + t) + ft), "sign"),
            Cand(sp.expand(ft * (x - t) + m), "swap"),
            Cand(sp.expand(m * (x - t) - ft), "sign"),
        ],
        steps=[
            f"y' = {poly(sp.diff(f, x))} なので、x = {t} での傾きは {L(m)}",
            f"接線は y - \\left({L(ft)}\\right) = {L(m)}\\left(x {signed(-t)}\\right)",
            f"整理すると y = {poly(ans)}",
        ],
        params={"f": str(f), "t": t},
        fmt=fmt,
        input_hint="y = の右辺を x の式で入力（かけ算の * は省略してよい）",
    )


@template("m2-diff-extremum", unit="m2-diff", label="極値", level="common", related=('m2-poly',))
def diff_extremum(rng: random.Random) -> Instance:
    while True:
        al, be = sorted(rng.sample(range(-3, 4), 2))
        if (al + be) % 2 == 0:
            break
    d = rng.choice([-3, -1, 0, 1, 2, 4])
    f = x ** 3 - sp.Rational(3, 2) * (al + be) * x ** 2 + 3 * al * be * x + d
    fmax, fmin = f.subs(x, al), f.subs(x, be)
    want_max = rng.random() < 0.5
    ans, other = (fmax, fmin) if want_max else (fmin, fmax)
    word = "極大値" if want_max else "極小値"
    at = al if want_max else be
    return Instance(
        task=f"3次関数の{word}を求める問題",
        givens=[f"f(x) = {poly(f)}", word],
        answer=ans,
        candidates=[
            Cand(other, "swap"),
            Cand(sp.Integer(at), "partial"),
            Cand(f.subs(x, -at), "sign"),
            Cand(f.subs(x, -(be if want_max else al)), "sign"),
        ],
        steps=[
            f"f'(x) = {poly(sp.diff(f, x))} = 3\\left(x {signed(-al)}\\right)\\left(x {signed(-be)}\\right)",
            f"増減表より x = {al} で極大値 {L(fmax)}、x = {be} で極小値 {L(fmin)}",
        ],
        params={"al": al, "be": be, "d": d, "max": want_max},
        input_hint=HINT_INT,
    )


# ---------------------------------------------------------------------------
# 数学II 積分法
# ---------------------------------------------------------------------------

@template("m2-int-definite", unit="m2-int", label="定積分の計算", level="basic")
def int_definite(rng: random.Random) -> Instance:
    while True:
        p = rng.choice([1, 2, 3, -1, -3])
        q = rng.choice([-4, -2, -1, 0, 1, 2, 3])
        r = rng.choice([-3, -2, -1, 1, 2, 4])
        a, b = sorted(rng.sample(range(-2, 4), 2))
        g = p * x ** 2 + q * x + r
        F = sp.integrate(g, x)
        ans = F.subs(x, b) - F.subs(x, a)
        if ans != 0 and a != 0:
            break
    wrongF = p * x ** 3 + q * x ** 2 + r * x
    return Instance(
        task="定積分の値を求める問題",
        givens=[f"\\int_{{{a}}}^{{{b}}} \\left({poly(g)}\\right) dx"],
        answer=ans,
        candidates=[
            Cand(-ans, "sign"),
            Cand(wrongF.subs(x, b) - wrongF.subs(x, a), "coef"),
            Cand(F.subs(x, b), "partial"),
            Cand(g.subs(x, b) - g.subs(x, a), "formula"),
        ],
        steps=[
            f"不定積分は {poly(F)}",
            f"\\left[{poly(F)}\\right]_{{{a}}}^{{{b}}} = {L(F.subs(x, b))} - \\left({L(F.subs(x, a))}\\right) = {L(ans)}",
        ],
        params={"p": p, "q": q, "r": r, "a": a, "b": b},
        input_hint=HINT_NUM,
    )


@template("m2-int-area", unit="m2-int", label="放物線で囲まれた面積", level="common", related=('m1-quad',))
def int_area(rng: random.Random) -> Instance:
    al, be = sorted(rng.sample(range(-4, 7), 2))
    width = be - al
    k = rng.choice([1, 1, 2, 3, -1, -2])  # 放物線の x^2 の係数
    ans = sp.Rational(abs(k) * width ** 3, 6)
    if rng.random() < 0.5:
        curve = sp.expand(k * (x - al) * (x - be))
        line = sp.Integer(0)
        givens = [f"y = {poly(curve)}", "x 軸"]
        task = "放物線と x 軸で囲まれた部分の面積を求める問題"
        first = f"放物線と x 軸の交点の x 座標は {al}, {be}"
    else:
        k = abs(k)  # 直線との場合は下に凸の放物線 y = kx^2 だけ
        ans = sp.Rational(k * width ** 3, 6)
        curve = k * x ** 2
        line = sp.expand(k * ((al + be) * x - al * be))
        givens = [f"y = {poly(curve)}", f"y = {poly(line)}"]
        task = "放物線と直線で囲まれた部分の面積を求める問題"
        first = f"{poly(curve)} = {poly(line)} を解くと、交点の x 座標は {al}, {be}"
    coef = "" if abs(k) == 1 else f"{abs(k)} \\cdot "
    return Instance(
        task=task,
        givens=givens,
        answer=ans,
        candidates=[
            Cand(sp.Rational(abs(k) * width ** 3, 3), "coef"),
            Cand(-ans, "sign"),
            Cand(sp.Rational(abs(k) * width ** 2, 6), "power"),
            Cand(sp.Rational(abs(k) * width ** 3, 2), "coef"),
            Cand(sp.Rational(width ** 3, 6), "coef"),
        ],
        steps=[
            first,
            f"S = \\left|\\int_{{{al}}}^{{{be}}} {L(abs(k)) if abs(k) != 1 else ''}\\left(x {signed(-al)}\\right)"
            f"\\left(x {signed(-be)}\\right) dx\\right| "
            f"= {coef}\\frac{{1}}{{6}}\\left({be} - \\left({al}\\right)\\right)^3 = {L(ans)}",
        ],
        params={"al": al, "be": be, "k": k, "curve": str(curve), "line": str(line)},
        input_hint=HINT_NUM,
    )


# ---------------------------------------------------------------------------
# 数学B 数列
# ---------------------------------------------------------------------------

@template("mb-seq-arith", unit="mb-seq", label="等差数列の和", level="basic")
def seq_arith(rng: random.Random) -> Instance:
    a = rng.choice([-7, -5, -3, -2, 1, 2, 3, 4, 5, 8])
    d = rng.choice([-4, -3, -2, 2, 3, 4, 5])
    n = rng.randint(8, 20)
    an = a + (n - 1) * d
    ans = sp.Integer(n * (2 * a + (n - 1) * d) // 2)
    return Instance(
        task="初項と公差が与えられた等差数列の、初項から第 n 項までの和を求める問題",
        givens=[f"a_{{1}} = {a}", f"d = {d}", f"S_{{{n}}}"],
        answer=ans,
        candidates=[
            Cand(sp.Integer(n * (2 * a + n * d) // 2) if (n * (2 * a + n * d)) % 2 == 0
                 else sp.Rational(n * (2 * a + n * d), 2), "calc"),
            Cand(sp.Integer(n * (a + an)), "coef"),
            Cand(sp.Integer(an), "swap"),
            Cand(sp.Rational(n * (2 * a - (n - 1) * d), 2), "sign"),
        ],
        steps=[
            f"第 {n} 項は a_{{{n}}} = {a} + ({n} - 1) \\cdot \\left({d}\\right) = {an}",
            f"S_{{{n}}} = \\frac{{{n}\\left({a} + \\left({an}\\right)\\right)}}{{2}} = {L(ans)}",
        ],
        params={"a": a, "d": d, "n": n},
        input_hint=HINT_INT,
    )


# ---------------------------------------------------------------------------
# 数学C ベクトル
# ---------------------------------------------------------------------------

_STD_ANGLES = {sp.Rational(1, 2): 60, -sp.Rational(1, 2): 120, sp.sqrt(2) / 2: 45, -sp.sqrt(2) / 2: 135,
               sp.sqrt(3) / 2: 30, -sp.sqrt(3) / 2: 150}


def _vector_pairs() -> list[tuple[tuple[int, int], tuple[int, int], int]]:
    """なす角が 45° か 135° になる整数成分のベクトルの組（整数成分では 30°・60° などは作れない）。"""
    from fractions import Fraction

    found = []
    comps = [(i, j) for i in range(-4, 5) for j in range(-4, 5) if (i, j) != (0, 0)]
    for a in comps:
        for b in comps:
            if a >= b:
                continue
            dot = a[0] * b[0] + a[1] * b[1]
            if dot != 0 and Fraction(dot * dot, (a[0] ** 2 + a[1] ** 2) * (b[0] ** 2 + b[1] ** 2)) == Fraction(1, 2):
                found.append((a, b, 45 if dot > 0 else 135))
    return found


def _sqrt3_vector(rng: random.Random, direction: int) -> tuple[Any, Any]:
    """向きが 30° の倍数のベクトル。成分は整数か √3 を含む（例: (√3, 1), (-2, 0)）。"""
    k = rng.choice([1, 2])
    rad = sp.rad(direction)
    return (sp.nsimplify(2 * k * sp.cos(rad)), sp.nsimplify(2 * k * sp.sin(rad)))


_PAIRS: list[tuple[tuple[int, int], tuple[int, int], int]] = []


@template("mc-vec-angle", unit="mc-vec", label="ベクトルのなす角", level="common")
def vec_angle(rng: random.Random) -> Instance:
    theta = rng.choice([30, 45, 60, 120, 135, 150])
    if theta in (45, 135):
        if not _PAIRS:
            _PAIRS.extend(_vector_pairs())
        a, b, _ = rng.choice([p for p in _PAIRS if p[2] == theta])
    else:
        d1 = rng.choice(range(0, 360, 30))
        d2 = (d1 + rng.choice([theta, -theta])) % 360
        a, b = _sqrt3_vector(rng, d1), _sqrt3_vector(rng, d2)
    dot = sp.simplify(a[0] * b[0] + a[1] * b[1])
    na, nb = sp.sqrt(a[0] ** 2 + a[1] ** 2), sp.sqrt(b[0] ** 2 + b[1] ** 2)
    angles = sorted(_STD_ANGLES.values())
    neighbor = angles[(angles.index(theta) + 1) % len(angles)]
    return Instance(
        task="成分で表された2つのベクトルのなす角 θ（0° ≦ θ ≦ 180°）を求める問題",
        givens=[f"\\vec{{a}} = {tex_tuple(a)}", f"\\vec{{b}} = {tex_tuple(b)}"],
        answer=sp.Integer(theta),
        candidates=[
            Cand(sp.Integer(180 - theta), "sign"),
            Cand(sp.Integer(abs(90 - theta)), "swap"),
            Cand(sp.Integer(neighbor), "calc"),
            Cand(sp.Integer(180 - neighbor), "calc"),
        ],
        steps=[
            f"\\vec{{a}} \\cdot \\vec{{b}} = {L(dot)}, |\\vec{{a}}| = {L(na)}, |\\vec{{b}}| = {L(nb)}",
            f"\\cos\\theta = \\frac{{\\vec{{a}} \\cdot \\vec{{b}}}}{{|\\vec{{a}}||\\vec{{b}}|}} = "
            f"{L(sp.nsimplify(dot / (na * nb)))}",
            f"0^\\circ \\leqq \\theta \\leqq 180^\\circ より \\theta = {theta}^\\circ",
        ],
        params={"a": [str(v) for v in a], "b": [str(v) for v in b]},
        fmt=deg,
        input_hint="角度を度数の数字だけで入力（° は付けなくてよい）",
    )
