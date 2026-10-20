"""数学の単元の一覧と、選択式の誤答につける「間違いの種類」。

小単元（テンプレート）とそのレベル・関連する単元は templates.py の @template に書く。
"""

from __future__ import annotations

import ast
import functools
import os
from dataclasses import dataclass

# 単元（大きなまとまり）。小単元（テンプレート）は templates.py の @template(..., unit=...) で決まる
UNITS: list[tuple[str, str]] = [
    ("m1-quad", "数学I 2次関数"),
    ("m1-trig", "数学I 図形と計量"),
    ("ma-case", "数学A 場合の数と確率"),
    ("m2-poly", "数学II 式と証明・複素数と方程式"),
    ("m2-trig", "数学II 三角関数"),
    ("m2-explog", "数学II 指数関数・対数関数"),
    ("m2-diff", "数学II 微分法"),
    ("m2-int", "数学II 積分法"),
    ("mb-seq", "数学B 数列"),
    ("mc-vec", "数学C ベクトル"),
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


@dataclass(frozen=True)
class TemplateInfo:
    """テンプレートの情報（小単元として画面に出る）。テンプレートを足すときは @template に書くだけでよい。"""

    id: str
    unit: str
    label: str
    level: str
    formats: tuple[str, ...] = ("choice", "input")
    related: tuple[str, ...] = ()


@functools.cache
def template_info() -> dict[str, TemplateInfo]:
    """templates.py の @template(...) の引数を、sympy を読み込まずに（ソースを ast で）読む。

    web は子供が操作する画面なので sympy を読み込まない（メモリと起動時間）。templates.py を import した
    ときの INFO と同じになることは tests/test_math_templates.py で確認する。
    """
    path = os.path.join(os.path.dirname(__file__), "templates.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    out: dict[str, TemplateInfo] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for deco in node.decorator_list:
            if isinstance(deco, ast.Call) and getattr(deco.func, "id", "") == "template":
                tid = ast.literal_eval(deco.args[0])
                kw = {k.arg: ast.literal_eval(k.value) for k in deco.keywords}
                out[tid] = TemplateInfo(tid, **kw)
    return out
