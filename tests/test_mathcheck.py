import pytest
from sandbox_service import mathcheck as mc
from sandbox_service import tasks


def check(kind, expected, given, **kw):
    res = tasks.run("compare", {"kind": kind, "expected": {"text": expected}, "given": {"text": given}, **kw})
    assert res["ok"], res
    return res


@pytest.mark.parametrize("kind,expected,given", [
    ("value", "sqrt(2)/2", "1/√2"),
    ("value", "1/2", "0.5"),
    ("value", "3/2", "x = 3/2"),
    ("value", "2*x**2 + 1", "2x²+1"),
    ("value", "sqrt(3 + 2*sqrt(2))", "1+√2"),
    ("value", "pi/6", "π/6"),
    ("value", "3 + 4*I", "3+4i"),
    ("value", "exp(2)", "e^2"),
    ("value", "-5", "ー5"),
    ("value", "log(3)", "ln 3"),
    ("value", "2*sqrt(3)", "2√3"),
    ("value", "(x + 1)**2", "x^2+2x+1"),
    ("set", "[1, -3]", "x = -3, 1"),
    ("set", "[(1 + sqrt(5))/2, (1 - sqrt(5))/2]", "x=(1±√5)/2"),
    ("set", "[2, 2]", "2, 2"),
    ("tuple", "[2, -1]", "(x, y) = (2, -1)"),
    ("tuple", "[2, -1]", "x=2, y=-1"),
    ("matrix", "[[1, 2], [3, 4]]", "1,2;3,4"),
    ("matrix", "[[1, 2], [3, 4]]", "[[1,2],[3,4]]"),
    ("interval", "1 < x < 3", "1<x<3"),
    ("interval", "x < 1, 3 < x", "x>3 または x<1"),
    ("interval", "x <= -1, 2 <= x", "x≦-1、2≦x"),
    ("interval", "x != 1", "x<1, 1<x"),
    ("interval", "解なし", "なし"),
    ("interval", "すべての実数", "すべての実数"),
    ("antiderivative", "x**3/3 + sin(x)", "x^3/3 + sin x + C"),
    ("antiderivative", "log(x)", "ln x + 5"),
])
def test_equal(kind, expected, given):
    assert check(kind, expected, given)["equal"]


@pytest.mark.parametrize("kind,expected,given", [
    ("value", "3/2", "2/3"),
    ("value", "10**40", "10**40 + 1"),
    ("value", "2*x + 1", "2x - 1"),
    ("set", "[1, 2]", "1"),
    ("set", "[1, 2]", "1, 3"),
    ("tuple", "[2, -1]", "(-1, 2)"),
    ("matrix", "[[1, 2], [3, 4]]", "1,2;3,5"),
    ("matrix", "[[1, 2], [3, 4]]", "1,2,3,4"),
    ("interval", "x <= -1, 2 <= x", "x<-1, 2<=x"),
    ("interval", "1 < x < 3", "1<=x<=3"),
    ("antiderivative", "x**2", "x**2 + x"),
])
def test_not_equal(kind, expected, given):
    assert not check(kind, expected, given)["equal"]


def test_decimal_approximation_is_wrong_but_flagged():
    res = check("value", "sqrt(2)/2", "0.7071")
    assert not res["equal"] and res["approx"]
    res = check("value", "sqrt(2)", "1.4142135623731")
    assert not res["equal"] and res["approx"]


def test_float_allowed_for_verification():
    res = check("value", "sqrt(2)", "1.4142135623730951", allow_float=True)
    assert res["equal"]


def test_count_mismatch_detail():
    res = check("set", "[1, 2]", "1")
    assert "2 個" in res["detail"]


def test_unreadable_input_is_reported_as_given_error():
    res = tasks.run("compare", {"kind": "value", "expected": {"text": "1"}, "given": {"text": "(("}})
    assert not res["ok"] and res["where"] == "given"


def test_srepr_round_trip():
    obj, var = mc.parse_answer("x < 1, 3 < x", "interval")
    desc = mc.describe(obj, "interval", var)
    res = tasks.run("compare", {"kind": "interval", "expected": {"srepr": desc["srepr"]}, "given": {"text": "x<1 または x>3"}})
    assert res["equal"]
    assert res["expected"]["latex"] == r"x < 1,\ 3 < x"


def test_degree_sign_is_ignored():
    assert check("value", "60", "60°")["equal"]


@pytest.mark.parametrize("expected,given", [
    ("1/2", "sin(5*pi/6)"), ("3/2", "log(27)/log(9)"), ("120", "binomial(10,3)"),
    ("9/2", "integrate(-(x-1)*(x-4),(x,1,4))"), ("1/2", "Rational(1,2)"),
])
def test_unevaluated_expressions_are_not_accepted(expected, given):
    """問題の式を書き写しただけの答えは、計算した値とはみなさない（読み取りエラーにする）。"""
    import sympy as sp

    res = tasks.run("compare", {"kind": "value", "expected": {"srepr": sp.srepr(sp.sympify(expected))},
                                "given": {"text": given}})
    assert not res["ok"] and res["where"] == "given" and "計算した値" in res["error"]


def test_distinct_finds_equal_pairs():
    import sympy as sp

    items = [sp.srepr(v) for v in (sp.sqrt(2) / 2, 1 / sp.sqrt(2), sp.Rational(1, 2), -sp.sqrt(2) / 2)]
    res = tasks.run("distinct", {"kind": "value", "items": items})
    assert res["ok"] and res["equal_pairs"] == [[0, 1]]


def test_distinct_interval():
    import sympy as sp

    items = [sp.srepr(sp.Interval.open(1, 3)), sp.srepr(sp.Interval(1, 3)),
             sp.srepr(sp.Union(sp.Interval.open(1, 2), sp.Interval.Ropen(2, 3)))]
    res = tasks.run("distinct", {"kind": "interval", "items": items})
    assert res["ok"] and res["equal_pairs"] == [[0, 2]]


def test_evaluate_task_is_gone():
    """LLM が書いたコードを実行する機能は持たない（数値はテンプレートの Python が作る）。"""
    assert "evaluate" not in tasks.TASKS


def test_preview():
    assert tasks.run("preview", {"text": "2x^2 - 3x + 1", "kind": "value"})["latex"] == "2 x^{2} - 3 x + 1"
    assert tasks.run("preview", {"text": "x<1, 3<x", "kind": "interval"})["latex"] == r"x < 1,\ 3 < x"
    assert not tasks.run("preview", {"text": "1 +", "kind": "value"})["ok"]


@pytest.mark.parametrize("kind,expected,given", [
    ("value", "1140", "1,140"),                      # 桁区切り
    ("value", "1140", "１１４０"),                     # 全角数字
    ("value", "3", "答えは3です"),                    # 日本語の書き添え
    ("value", "3", "答え: 3"),
    ("value", "3", "よって、答えは x = 3 である。"),
    ("value", "3", "**3**"),                          # Markdown の太字
    ("value", "3", "\\boxed{3}"),
    ("value", "3/5", "\\frac{3}{5}"),                 # LaTeX
    ("value", "3/5", "$\\dfrac{3}{5}$"),
    ("value", "2*sqrt(21)", "2\\sqrt{21}"),
    ("value", "2*sqrt(21)", "2√(21)"),
    ("value", "-sqrt(3)/2", "-\\frac{\\sqrt{3}}{2}"),
    ("value", "pi/3", "\\frac{\\pi}{3}"),
    ("value", "60", "60^{\\circ}"),
    ("value", "exp(2)", "e^{2}"),
    ("interval", "1 <= x <= 3", "1 \\leq x \\leq 3"),
    ("set", "5", "x=5です"),
])
def test_answers_written_in_many_styles_are_read(kind, expected, given):
    """検証役（モデルごとに書き方が違う）やキーボード入力の答えを、同じ値として読める。"""
    assert check(kind, expected, given)["equal"]


@pytest.mark.parametrize("given", [",", "わかりません", "答えは三です", "x は求められない"])
def test_reading_failure_is_not_a_wrong_value(given):
    """「,」「わかりません」のように読めない答えは、「数値が違う」ではなく読み取りエラーにする。"""
    res = tasks.run("compare", {"kind": "value", "expected": {"text": "3"}, "given": {"text": given}})
    assert not res["ok"] and res["where"] == "given"


def test_copied_inequality_is_not_an_answer():
    """問題の不等式を書き写しただけの答えは、範囲の答えとして読まない（正解にしない）。"""
    for text in ("x^2-2x-3>=0", "(x+1)(x-3) \\geqq 0"):
        with pytest.raises(mc.AnswerError):
            mc.parse_answer(text, "interval")
    import sympy as sp
    assert mc.parse_answer("x<=-1, 3<=x", "interval")[0] == sp.Union(sp.Interval(-sp.oo, -1), sp.Interval(3, sp.oo))


def test_every_task_has_a_timeout():
    """サンドボックスのタスクには、すべて時間の上限がある（足し忘れると HTTP で呼べない）。"""
    from sandbox_service import server

    assert set(tasks.TASKS) == set(server.TIMEOUTS)


def test_equalities_in_explanations():
    res = tasks.run("equalities", {"segments": [
        r"58 - (-21) \cdot \frac{1}{2} = 79", r"58 - (-21) = 79", r"a^2 = 9 + 16 = 25", r"\sqrt{2} = 1.414",
        r"\sin 120^\circ = \frac{\sqrt{3}}{2}", r"\log_{2} 8 = 3", r"x = 3"]})
    assert res == {"ok": True, "mismatches": [r"58 - (-21) \cdot \frac{1}{2} = 79"]}
