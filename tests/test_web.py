import random
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from quizapp.db import Attempt, Job, Problem, StudySession
from quizapp.web import create_app
from quizapp.worker import Worker
from sqlalchemy import select

from .conftest import OracleLLM, answer_text, true_answer


@pytest.fixture
def client(settings, sessions, sandbox):
    app = create_app(settings, sessions, sandbox)
    return TestClient(app)


def stock_up(settings, sessions, sandbox, **job):
    """worker で問題を作ってストックに入れる（テスト用の LLM を使う）。"""
    llm = OracleLLM(sessions)
    w = Worker(settings, sessions, llm, sandbox, rng=random.Random(3))
    w.last_backup_check = 1e18
    with sessions.begin() as s:
        s.add(Job(**{"subject": "math", "scope": "", "format": "choice", "count": 4, **job}))
    w.run_once()


def all_rows(sessions, model):
    with sessions() as s:
        return list(s.execute(select(model).order_by(model.id)).scalars())


def start(client, **form):
    data = {"subject": "math", "scope": "", "device": "phone", "count": "20", **form}
    r = client.post("/start", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"]


def current_problem_id(html):
    return int(re.search(r'name="problem_id" value="(\d+)"', html).group(1))


def test_web_never_uses_the_llm():
    """設計の原則：画面の処理では LLM を呼ばない（web は Ollama のクライアントを import しない）。"""
    import ast

    tree = ast.parse((Path(__file__).resolve().parents[1] / "app/quizapp/web.py").read_text())
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    names = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    assert "llm" not in modules and "OllamaClient" not in names and "httpx" not in names
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "OllamaClient" not in used


def test_web_process_does_not_load_the_llm_client():
    """画面の処理の中で、Ollama のクライアント（quizapp.llm）が読み込まれない。"""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    code = ("import sys; sys.path[:0]=['app']; import quizapp.web, quizapp.stock;"
            " print('quizapp.llm' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_blank_count_uses_default(client, settings, sessions, sandbox):
    stock_up(settings, sessions, sandbox, count=2)
    r = client.post("/start", data={"subject": "math", "scope": "", "device": "phone", "count": ""},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/play/")


def test_short_stock_is_announced_on_first_question(client, settings, sessions, sandbox):
    stock_up(settings, sessions, sandbox, count=2)
    r = client.get(start(client, count="20"))
    assert "20 問のところ 2 問で行います" in r.text


def test_home_without_stock_shows_low_stock(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "ストックが残り 0 問" in r.text
    loc = start(client)
    assert loc == "/?msg=empty"


def test_phone_session_answer_and_records(client, settings, sessions, sandbox):
    """完了の確認：デスクトップの電源が切れていても（LLM なしで）ストックから解ける。"""
    stock_up(settings, sessions, sandbox, count=4)
    stock_up(settings, sessions, sandbox, format="input", count=2)
    loc = start(client, count="3")
    r = client.get(loc)
    assert r.status_code == 200 and 'class="choice"' in r.text
    sid = int(loc.rsplit("/", 1)[1])
    with sessions() as s:
        ids = s.get(StudySession, sid).problem_ids
        assert len(ids) == 3
        # スマホでは選択式だけを出す
        assert {s.get(Problem, i).format for i in ids} == {"choice"}

    pid = current_problem_id(r.text)
    shown = int(re.search(r'name="shown_at" value="(\d+)"', r.text).group(1))
    # 読み込み直しても、最初に表示した時刻は変わらない
    assert int(re.search(r'name="shown_at" value="(\d+)"', client.get(loc).text).group(1)) == shown
    with sessions.begin() as s:  # 4.2 秒前に表示したことにする
        from datetime import timedelta
        row = s.get(StudySession, sid)
        row.current_shown_at = row.current_shown_at - timedelta(milliseconds=4200)
    with sessions() as s:
        choices = s.get(Problem, pid).choices
    wrong = next(i for i, c in enumerate(choices) if not c["correct"])
    r = client.post(f"/play/{sid}/answer", data={"problem_id": pid, "response": wrong, "shown_at": shown,
                                                  "hint_viewed": "1"}, follow_redirects=False)
    assert r.status_code == 303
    # 二重送信しても記録は 1 件だけ
    r2 = client.post(f"/play/{sid}/answer", data={"problem_id": pid, "response": wrong, "shown_at": shown},
                     follow_redirects=False)
    assert r2.headers["location"] == f"/play/{sid}"
    assert len(all_rows(sessions, Attempt)) == 1
    r = client.get(r.headers["location"])
    assert "不正解" in r.text and "解説" in r.text
    att = all_rows(sessions, Attempt)[-1]
    assert not att.is_correct and att.attempt_no == 1 and att.hint_viewed and not att.explanation_viewed
    assert att.chosen_index == wrong and att.chosen_mistake == choices[wrong]["mistake"]
    assert att.duration_ms is not None and 4200 <= att.duration_ms < 60000
    assert att.subject == "math" and att.unit and att.template and att.format == "choice" and att.user_id == 1
    assert att.device == "phone"

    # 次の問題は正解する
    r = client.get(f"/play/{sid}")
    pid2 = current_problem_id(r.text)
    assert pid2 != pid
    with sessions() as s:
        right = next(i for i, c in enumerate(s.get(Problem, pid2).choices) if c["correct"])
    r = client.post(f"/play/{sid}/answer", data={"problem_id": pid2, "response": right, "shown_at": 1},
                    follow_redirects=True)
    assert "正解！" in r.text
    # 3 問目は「わからない」
    r = client.get(f"/play/{sid}")
    pid3 = current_problem_id(r.text)
    client.post(f"/play/{sid}/answer", data={"problem_id": pid3, "action": "giveup", "shown_at": 1})
    att3 = all_rows(sessions, Attempt)[-1]
    assert att3.gave_up and att3.explanation_viewed and not att3.is_correct
    r = client.get(f"/play/{sid}")
    assert "おつかれさま" in r.text and "1 / 3" in r.text


def test_keyboard_input_is_graded_with_sympy(client, settings, sessions, sandbox):
    stock_up(settings, sessions, sandbox, scope="tmpl:m1-trig-cosine", format="input", count=2)
    loc = start(client, device="keyboard", count="2")
    sid = int(loc.rsplit("/", 1)[1])
    r = client.get(loc)
    # キーボード入力の欄は、自動修正・候補表示・スペルチェック・先頭の大文字化をオフにする
    for attr in ('autocomplete="off"', 'autocorrect="off"', 'autocapitalize="off"', 'spellcheck="false"'):
        assert attr in r.text
    pid = current_problem_id(r.text)
    # 読み取れない入力は記録せず、もう一度入力させる
    r = client.post(f"/play/{sid}/answer", data={"problem_id": pid, "response": "sqrt(", "shown_at": 1})
    assert r.status_code == 422 and "読み取れません" in r.text
    assert all_rows(sessions, Attempt) == []
    with sessions() as s:
        text = true_answer(s.get(Problem, pid))
    # 同じ値の別の書き方（2*sqrt(7) → 2√7 など）も正解にする
    alt = text.replace("sqrt(", "√(").replace("*", "")
    client.post(f"/play/{sid}/answer", data={"problem_id": pid, "response": alt, "shown_at": 1})
    att = all_rows(sessions, Attempt)[-1]
    assert att.is_correct and att.format == "input" and att.response == alt


def test_wrong_problems_are_mixed_in_and_retry(client, settings, sessions, sandbox):
    stock_up(settings, sessions, sandbox, count=8)
    loc = start(client, count="2")
    sid = int(loc.rsplit("/", 1)[1])
    for _ in range(2):
        r = client.get(f"/play/{sid}")
        pid = current_problem_id(r.text)
        with sessions() as s:
            wrong = next(i for i, c in enumerate(s.get(Problem, pid).choices) if not c["correct"])
        client.post(f"/play/{sid}/answer", data={"problem_id": pid, "response": wrong, "shown_at": 1})
    wrong_ids = {a.problem_id for a in all_rows(sessions, Attempt)}
    # 次のセットには、間違えた問題が（30% まで）混ざる
    loc = start(client, count="5")
    with sessions() as s:
        ids = s.get(StudySession, int(loc.rsplit("/", 1)[1])).problem_ids
    assert len(ids) == 5 and len(set(ids) & wrong_ids) == 1

    # 過去の問題から再挑戦すると、2 回目として記録される
    pid = sorted(wrong_ids)[0]
    r = client.get(f"/problems/{pid}")
    assert "再挑戦する" in r.text and "これまでの回答" in r.text
    r = client.post(f"/problems/{pid}/retry", follow_redirects=False)
    rsid = int(r.headers["location"].rsplit("/", 1)[1])
    with sessions() as s:
        right = next(i for i, c in enumerate(s.get(Problem, pid).choices) if c["correct"])
    client.post(f"/play/{rsid}/answer", data={"problem_id": pid, "response": right, "shown_at": 1})
    att = all_rows(sessions, Attempt)[-1]
    assert att.problem_id == pid and att.attempt_no == 2 and att.is_correct
    r = client.get("/history?filter=wrong")
    assert f"/problems/{pid}" not in r.text


def test_report_excludes_problem(client, settings, sessions, sandbox):
    stock_up(settings, sessions, sandbox, count=4)
    loc = start(client, count="4")
    sid = int(loc.rsplit("/", 1)[1])
    r = client.get(loc)
    pid = current_problem_id(r.text)
    r = client.post(f"/problems/{pid}/report", data={"note": "答えが2つある", "back": f"/play/{sid}"},
                    follow_redirects=True)
    assert "報告しました" in r.text
    assert current_problem_id(r.text) != pid  # 報告した問題は飛ばす
    with sessions() as s:
        p = s.get(Problem, pid)
        assert p.reported and p.report_note == "答えが2つある"
    # 以後のセットにも出ない
    loc = start(client, count="20")
    with sessions() as s:
        assert pid not in s.get(StudySession, int(loc.rsplit("/", 1)[1])).problem_ids
    assert f"/problems/{pid}" in client.get("/status").text
    client.post(f"/problems/{pid}/unreport")
    with sessions() as s:
        assert not s.get(Problem, pid).reported


def test_template_scope_session(client, settings, sessions, sandbox):
    stock_up(settings, sessions, sandbox, count=6)
    stock_up(settings, sessions, sandbox, scope="tmpl:m2-trig-double", count=3)
    loc = start(client, scope="tmpl:m2-trig-double", count="20")
    with sessions() as s:
        ids = s.get(StudySession, int(loc.rsplit("/", 1)[1])).problem_ids
        assert ids and {s.get(Problem, i).template for i in ids} == {"m2-trig-double"}
    r = client.post("/start", data={"subject": "math", "scope": "tmpl:nope", "device": "phone", "count": 5})
    assert r.status_code == 400


def test_generate_request_queues_job(client, sessions):
    r = client.get("/generate")
    assert r.status_code == 200 and "三角関数の合成" in r.text
    r = client.post("/generate", data={"subject": "math", "scope": "unit:m2-trig", "format": "input", "count": 5},
                    follow_redirects=True)
    assert "順番待ちに入れました" in r.text
    job = all_rows(sessions, Job)[-1]
    assert (job.scope, job.format, job.count, job.status) == ("unit:m2-trig", "input", 5, "pending")
    assert client.post("/generate", data={"subject": "math", "format": "essay", "count": 1}).status_code == 400


def test_status_without_ollama_and_settings(client, settings, sessions, sandbox):
    """デスクトップが止まっていても /status は表示できる。"""
    r = client.get("/status")
    assert r.status_code == 200 and "未確認" in r.text and "ストック" in r.text
    j = client.get("/status.json").json()
    assert j["sandbox_ok"] and {row["device"] for row in j["stock"]} == {"phone", "keyboard"}
    assert j["stock"][0]["low"] is True
    r = client.post("/status/models", data={"gen_model": "gemma4:26b-a4b-it-qat", "verify_model": "gemma4:12b-it-qat",
                                            "verify_count": "3", "verify_model_2": "-"}, follow_redirects=True)
    assert "保存しました" in r.text and "gemma4:12b-it-qat ×3" in r.text
    client.post("/status/test", data={"target": "math|m2-int-area"})
    job = all_rows(sessions, Job)[-1]
    assert job.is_test and job.scope == "tmpl:m2-int-area" and job.count == 1


def test_stock_shows_breakdown_by_format(client, settings, sessions, sandbox):
    """キーボードの端末では選択式も出すので、出せる数は合計になる。その内訳を表示する。"""
    stock_up(settings, sessions, sandbox, count=3)
    stock_up(settings, sessions, sandbox, format="input", count=2)
    rows = {r["device"]: r for r in client.get("/status.json").json()["stock"]}
    assert rows["phone"]["count"] == 3 and rows["keyboard"]["count"] == 5
    assert rows["keyboard"]["detail"] == "キーボード入力 2・選択式 3"
    assert "キーボード入力 2・選択式 3" in client.get("/").text


def test_rejection_stats_on_status(client, settings, sessions, sandbox):
    llm = OracleLLM(sessions, solve_answer="0")
    w = Worker(settings, sessions, llm, sandbox, rng=random.Random(5))
    w.last_backup_check = 1e18
    with sessions.begin() as s:
        s.add(Job(subject="math", scope="tmpl:m2-explog-base", format="choice", count=1))
    w.run_once()
    j = client.get("/status.json").json()["verify"]
    assert j["by_model"][0]["model"] == "gemma4:e4b" and j["by_model"][0]["rate"] == 100
    assert dict(j["by_code"])["solver_mismatch"] == settings.max_attempts
    r = client.get("/status")
    assert "検証役の答えが正解と一致しない" in r.text


def test_sandbox_down_keeps_choice_working(client, settings, sessions, sandbox):
    stock_up(settings, sessions, sandbox, count=2)
    stock_up(settings, sessions, sandbox, format="input", count=1)
    sandbox.up = False
    loc = start(client, count="2")
    sid = int(loc.rsplit("/", 1)[1])
    pid = current_problem_id(client.get(loc).text)
    r = client.post(f"/play/{sid}/answer", data={"problem_id": pid, "response": 0, "shown_at": 1},
                    follow_redirects=True)
    assert r.status_code == 200
    loc = start(client, device="keyboard", count="5")
    sid = int(loc.rsplit("/", 1)[1])
    html = client.get(loc).text
    pid = current_problem_id(html)
    with sessions() as s:
        fmt = s.get(Problem, pid).format
    if fmt == "input":
        r = client.post(f"/play/{sid}/answer", data={"problem_id": pid, "response": "1", "shown_at": 1})
        assert r.status_code == 422 and "sandbox" in r.text


def test_answer_text_helper_round_trip(sandbox):
    import sympy as sp

    s = sp.Union(sp.Interval.open(-sp.oo, 1), sp.Interval.open(3, sp.oo))
    assert answer_text(s, "interval") == "x < 1, 3 < x"
