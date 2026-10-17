"""問題の生成と検証（作問 → Python 検算 → 独立解答 → 審査）。

合格の条件:
  1. 作問モデルが書いた検算コードをサンドボックスで実行し、その結果が作問時の答えと一致する
  2. 答えを見せずに問題文だけを渡した解答役が、同じ答えにたどり着く（解答役の Python か最終解答のどちらか）
  3. 審査役が「解ける・答えが1つ・答えが問いに対応・解説が正しい」と判断する（REVIEW_ENABLED=false で省略）
学習者に見せる正答は Python で計算した値（小数しか得られない場合は、数値的に一致した作問時の厳密な答え）。
"""

from __future__ import annotations

import random
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol

from . import prompts
from .config import Settings
from .llm import LLMError
from .topics import Topic


class LLM(Protocol):
    def chat_json(self, model: str, system: str, user: str, schema: dict[str, Any], *,
                  temperature: float = 0.7) -> dict[str, Any]: ...


class Sandbox(Protocol):
    def evaluate(self, code: str, kind: str) -> dict[str, Any]: ...

    def compare(self, kind: str, expected: dict[str, str], given: dict[str, str], *,
                var: str = "x", allow_float: bool = False) -> dict[str, Any]: ...


@dataclass
class Step:
    name: str
    ok: bool
    summary: str
    code: str = ""
    detail: str = ""


@dataclass
class Candidate:
    topic_id: str
    difficulty: int
    subtopic: str
    question: str
    answer_kind: str
    answer_srepr: str
    answer_latex: str
    answer_var: str
    explanation: str
    models: str
    steps: list[Step] = field(default_factory=list)

    def log(self) -> dict[str, Any]:
        return {"steps": [asdict(s) for s in self.steps]}


@dataclass
class Rejection:
    topic_id: str
    difficulty: int
    subtopic: str
    question: str
    answer_kind: str
    reason: str
    models: str
    steps: list[Step] = field(default_factory=list)

    def log(self) -> dict[str, Any]:
        return {"steps": [asdict(s) for s in self.steps]}


class Rejected(Exception):
    pass


class Aborted(Exception):
    """キャンセルや、優先度の高い依頼が来たため中断した。"""


_CTRL_MAP = {"\x08": "b", "\x0c": "f", "\t": "t", "\r": "r", "\n": "n"}
_CTRL_RE = re.compile(r"[\x08\x0c\t\r\n](?=[A-Za-z])")


def repair_latex(text: str) -> str:
    r"""JSON の "\frac" が改ページ文字 + "rac" になる、といった崩れを数式の中だけ直す。"""
    out: list[str] = []
    in_math, delim = False, ""
    for part in re.split(r"(\$\$|\$)", text):
        if part in ("$", "$$"):
            if not in_math:
                in_math, delim = True, part
            elif part == delim:
                in_math = False
            out.append(part)
        elif in_math:
            out.append(_CTRL_RE.sub(lambda m: "\\" + _CTRL_MAP[m.group()], part))
        else:
            out.append(part)
    return "".join(out)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


class Pipeline:
    def __init__(self, llm: LLM, sandbox: Sandbox, settings: Settings, rng: random.Random | None = None) -> None:
        self.llm = llm
        self.sandbox = sandbox
        self.settings = settings
        self.rng = rng or random.Random()

    def run(self, topic: Topic, difficulty: int, recent: list[str], *,
            progress: Callable[[str], None] | None = None,
            should_abort: Callable[[], bool] | None = None) -> tuple[Candidate | None, list[Rejection]]:
        """合格するまで最大 max_attempts 回作り直す。LLMUnavailable / SandboxUnavailable はそのまま投げる。"""
        rejections: list[Rejection] = []
        total = self.settings.max_attempts
        for n in range(1, total + 1):
            def report(stage: str, n: int = n) -> None:
                if should_abort and should_abort():
                    raise Aborted
                if progress:
                    progress(f"{n}/{total} 回目：{stage}")

            subtopic = self.rng.choice(topic.subtopics)
            steps: list[Step] = []
            draft: dict[str, str] = {"question": "", "answer_kind": ""}
            try:
                return self._attempt(topic, difficulty, subtopic, recent, steps, draft, report), rejections
            except Rejected as exc:
                reason = str(exc)
            except LLMError as exc:
                reason = f"モデルの出力を読めませんでした: {exc}"
                steps.append(Step("モデル出力", False, reason))
            rejections.append(Rejection(
                topic_id=topic.id, difficulty=difficulty, subtopic=subtopic,
                question=draft["question"], answer_kind=draft["answer_kind"] or "value",
                reason=reason, models=", ".join(self.settings.models), steps=steps,
            ))
        return None, rejections

    def _attempt(self, topic: Topic, difficulty: int, subtopic: str, recent: list[str],
                 steps: list[Step], draft: dict[str, str], report: Callable[[str], None]) -> Candidate:
        s = self.settings

        def fail(name: str, summary: str, code: str = "", detail: str = "") -> None:
            steps.append(Step(name, False, summary, code=code, detail=detail))
            raise Rejected(summary)

        # 1. 作問
        report("作問中")
        gen = self.llm.chat_json(
            s.gen_model, prompts.GEN_SYSTEM, prompts.gen_user(topic, difficulty, subtopic, recent),
            prompts.GEN_SCHEMA, temperature=0.8,
        )
        question = repair_latex(str(gen.get("question") or "").strip())
        explanation = repair_latex(str(gen.get("explanation") or "").strip())
        kind = str(gen.get("answer_kind") or "").strip()
        answer = str(gen.get("answer") or "").strip()
        code = str(gen.get("verify_code") or "").strip()
        draft.update(question=question, answer_kind=kind if kind in prompts.KINDS else "")
        if not (question and answer and code and explanation):
            fail("作問", "作問の出力に足りない項目があります")
        if kind not in prompts.KINDS:
            fail("作問", f"答えの形式 {kind!r} は扱えません")
        if _norm(question) in {_norm(q) for q in recent}:
            fail("作問", "最近の問題と同じ問題が作られました")
        steps.append(Step("作問", True, f"{s.gen_model} が作問（{subtopic}）", detail=f"作問時の答え: {answer}"))

        # 2. Python 検算
        report("Python で検算中")
        py = self.sandbox.evaluate(code, kind)
        if not py.get("ok"):
            fail("Python検算", f"検算コードを実行できません: {py.get('error')}", code=code)
        var = py.get("var") or "x"
        cmp = self.sandbox.compare(kind, {"srepr": py["srepr"]}, {"text": answer}, var=var, allow_float=True)
        if not cmp.get("ok"):
            fail("Python検算", f"作問時の答え「{answer}」を式として読めません: {cmp.get('error')}", code=code)
        if not cmp["equal"]:
            fail("Python検算",
                 f"検算コードの結果 ${py['latex']}$ と作問時の答え ${cmp['given']['latex']}$ が一致しません",
                 code=code)
        if py.get("has_float"):
            if cmp["given"].get("has_float"):
                fail("Python検算", "厳密な値が得られません（小数の近似値しかありません）", code=code)
            canonical = cmp["given"]  # 近似値と数値的に一致した厳密な答えを採用
        else:
            canonical = cmp["expected"]
        steps.append(Step("Python検算", True, f"検算コードの結果 ${canonical['latex']}$ が作問時の答えと一致",
                          code=code))

        # 3. 独立解答（答えを見せずに解かせる）
        report("別の解答者が解いています")
        sol = self.llm.chat_json(
            s.solve_model, prompts.SOLVE_SYSTEM, prompts.solve_user(question, kind),
            prompts.SOLVE_SCHEMA, temperature=0.3,
        )
        sol_answer = str(sol.get("answer") or "").strip()
        sol_code = str(sol.get("code") or "").strip()
        notes: list[str] = []
        agree = False
        if sol_code:
            run = self.sandbox.evaluate(sol_code, kind)
            if run.get("ok"):
                c = self.sandbox.compare(kind, {"srepr": canonical["srepr"]}, {"srepr": run["srepr"]},
                                         var=var, allow_float=True)
                same = bool(c.get("ok") and c.get("equal"))
                agree = agree or same
                notes.append(f"Python の結果 ${run['latex']}$（{'一致' if same else '不一致'}）")
            else:
                notes.append(f"Python は実行できず（{run.get('error')}）")
        if sol_answer:
            c = self.sandbox.compare(kind, {"srepr": canonical["srepr"]}, {"text": sol_answer},
                                     var=var, allow_float=True)
            if c.get("ok"):
                same = bool(c.get("equal"))
                agree = agree or same
                notes.append(f"最終解答 ${c['given']['latex']}$（{'一致' if same else '不一致'}）")
            else:
                notes.append(f"最終解答「{sol_answer}」は読み取れず")
        summary = f"{s.solve_model}: " + (" ／ ".join(notes) or "解答なし")
        if not agree:
            fail("独立解答", summary, code=sol_code, detail=str(sol.get("work") or ""))
        steps.append(Step("独立解答", True, summary, code=sol_code, detail=str(sol.get("work") or "")))

        # 4. 審査
        if s.review_enabled:
            report("問題文を審査中")
            rev = self.llm.chat_json(
                s.review_model, prompts.REVIEW_SYSTEM,
                prompts.review_user(topic, difficulty, question, kind, canonical["latex"], explanation),
                prompts.REVIEW_SCHEMA, temperature=0.1,
            )
            issues = "\n".join(str(i) for i in (rev.get("issues") or []) if str(i).strip())
            failed = [label for key, label in prompts.REVIEW_FLAGS.items() if rev.get(key) is not True]
            if failed:
                fail("審査", "審査で指摘あり: " + "、".join(failed), detail=issues)
            summary = "問題なし" if rev.get("level_ok", True) else "問題なし（難易度が対象とずれている可能性あり）"
            steps.append(Step("審査", True, summary, detail=issues))

        return Candidate(
            topic_id=topic.id, difficulty=difficulty, subtopic=subtopic, question=question,
            answer_kind=kind, answer_srepr=canonical["srepr"], answer_latex=canonical["latex"],
            answer_var=var, explanation=explanation, models=", ".join(s.models), steps=steps,
        )
