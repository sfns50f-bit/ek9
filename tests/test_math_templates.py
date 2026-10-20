"""数学テンプレートの確認。

- 小単元の一覧（catalog）とテンプレートの登録が一致する
- どのテンプレートも、選択式の誤答 3 つが正解とも互いとも一致しない
- テンプレートが作る正解を、別の計算方法（sympy の solve / integrate / 数値計算など）で確かめる
"""

import random
import re

import pytest
import sympy as sp
from quizapp import textcheck
from quizapp.subjects.base import GenContext
from quizapp.subjects.math import MathSubject
from quizapp.subjects.base import LEVELS
from quizapp.subjects.math.catalog import MISTAKES, UNITS, template_info
from quizapp.subjects.math.templates import INFO, TEMPLATES

from .conftest import answer_text

x = sp.Symbol("x")
IDS = [su.id for u in MathSubject().units() for su in u.subunits]
SEEDS = range(40)


def test_catalog_matches_templates():
    assert set(IDS) == set(TEMPLATES)
    assert len(IDS) >= 15


def test_template_info_is_read_without_sympy():
    """web が sympy なしで読む @template の情報（ast）が、実際に登録された INFO と同じ。"""
    assert template_info() == INFO
    units = {uid for uid, _ in UNITS}
    for info in INFO.values():
        assert info.unit in units and info.level in LEVELS, info
        assert set(info.related) <= units - {info.unit}, info
        assert set(info.formats) <= {"choice", "input"} and info.formats, info


def test_givens_check_ignores_digits_inside_numbers():
    assert not textcheck.contains("12人の中から", "2人")
    assert textcheck.contains("12人の中から 2人 を選ぶ", "2人")
    assert textcheck.contains("$b = 3$ とする", "b = 3")
    assert not textcheck.contains("$b = 30$", "b = 3")
    assert textcheck.missing("$\\sin\\left(\\frac{5\\pi}{6}\\right)$", [["\\sin \\frac{5\\pi}{6}",
                                                                      "\\sin\\left(\\frac{5\\pi}{6}\\right)"]]) == []


def test_triangle_givens_accept_common_ways_to_write_the_angle():
    from quizapp.subjects.math.templates import triangle_givens

    g = triangle_givens(6, 2, 30)
    for q in ("$b=6$, $c=2$, $A=30^\\circ$", "$AC=6$, $AB=2$, $\\angle BAC = 30^{\\circ}$",
              "CA = 6、AB = 2、∠A = 30°", "辺 b=6、c=2、角Aは30度"):
        assert textcheck.missing(q, g) == [], q
    assert textcheck.missing("b=6, c=2, A=35°", g) == ["A = 30^\\circ"]
    # 度の記号の書き方の揺れ、度の記号なし、弧度法も、値が同じなら認める
    for angle in ("$A=30\\circ$", "$A=30^o$", "A=30º", "A=30˚", "$A=30∘$", "$\\angle A=30$",
                  "$A=\\frac{\\pi}{6}$"):
        assert textcheck.missing(f"$b=6$, $c=2$, {angle}", g) == [], angle


def test_vector_angles_cover_all_standard_angles():
    seen = {int(TEMPLATES["mc-vec-angle"](random.Random(seed)).answer) for seed in range(300)}
    assert seen == {30, 45, 60, 120, 135, 150}


def test_mistake_tags_are_known():
    for tid, build in TEMPLATES.items():
        for seed in SEEDS:
            for c in build(random.Random(seed)).candidates:
                assert c.mistake in MISTAKES, (tid, c.mistake)


@pytest.mark.parametrize("tid", IDS)
def test_choices_are_distinct_from_answer_and_each_other(tid, sandbox):
    subject = MathSubject()
    ctx = GenContext(llm=None, sandbox=sandbox, gen_model="", rng=random.Random(1))
    for _ in range(15):
        inst, choices, _key = subject._instance(ctx, tid, "choice")
        assert len(choices) == 4 and sum(c["correct"] for c in choices) == 1
        # 表示の重複がない
        assert len({textcheck.normalize(c["text"]) for c in choices}) == 4
        # sympy で見ても、正解と一致する誤答はなく、誤答どうしも一致しない
        values = [inst.answer] + [c.value for c in inst.candidates]
        texts = [f"${inst.fmt(v)}$" for v in values]
        picked = [values[texts.index(c["text"])] for c in choices]
        res = sandbox.distinct(inst.kind, [sp.srepr(v) for v in picked], inst.var)
        assert res["ok"] and res["equal_pairs"] == [], (tid, [c["text"] for c in choices])


def test_input_hints_show_no_values():
    """入力の例に具体的な数値を書かない（正解と同じ値になると答えが見えてしまう）。"""
    import re

    for tid, build in TEMPLATES.items():
        hint = build(random.Random(0)).input_hint.replace("2つ", "")
        assert not re.search(r"\d", hint), (tid, hint)


@pytest.mark.parametrize("tid", IDS)
def test_keyboard_answer_is_graded_correct(tid, sandbox):
    for seed in range(10):
        inst = TEMPLATES[tid](random.Random(seed))
        res = sandbox.compare(inst.kind, {"srepr": sp.srepr(inst.answer)},
                              {"text": answer_text(inst.answer, inst.kind)}, var=inst.var)
        assert res["ok"] and res["equal"], (tid, seed, res)


@pytest.mark.parametrize("tid", IDS)
def test_givens_and_answer_survive_normalization(tid):
    for seed in range(10):
        inst = TEMPLATES[tid](random.Random(seed))
        q = "問題文 " + "、".join(f"$ {textcheck.first(g)} $" for g in inst.givens)
        assert textcheck.missing(q, inst.givens) == []
        assert textcheck.missing(f"答え: ${inst.fmt(inst.answer)}$", inst.answer_checks()) == []


# ---------------------------------------------------------------------------
# 正解を別の方法で確かめる
# ---------------------------------------------------------------------------

def _num(v):
    return complex(sp.N(v, 30))


def _close(a, b):
    return abs(_num(a) - _num(b)) < 1e-9


def check_quad_vertex(inst):
    a, p, q = (inst.params[k] for k in "apq")
    f = sp.expand(a * (x - p) ** 2 + q)
    xv = sp.solve(sp.diff(f, x), x)[0]
    assert sp.Tuple(xv, f.subs(x, xv)) == inst.answer


def check_quad_ineq(inst):
    al, be, rel = inst.params["al"], inst.params["be"], inst.params["rel"]
    f = (x - al) * (x - be)
    xr = sp.Symbol("x", real=True)
    g = f.subs(x, xr)
    cond = {"<": g < 0, ">": g > 0, "\\leqq": g <= 0, "\\geqq": g >= 0}[rel]
    assert sp.solve_univariate_inequality(cond, xr, relational=False) == inst.answer


def check_cosine(inst):
    A, b, c = inst.params["A"], inst.params["b"], inst.params["c"]
    B = sp.Point(c, 0)  # 頂点 A を原点に置く
    C = sp.Point(b * sp.cos(sp.rad(A)), b * sp.sin(sp.rad(A)))
    assert _close(B.distance(C), inst.answer)


def check_area(inst):
    A, b, c = inst.params["A"], inst.params["b"], inst.params["c"]
    tri = sp.Triangle(sp.Point(0, 0), sp.Point(c, 0), sp.Point(b * sp.cos(sp.rad(A)), b * sp.sin(sp.rad(A))))
    assert _close(abs(tri.area), inst.answer)


def check_comb(inst):
    import itertools

    n, r = inst.params["n"], inst.params["r"]
    assert sum(1 for _ in itertools.combinations(range(n), r)) == inst.answer


def check_repeat(inst):
    import itertools

    event, n, k = inst.params["event"], inst.params["n"], inst.params["k"]
    trial = inst.params["trial"]
    # 1 回の試行で起こりうる結果（同様に確からしい）と、そのうち「起きる」もの
    outcomes, hit = {
        "1の目が出る": (range(1, 7), {1}), "3の倍数の目が出る": (range(1, 7), {3, 6}),
        "偶数の目が出る": (range(1, 7), {2, 4, 6}), "5以上の目が出る": (range(1, 7), {5, 6}),
        "2以下の目が出る": (range(1, 7), {1, 2}), "6の約数の目が出る": (range(1, 7), {1, 2, 3, 6}),
        "表が出る": ("表裏", {"表"}),
    }.get(event) or (["赤"] * int(re.search(r"赤玉(\d+)個", trial).group(1))
                     + ["白"] * int(re.search(r"白玉(\d+)個", trial).group(1)), {"赤"})
    outcomes = list(outcomes)
    good = sum(1 for res in itertools.product(outcomes, repeat=n) if sum(r in hit for r in res) == k)
    assert sp.Rational(good, len(outcomes) ** n) == inst.answer


def check_vieta(inst):
    p, q = inst.params["p"], inst.params["q"]
    r1, r2 = sp.solve(x ** 2 + p * x + q, x)
    value = r1 ** 2 + r2 ** 2 if "beta^2" in inst.params["target"] else 1 / r1 + 1 / r2
    assert sp.simplify(value - inst.answer) == 0


def check_remainder(inst):
    a, b, c, k = (inst.params[n] for n in "abck")
    _q, r = sp.div(x ** 3 + a * x ** 2 + b * x + c, x - k, x)
    assert r == inst.answer


def check_trig_value(inst):
    name, m, d = inst.params["name"], inst.params["m"], inst.params["d"]
    import math

    assert -2 * d < m < 2 * d and m != 0
    value = getattr(math, name)(math.pi * m / d)
    assert abs(value - float(inst.answer)) < 1e-9


def check_addition(inst):
    import math

    sa, cb = float(sp.Rational(inst.params["sa"])), float(sp.Rational(inst.params["cb"]))
    al, be = math.asin(sa), math.acos(cb)
    value = {"sin+": math.sin(al + be), "cos+": math.cos(al + be), "sin-": math.sin(al - be)}[inst.params["kind"]]
    assert abs(value - float(inst.answer)) < 1e-9


def check_double(inst):
    import math

    s = float(sp.Rational(inst.params["s"]))
    th = math.pi - math.asin(s) if inst.params["obtuse"] else math.asin(s)
    name = re.search(r"\\(sin|cos|tan) 2", inst.params["target"]).group(1)
    value = getattr(math, name)(2 * th)
    assert abs(value - float(inst.answer)) < 1e-9
    # 問題文に入れる値（sin θ または cos θ）を読み直し、角と合っているかを確かめる
    m = re.fullmatch(r"\\(sin|cos)\\theta = (- ?)?\\frac\{(\d+)\}\{(\d+)\}", inst.givens[0])
    assert m and m.group(1) == inst.params["given"], inst.givens[0]
    given = (-1 if m.group(2) else 1) * int(m.group(3)) / int(m.group(4))
    assert abs(getattr(math, m.group(1))(th) - given) < 1e-12


def check_synth(inst):
    import math

    a, b = float(sp.sympify(inst.params["a"])), float(sp.sympify(inst.params["b"]))
    ys = [a * math.sin(t) + b * math.cos(t) for t in (2 * math.pi * i / 100000 for i in range(100000))]
    target = max(ys) if inst.params["max"] else min(ys)
    assert abs(target - float(inst.answer)) < 1e-6


def check_log_base(inst):
    c, m, n = inst.params["c"], inst.params["m"], inst.params["n"]
    b = sp.Rational(1, c ** n) if inst.params["inv"] else c ** n
    assert sp.simplify(sp.log(b) / sp.log(c ** m) - inst.answer) == 0


def check_log_equation(inst):
    c, p, q, k = (inst.params[n] for n in ("c", "p", "q", "k"))
    roots = sp.solve(sp.Eq((x - p) * (x - q), c ** k), x)
    valid = [r for r in roots if r > p and r > q]  # 真数条件
    assert sp.FiniteSet(*valid) == inst.answer
    for r in valid:
        assert abs(float(sp.log(r - p, c) + sp.log(r - q, c)) - k) < 1e-9


def check_tangent(inst):
    f, t = sp.sympify(inst.params["f"]), inst.params["t"]
    line = sp.diff(f, x).subs(x, t) * (x - t) + f.subs(x, t)
    assert sp.expand(line - inst.answer) == 0
    # 接点で重解になる（接している）
    assert sp.rem(sp.expand(f - inst.answer), (x - t) ** 2, x) == 0


def check_extremum(inst):
    al, be, d, want_max = (inst.params[k] for k in ("al", "be", "d", "max"))
    f = x ** 3 - sp.Rational(3, 2) * (al + be) * x ** 2 + 3 * al * be * x + d
    crit = sp.solve(sp.diff(f, x), x)
    kind = [c for c in crit if (sp.diff(f, x, 2).subs(x, c) < 0) == want_max][0]
    assert f.subs(x, kind) == inst.answer


def check_definite(inst):
    p, q, r, a, b = (inst.params[k] for k in "pqrab")
    assert sp.integrate(p * x ** 2 + q * x + r, (x, a, b)) == inst.answer


def check_area_int(inst):
    curve, line = sp.sympify(inst.params["curve"]), sp.sympify(inst.params["line"])
    # 交点を解き直し、その間で |曲線 - 直線| を積分する（どちらが上かはテンプレートに頼らない）
    a, b = sorted(sp.solve(sp.Eq(curve, line), x))
    assert (a, b) == (inst.params["al"], inst.params["be"])
    mid = (a + b) / 2
    top, bottom = (curve, line) if curve.subs(x, mid) > line.subs(x, mid) else (line, curve)
    assert sp.integrate(top - bottom, (x, a, b)) == inst.answer


def check_arith(inst):
    a, d, n = inst.params["a"], inst.params["d"], inst.params["n"]
    assert sum(a + k * d for k in range(n)) == inst.answer


def check_vec(inst):
    import math

    a = [float(sp.sympify(v)) for v in inst.params["a"]]
    b = [float(sp.sympify(v)) for v in inst.params["b"]]
    cos = (a[0] * b[0] + a[1] * b[1]) / (math.hypot(*a) * math.hypot(*b))
    assert abs(math.degrees(math.acos(cos)) - float(inst.answer)) < 1e-9


CHECKS = {
    "m1-quad-vertex": check_quad_vertex, "m1-quad-ineq": check_quad_ineq, "m1-trig-cosine": check_cosine,
    "m1-trig-area": check_area, "ma-case-comb": check_comb, "ma-case-repeat": check_repeat,
    "m2-poly-vieta": check_vieta, "m2-poly-remainder": check_remainder, "m2-trig-value": check_trig_value,
    "m2-trig-addition": check_addition, "m2-trig-double": check_double, "m2-trig-synth": check_synth,
    "m2-explog-base": check_log_base, "m2-explog-equation": check_log_equation, "m2-diff-tangent": check_tangent,
    "m2-diff-extremum": check_extremum, "m2-int-definite": check_definite, "m2-int-area": check_area_int,
    "mb-seq-arith": check_arith, "mc-vec-angle": check_vec,
}


def test_every_template_has_an_independent_check():
    assert set(CHECKS) == set(TEMPLATES)


@pytest.mark.parametrize("tid", IDS)
def test_answer_is_correct_by_another_method(tid):
    seeds = range(8) if tid == "m2-trig-synth" else SEEDS
    for seed in seeds:
        CHECKS[tid](TEMPLATES[tid](random.Random(seed)))
