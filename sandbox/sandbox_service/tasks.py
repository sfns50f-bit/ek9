"""サンドボックスの子プロセスで実行するタスク。

server.py が 1 リクエストごとに子プロセスを作り、child_main() を呼ぶ。
子プロセスは資源制限をかけてから、利用者や LLM が書いた答えの文字列を読み取り
（SymPy の parse_expr は内部で eval を使うため、ここでだけ扱う）、
結果を JSON で親に返して終了する。
"""

from __future__ import annotations

import json
import os
import resource
from typing import Any, Callable

import sympy as sp  # noqa: F401 - forkserver で先読みしておく

from . import mathcheck as mc


def _apply_limits(cpu_seconds: float) -> None:
    mem = int(os.environ.get("SANDBOX_MEMORY_MB", "384")) * 1024 * 1024
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


def task_compare(payload: dict[str, Any]) -> dict[str, Any]:
    """expected と given を比べる。どちらも {"srepr": ...} か {"text": ...} で渡す。"""
    kind = str(payload.get("kind") or "value")
    expected, exp_var = _load(payload.get("expected"), kind)
    given_spec = payload.get("given")
    if isinstance(given_spec, dict) and not given_spec.get("srepr"):
        # 文字列の答え（子供の入力・検証役の答え）には、正解にない関数を使わせない
        mc.ALLOWED_FUNCS = mc.function_types(expected)
    try:
        given, giv_var = _load(given_spec, kind)
    except mc.AnswerError as exc:
        return {"ok": False, "error": str(exc), "where": "given"}
    finally:
        mc.ALLOWED_FUNCS = None
    var = str(payload.get("var") or exp_var or giv_var or "x")
    res = mc.compare(expected, given, kind, var=var, allow_float=bool(payload.get("allow_float")))
    return {
        "ok": True,
        **res,
        "expected": mc.describe(expected, kind, var),
        "given": mc.describe(given, kind, var),
    }


def task_distinct(payload: dict[str, Any]) -> dict[str, Any]:
    """items（srepr の一覧）のうち、同値なものの組を返す（選択肢の重複・誤答と正解の一致の確認）。"""
    kind = str(payload.get("kind") or "value")
    items = payload.get("items") or []
    if not isinstance(items, list) or not 2 <= len(items) <= 12:
        raise mc.AnswerError("items は 2〜12 個にしてください")
    objs = [_load({"srepr": str(s)}, kind)[0] for s in items]
    var = str(payload.get("var") or "x")
    pairs = []
    for i in range(len(objs)):
        for j in range(i + 1, len(objs)):
            res = mc.compare(objs[i], objs[j], kind, var=var, allow_float=True)
            if res["equal"]:
                pairs.append([i, j])
    return {"ok": True, "equal_pairs": pairs, "latex": [mc.to_latex(o, kind, var) for o in objs]}


def task_preview(payload: dict[str, Any]) -> dict[str, Any]:
    """入力途中の答えを LaTeX にして返す（画面のプレビュー用）。"""
    kind = str(payload.get("kind") or "value")
    obj, var = mc.parse_answer(str(payload.get("text", "")), kind)
    return {"ok": True, "latex": mc.to_latex(obj, kind, str(payload.get("var") or var))}


TASKS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "distinct": task_distinct,
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
    except BaseException as exc:  # 想定外の入力でもサンドボックスは止めない
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
