"""サンドボックスの子プロセスで実行するタスク。

server.py が 1 リクエストごとに子プロセスを作り、child_main() を呼ぶ。
子プロセスは資源制限をかけてから LLM が書いたコードや利用者の入力を扱い、
結果を JSON で親に返して終了する。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import resource
from typing import Any, Callable

import sympy as sp  # noqa: F401 - forkserver で先読みしておく

from . import mathcheck as mc


# 検算コードの前に実行する準備。小さなモデルは x などを定義し忘れやすいので先に用意しておく。
_PRELUDE = """
import math
import sympy
import sympy as sp
from sympy import *
x, y, z, t, a, b, n, k = symbols("x y z t a b n k")
"""


def _apply_limits(cpu_seconds: float) -> None:
    mem = int(os.environ.get("SANDBOX_MEMORY_MB", "768")) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    cpu = int(cpu_seconds) + 1
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1 << 20, 1 << 20))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _load(spec: dict[str, Any], kind: str) -> tuple[Any, str]:
    if not isinstance(spec, dict):
        raise mc.AnswerError("答えの指定が不正です")
    if spec.get("srepr"):
        return mc.normalize_with_var(mc.from_srepr(str(spec["srepr"])), kind)
    return mc.parse_answer(str(spec.get("text", "")), kind)


def task_evaluate(payload: dict[str, Any]) -> dict[str, Any]:
    """LLM が書いた検算コードを実行し、変数 answer の値を正規化して返す。"""
    code = str(payload.get("code") or "")
    kind = str(payload.get("kind") or "value")
    if not code.strip():
        raise mc.AnswerError("コードが空です")
    namespace: dict[str, Any] = {"__name__": "__verify__"}
    exec(_PRELUDE, namespace)
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        exec(compile(code, "<verify_code>", "exec"), namespace)
    if "answer" not in namespace:
        raise mc.AnswerError("変数 answer が定義されていません")
    obj, var = mc.normalize_with_var(namespace["answer"], kind)
    return {"ok": True, **mc.describe(obj, kind, var), "stdout": out.getvalue()[-2000:]}


def task_compare(payload: dict[str, Any]) -> dict[str, Any]:
    """expected と given を比べる。どちらも {"srepr": ...} か {"text": ...} で渡す。"""
    kind = str(payload.get("kind") or "value")
    expected, exp_var = _load(payload.get("expected"), kind)
    try:
        given, giv_var = _load(payload.get("given"), kind)
    except mc.AnswerError as exc:
        return {"ok": False, "error": str(exc), "where": "given"}
    var = str(payload.get("var") or exp_var or giv_var or "x")
    res = mc.compare(expected, given, kind, var=var, allow_float=bool(payload.get("allow_float")))
    return {
        "ok": True,
        **res,
        "expected": mc.describe(expected, kind, var),
        "given": mc.describe(given, kind, var),
    }


def task_preview(payload: dict[str, Any]) -> dict[str, Any]:
    """入力途中の答えを LaTeX にして返す（画面のプレビュー用）。"""
    kind = str(payload.get("kind") or "value")
    obj, var = mc.parse_answer(str(payload.get("text", "")), kind)
    return {"ok": True, "latex": mc.to_latex(obj, kind, str(payload.get("var") or var))}


TASKS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "evaluate": task_evaluate,
    "compare": task_compare,
    "preview": task_preview,
}


def run(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """例外を結果の辞書に変換してタスクを実行する（テストからも使う）。"""
    try:
        return TASKS[name](payload)
    except mc.AnswerError as exc:
        return {"ok": False, "error": str(exc)}
    except MemoryError:
        return {"ok": False, "error": "メモリの上限を超えました"}
    except BaseException as exc:  # LLM のコードは SystemExit なども投げうる
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:1000]}


def child_main(name: str, payload: dict[str, Any], conn: Any, cpu_seconds: float) -> None:
    _apply_limits(cpu_seconds)
    result = run(name, payload)
    try:
        data = json.dumps(result, ensure_ascii=False, default=str).encode()
    except BaseException as exc:
        data = json.dumps({"ok": False, "error": f"結果を返せません: {exc}"[:500]}).encode()
    conn.send_bytes(data)
    conn.close()
    os._exit(0)
