import random
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "sandbox")]

from quizapp.config import Settings  # noqa: E402
from quizapp.datadir import MARKER  # noqa: E402
from quizapp.db import Problem, init_db  # noqa: E402
from quizapp.llm import LLMUnavailable  # noqa: E402
from sandbox_service import tasks  # noqa: E402


class InProcessSandbox:
    """サンドボックスの HTTP を通さずに同じ処理を呼ぶ（テスト用。資源制限はかからない）。"""

    def __init__(self):
        self.up = True

    def _run(self, name, payload):
        if not self.up:
            from quizapp.sandbox_client import SandboxUnavailable
            raise SandboxUnavailable("down")
        return tasks.run(name, payload)

    def compare(self, kind, expected, given, *, var="x", allow_float=False):
        return self._run("compare", {"kind": kind, "expected": expected, "given": given,
                                     "var": var, "allow_float": allow_float})

    def distinct(self, kind, items, var="x"):
        return self._run("distinct", {"kind": kind, "items": items, "var": var})

    def preview(self, text, kind, var="x"):
        return self._run("preview", {"text": text, "kind": kind, "var": var})

    def equalities(self, segments):
        return self._run("equalities", {"segments": segments})

    def healthy(self):
        return self.up


def answer_text(value, kind):
    """sympy の答えを、検証役やキーボード入力が書く文字列にする。"""
    import sympy as sp

    if kind == "tuple":
        return "(" + ", ".join(str(v) for v in value) + ")"
    if kind == "set":
        return ", ".join(str(v) for v in value)
    if kind == "interval":
        pieces = list(value.args) if isinstance(value, sp.Union) else [value]
        out = []
        for p in pieces:
            lo = "<" if p.left_open else "<="
            hi = "<" if p.right_open else "<="
            if p.start == -sp.oo:
                out.append(f"x {hi} {p.end}")
            elif p.end == sp.oo:
                out.append(f"{p.start} {lo} x")
            else:
                out.append(f"{p.start} {lo} x {hi} {p.end}")
        return ", ".join(out)
    return str(value)


def true_answer(problem):
    """問題の params（seed）からテンプレートを作り直して、本当の正解を得る（ずらす前の値）。"""
    from quizapp.subjects.math.templates import TEMPLATES

    inst = TEMPLATES[problem.template](random.Random(problem.params["seed"]))
    return answer_text(inst.answer, inst.kind)


class OracleLLM:
    """テスト用の LLM。

    作成役：与えられた式・値をそのまま問題文に入れ、解説の最後に正解を書く
    検証役：問題文からデータベースの問題を探し、テンプレートを作り直して本当の正解を答える
    （solve_answer を渡すと、その答えを返す＝間違える検証役）
    """

    def __init__(self, sessions, *, solve_answer=None, alter_givens=False, down_calls=0):
        self.sessions = sessions
        self.solve_answer = solve_answer
        self.alter_givens = alter_givens
        self.down_calls = down_calls
        self.calls = []
        self.reviews = []  # 解説の見直しを頼まれたモデル
        self.review_verdict = "問題なし"
        self.n = 0

    def chat_json(self, model, system, user, schema, *, temperature=0.7):
        if "verdict" in schema["properties"]:  # 解説の見直し：review_verdict を返す
            self.reviews.append(model)
            return {"verdict": self.review_verdict, "issues": "なし" if "なし" in self.review_verdict else "途中式が違う"}
        role = "gen" if "hint" in schema["properties"] else "solve"
        if self.down_calls > 0:
            self.down_calls -= 1
            raise LLMUnavailable("Ollama に接続できません（テスト）")
        self.calls.append((role, model))
        if role == "gen":
            self.n += 1
            givens = re.findall(r"^- (.*)$", user.split("正解:")[0], re.M)
            answer = re.search(r"^正解: \$(.*)\$$", user, re.M).group(1)
            if self.alter_givens:
                givens = [re.sub(r"\d", lambda m: str((int(m.group()) + 1) % 10), g) for g in givens]
            question = f"（問{self.n}）" + "、".join(givens) + " のとき、答えを求めよ。"
            return {"question": question, "hint": "公式を思い出そう。",
                    "explanation": "まず使う公式を確かめ、与えられた値をそのまま代入する。\n"
                                   "途中の計算を一つずつ進めて、最後に答えの形に整える。\n"
                                   f"答え: ${answer}$"}
        question = user.split("問題:\n", 1)[1].split("\n\n答え方", 1)[0]
        if self.solve_answer is not None:
            return {"work": "...", "answer": self.solve_answer}
        with self.sessions() as s:
            from sqlalchemy import select
            problem = s.execute(select(Problem).where(Problem.question == question)).scalars().first()
            return {"work": "テンプレートから計算", "answer": true_answer(problem)}

    def list_models(self):
        return [{"name": "gemma4:12b-it-qat", "size_gb": 8.1, "parameter_size": "12B", "quantization": "Q4"},
                {"name": "gemma4:e4b", "size_gb": 3.1, "parameter_size": "8B", "quantization": "Q4"}]


@pytest.fixture
def sandbox():
    return InProcessSandbox()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key in ("GEN_MODEL", "VERIFY_MODEL", "VERIFY_COUNT", "VERIFY_MODEL_2", "OLLAMA_THINK", "SKIP_DATA_CHECK"):
        monkeypatch.delenv(key, raising=False)
    data = tmp_path / "data"
    data.mkdir()
    (data / MARKER).write_text("ai-quiz\n")
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434")
    return Settings()


@pytest.fixture
def sessions(settings):
    return init_db(settings.database_path)
