import random

from quizapp.db import Job, Problem, kv_json
from quizapp.runtime import save_overrides
from quizapp.worker import Worker
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
    from quizapp.subjects.math.catalog import UNITS

    llm = OracleLLM(sessions)
    ids = [add_job(sessions, scope=f"tmpl:{su.id}", format="input", count=1, is_test=True)
           for u in UNITS for su in u.subunits]
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


def test_no_duplicate_numbers(settings, sessions, sandbox):
    llm = OracleLLM(sessions)
    add_job(sessions, scope="tmpl:m2-explog-base", format="input", count=6)
    make_worker(settings, sessions, sandbox, llm).run_once()
    keys = [p.params_key for p in problems(sessions, status="ready")]
    assert len(keys) == len(set(keys)) == 6
