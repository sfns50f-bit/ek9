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
    assert normalize(text) == r"$A = 120^\circ$"


def test_other_repairs_keep_real_math():
    assert normalize(r"$2^{10}$ と $x^2$ と $\theta$") == r"$2^{10}$ と $x^2$ と $\theta$"  # 本当の指数はそのまま
    assert normalize(r"\\sin x = \\frac{1}{2}") == r"\sin x = \frac{1}{2}"
    assert normalize(r"よって\n答え: $3$") == "よって\n答え: $3$"
    assert normalize(r"$a \neq b$、$\nabla$、$\not\in$") == r"$a \neq b$、$\nabla$、$\not\in$"
    assert normalize("\x0crac{1}{2}") == r"\frac{1}{2}"
    assert normalize(r"角 120^\circ の三角形") == "角 120° の三角形"  # 数式の外は ° に
    assert normalize(r"$\ngle BAC = 30^\circ$") == r"$\angle BAC = 30^\circ$"


def test_problems_found():
    found = " ".join(problems(r"$3回$ と $\root{148}$ と 边长 と \$5", field="問題文", choice=True))
    for word in ("日本語", r"\root", "中国語", r"\$"):
        assert word in found, word
    assert "奇数" in " ".join(problems("$x$ と $y"))
    assert "制御文字" in " ".join(problems("a\x07b"))
    assert "改行" in " ".join(problems("$a =\n b$"))
    assert "示せ" in " ".join(problems("途中の計算過程も示せ。", choice=True))
    assert problems("途中の計算過程も示せ。", choice=False) == []
    assert "求める" in " ".join(question_problems("面積 $S$ を求める。", choice=False))
    # 正しい書き方は引っかからない（\text の中の日本語は表示できる）
    ok = r"$\frac{15\sqrt{3}}{2}$、$\angle A = 30^\circ$、$\text{9人}$、${}_{5}\mathrm{C}_{2}$、$\left(x - 1\right)$"
    assert problems(ok) == [] and question_problems("面積 $S$ を求めよ。", choice=True) == []
