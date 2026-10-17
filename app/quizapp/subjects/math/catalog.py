"""数学の単元と小単元（テンプレート）の一覧。web からも読むので sympy を使わない。

小単元の ID は templates.py の @template("…") と一致させる（tests/test_math_templates.py で確認）。
"""

from __future__ import annotations

from ..base import Subunit, Unit

BOTH = ("choice", "input")

UNITS: list[Unit] = [
    Unit("m1-quad", "数学I 2次関数", (
        Subunit("m1-quad-vertex", "放物線の頂点", BOTH),
        Subunit("m1-quad-ineq", "2次不等式", BOTH),
    )),
    Unit("m1-trig", "数学I 図形と計量", (
        Subunit("m1-trig-cosine", "余弦定理で辺の長さ", BOTH),
        Subunit("m1-trig-area", "三角形の面積", BOTH),
    )),
    Unit("ma-case", "数学A 場合の数と確率", (
        Subunit("ma-case-comb", "組合せ", BOTH),
        Subunit("ma-case-repeat", "反復試行の確率", BOTH),
    )),
    Unit("m2-poly", "数学II 式と証明・複素数と方程式", (
        Subunit("m2-poly-vieta", "解と係数の関係", BOTH),
        Subunit("m2-poly-remainder", "剰余の定理", BOTH),
    )),
    Unit("m2-trig", "数学II 三角関数", (
        Subunit("m2-trig-value", "弧度法と三角関数の値", BOTH),
        Subunit("m2-trig-addition", "加法定理", BOTH),
        Subunit("m2-trig-double", "2倍角の公式", BOTH),
        Subunit("m2-trig-synth", "三角関数の合成と最大・最小", BOTH),
    )),
    Unit("m2-explog", "数学II 指数関数・対数関数", (
        Subunit("m2-explog-base", "対数の値（底の変換）", BOTH),
        Subunit("m2-explog-equation", "対数方程式", BOTH),
    )),
    Unit("m2-diff", "数学II 微分法", (
        Subunit("m2-diff-tangent", "接線の方程式", BOTH),
        Subunit("m2-diff-extremum", "極値", BOTH),
    )),
    Unit("m2-int", "数学II 積分法", (
        Subunit("m2-int-definite", "定積分の計算", BOTH),
        Subunit("m2-int-area", "放物線で囲まれた面積", BOTH),
    )),
    Unit("mb-seq", "数学B 数列", (
        Subunit("mb-seq-arith", "等差数列の和", BOTH),
    )),
    Unit("mc-vec", "数学C ベクトル", (
        Subunit("mc-vec-angle", "ベクトルのなす角", BOTH),
    )),
]

# 選択式の誤答につける「間違いの種類」。回答記録に残し、後のフェーズで得意・苦手の分析に使う
MISTAKES: dict[str, str] = {
    "sign": "符号ミス",
    "formula": "公式の取り違え",
    "coef": "係数の付け忘れ・かけ忘れ",
    "power": "2乗・平方根の付け忘れ",
    "swap": "値・役割の取り違え",
    "reciprocal": "逆数・分母分子の取り違え",
    "domain": "条件・範囲の見落とし",
    "ineq": "不等号の向きの取り違え",
    "boundary": "端点（等号）の扱いの誤り",
    "partial": "途中で止めた・一部だけ",
    "calc": "計算の取り違え",
}
