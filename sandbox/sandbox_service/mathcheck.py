"""SymPy による解答の読み取り・正規化・比較。

parse_expr() と srepr の復元は内部で eval() を使うため、このモジュールは
隔離されたサンドボックスコンテナの中だけで使う（web / worker からは HTTP 経由で呼ぶ）。

答えの形式 (kind):
    value           1つの数・式
    set             複数の値（順不同）      例: 方程式の解すべて
    tuple           順序のある組            例: 座標 (x, y)
    matrix          行列
    interval        不等式の解（x の範囲）
    antiderivative  不定積分（積分定数の差は無視して比較）
"""

from __future__ import annotations

import math
import random
import re
import unicodedata
from typing import Any

import sympy as sp
from sympy.logic.boolalg import BooleanFunction
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

KINDS = ("value", "set", "tuple", "matrix", "interval", "antiderivative")
MAX_INPUT = 500

_TRANSFORMS = standard_transformations + (implicit_multiplication_application, convert_xor)
_SYMPY_NS: dict[str, Any] = {}
exec("from sympy import *", _SYMPY_NS)  # srepr 復元用の名前空間

_EMPTY_WORDS = {"解なし", "なし", "空集合", "∅", "emptyset", "none"}
_ALL_WORDS = {"すべての実数", "全ての実数", "実数全体", "任意の実数", "reals"}

_PRE_NFKC = {"²": "^2", "³": "^3", "⁴": "^4"}  # NFKC で「²」が「2」になる前に置換する
_POST_NFKC = [
    ("×", "*"), ("÷", "/"), ("·", "*"), ("∙", "*"), ("−", "-"), ("–", "-"), ("ー", "-"),
    ("π", "pi"), ("∞", "oo"), ("θ", "theta"), ("α", "alpha"), ("β", "beta"),
    ("≦", "<="), ("≤", "<="), ("≧", ">="), ("≥", ">="), ("≠", "!="), ("、", ","),
]
_ASSIGN_RE = re.compile(r"(^|[,(\[{])\s*[A-Za-z_]\w*(?:\([^()]*\))?\s*=(?![=])")
_LONE_EQ_RE = re.compile(r"(?<![<>=!])=(?!=)")
_REL_RE = re.compile(r"(<=|>=|!=|<|>)")
_REL_FUNCS = {"<": sp.Lt, "<=": sp.Le, ">": sp.Gt, ">=": sp.Ge, "!=": sp.Ne}


class AnswerError(ValueError):
    """解答文字列を読み取れない、または形式が合わない。"""


# ---------------------------------------------------------------------------
# 文字列 → SymPy
# ---------------------------------------------------------------------------

def _local_dict() -> dict[str, Any]:
    return {
        "e": sp.E,
        "i": sp.I,
        "ln": sp.log,
        "arcsin": sp.asin,
        "arccos": sp.acos,
        "arctan": sp.atan,
    }


def preprocess(text: str) -> str:
    """全角文字や数学記号を SymPy が読める形に寄せる。"""
    s = str(text)
    for src, dst in _PRE_NFKC.items():
        s = s.replace(src, dst)
    s = unicodedata.normalize("NFKC", s)
    for src, dst in _POST_NFKC:
        s = s.replace(src, dst)
    s = re.sub(r"√\s*(\d+(?:\.\d+)?|[A-Za-z])", r"sqrt(\1)", s)
    s = s.replace("√", "sqrt")
    s = re.sub(r"(?<![A-Za-z])sqrt\s*(\d+)", r"sqrt(\1)", s)
    return s.strip()


def split_top_level(text: str, sep: str = ",") -> list[str]:
    """括弧の外側にある区切り文字だけで分割する。"""
    parts, depth, buf = [], 0, []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _strip_outer(text: str) -> str:
    """文字列全体を囲む 1 組の括弧を外す。"(1+√5)/2, 3" のような場合は外さない。"""
    s = text.strip()
    pairs = {"(": ")", "[": "]", "{": "}"}
    if len(s) < 2 or s[0] not in pairs or s[-1] != pairs[s[0]]:
        return s
    depth = 0
    for idx, ch in enumerate(s):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0 and idx != len(s) - 1:
                return s
    return s[1:-1].strip()


def _strip_assignments(text: str) -> str:
    """"x = 1, x = 2" や "f(x) = x^2" の左辺を取り除く。"""
    s = _ASSIGN_RE.sub(r"\1", text)
    found = list(_LONE_EQ_RE.finditer(s))
    if found:
        s = s[found[-1].end():]
    return s.strip()


def _expand_pm(text: str, depth: int = 0) -> list[str]:
    """"1±√2" を ["1+√2", "1-√2"] に展開する。"""
    if depth > 3:
        raise AnswerError("± が多すぎます")
    for sym, first, second in (("±", "+", "-"), ("∓", "-", "+")):
        if sym in text:
            return (_expand_pm(text.replace(sym, first, 1), depth + 1)
                    + _expand_pm(text.replace(sym, second, 1), depth + 1))
    return [text]


def _parse(text: str) -> Any:
    if not text or not text.strip():
        raise AnswerError("答えが空です")
    try:
        return parse_expr(text, local_dict=_local_dict(), transformations=_TRANSFORMS)
    except Exception as exc:  # parse_expr は多種の例外を投げる
        raise AnswerError(f"式として読み取れません: {text.strip()}") from exc


def _parse_interval(text: str) -> tuple[Any, str]:
    s = text.replace("または", ",").replace(" or ", ",")
    sets, var = [], None
    for part in split_top_level(s):
        tokens = _REL_RE.split(part)
        cond = None
        if len(tokens) >= 3:
            try:
                exprs = [_parse(t) for t in tokens[0::2]]
                ops = tokens[1::2]
                cond = sp.And(*[_REL_FUNCS[op](exprs[k], exprs[k + 1]) for k, op in enumerate(ops)])
            except AnswerError:
                cond = None  # "(1 < x) & (x < 3)" のような SymPy 記法は下で読む
        if cond is None:
            cond = _parse(part)
        obj, v = normalize_with_var(cond, "interval")
        sets.append(obj)
        var = var or v
    if not sets:
        raise AnswerError("答えが空です")
    return sp.Union(*sets), var or "x"


def _parse_matrix(text: str) -> Any:
    s = _strip_assignments(text)
    if ";" in s:
        body = _strip_outer(s)
        rows = [split_top_level(_strip_outer(r)) for r in body.split(";") if r.strip()]
        return [[_parse(c) for c in row] for row in rows]
    return _parse(s)


def parse_answer(text: str, kind: str) -> tuple[Any, str]:
    """利用者や LLM が書いた答えの文字列を正規化済みの SymPy オブジェクトにする。"""
    if kind not in KINDS:
        raise AnswerError(f"未知の答えの形式です: {kind}")
    if len(str(text)) > MAX_INPUT:
        raise AnswerError("答えが長すぎます")
    s = preprocess(text)
    key = s.replace(" ", "").lower()
    if kind == "interval":
        if key in _EMPTY_WORDS:
            return sp.S.EmptySet, "x"
        if key in _ALL_WORDS:
            return sp.S.Reals, "x"
        return _parse_interval(s)
    if kind == "set" and key in _EMPTY_WORDS:
        return sp.Tuple(), "x"
    if kind == "matrix":
        obj = _parse_matrix(s)
    elif kind in ("set", "tuple"):
        body = _strip_outer(_strip_assignments(s))
        pieces = []
        for part in split_top_level(body):
            pieces.extend(_expand_pm(part) if kind == "set" else [part])
        if any(("±" in p or "∓" in p) for p in pieces):
            raise AnswerError("± はこの形式では使えません")
        obj = [_parse(p) for p in pieces]
    else:
        s = _strip_assignments(s)
        if "±" in s or "∓" in s:
            raise AnswerError("± は答えが複数ある問題でだけ使えます")
        obj = _parse(s)
    return normalize_with_var(obj, kind)


def from_srepr(text: str) -> Any:
    """sp.srepr() の出力から SymPy オブジェクトを復元する。"""
    try:
        return eval(text, dict(_SYMPY_NS))  # noqa: S307 - サンドボックス内でのみ実行
    except Exception as exc:
        raise AnswerError("保存された答えを復元できません") from exc


# ---------------------------------------------------------------------------
# 正規化
# ---------------------------------------------------------------------------

def _from_python(obj: Any) -> Any:
    """numpy の値や Python の set などを扱いやすい形に寄せる。"""
    if type(obj).__module__.startswith("numpy"):
        obj = obj.tolist() if hasattr(obj, "tolist") else obj.item()
    if obj is None or isinstance(obj, (bool, bytes)) or obj in (sp.true, sp.false):
        raise AnswerError("答えが数や式になっていません")
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    return obj


def _scalar(obj: Any) -> sp.Expr:
    obj = _from_python(obj)
    if isinstance(obj, dict):
        if len(obj) != 1:
            raise AnswerError("答えが1つに定まっていません")
        obj = _from_python(next(iter(obj.values())))
    if isinstance(obj, (list, tuple, sp.Tuple, sp.FiniteSet)) and len(obj) == 1:
        return _scalar(list(obj)[0])
    if isinstance(obj, sp.MatrixBase):
        raise AnswerError("1つの数や式ではなく行列になっています")
    try:
        expr = sp.sympify(obj)
    except Exception as exc:
        raise AnswerError("答えを数式として扱えません") from exc
    if isinstance(expr, sp.Equality):
        expr = expr.rhs
    if not isinstance(expr, sp.Expr) or isinstance(expr, sp.MatrixBase):
        raise AnswerError("1つの数や式になっていません")
    return expr


def _to_items(obj: Any, kind: str) -> list[Any]:
    if obj == sp.S.EmptySet:
        return []
    if isinstance(obj, sp.FiniteSet):
        return list(obj.args)
    if isinstance(obj, dict):
        if kind == "tuple":
            return [v for _, v in sorted(obj.items(), key=lambda kv: str(kv[0]))]
        return list(obj.values())
    if isinstance(obj, sp.MatrixBase) and 1 in obj.shape:
        return list(obj)
    if isinstance(obj, (list, tuple, sp.Tuple)):
        items = []
        for value in obj:
            value = _from_python(value)
            if isinstance(value, dict) and len(value) == 1:  # solve(..., dict=True) の結果
                value = next(iter(value.values()))
            if isinstance(value, (list, tuple, sp.Tuple, dict, sp.MatrixBase)):
                raise AnswerError("入れ子になった組には対応していません")
            items.append(value)
        return items
    return [obj]


def _to_matrix(obj: Any) -> sp.ImmutableMatrix:
    if isinstance(obj, sp.MatrixBase):
        mat = obj
    elif isinstance(obj, (list, tuple, sp.Tuple)):
        try:
            mat = sp.Matrix([_from_python(row) for row in obj])
        except Exception as exc:
            raise AnswerError("行列の形になっていません") from exc
    else:
        raise AnswerError("行列ではありません")
    return sp.ImmutableMatrix(mat.applyfunc(_scalar))


def _single_var(obj: Any) -> str:
    syms = sorted((s for s in obj.free_symbols if isinstance(s, sp.Symbol)), key=lambda s: s.name)
    if len(syms) != 1:
        raise AnswerError("不等式の変数は1つにしてください")
    return syms[0].name


def _to_set(obj: Any) -> tuple[sp.Set, str | None]:
    if isinstance(obj, sp.Set):
        return obj, None
    if isinstance(obj, (sp.Rel, BooleanFunction)):
        var = _single_var(obj)
        try:
            return obj.as_set(), var
        except Exception as exc:
            raise AnswerError("不等式を範囲に直せません") from exc
    if isinstance(obj, (list, tuple, sp.Tuple)):
        sets, var = [], None
        for item in obj:
            item = _from_python(item)
            if isinstance(item, (sp.Set, sp.Rel, BooleanFunction)):
                piece, v = _to_set(item)
            else:
                piece, v = sp.FiniteSet(_scalar(item)), None
            sets.append(piece)
            var = var or v
        return sp.Union(*sets), var
    if isinstance(obj, (int, float, sp.Expr)):
        return sp.FiniteSet(_scalar(obj)), None
    raise AnswerError("範囲（不等式の解）になっていません")


def _strip_assumptions(obj: Any) -> Any:
    """Symbol('x', real=True) と Symbol('x') を同じ文字として扱えるようにする。"""
    if not isinstance(obj, sp.Basic):
        return obj
    syms = [s for s in obj.free_symbols if isinstance(s, sp.Symbol)]
    if not syms:
        return obj
    return obj.xreplace({s: sp.Symbol(s.name) for s in syms})


def _short_rational(value: sp.Float) -> sp.Rational | None:
    """2.5 や 0.1 のような短い小数を有理数にする。長い小数（近似値）はそのまま。"""
    v = float(value)
    if not math.isfinite(v):
        return None
    text = format(v, ".10g")
    if abs(float(text) - v) > 1e-12 * max(1.0, abs(v)):
        return None
    digits = text.lower().split("e")[0].replace("-", "").replace(".", "").lstrip("0")
    if len(digits) > 8:
        return None
    return sp.Rational(text)


def _rationalize(obj: Any) -> Any:
    if not isinstance(obj, sp.Basic):
        return obj
    repl = {}
    for f in obj.atoms(sp.Float):
        r = _short_rational(f)
        if r is not None:
            repl[f] = r
    return obj.xreplace(repl) if repl else obj


def _main_var(obj: Any) -> str:
    if not isinstance(obj, sp.Basic):
        return "x"
    names = sorted(s.name for s in obj.free_symbols if isinstance(s, sp.Symbol))
    if "x" in names or not names:
        return "x"
    return names[0]


def normalize_with_var(obj: Any, kind: str) -> tuple[Any, str]:
    """任意の答え（Python / SymPy / numpy）を kind に合わせた正規形にする。"""
    if kind not in KINDS:
        raise AnswerError(f"未知の答えの形式です: {kind}")
    if isinstance(obj, str):  # answer = "3/2" のように文字列で書かれた場合
        return parse_answer(obj, kind)
    obj = _from_python(obj)
    var = None
    if kind == "matrix":
        result = _to_matrix(obj)
    elif kind == "interval":
        result, var = _to_set(obj)
    elif kind in ("set", "tuple"):
        result = sp.Tuple(*[_scalar(v) for v in _to_items(obj, kind)])
    else:
        result = _scalar(obj)
    result = _rationalize(_strip_assumptions(result))
    return result, var or _main_var(result)


def has_float(obj: Any) -> bool:
    return isinstance(obj, sp.Basic) and bool(obj.atoms(sp.Float))


# ---------------------------------------------------------------------------
# 表示
# ---------------------------------------------------------------------------

def _interval_latex(s: sp.Set, var: str) -> str:
    v = sp.latex(sp.Symbol(var))
    if s == sp.S.EmptySet:
        return r"\text{解なし}"
    if s == sp.S.Reals:
        return r"\text{すべての実数}"
    pieces = list(s.args) if isinstance(s, sp.Union) else [s]
    out = []
    for p in pieces:
        if isinstance(p, sp.Interval):
            lo, hi = p.start, p.end
            lop = "<" if p.left_open else r"\leqq"
            hip = "<" if p.right_open else r"\leqq"
            if lo == -sp.oo and hi == sp.oo:
                out.append(r"\text{すべての実数}")
            elif lo == -sp.oo:
                out.append(f"{v} {hip} {sp.latex(hi)}")
            elif hi == sp.oo:
                out.append(f"{sp.latex(lo)} {lop} {v}")
            else:
                out.append(f"{sp.latex(lo)} {lop} {v} {hip} {sp.latex(hi)}")
        elif isinstance(p, sp.FiniteSet):
            out.extend(f"{v} = {sp.latex(e)}" for e in p.args)
        else:
            out.append(sp.latex(p))
    return ",\\ ".join(out)


def to_latex(obj: Any, kind: str, var: str = "x") -> str:
    if kind == "interval":
        return _interval_latex(obj, var)
    if kind == "set":
        return ",\\ ".join(sp.latex(v) for v in obj.args) if obj.args else r"\text{解なし}"
    if kind == "tuple":
        return r"\left(" + ",\\ ".join(sp.latex(v) for v in obj.args) + r"\right)"
    if kind == "antiderivative":
        has_c = any(getattr(s, "name", "") == "C" for s in obj.free_symbols)
        return sp.latex(obj) + ("" if has_c else " + C")
    return sp.latex(obj)


def describe(obj: Any, kind: str, var: str = "x") -> dict[str, Any]:
    return {
        "srepr": sp.srepr(obj),
        "latex": to_latex(obj, kind, var),
        "has_float": has_float(obj),
        "var": var,
        "kind": kind,
    }


# ---------------------------------------------------------------------------
# 比較
# ---------------------------------------------------------------------------

def _numeric_close(a: Any, b: Any, tol: float) -> tuple[bool | None, bool]:
    """(ほぼ等しいか, 1e-4 の範囲で近いか)。評価できなければ (None, False)。"""
    try:
        va, vb = sp.N(a, 60), sp.N(b, 60)
        if va == vb:
            return True, True
        if not (va.is_number and vb.is_number) or va.has(sp.nan, sp.zoo) or vb.has(sp.nan, sp.zoo):
            return None, False
        diff = abs(sp.N(va - vb, 60))
        mag = max(sp.Integer(1), abs(va), abs(vb))
        if not diff.is_finite or not mag.is_finite:
            return False, False
        return bool(diff <= tol * mag), bool(diff <= sp.Rational(1, 10**4) * mag)
    except (TypeError, ValueError, ArithmeticError):
        return None, False


def _scalar_eq(a: sp.Expr, b: sp.Expr, *, antiderivative: bool = False, var: str = "x",
               allow_float: bool = False) -> tuple[bool, bool]:
    """(等しいか, 近似的に近いか)。"""
    if antiderivative:
        sym = sp.Symbol(var)
        a, b = sp.diff(a, sym), sp.diff(b, sym)
    if a == b:
        return True, False
    if a.is_Rational and b.is_Rational:  # 整数・分数どうしは厳密に比べる
        return False, _numeric_close(a, b, 0)[1]
    floats = has_float(a) or has_float(b)
    diff = a - b
    if not floats:
        try:
            if sp.expand(diff) == 0:
                return True, False
        except Exception:
            pass
    tol = 1e-9 if floats else 1e-40
    syms = sorted((s for s in diff.free_symbols if isinstance(s, sp.Symbol)), key=lambda s: s.name)
    if not syms:
        equal, close = _numeric_close(a, b, tol)
        if equal and floats and not allow_float:
            return False, True
        return bool(equal), close
    rng = random.Random(20240601)
    valid = 0
    for _ in range(15):
        subs = {s: sp.Rational(rng.randint(30, 270), 100) for s in syms}
        equal, close = _numeric_close(a.subs(subs), b.subs(subs), tol)
        if equal is None:
            continue
        if not equal:
            return False, False
        valid += 1
        if valid >= 5:
            break
    if valid == 0:
        try:
            return sp.simplify(diff) == 0, False
        except Exception:
            return False, False
    if floats and not allow_float:
        return False, True
    return True, False


def _sets_equal(a: sp.Set, b: sp.Set) -> bool:
    if a == b:
        return True
    try:
        return a.symmetric_difference(b).is_empty is True
    except Exception:
        return False


def compare(expected: Any, given: Any, kind: str, *, var: str = "x",
            allow_float: bool = False) -> dict[str, Any]:
    """正規化済みの 2 つの答えを比べる。

    allow_float=False のとき、小数の近似値は厳密な値と一致しても不正解（approx=True）にする。
    """
    def result(equal: bool, approx: bool = False, detail: str = "") -> dict[str, Any]:
        return {"equal": bool(equal), "approx": bool(approx and not equal), "detail": detail}

    if kind == "interval":
        return result(_sets_equal(expected, given))
    if kind == "matrix":
        if expected.shape != given.shape:
            return result(False, detail="行列の大きさが違います")
        pairs = [_scalar_eq(a, b, allow_float=allow_float) for a, b in zip(expected, given)]
        return result(all(p[0] for p in pairs), all(p[0] or p[1] for p in pairs))
    if kind in ("set", "tuple"):
        exp_items, giv_items = list(expected.args), list(given.args)
        if len(exp_items) != len(giv_items):
            return result(False, detail=f"答えの個数が違います（正しくは {len(exp_items)} 個）")
        if kind == "tuple":
            pairs = [_scalar_eq(a, b, allow_float=allow_float) for a, b in zip(exp_items, giv_items)]
            return result(all(p[0] for p in pairs), all(p[0] or p[1] for p in pairs))
        remaining = list(giv_items)
        approx_all = True
        for a in exp_items:
            match = None
            near = False
            for idx, b in enumerate(remaining):
                equal, close = _scalar_eq(a, b, allow_float=allow_float)
                if equal:
                    match = idx
                    break
                near = near or close
            if match is None:
                if not near:
                    approx_all = False
                return result(False, approx_all)
            remaining.pop(match)
        return result(True)
    equal, approx = _scalar_eq(expected, given, antiderivative=(kind == "antiderivative"),
                               var=var, allow_float=allow_float)
    return result(equal, approx)
