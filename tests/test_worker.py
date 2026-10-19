import random
import threading
import time

from quizapp.db import Job, Problem, kv_json
from quizapp.runtime import save_overrides
from quizapp.worker import Worker
import pytest
from sqlalchemy import select

from .conftest import OracleLLM


def make_worker(settings, sessions, sandbox, llm):
    w = Worker(settings, sessions, llm, sandbox, rng=random.Random(7))
    w.last_backup_check = 1e18  # バックアップはこのテストでは取らない
    return w


def add_job(sessions, **kw):
    values = {"subject": "math", "scope": "", "format": "choice", "count": 3, **kw}
    with sessions.begin() as s:
        job = Job(**values)
        s.add(job)
        s.flush()
        return job.id


def problems(sessions, **filters):
    with sessions() as s:
        q = select(Problem)
        for k, v in filters.items():
            q = q.where(getattr(Problem, k) == v)
        return list(s.execute(q.order_by(Problem.id)).scalars())


def job(sessions, job_id):
    with sessions() as s:
        return s.get(Job, job_id)


def test_batch_generates_all_then_verifies(settings, sessions, sandbox):
    llm = OracleLLM(sessions)
    jid = add_job(sessions, count=3)
    assert make_worker(settings, sessions, sandbox, llm).run_once()
    roles = [r for r, _ in llm.calls]
    # 先に 3 問分の文章を全部作り、その後で検証（既定は e4b で 2 回）をまとめて行う
    assert roles == ["gen"] * 3 + ["solve"] * 6
    assert [m for r, m in llm.calls if r == "gen"] == ["gemma4:12b-it-qat"] * 3
    assert {m for r, m in llm.calls if r == "solve"} == {"gemma4:e4b"}
    ready = problems(sessions, status="ready")
    assert len(ready) == 3
    j = job(sessions, jid)
    assert j.status == "done" and j.made == 3
    for p in ready:
        assert p.format == "choice" and p.hint and p.explanation
        choices = p.choices
        assert len(choices) == 4 and sum(c["correct"] for c in choices) == 1
        assert all(c["mistake"] for c in choices if not c["correct"])
        assert len({c["text"] for c in choices}) == 4
        steps = [e["step"] for e in p.verification]
        assert steps.count("検証") == 2 and "問題文の確認" in steps


def test_scope_by_template_and_input_format(settings, sessions, sandbox):
    llm = OracleLLM(sessions)
    add_job(sessions, scope="tmpl:m2-trig-addition", format="input", count=2)
    make_worker(settings, sessions, sandbox, llm).run_once()
    ready = problems(sessions, status="ready")
    assert len(ready) == 2
    assert {p.template for p in ready} == {"m2-trig-addition"}
    assert all(p.format == "input" and not p.choices and p.input_hint for p in ready)


def test_wrong_verifier_rejects_everything(settings, sessions, sandbox):
    llm = OracleLLM(sessions, solve_answer="123456")
    jid = add_job(sessions, count=2)
    make_worker(settings, sessions, sandbox, llm).run_once()
    assert problems(sessions, status="ready") == []
    rejected = problems(sessions, status="rejected")
    assert len(rejected) == 2 * settings.max_attempts
    assert {p.reject_code for p in rejected} == {"solver_mismatch"}
    assert job(sessions, jid).status == "failed"


def test_altered_numbers_in_question_are_rejected(settings, sessions, sandbox):
    llm = OracleLLM(sessions, alter_givens=True)
    add_job(sessions, count=1)
    make_worker(settings, sessions, sandbox, llm).run_once()
    rejected = problems(sessions, status="rejected")
    assert rejected and {p.reject_code for p in rejected} == {"text_givens"}
    # 文章の確認で落ちた下書きは検証役に回さない
    assert all(r == "gen" for r, _ in llm.calls)


def test_tampered_answer_is_rejected_by_verification(settings, sessions, sandbox):
    """完了の確認：わざと答えを間違えた問題を流すと、検証で弾かれる。"""
    llm = OracleLLM(sessions)
    jid = add_job(sessions, format="input", count=1, is_test=True)
    make_worker(settings, sessions, sandbox, llm).run_once()
    j = job(sessions, jid)
    assert j.status == "done" and "期待どおり" in j.error
    assert problems(sessions, status="test") == []
    assert problems(sessions, status="ready") == []
    rejected = problems(sessions, status="rejected")
    assert len(rejected) == 1 and rejected[0].reject_code == "solver_mismatch"
    assert rejected[0].params["test"] is True
    # 検証役との突き合わせまで届いている
    assert any(e["step"] == "検証" and not e["ok"] for e in rejected[0].verification)


def test_verify_test_that_never_reaches_verification_is_not_reported_as_success(settings, sessions, sandbox):
    llm = OracleLLM(sessions, alter_givens=True)  # 文章の確認で必ず落ちる
    jid = add_job(sessions, format="input", count=1, is_test=True)
    make_worker(settings, sessions, sandbox, llm).run_once()
    j = job(sessions, jid)
    assert j.status == "failed" and "判定できませんでした" in j.error and "text_givens" in j.error
    assert len(problems(sessions, status="rejected")) == 3


def test_tampered_answer_for_every_template_is_rejected(settings, sessions, sandbox):
    from quizapp.subjects.math import MathSubject

    llm = OracleLLM(sessions)
    ids = [add_job(sessions, scope=f"tmpl:{su.id}", format="input", count=1, is_test=True)
           for u in MathSubject().units() for su in u.subunits]
    w = make_worker(settings, sessions, sandbox, llm)
    for _ in ids:
        w.run_once()
    for jid in ids:
        assert "期待どおり" in job(sessions, jid).error, job(sessions, jid).scope


def test_ollama_down_waits_and_resumes(settings, sessions, sandbox):
    llm = OracleLLM(sessions)
    jid = add_job(sessions, count=2)
    w = make_worker(settings, sessions, sandbox, llm)
    # 1 問目の文章を作った後、Ollama が落ちる
    real = llm.chat_json
    state = {"n": 0}

    def flaky(*a, **kw):
        state["n"] += 1
        if state["n"] == 2:
            from quizapp.llm import LLMUnavailable
            raise LLMUnavailable("Ollama に接続できません（テスト）")
        return real(*a, **kw)

    llm.chat_json = flaky
    assert w.run_once()  # 落ちずに戻る
    j = job(sessions, jid)
    assert j.status == "waiting" and "再試行" in j.progress
    assert len(problems(sessions, status="draft")) == 1  # 作った下書きは残る
    assert j.gen_attempts == 1
    # 待ち時間の間は何もしない
    assert not w.run_once()
    # 待ち時間が過ぎたら続きから再開する
    w.unavailable_until = 0
    w.last_ollama_check = -1e9
    assert w.run_once()
    j = job(sessions, jid)
    assert j.status == "done" and j.made == 2
    assert len(problems(sessions, status="ready")) == 2


def test_ollama_status_is_recorded_for_web(settings, sessions, sandbox):
    llm = OracleLLM(sessions)
    w = make_worker(settings, sessions, sandbox, llm)
    w.check_ollama(force=True)
    with sessions() as s:
        info = kv_json(s, "ollama")
    assert info["ok"] and info["models"][0]["name"] == "gemma4:12b-it-qat"

    def down():
        from quizapp.llm import LLMUnavailable
        raise LLMUnavailable("つながりません")

    llm.list_models = down
    add_job(sessions, count=1)
    assert not w.check_ollama(force=True)
    assert not w.run_once()  # Ollama が落ちていれば依頼に手をつけない
    with sessions() as s:
        assert kv_json(s, "ollama")["ok"] is False


def test_models_and_count_from_status_settings(settings, sessions, sandbox):
    with sessions.begin() as s:
        save_overrides(s, {"gen_model": "gemma4:e4b", "verify_model": "gemma4:12b-it-qat",
                           "verify_count": "3", "verify_model_2": "qwen2.5:7b"})
    llm = OracleLLM(sessions)
    add_job(sessions, count=1)
    make_worker(settings, sessions, sandbox, llm).run_once()
    assert llm.calls == [("gen", "gemma4:e4b")] + [("solve", "gemma4:12b-it-qat")] * 3 + [("solve", "qwen2.5:7b")]
    p = problems(sessions, status="ready")[0]
    assert "qwen2.5:7b" in p.verify_models


def test_cancelled_job_stops(settings, sessions, sandbox):
    llm = OracleLLM(sessions)
    jid = add_job(sessions, count=3)
    w = make_worker(settings, sessions, sandbox, llm)
    real = llm.chat_json

    def cancel_after_first(*a, **kw):
        out = real(*a, **kw)
        with sessions.begin() as s:
            s.get(Job, jid).status = "cancelled"
        return out

    llm.chat_json = cancel_after_first
    w.run_once()
    assert job(sessions, jid).status == "cancelled"
    assert len(llm.calls) == 1


def test_answer_in_question_is_rejected(settings, sessions, sandbox):
    llm = OracleLLM(sessions)
    real = llm.chat_json

    def leak(model, system, user, schema, **kw):
        out = real(model, system, user, schema, **kw)
        if "hint" in schema["properties"]:
            import re
            ans = re.search(r"^正解: \$(.*)\$$", user, re.M).group(1)
            out["hint"] = f"答えは ${ans}$ になる。"
        return out

    llm.chat_json = leak
    add_job(sessions, scope="tmpl:m2-trig-addition", format="input", count=1)
    make_worker(settings, sessions, sandbox, llm).run_once()
    assert {p.reject_code for p in problems(sessions, status="rejected")} == {"answer_leak"}


def test_status_is_not_rewritten_every_loop(settings, sessions, sandbox):
    w = make_worker(settings, sessions, sandbox, OracleLLM(sessions))
    w.set_status("待機中")
    with sessions() as s:
        first = kv_json(s, "worker")["at"]
    w.set_status("待機中")
    with sessions() as s:
        assert kv_json(s, "worker")["at"] == first


def test_passages_from_a_subject_are_saved_and_shared(settings, sessions, sandbox):
    """本文つきの科目（英語・公共）を、中心部分を変えずに足せることの確認。"""
    from quizapp.subjects import REGISTRY
    from quizapp.subjects.base import Draft, Subject, Subunit, Unit, VerifyResult

    class Reading(Subject):
        id, name, formats = "reading", "読解（テスト用）", ("choice",)

        def units(self):
            return [Unit("r", "読解", (Subunit("r-main", "内容一致", ("choice",)),))]

        def generate(self, ctx, template, fmt):
            passage = {"title": "T", "body": "A short text.", "source_title": "src", "source_url": "https://x"}
            choices = [{"text": "a", "correct": True, "mistake": None}, {"text": "b", "correct": False,
                                                                          "mistake": "detail"}]
            return [Draft(unit="r", template="r-main", format=fmt, question=f"Q{i}", answer={"index": 0},
                          answer_display="a", explanation="e", hint="h", choices=choices, passage=passage)
                    for i in range(2)]

        def verify(self, llm, sandbox, model, problem):
            assert problem.passage.body == "A short text."  # 検証で本文を読める
            return VerifyResult(True, "ok")

    REGISTRY["reading"] = Reading()
    try:
        add_job(sessions, subject="reading", count=2)
        make_worker(settings, sessions, sandbox, OracleLLM(sessions)).run_once()
        ready = problems(sessions, status="ready")
        assert len(ready) == 2 and ready[0].passage_id == ready[1].passage_id is not None
    finally:
        del REGISTRY["reading"]


def test_failed_wording_is_fixed_instead_of_regenerated(settings, sessions, sandbox):
    """文章の確認で落ちたら、同じ数値のまま直させる（作り直しの回数を使わない）。"""
    llm = OracleLLM(sessions)
    real = llm.chat_json
    state = {"gen": 0}

    def first_bad(model, system, user, schema, **kw):
        out = real(model, system, user, schema, **kw)
        if "hint" in schema["properties"]:
            state["gen"] += 1
            if state["gen"] == 1:  # 1 回目は式を書き写さない
                out["question"] = "三角形の問題です。答えを求めよ。"
            else:
                assert "直してほしい点" in user and "前回の出力" in user
        return out

    llm.chat_json = first_bad
    jid = add_job(sessions, scope="tmpl:m1-trig-area", format="input", count=1)
    make_worker(settings, sessions, sandbox, llm).run_once()
    assert job(sessions, jid).status == "done" and job(sessions, jid).gen_attempts == 1
    p = problems(sessions, status="ready")[0]
    assert any("直しを依頼します" in e["summary"] for e in p.verification)


def test_broken_json_is_retried_once(settings, sessions, sandbox):
    from quizapp.llm import LLMError

    llm = OracleLLM(sessions)
    real = llm.chat_json
    state = {"n": 0}

    def broken_once(model, system, user, schema, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise LLMError("モデルの出力に JSON が見つかりません")
        return real(model, system, user, schema, **kw)

    llm.chat_json = broken_once
    jid = add_job(sessions, format="input", count=1)
    make_worker(settings, sessions, sandbox, llm).run_once()
    assert job(sessions, jid).status == "done" and job(sessions, jid).gen_attempts == 1


def test_llm_call_timings_are_recorded_for_status(settings, sessions, sandbox):
    """遅いときの原因（モデルの読み込み・出力の長さ・速さ）を /status で見られるように残す。"""
    from quizapp.llm import OllamaClient

    client = OllamaClient("http://ollama.test")
    client.label = "作成"
    client._record("gemma4:12b-it-qat", 42.0, {"load_duration": 12_500_000_000, "eval_count": 900,
                                               "eval_duration": 30_000_000_000, "prompt_eval_count": 700})
    (call,) = client.pop_calls()
    assert (call["label"], call["load_s"], call["tokens"], call["tok_per_s"]) == ("作成", 12.5, 900, 30.0)

    class TimedLLM(OracleLLM):
        label = ""

        def chat_json(self, model, system, user, schema, **kw):
            out = super().chat_json(model, system, user, schema, **kw)
            self.timed.append({"at": 0, "model": model, "label": self.label, "seconds": 1.0, "load_s": 0.0,
                               "prompt_tokens": 1, "tokens": 1, "tok_per_s": 1.0})
            return out

        def pop_calls(self):
            calls, self.timed = self.timed, []
            return calls

    llm = TimedLLM(sessions)
    llm.timed = []
    add_job(sessions, count=2)
    make_worker(settings, sessions, sandbox, llm).run_once()
    with sessions() as s:
        labels = [c["label"] for c in kv_json(s, "llm_calls")]
    assert labels == ["作成"] * 2 + ["検証"] * 4


def test_no_duplicate_numbers(settings, sessions, sandbox):
    llm = OracleLLM(sessions)
    add_job(sessions, scope="tmpl:m2-explog-base", format="input", count=6)
    make_worker(settings, sessions, sandbox, llm).run_once()
    keys = [p.params_key for p in problems(sessions, status="ready")]
    assert len(keys) == len(set(keys)) == 6


class SlowLLM(OracleLLM):
    """生成に時間がかかる LLM。should_abort（worker が設定する）が真になったら LLMCancelled を投げる。"""

    should_abort = None
    slow_from = 0  # この回数目の呼び出しから遅くする（0 なら最初から）

    def chat_json(self, model, system, user, schema, **kw):
        from quizapp.llm import LLMCancelled

        if len(self.calls) >= self.slow_from:
            end = time.monotonic() + 30
            while time.monotonic() < end:
                if self.should_abort is not None and self.should_abort():
                    raise LLMCancelled("取り消されました")
                time.sleep(0.05)
        return super().chat_json(model, system, user, schema, **kw)


def _cancel_later(sessions, jid, after):
    def run():
        time.sleep(after)
        with sessions.begin() as s:
            s.get(Job, jid).status = "cancelling"  # 画面の「取り消す」と同じ（処理中の依頼）
    t = threading.Thread(target=run)
    t.start()
    return t


def test_cancel_during_generation_stops_within_seconds(settings, sessions, sandbox):
    llm = SlowLLM(sessions)
    jid = add_job(sessions, count=3)
    w = make_worker(settings, sessions, sandbox, llm)
    t = _cancel_later(sessions, jid, 0.5)
    started = time.monotonic()
    assert w.run_once()
    t.join()
    assert time.monotonic() - started < 4
    j = job(sessions, jid)
    assert j.status == "cancelled" and "取り消しました" in j.error
    assert problems(sessions, status="draft") == []


def test_cancel_keeps_problems_that_already_passed(settings, sessions, sandbox):
    with sessions.begin() as s:
        save_overrides(s, {"verify_count": "1"})
    llm = SlowLLM(sessions)
    llm.slow_from = 4  # 作成 3 回と 1 問目の検証は速く、2 問目の検証で止まる
    jid = add_job(sessions, count=3)
    w = make_worker(settings, sessions, sandbox, llm)
    t = _cancel_later(sessions, jid, 1.0)
    w.run_once()
    t.join()
    j = job(sessions, jid)
    assert j.status == "cancelled" and j.made == 1 and "1 問はストックに残しました" in j.error
    assert len(problems(sessions, status="ready")) == 1
    assert problems(sessions, status="draft") == []


def test_cancelled_pending_job_is_never_processed(settings, sessions, sandbox):
    llm = OracleLLM(sessions)
    jid = add_job(sessions, count=1)
    with sessions.begin() as s:
        s.get(Job, jid).status = "cancelled"
    assert not make_worker(settings, sessions, sandbox, llm).run_once()
    assert llm.calls == []


class AnswerStyleLLM(OracleLLM):
    """検証役が、モデルごとの癖のある書き方で答える。"""

    def __init__(self, sessions, style):
        super().__init__(sessions)
        self.style = style

    def chat_json(self, model, system, user, schema, **kw):
        out = super().chat_json(model, system, user, schema, **kw)
        if "work" in schema["properties"]:
            ans = out["answer"]
            out = {"答えは": {"work": "...", "answer": f"答えは {ans} です"},
                   "boxed": {"work": f"計算すると \\boxed{{{ans}}}", "answer": ","},
                   # 実機の gemma4:e4b：answer 欄が崩れ、work の最後は正しい
                   "garbled": {"work": f"底の変換をして\nS = {ans}", "answer": f"@{ans}"},
                   "unreadable": {"work": "わからない", "answer": "わかりません"}}[self.style]
        return out


@pytest.mark.parametrize("style", ["答えは", "boxed", "garbled"])
def test_verifier_answer_styles_are_read(settings, sessions, sandbox, style):
    add_job(sessions, scope="tmpl:m2-explog-base", format="input", count=1)
    make_worker(settings, sessions, sandbox, AnswerStyleLLM(sessions, style)).run_once()
    assert len(problems(sessions, status="ready")) == 1


def test_unreadable_answer_is_classified_and_raw_output_kept(settings, sessions, sandbox):
    add_job(sessions, scope="tmpl:m2-explog-base", format="input", count=1)
    make_worker(settings, sessions, sandbox, AnswerStyleLLM(sessions, "unreadable")).run_once()
    p = problems(sessions, status="rejected")[0]
    assert p.reject_code == "solver_unreadable"
    fail = [e for e in p.verification if e["step"] == "検証" and not e["ok"]][0]
    assert "わかりません" in fail["raw"] and fail["model"] == "gemma4:e4b"


def test_broken_verifier_output_is_retried_once(settings, sessions, sandbox):
    from quizapp.llm import LLMError

    llm = OracleLLM(sessions)
    real = llm.chat_json
    state = {"broken": 0}

    def once(model, system, user, schema, **kw):
        if "work" in schema["properties"] and state["broken"] == 0:
            state["broken"] += 1
            raise LLMError("モデルの出力に JSON が見つかりません", raw="{answer: ")
        return real(model, system, user, schema, **kw)

    llm.chat_json = once
    add_job(sessions, format="input", count=1)
    make_worker(settings, sessions, sandbox, llm).run_once()
    assert len(problems(sessions, status="ready")) == 1


def test_explanation_with_another_notation_is_accepted(sandbox):
    """解説の正解の書き方が違っても（√28 と 2√7 など）、sympy で同じ値なら合格にする。"""
    import random as _r

    import sympy as sp
    from quizapp import textcheck
    from quizapp.subjects.math import _explanation_has_value
    from quizapp.subjects.math.templates import TEMPLATES

    for seed in range(40):
        inst = TEMPLATES["m1-trig-cosine"](_r.Random(seed))
        sq = inst.answer ** 2
        if sq.is_Integer and not sp.sqrt(sq).is_Integer and sp.sqrt(sq) != sp.sqrt(sq, evaluate=False):
            break
    other = f"よって $a = \\sqrt{{{sq}}}$ となる。"
    assert textcheck.missing(other, inst.answer_checks())  # 表記だけでは見つからない
    assert _explanation_has_value(sandbox, inst, sp.srepr(inst.answer), other)
    assert not _explanation_has_value(sandbox, inst, sp.srepr(inst.answer), "よって $a = \\sqrt{2}$")


def test_think_setting_is_applied_per_model(settings, sessions, sandbox):
    from quizapp.runtime import resolve_think, save_think_setting, think_settings

    with sessions.begin() as s:
        save_think_setting(s, "gemma4:e4b", "off")
    with sessions() as s:
        choices = think_settings(s)
    assert resolve_think("qwen3.5:9b-q4_K_M", choices, None) is False   # 自動：qwen3 系は切る
    assert resolve_think("gemma4:12b-it-qat", choices, None) is None    # 自動：gemma4 は既定のまま
    assert resolve_think("gemma4:e4b", choices, None) is False          # 画面で選んだ設定
    with sessions.begin() as s:
        save_think_setting(s, "qwen3.5:9b-q4_K_M", "on")
    with sessions() as s:
        assert resolve_think("qwen3.5:9b-q4_K_M", think_settings(s), None) is True

    class ThinkLLM(OracleLLM):
        think_for = None

        def chat_json(self, model, *a, **kw):
            self.seen = getattr(self, "seen", []) + [(model, self.think_for(model))]
            return super().chat_json(model, *a, **kw)

    llm = ThinkLLM(sessions)
    add_job(sessions, count=1)
    make_worker(settings, sessions, sandbox, llm).run_once()
    assert ("gemma4:e4b", False) in llm.seen and ("gemma4:12b-it-qat", None) in llm.seen


def test_missing_model_fails_the_job_instead_of_waiting(settings, sessions, sandbox):
    from quizapp.llm import LLMModelMissing

    llm = OracleLLM(sessions)

    def missing(*a, **kw):
        raise LLMModelMissing("モデル gemma4:12b-it-qat が Ollama に見つかりません")

    llm.chat_json = missing
    jid = add_job(sessions, count=1)
    make_worker(settings, sessions, sandbox, llm).run_once()
    j = job(sessions, jid)
    assert j.status == "failed" and "見つかりません" in j.error
