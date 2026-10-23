"""科目ごと・役割ごとのモデルの設定（docs/reviews/03_english.md の 4 章）のテスト。"""

import random

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from quizapp import runtime
from quizapp.db import Job
from quizapp.subjects import get_subject
from quizapp.web import create_app
from quizapp.worker import Worker

from .conftest import OracleLLM
from .test_english import EnglishOracle
from .test_worker import PricedApi, add_job, job, make_worker, problems


@pytest.fixture
def client(settings, sessions, sandbox):
    return TestClient(create_app(settings, sessions, sandbox))


def jobs(sessions):
    with sessions() as s:
        return list(s.execute(select(Job).order_by(Job.id)).scalars())


def test_subject_settings_override_the_default(settings, sessions):
    english, math = get_subject("english"), get_subject("math")
    with sessions.begin() as s:
        runtime.save_overrides(s, {"gen_model": "qwen3.5:9b", "verify_model": "gemma4:e4b"})
        runtime.save_subject_settings(s, "english", {"passage": "anthropic/claude-haiku-5-5", "review": "gemma4:12b",
                                                     "gen": ""})
    with sessions() as s:
        m, e = runtime.model_plan(settings, s, math), runtime.model_plan(settings, s, english)
        default = runtime.model_plan(settings, s)
    # 数学は全体の設定のまま。英語は本文作成と見直しだけ上書き（空の欄は全体の設定）
    assert m == default and m.gen_model == "qwen3.5:9b" and m.reviewer == "gemma4:e4b"
    assert e.gen_model == "qwen3.5:9b" and e.verify_model == "gemma4:e4b" and e.reviewer == "gemma4:12b"
    assert e.model_for("passage") == "anthropic/claude-haiku-5-5"
    assert m.model_for("passage") == "qwen3.5:9b"  # 科目だけの役割の設定がなければ作成役のモデル
    # 既定値に戻す
    with sessions.begin() as s:
        runtime.save_subject_settings(s, "english", {})
        assert runtime.subject_settings(s) == {}
        assert runtime.model_plan(settings, s, english) == default


def test_previous_settings_become_the_default(settings, sessions):
    """前の版の設定（全体で 1 つ）は、そのまま全体の既定値になる（移す作業は要らない）。"""
    from quizapp.db import kv_set

    with sessions.begin() as s:
        kv_set(s, "setting_gen_model", "qwen2.5:7b")
    with sessions() as s:
        assert runtime.model_plan(settings, s, get_subject("english")).gen_model == "qwen2.5:7b"


def test_status_page_saves_subject_models_and_warns_same_model(client, sessions):
    r = client.post("/status/subject-models", data={"subject": "english", "passage": "anthropic/claude-haiku-5-5",
                                                    "gen": "gemma4:e4b", "verify": "", "review": "",
                                                    "api_daily_usd": "0.5", "api_daily_calls": ""},
                    follow_redirects=True)
    assert r.status_code == 200 and "科目ごとの設定を保存しました" in r.text
    with sessions() as s:
        saved = runtime.subject_settings(s)["english"]
    assert saved == {"passage": "anthropic/claude-haiku-5-5", "gen": "gemma4:e4b", "api_daily_usd": "0.5"}
    # 英語は作成役も検証役も gemma4:e4b になったので注意が出る（数学は出ない）
    page = client.get("/status/models").text
    english = page.split('id="subject-english"')[1].split("</form>")[0]
    math = page.split('id="subject-math"')[1].split("</form>")[0]
    assert "本文作成役" in english and 'class="notice warn same-model" >' in english
    assert 'class="notice warn same-model" hidden' in math
    # 作る画面の英語の初期値
    gen = client.get("/generate").text
    box = gen.split('data-subject="english"', 1)[1].split("</details>\n  {% endfor %}")[0]
    assert 'name="m_english_role_passage"' in box and "anthropic/claude-haiku-5-5" in box
    # 既定値に戻す
    client.post("/status/subject-models", data={"subject": "english", "action": "reset"})
    with sessions() as s:
        assert "english" not in runtime.subject_settings(s)


def test_generate_records_role_models_per_subject(client, sessions):
    with sessions.begin() as s:
        runtime.save_subject_settings(s, "english", {"passage": "anthropic/claude-haiku-5-5"})
    # 英語：送らなかった役割は英語の設定（本文作成は Haiku）、選んだ欄はその値
    client.post("/generate", data={"subject": "english", "format": "choice", "count": "2",
                                   "m_english_gen_model": "qwen3.5:9b", "m_english_review_model": "gemma4:12b",
                                   "m_math_gen_model": "ignored"})
    j = jobs(sessions)[-1]
    assert j.subject == "english" and j.gen_model == "qwen3.5:9b" and j.review_model == "gemma4:12b"
    assert j.extra_models == {"passage": "anthropic/claude-haiku-5-5"}
    # 打ち込んだ名前が優先
    client.post("/generate", data={"subject": "english", "format": "choice", "count": "2",
                                   "m_english_role_passage": "gemma4:12b-it-qat",
                                   "m_english_role_passage_text": "anthropic/claude-sonnet-5-5"})
    assert jobs(sessions)[-1].extra_models == {"passage": "anthropic/claude-sonnet-5-5"}
    # 数学には科目だけの役割がない
    client.post("/generate", data={"subject": "math", "format": "choice", "count": "1"})
    assert jobs(sessions)[-1].extra_models == {} and jobs(sessions)[-1].review_model == ""


def test_role_models_are_used_for_each_step(settings, sessions, sandbox):
    """本文作成・設問の作成・本文なしの確認・検証・見直しに、それぞれの役割のモデルを使う。"""
    with sessions.begin() as s:
        runtime.save_subject_settings(s, "english", {"passage": "passage-model", "review": "review-model"})
    llm = EnglishOracle(sessions)
    add_job(sessions, subject="english", scope="tmpl:en-notice", count=2)
    make_worker(settings, sessions, sandbox, llm).run_once()
    used = {}
    for role, model in llm.calls:
        used.setdefault(role, set()).add(model)
    assert used == {"passage": {"passage-model"}, "passage_review": {"review-model"}, "gen": {"gemma4:12b-it-qat"},
                    "blind": {"gemma4:12b-it-qat"}, "solve": {"gemma4:e4b"}, "review": {"review-model"}}
    j = jobs(sessions)[-1]
    assert j.extra_models == {"passage": "passage-model"} and j.review_model == "review-model"
    ready = problems(sessions, status="ready")
    with sessions() as s:
        from quizapp.db import Problem

        p = s.get(Problem, ready[0].id)
        assert p.passage.gen_model == "passage-model" and p.passage.status == "ok" and p.passage.topic
        assert [e["step"] for e in p.passage.log][:3] == ["題材", "本文の点検", "本文の見直し"]
    assert ready[0].params["passage_model"] == "passage-model"


def test_queue_keeps_the_same_subject_together(settings, sessions, sandbox):
    """科目を交互に作るとモデルの入れ替えが増えるので、直前と同じ科目の依頼を先にする。"""
    m1 = add_job(sessions, count=1)
    e1 = add_job(sessions, subject="english", scope="tmpl:en-vocab-blank", count=1)
    m2 = add_job(sessions, count=1)
    w = make_worker(settings, sessions, sandbox, OracleLLM(sessions))
    assert w.next_job_id() == m1
    w.last_subject = "math"
    with sessions.begin() as s:
        s.get(Job, m1).status = "done"
    assert w.next_job_id() == m2  # 英語の依頼の方が古いが、数学を続ける
    w.last_subject = "english"
    assert w.next_job_id() == e1


def test_subject_daily_api_limit(settings, sessions, sandbox):
    """API のモデルを使う役割には、科目ごとの 1 日の上限（金額・回数）があり、利用を科目ごと・日ごとに残す。"""
    from quizapp.llm import LLMRouter

    api = PricedApi(sessions)
    w = Worker(settings, sessions, LLMRouter(OracleLLM(sessions), {"anthropic": api}), sandbox,
               rng=random.Random(1), lane="api")
    haiku = {"gen_model": "anthropic/claude-haiku-5-5", "verify_model": "anthropic/claude-haiku-5-5",
             "verify_count": 1}
    first = add_job(sessions, count=1, **haiku)
    w.run_once()
    assert job(sessions, first).status == "done"
    with sessions() as s:
        today = runtime.api_daily(s)[runtime.day_key(settings)]
    assert today["math"]["usd"] == pytest.approx(0.03) and today["math"]["calls"] == 3
    # 回数の上限
    with sessions.begin() as s:
        runtime.save_subject_settings(s, "math", {"api_daily_calls": "4"})
    capped = add_job(sessions, count=1, **haiku)
    w.run_once()
    assert job(sessions, capped).status == "failed" and "数学の今日の API の呼び出しが上限" in job(sessions, capped).error
    # 金額の上限（ほかの科目には効かない）
    with sessions.begin() as s:
        runtime.save_subject_settings(s, "math", {"api_daily_usd": "0.03"})
    over = add_job(sessions, count=1, **haiku)
    w.run_once()
    assert job(sessions, over).status == "failed" and "数学の今日の API の利用額が上限" in job(sessions, over).error
    with sessions() as s:
        assert runtime.daily_limits(s, "english") == (0.0, 0)


def test_review_model_defaults_to_verifier_and_plan_needs_ollama():
    plan = runtime.ModelPlan("a", "b", 2, "", review_model="")
    assert plan.reviewer == "b" and plan.model_for("passage") == "a" and plan.same_gen_verify is False
    api = runtime.ModelPlan("anthropic/x", "anthropic/x", 1, "", extra=(("passage", "anthropic/y"),))
    assert not api.needs_ollama and api.same_gen_verify
    local_passage = runtime.ModelPlan("anthropic/x", "anthropic/x", 1, "", extra=(("passage", "gemma4:12b"),))
    assert local_passage.needs_ollama  # 本文作成役がローカルなら Ollama の順番待ち
    local_review = runtime.ModelPlan("anthropic/x", "anthropic/x", 1, "", review_model="gemma4:e4b")
    assert local_review.needs_ollama
    assert not runtime.ModelPlan("anthropic/x", "anthropic/x", 1, "", review=False,
                                 review_model="gemma4:e4b").needs_ollama
