import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "sandbox")]

from quizapp.config import Settings  # noqa: E402
from sandbox_service import tasks  # noqa: E402


class InProcessSandbox:
    """サンドボックスの HTTP を通さずに同じ処理を呼ぶ（テスト用。資源制限はかからない）。"""

    def evaluate(self, code, kind):
        return tasks.run("evaluate", {"code": code, "kind": kind})

    def compare(self, kind, expected, given, *, var="x", allow_float=False):
        return tasks.run("compare", {"kind": kind, "expected": expected, "given": given,
                                     "var": var, "allow_float": allow_float})

    def preview(self, text, kind, var="x"):
        return tasks.run("preview", {"text": text, "kind": kind, "var": var})

    def healthy(self):
        return True


class ScriptedLLM:
    """役割（作問・解答・審査）ごとに用意した応答を順に返す。"""

    def __init__(self, gen=(), solve=(), review=()):
        self.queues = {"gen": list(gen), "solve": list(solve), "review": list(review)}
        self.calls = []

    def chat_json(self, model, system, user, schema, *, temperature=0.7):
        props = schema["properties"]
        role = "gen" if "verify_code" in props else "solve" if "work" in props else "review"
        self.calls.append((role, model, user))
        queue = self.queues[role]
        if not queue:
            raise AssertionError(f"no scripted response for {role}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def list_models(self):
        return [
            {"name": "gemma4:e4b", "size_gb": 3.1, "parameter_size": "8B", "quantization": "Q4_K_M"},
            {"name": "gemma4:12b", "size_gb": 8.1, "parameter_size": "12B", "quantization": "Q4_K_M"},
        ]


@pytest.fixture
def sandbox():
    return InProcessSandbox()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key in ("GEN_MODEL", "SOLVE_MODEL", "REVIEW_MODEL", "OLLAMA_THINK"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "quiz.db"))
    monkeypatch.setenv("OLLAMA_MODEL", "gemma4:e4b")
    return Settings()


GOOD_GEN = {
    "question": "2次方程式 $x^2 - 4x + 3 = 0$ の解をすべて求めよ。",
    "explanation": "因数分解すると $(x-1)(x-3)=0$ なので $x = 1, 3$。",
    "answer_kind": "set",
    "answer": "[1, 3]",
    "verify_code": "answer = solve(x**2 - 4*x + 3, x)",
}
GOOD_SOLVE = {"work": "(x-1)(x-3)=0", "answer": "[3, 1]", "code": "answer = solve(x**2-4*x+3, x)"}
GOOD_REVIEW = {"issues": [], "solvable": True, "unique": True, "answer_matches": True,
               "explanation_ok": True, "level_ok": True}
