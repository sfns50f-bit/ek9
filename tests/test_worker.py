import random
from datetime import timedelta

from sqlalchemy import select

from quizapp.db import Job, Problem, init_db, kv_set, utcnow
from quizapp.llm import LLMUnavailable
from quizapp.pipeline import Pipeline
from quizapp.worker import Worker

from .conftest import GOOD_GEN, GOOD_REVIEW, GOOD_SOLVE, ScriptedLLM


def make_worker(settings, sandbox, **script):
    sessions = init_db(settings.database_path)
    pipeline = Pipeline(ScriptedLLM(**script), sandbox, settings, rng=random.Random(0))
    return Worker(settings, sessions, pipeline), sessions


def add_job(sessions, topic_id="m1-quad", difficulty=2):
    with sessions.begin() as s:
        job = Job(topic_id=topic_id, difficulty=difficulty)
        s.add(job)
        s.flush()
        return job.id


def test_job_produces_verified_problem(settings, sandbox):
    worker, sessions = make_worker(settings, sandbox, gen=[GOOD_GEN], solve=[GOOD_SOLVE], review=[GOOD_REVIEW])
    job_id = add_job(sessions)
    assert worker.run_once()
    with sessions() as s:
        job = s.get(Job, job_id)
        assert job.status == "done"
        problem = s.get(Problem, job.problem_id)
        assert problem.status == "ready" and problem.answer_latex == r"1,\ 3"
        assert problem.served_at is None  # 画面で開いたときに出題済みになる
        assert problem.verification_log["steps"][1]["name"] == "Python検算"


def test_job_failure_keeps_rejections(settings, sandbox):
    settings.max_attempts = 2
    wrong = dict(GOOD_GEN, answer="[5]")
    worker, sessions = make_worker(settings, sandbox, gen=[wrong, wrong])
    job_id = add_job(sessions)
    worker.run_once()
    with sessions() as s:
        job = s.get(Job, job_id)
        assert job.status == "failed" and "2 回作り直しました" in job.error
        rejected = s.execute(select(Problem).where(Problem.status == "rejected")).scalars().all()
        assert len(rejected) == 2


def test_ollama_down_fails_job_with_message(settings, sandbox):
    worker, sessions = make_worker(settings, sandbox, gen=[LLMUnavailable("Ollama に接続できません")])
    job_id = add_job(sessions)
    worker.run_once()
    with sessions() as s:
        assert "Ollama" in s.get(Job, job_id).error
    assert worker.run_once() is False  # しばらく待ってから再試行する


def test_cancelled_job_is_not_overwritten(settings, sandbox):
    worker, sessions = make_worker(settings, sandbox, gen=[GOOD_GEN])
    job_id = add_job(sessions)

    job = worker.claim_job()
    with sessions.begin() as s:
        s.get(Job, job_id).status = "cancelled"
    worker.handle_job(job)
    with sessions() as s:
        assert s.get(Job, job_id).status == "cancelled"


def test_recover_resets_running_jobs(settings, sandbox):
    worker, sessions = make_worker(settings, sandbox)
    job_id = add_job(sessions)
    worker.claim_job()
    worker.recover()
    with sessions() as s:
        assert s.get(Job, job_id).status == "pending"


def test_refill_targets_recently_used_topics(settings, sandbox):
    worker, sessions = make_worker(settings, sandbox, gen=[GOOD_GEN], solve=[GOOD_SOLVE], review=[GOOD_REVIEW])
    assert worker.pick_refill_target() is None
    with sessions.begin() as s:
        s.add(Problem(topic_id="m1-quad", difficulty=1, question="q", status="ready", served_at=utcnow()))
        s.add(Problem(topic_id="m2-diff", difficulty=2, question="old", status="ready",
                      served_at=utcnow() - timedelta(days=60)))
    assert worker.pick_refill_target() == ("m1-quad", 1)
    assert worker.run_once()
    with sessions() as s:
        stock = s.execute(select(Problem).where(Problem.served_at.is_(None))).scalars().all()
        assert len(stock) == 1 and stock[0].difficulty == 1


def test_refill_can_be_paused(settings, sandbox):
    worker, sessions = make_worker(settings, sandbox)
    with sessions.begin() as s:
        s.add(Problem(topic_id="m1-quad", difficulty=1, question="q", status="ready", served_at=utcnow()))
        kv_set(s, "refill_paused", "1")
    assert worker.run_once() is False


def test_refill_stops_when_stock_is_full(settings, sandbox):
    settings.pool_target = 1
    worker, sessions = make_worker(settings, sandbox)
    with sessions.begin() as s:
        s.add(Problem(topic_id="m1-quad", difficulty=1, question="q", status="ready", served_at=utcnow()))
        s.add(Problem(topic_id="m1-quad", difficulty=1, question="stock", status="ready"))
    assert worker.pick_refill_target() is None


def test_refill_aborts_when_job_arrives(settings, sandbox):
    worker, sessions = make_worker(settings, sandbox, gen=[GOOD_GEN])
    with sessions.begin() as s:
        s.add(Problem(topic_id="m1-quad", difficulty=1, question="q", status="ready", served_at=utcnow()))
    add_job(sessions, difficulty=3)
    worker.refill(("m1-quad", 1))  # 依頼があるので作問前に中断する
    with sessions() as s:
        assert s.execute(select(Problem).where(Problem.served_at.is_(None))).first() is None


def test_worker_uses_models_chosen_on_screen(settings, sandbox):
    worker, sessions = make_worker(settings, sandbox, gen=[GOOD_GEN], solve=[GOOD_SOLVE], review=[GOOD_REVIEW])
    with sessions.begin() as s:
        kv_set(s, "model_gen", "gemma4:12b")
    job_id = add_job(sessions)
    worker.run_once()
    roles = {role: model for role, model, _ in worker.pipeline.llm.calls}
    assert roles == {"gen": "gemma4:12b", "solve": "gemma4:e4b", "review": "gemma4:e4b"}
    with sessions() as s:
        problem = s.get(Problem, s.get(Job, job_id).problem_id)
        assert problem.models == "gemma4:12b, gemma4:e4b"
    # 画面で「.env の設定」に戻すと元のモデルに戻る
    with sessions.begin() as s:
        kv_set(s, "model_gen", "")
    worker.apply_model_settings()
    assert worker.pipeline.settings.gen_model == "gemma4:e4b"
