import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from quizapp.db import Attempt, Job, Problem, init_db, utcnow
from quizapp.web import create_app

from .conftest import ScriptedLLM


@pytest.fixture
def env(settings, sandbox):
    sessions = init_db(settings.database_path)
    app = create_app(settings, sessions, sandbox, ScriptedLLM())
    return TestClient(app), sessions


def add_problem(sessions, served=True, **kw):
    values = dict(
        topic_id="m1-quad", subtopic="2次方程式の解", difficulty=2,
        question="$x^2 - 4x + 3 = 0$ の解をすべて求めよ。", answer_kind="set",
        answer_srepr="Tuple(Integer(1), Integer(3))", answer_latex=r"1,\ 3", explanation="因数分解する。",
        status="ready", verification=json.dumps({"steps": [{"name": "作問", "ok": True, "summary": "ok",
                                                            "code": "", "detail": ""}]}),
        served_at=utcnow() if served else None,
    )
    values.update(kw)
    with sessions.begin() as s:
        p = Problem(**values)
        s.add(p)
        s.flush()
        return p.id


def test_index(env):
    client, _ = env
    res = client.get("/")
    assert res.status_code == 200
    assert "数学I" in res.text


def test_generate_serves_stock_first(env):
    client, sessions = env
    pid = add_problem(sessions, served=False)
    res = client.post("/generate", data={"topic_id": "m1-quad", "difficulty": "2"}, follow_redirects=False)
    assert res.headers["location"] == f"/problems/{pid}"
    with sessions() as s:
        assert s.get(Problem, pid).served_at is not None


def test_generate_creates_job_without_stock(env):
    client, sessions = env
    res = client.post("/generate", data={"topic_id": "m1-quad", "difficulty": "3"}, follow_redirects=False)
    assert res.headers["location"].startswith("/jobs/")
    job_id = int(res.headers["location"].rsplit("/", 1)[1])
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "pending"
    page = client.get(f"/jobs/{job_id}")
    assert "問題を作っています" in page.text


def test_done_job_marks_problem_served(env):
    client, sessions = env
    pid = add_problem(sessions, served=False)
    with sessions.begin() as s:
        job = Job(topic_id="m1-quad", difficulty=2, status="done", problem_id=pid)
        s.add(job)
        s.flush()
        job_id = job.id
    res = client.get(f"/jobs/{job_id}", follow_redirects=False)
    assert res.headers["location"] == f"/problems/{pid}"
    with sessions() as s:
        assert s.get(Problem, pid).served_at is not None


def test_unknown_topic_is_rejected(env):
    client, _ = env
    assert client.post("/generate", data={"topic_id": "nope", "difficulty": "2"}).status_code == 400


def test_answer_correct_and_history(env):
    client, sessions = env
    pid = add_problem(sessions)
    page = client.get(f"/problems/{pid}")
    assert page.status_code == 200 and "data-kind=\"set\"" in page.text
    res = client.post(f"/problems/{pid}/answer", data={"answer": "x = 3, 1"}, follow_redirects=False)
    assert res.status_code == 303
    review = client.get(res.headers["location"])
    assert "正解！" in review.text
    with sessions() as s:
        attempt = s.execute(select(Attempt)).scalar_one()
        assert attempt.is_correct and attempt.user_input == "x = 3, 1"
    assert "復習が必要な問題はありません" in client.get("/history?filter=wrong").text


def test_answer_wrong_goes_to_review_list(env):
    client, sessions = env
    pid = add_problem(sessions)
    res = client.post(f"/problems/{pid}/answer", data={"answer": "1"})
    assert "不正解" in res.text and "正しくは 2 個" in res.text
    wrong = client.get("/history?filter=wrong").text
    assert f"/problems/{pid}" in wrong
    # 再挑戦して正解すると復習リストから外れる
    client.post(f"/problems/{pid}/answer", data={"answer": "1, 3"})
    assert f"/problems/{pid}\"" not in client.get("/history?filter=wrong").text
    history = client.get("/history").text
    assert "2 回挑戦" in history


def test_unreadable_answer_is_not_recorded(env):
    client, sessions = env
    pid = add_problem(sessions)
    res = client.post(f"/problems/{pid}/answer", data={"answer": "(("})
    assert res.status_code == 422 and "読み取れませんでした" in res.text
    with sessions() as s:
        assert s.execute(select(Attempt)).first() is None


def test_approximate_answer_feedback(env):
    client, sessions = env
    pid = add_problem(sessions, answer_kind="value", answer_srepr="Pow(Integer(2), Rational(1, 2))",
                      answer_latex=r"\sqrt{2}")
    res = client.post(f"/problems/{pid}/answer", data={"answer": "1.41421356"})
    assert "厳密な値" in res.text


def test_give_up_and_report(env):
    client, sessions = env
    pid = add_problem(sessions)
    res = client.post(f"/problems/{pid}/giveup")
    assert "答えを確認しましょう" in res.text
    client.post(f"/problems/{pid}/report", data={"note": "条件が足りない"})
    with sessions() as s:
        p = s.get(Problem, pid)
        assert p.status == "reported" and p.report_note == "条件が足りない"
    assert f"/problems/{pid}" in client.get("/history?filter=reported").text
    assert f"/problems/{pid}\"" not in client.get("/history?filter=wrong").text
    client.post(f"/problems/{pid}/unreport")
    with sessions() as s:
        assert s.get(Problem, pid).status == "ready"


def test_rejected_problem_is_hidden(env):
    client, sessions = env
    pid = add_problem(sessions, status="rejected")
    assert client.get(f"/problems/{pid}").status_code == 404


def test_preview_api(env):
    client, _ = env
    res = client.post("/api/preview", json={"text": "x^2", "kind": "value"}).json()
    assert res == {"ok": True, "latex": "x^{2}", "error": ""}
    assert client.post("/api/preview", json={"text": "", "kind": "value"}).json()["ok"] is False


def test_status_page_and_refill_toggle(env):
    client, sessions = env
    add_problem(sessions, status="rejected", reject_reason="一致しません")
    page = client.get("/status")
    assert page.status_code == 200 and "一致しません" in page.text and "接続OK" in page.text
    client.post("/status/refill", data={"paused": "1"})
    assert "再開する" in client.get("/status").text
