"""LLM が書いた文章の表記をそろえる・崩れを見つける（latexfix）。"""

import pytest
from quizapp.latexfix import normalize, problems, question_problems

# 実際に作られた問題で見つかった崩れ（Ollama の JSON スキーマで、\circ などが別の文字に押し曲げられていた）
DEGREES = [r"$A = 120^\top$", r"$A = 120^\rho$", r"$A = 120^\theta$", r"$A = 120^\times$", r"$A = 120^\bullet$",
           r"$A = 120^\triangle$", r"$A = 120^\textcirc$", r"$A = 120^\text{deg}$", r"$A = 120^\text{\circ}$",
           r"$A = 120^\fr{circ}$", r"$A = 120^\ngle$", "$A = 120^°$", r"$A = 120^°$", r"$A = 120^{\circ}$",
           r"$A = 120^\text{o}$", "$A = 120°$"]


@pytest.mark.parametrize("text", DEGREES)
def test_degree_variants_become_circ(text):
    assert normalize("角 " + text) == r"角 $A = 120^\circ$"


def test_other_repairs_keep_real_math():
    assert normalize(r"$2^{10}$ と $x^2$ と $\theta$") == r"$2^{10}$ と $x^2$ と $\theta$"  # 本当の指数はそのまま
    assert normalize(r"\\sin x = \\frac{1}{2}") == r"\sin x = \frac{1}{2}"
    assert normalize(r"よって\n答え: $3$") == "よって\n答え: $3$"
    assert normalize(r"$a \neq b$、$\nabla$、$\not\in$") == r"$a \neq b$、$\nabla$、$\not\in$"
    assert normalize("\x0crac{1}{2}") == r"\frac{1}{2}"
    assert normalize(r"角 120^\circ の三角形") == "角 120° の三角形"  # 数式の外は ° に
    assert normalize(r"$\ngle BAC = 30^\circ$") == r"$\angle BAC = 30^\circ$"


def test_normalize_keeps_real_math():
    """本当の指数・LaTeX の改行・\\nearrow などは変えない（レビューで見つかった誤った直し）。"""
    for text in (r"$2^\theta$ と $3^\rho$", r"$10^{o}$", r"$\nearrow$ と $\searrow$ と $\nexists$ と $\ngtr$",
                 r"$\begin{cases} x+y=1 \\x-y=3 \end{cases}$", r"$$x = 3$$ と \(y\)"):
        assert normalize(text) == text, text
    assert normalize(r"$$60^\top$$ の角") == r"$$60^\circ$$ の角"  # $$…$$ も数式として直す


def test_valid_katex_is_not_flagged():
    ok = (r"$\searrow$、$\nearrow$、$\boldsymbol{a}$、$\lvert x \rvert$、$\Longrightarrow$、$\deg$、$\pmod{3}$、"
          r"$\frac12$、$\dfrac{a}{b}$、$\begin{cases} x=1 \\ y=2 \end{cases}$、$$x^2$$")
    assert problems(ok) == []


def test_more_breakages_are_found():
    found = " ".join(problems(r"$\frac{\cos A}$ と ¶ と $\x-y$ と $rac{1}{2}$ と $$3回$$"))
    for word in ("引数", "¶", r"\x", "バックスラッシュが抜けている", "日本語"):
        assert word in found, word


def test_problems_found():
    found = " ".join(problems(r"$3回$ と $\root{148}$ と 边长 と \$5", field="問題文", choice=True))
    for word in ("日本語", r"\root", "中国語", r"\$"):
        assert word in found, word
    assert "数が合わず" in " ".join(problems("$x$ と $y"))
    assert "制御文字" in " ".join(problems("a\x07b"))
    assert "改行" in " ".join(problems("$a =\n b$"))
    assert "示せ" in " ".join(problems("途中の計算過程も示せ。", choice=True))
    assert problems("途中の計算過程も示せ。", choice=False) == []
    assert "求める" in " ".join(question_problems("面積 $S$ を求める。", choice=False))
    # 正しい書き方は引っかからない（\text の中の日本語は表示できる）
    ok = r"$\frac{15\sqrt{3}}{2}$、$\angle A = 30^\circ$、$\text{9人}$、${}_{5}\mathrm{C}_{2}$、$\left(x - 1\right)$"
    assert problems(ok) == [] and question_problems("面積 $S$ を求めよ。", choice=True) == []


def test_second_review_fixes():
    """第 2 回の点検で見つかったもの：中国語の字・$ のない数式環境・$ のない答え・数式の外の命令など。"""
    assert normalize("ヒント：计算する") == "ヒント：計算する"
    assert normalize("\\begin{align*} a &= 1 \\end{align*}") == "$$\\begin{aligned} a &= 1 \\end{aligned}$$"
    assert normalize("$$\\begin{align} a &= 1 \\end{align}$$") == "$$\\begin{aligned} a &= 1 \\end{aligned}$$"
    assert normalize("答え: \\frac{8}{3}") == "答え: $\\frac{8}{3}$"
    assert normalize("答え: $3$") == "答え: $3$" and normalize("答え: 3 通り") == "答え: 3 通り"
    assert normalize("△ABC で $A = 120^\\rho$") == "△ABC で $A = 120^\\circ$"
    found = " ".join(problems("\\int_{-2}^{1} f(x)dx と $3\\left(x + 0\\right)$ と 与えられた解き方の要点に従うと 2 点形の式 $°$"))
    for word in ("数式の外", "+ 0", "解き方の要点", "点と傾きの式", "中身のない"):
        assert word in found, word
    assert problems("$10 - 0.5$ と 2 点間の距離の式 と $x^2 - 4x + 3 = 0$") == []


def test_third_review_false_positives_and_wrap():
    """正しい計算の途中（8 - 0 = 8 など）は「不要な項」としない。答えの行の囲み方は文章を変えない。"""
    for ok in (r"$\left[x^3\right]_0^2 = 8 - 0 = 8$", r"$y - 0 = 2(x-1)$", r"$\frac{1}{2}(4 - 0)$"):
        assert problems(ok) == [], ok
    assert normalize("答え: 1/3\n\n以上より") == "答え: $1/3$\n\n以上より"
    for same in ("答え:\n**x = 3**", "答え: 10%", "根据", "据える"):
        assert normalize(same) == same.replace("根据", "根据"), same
    assert normalize("答え: 120°") == "答え: $120^\\circ$" == normalize(normalize("答え: 120°"))
