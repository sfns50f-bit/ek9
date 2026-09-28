import random

import pytest

from quizapp.llm import LLMError, LLMUnavailable, extract_json
from quizapp.pipeline import Aborted, Pipeline, repair_latex
from quizapp.topics import get_topic

from .conftest import GOOD_GEN, GOOD_REVIEW, GOOD_SOLVE, ScriptedLLM

TOPIC = get_topic("m1-quad")


def make(settings, sandbox, **script):
    llm = ScriptedLLM(**script)
    return Pipeline(llm, sandbox, settings, rng=random.Random(0)), llm


def test_accepts_verified_problem(settings, sandbox):
    pipe, llm = make(settings, sandbox, gen=[GOOD_GEN], solve=[GOOD_SOLVE], review=[GOOD_REVIEW])
    progress = []
    cand, rejections = pipe.run(TOPIC, 2, [], progress=progress.append)
    assert rejections == []
    assert cand.answer_kind == "set"
    assert cand.answer_latex == r"1,\ 3"
    assert [s.name for s in cand.steps] == ["作問", "Python検算", "独立解答", "審査"]
    assert all(s.ok for s in cand.steps)
    assert progress[0].startswith("1/4 回目")
    # 解答役には答えを見せない
    solve_prompt = next(user for role, _, user in llm.calls if role == "solve")
    assert "[1, 3]" not in solve_prompt and "solve(" not in solve_prompt


def test_rejects_when_python_disagrees_then_retries(settings, sandbox):
    wrong = dict(GOOD_GEN, answer="[1, 2]")
    pipe, _ = make(settings, sandbox, gen=[wrong, GOOD_GEN], solve=[GOOD_SOLVE], review=[GOOD_REVIEW])
    cand, rejections = pipe.run(TOPIC, 2, [])
    assert cand is not None
    assert len(rejections) == 1
    assert "一致しません" in rejections[0].reason
    assert rejections[0].question == GOOD_GEN["question"]


def test_rejects_when_solver_disagrees(settings, sandbox):
    settings.max_attempts = 1
    bad_solve = {"work": "", "answer": "[2]", "code": "answer = [2]"}
    pipe, _ = make(settings, sandbox, gen=[GOOD_GEN], solve=[bad_solve])
    cand, rejections = pipe.run(TOPIC, 2, [])
    assert cand is None
    assert rejections[0].steps[-1].name == "独立解答"


def test_solver_text_answer_is_enough_when_code_fails(settings, sandbox):
    solve = {"work": "", "answer": "1, 3", "code": "answer = undefined_name"}
    pipe, _ = make(settings, sandbox, gen=[GOOD_GEN], solve=[solve], review=[GOOD_REVIEW])
    cand, _ = pipe.run(TOPIC, 2, [])
    assert cand is not None


def test_rejects_on_review(settings, sandbox):
    settings.max_attempts = 1
    review = dict(GOOD_REVIEW, unique=False, issues=["答えが複数ある"])
    pipe, _ = make(settings, sandbox, gen=[GOOD_GEN], solve=[GOOD_SOLVE], review=[review])
    cand, rejections = pipe.run(TOPIC, 2, [])
    assert cand is None
    assert "答えが1通りに定まらない" in rejections[0].reason


def test_review_can_be_disabled(settings, sandbox):
    settings.review_enabled = False
    pipe, llm = make(settings, sandbox, gen=[GOOD_GEN], solve=[GOOD_SOLVE])
    cand, _ = pipe.run(TOPIC, 2, [])
    assert cand is not None
    assert all(role != "review" for role, _, _ in llm.calls)


def test_float_python_result_uses_exact_claimed_answer(settings, sandbox):
    gen = dict(GOOD_GEN, question="$\\sqrt{2}$ を求めよ。", answer_kind="value", answer="sqrt(2)",
               verify_code="import math\nanswer = math.sqrt(2)")
    solve = {"work": "", "answer": "sqrt(2)", "code": "answer = sqrt(2)"}
    pipe, _ = make(settings, sandbox, gen=[gen], solve=[solve], review=[GOOD_REVIEW])
    cand, _ = pipe.run(TOPIC, 2, [])
    assert cand.answer_latex == r"\sqrt{2}"


def test_duplicate_question_is_rejected(settings, sandbox):
    settings.max_attempts = 1
    pipe, _ = make(settings, sandbox, gen=[GOOD_GEN])
    cand, rejections = pipe.run(TOPIC, 2, [GOOD_GEN["question"]])
    assert cand is None and "同じ問題" in rejections[0].reason


def test_llm_output_error_counts_as_attempt(settings, sandbox):
    pipe, _ = make(settings, sandbox, gen=[LLMError("broken"), GOOD_GEN], solve=[GOOD_SOLVE], review=[GOOD_REVIEW])
    cand, rejections = pipe.run(TOPIC, 2, [])
    assert cand is not None and len(rejections) == 1


def test_llm_unavailable_propagates(settings, sandbox):
    pipe, _ = make(settings, sandbox, gen=[LLMUnavailable("down")])
    with pytest.raises(LLMUnavailable):
        pipe.run(TOPIC, 2, [])


def test_abort(settings, sandbox):
    pipe, _ = make(settings, sandbox, gen=[GOOD_GEN])
    with pytest.raises(Aborted):
        pipe.run(TOPIC, 2, [], should_abort=lambda: True)


def test_repair_latex_only_inside_math():
    broken = "分数 $\x0crac{1}{2}$ と $\theta$ と $x \times y$。\n次の行"
    assert repair_latex(broken) == "分数 $\\frac{1}{2}$ と $\\theta$ と $x \\times y$。\n次の行"


def test_extract_json_handles_fences_and_noise():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('答えは {"a": 2} です') == {"a": 2}
    with pytest.raises(LLMError):
        extract_json("no json")
