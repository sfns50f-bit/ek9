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
    code = ("import sys; sys.path[:0]=['app']; import quizapp.web, quizapp.stock, quizapp.review;"
            " from quizapp.subjects import all_subjects; [s.units() for s in all_subjects()];"
            " print('quizapp.llm' in sys.modules, 'sympy' in sys.modules, 'anthropic' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=True)
    # sympy も読まない（小単元の一覧は templates.py を ast で読む。web のメモリと起動時間のため）。API の SDK も読まない
    assert out.stdout.strip() == "False False False"


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
    rows = {r["device"]: r for r in client.get("/status.json").json()["stock"] if r["subject"] == "math"}
    assert rows["phone"]["count"] == 3 and rows["keyboard"]["count"] == 5
    assert rows["keyboard"]["detail"] == "キーボード入力 2・選択式 3"
    assert "キーボード入力 2・選択式 3" in client.get("/").text


def test_status_shows_recent_llm_calls(client, sessions):
    from quizapp.db import kv_set

    with sessions.begin() as s:
        kv_set(s, "llm_calls", '[{"at": 1760000000, "model": "gemma4:e4b", "label": "検証", "seconds": 33.5,'
                               ' "load_s": 21.0, "prompt_tokens": 300, "tokens": 800, "tok_per_s": 64.0}]')
    r = client.get("/status/llm")
    assert "最近の LLM 呼び出し" in r.text and "33.5秒" in r.text and "21.0秒" in r.text and "64.0/秒" in r.text


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
    r = client.get("/status/quality")
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


def test_cancel_states_are_shown(client, sessions):
    with sessions.begin() as s:
        s.add(Job(subject="math", scope="", format="choice", count=3))
        s.add(Job(subject="math", scope="", format="choice", count=3, status="running"))
    pending, running = [j.id for j in all_rows(sessions, Job)]
    client.post(f"/jobs/{pending}/cancel", data={"back": "/generate"})
    client.post(f"/jobs/{running}/cancel", data={"back": "/status"})
    with sessions() as s:
        assert s.get(Job, pending).status == "cancelled"   # まだ始まっていない依頼はすぐ取り消し済み
        assert s.get(Job, running).status == "cancelling"  # 処理中の依頼は worker が止めるまで取り消し中
    for page in ("/generate", "/status"):
        text = client.get(page).text
        assert "取り消し中" in text and "取り消し済み" in text


def test_rejection_list_shows_models_question_and_raw_output(client, settings, sessions, sandbox):
    from .test_worker import AnswerStyleLLM

    w = Worker(settings, sessions, AnswerStyleLLM(sessions, "unreadable"), sandbox, rng=random.Random(1))
    w.last_backup_check = 1e18
    with sessions.begin() as s:
        s.add(Job(subject="math", scope="tmpl:m2-explog-base", format="input", count=1))
    w.run_once()
    text = client.get("/status/quality").text
    assert "検証役の答えが読み取れなかった" in text
    assert "作成: gemma4:12b-it-qat" in text and "検証: gemma4:e4b" in text
    assert "検証役の生の出力" in text and "わかりません" in text and "問題文" in text


def test_think_setting_on_status(client, sessions):
    from quizapp.db import dumps, kv_set

    with sessions.begin() as s:
        kv_set(s, "ollama", dumps({"ok": True, "models": [{"name": "qwen3.5:9b-q4_K_M"}, {"name": "gemma4:e4b"}]}))
    text = client.get("/status/models").text
    assert "思考モード" in text and "qwen3.5:9b-q4_K_M" in text
    r = client.post("/status/think", data={"model": "qwen3.5:9b-q4_K_M", "choice": "on"}, follow_redirects=True)
    assert "思考モードの設定を保存しました" in r.text
    from quizapp.runtime import think_settings
    with sessions() as s:
        assert think_settings(s) == {"qwen3.5:9b-q4_K_M": "on"}


def test_generate_with_models_units_and_levels(client, settings, sessions):
    """作成依頼でモデル・複数の単元・レベルを選べる。モデルの初期値は /status の設定。"""
    r = client.get("/generate")
    assert 'value="gemma4:12b-it-qat"' in r.text and "一覧にないモデル" in r.text and 'name="levels"' in r.text and 'value="unit:m2-int"' in r.text
    r = client.post("/generate", data={"subject": "math", "scope": ["unit:m1-quad", "tmpl:m1-quad-vertex",
                                                                     "tmpl:m2-int-area"],
                                       "levels": ["basic", "common"], "format": "choice", "count": "4",
                                       "gen_model": "qwen3.5:4b", "verify_model": "", "verify_count": "1",
                                       "verify_model_2": ""}, follow_redirects=True)
    assert "順番待ちに入れました" in r.text and "qwen3.5:4b" in r.text
    job = all_rows(sessions, Job)[-1]
    # 単元を選ぶと小単元にもチェックが付くので、その単元の小単元は省いて記録する
    assert job.scope == "unit:m1-quad,tmpl:m2-int-area" and job.levels == "basic,common"
    assert (job.gen_model, job.verify_model, job.verify_count, job.verify_model_2) == \
        ("qwen3.5:4b", "gemma4:e4b", 1, "")
    client.post("/generate", data={"subject": "math", "format": "choice", "count": "1", "gen_model": "gemma4:e4b",
                                   "gen_model_text": " anthropic/claude-x ", "verify_model_2": "-"})
    job = all_rows(sessions, Job)[-1]
    assert job.gen_model == "anthropic/claude-x" and job.verify_model_2 == ""
    bad = {"subject": "math", "scope": "unit:m1-quad", "levels": "jhs", "format": "choice", "count": "1"}
    assert client.post("/generate", data=bad).status_code == 400  # 中学レベルのテンプレートはまだない
    assert client.post("/generate", data={**bad, "levels": "nope"}).status_code == 400


def test_set_spreads_templates_and_follows_units_and_levels(client, settings, sessions, sandbox):
    stock_up(settings, sessions, sandbox, scope="tmpl:m1-quad-vertex", count=4)
    stock_up(settings, sessions, sandbox, scope="tmpl:m2-int-area", count=3)
    stock_up(settings, sessions, sandbox, scope="tmpl:ma-case-comb", count=3)
    loc = start(client, scope=["tmpl:m1-quad-vertex", "unit:m2-int"], count="20")
    with sessions() as s:
        ids = s.get(StudySession, int(loc.rsplit("/", 1)[1])).problem_ids
        temps = [s.get(Problem, i).template for i in ids]
    assert sorted(set(temps)) == ["m1-quad-vertex", "m2-int-area"] and len(temps) == 7
    assert all(a != b for a, b in zip(temps, temps[1:-1])), temps  # 同じ小単元が続かない（最後の 1 つは避けられない）
    loc = start(client, levels="common", count="20")
    with sessions() as s:
        row = s.get(StudySession, int(loc.rsplit("/", 1)[1]))
        assert row.levels == "common" and {s.get(Problem, i).template for i in row.problem_ids} == {"m2-int-area"}


def test_spread_order_never_repeats_when_possible():
    from quizapp.stock import spread_order, spread_pick

    rng = random.Random(0)
    for _ in range(200):
        items = [(i, rng.choice("abc")) for i in range(rng.randrange(1, 20))]
        order = spread_order(items, rng)
        kinds = dict(items)
        seq = [kinds[i] for i in order]
        most = max(seq.count(k) for k in set(seq))
        if most <= (len(seq) + 1) // 2:  # 並べ方があるときは必ず続かない
            assert all(a != b for a, b in zip(seq, seq[1:])), seq
    picked = spread_pick([(i, "a") for i in range(10)] + [(10, "b"), (11, "c")], 6, random.Random(1))
    assert {"b", "c"} <= {t for _i, t in picked}


def test_unseen_problem_page_needs_review_mode(client, settings, sessions, sandbox, monkeypatch):
    """まだ解いていないストックの問題は /problems/<番号> で正解ごと見えない。解いた後と問題検証モードでは見える。"""
    stock_up(settings, sessions, sandbox, count=2)
    pid = all_rows(sessions, Problem)[0].id
    assert client.get(f"/problems/{pid}").status_code == 404
    with sessions.begin() as s:
        s.add(Attempt(user_id=1, problem_id=pid, subject="math", unit="u", template="t", format="choice"))
    assert client.get(f"/problems/{pid}").status_code == 200
    other = all_rows(sessions, Problem)[1].id
    assert client.get("/review").status_code == 200  # パスワードなし（初期）なら問題検証モードに入れる
    assert client.get(f"/problems/{other}").status_code == 404
    assert client.get(f"/problems/{other}?review=1").status_code == 200  # 問題検証モードの一覧からのリンク


def test_review_mode_password_delete_and_export(settings, sessions, sandbox, monkeypatch):
    monkeypatch.setenv("REVIEW_PASSWORD", "himitsu")
    from quizapp.config import Settings

    client = TestClient(create_app(Settings(), sessions, sandbox))
    stock_up(settings, sessions, sandbox, count=3)
    r = client.get("/review", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/review/login"
    assert client.get("/review/export.md", follow_redirects=False).status_code == 303
    pid = all_rows(sessions, Problem)[0].id
    r = client.get(f"/problems/{pid}?review=1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/review/login"
    assert client.get(f"/problems/{pid}").status_code == 404
    assert "パスワードが違います" in client.post("/review/login", data={"password": "x"}, follow_redirects=True).text
    r = client.post("/review/login", data={"password": "himitsu"}, follow_redirects=True)
    assert "問題検証モード" in r.text and "正解" in r.text and "gemma4:12b-it-qat" in r.text
    assert client.get("/").status_code == 200  # ほかの画面にはパスワードをかけない
    assert client.get(f"/problems/{pid}?review=1").status_code == 200
    md = client.get("/review/export.md?subject=math&level=basic").text
    assert md.startswith("高校生向けの問題として、正確さ・分かりやすさ・難易度の適切さを評価してください")
    assert "### 正解" in md and "### 解説" in md and "### ヒント" in md and "作成役：gemma4:12b-it-qat" in md
    assert "（正解）" in md and "（誤答：" in md
    client.post(f"/review/{pid}/delete", data={"back": "/review"})
    with sessions() as s:
        assert s.get(Problem, pid).status == "deleted"
    loc = start(client, count="20")
    with sessions() as s:
        assert pid not in s.get(StudySession, int(loc.rsplit("/", 1)[1])).problem_ids
    assert f"#{pid}" in client.get("/review?status=deleted").text
    client.post(f"/review/{pid}/restore", data={"back": "/review"})
    with sessions() as s:
        assert s.get(Problem, pid).status == "ready"
    client.post("/review/logout")
    assert client.get("/review", follow_redirects=False).status_code == 303


def test_quality_page_compares_jobs_and_gen_models(client, settings, sessions, sandbox):
    stock_up(settings, sessions, sandbox, scope="tmpl:m2-explog-base", count=2)
    w = Worker(settings, sessions, OracleLLM(sessions, solve_answer="0"), sandbox, rng=random.Random(5))
    w.last_backup_check = 1e18
    with sessions.begin() as s:
        s.add(Job(subject="math", scope="tmpl:m2-explog-base", format="choice", count=1, gen_model="qwen3.5:4b",
                  verify_model="gemma4:e4b", verify_count=1))
    w.run_once()
    text = client.get("/status/quality?days=0").text
    assert "依頼ごとの比較" in text and "qwen3.5:4b" in text and "作成役のモデルごと" in text
    last = all_rows(sessions, Job)[-1].id
    text = client.get(f"/status/quality?days=0&from_job={last}").text
    assert "作成 3 問のうち合格 0 問" in text  # 前の依頼の分は外れる


def test_think_list_shows_only_installed_models(client, sessions):
    from quizapp.db import dumps, kv_set
    from quizapp.runtime import save_think_setting

    with sessions.begin() as s:
        kv_set(s, "ollama", dumps({"ok": True, "models": [{"name": "gemma4:e4b"}]}))
        save_think_setting(s, "qwen3.5:9b-q4_K_M", "on")  # Ollama から消したモデルの設定
    text = client.get("/status/models").text
    assert "gemma4:e4b" in text and "qwen3.5:9b-q4_K_M" not in text


def test_status_pages_are_split(client):
    for page in ("/status", "/status/models", "/status/quality", "/status/llm"):
        r = client.get(page)
        assert r.status_code == 200 and "問題検証モード" in r.text, page


def test_drafts_and_duplicates_do_not_show_answers(client, settings, sessions, sandbox):
    """検証待ちの下書きと、同じ数値の問題がストックにある duplicate は、正解つきで見せない。"""
    stock_up(settings, sessions, sandbox, count=1)
    with sessions.begin() as s:
        ready = s.execute(select(Problem)).scalars().first()
        common = dict(subject="math", unit=ready.unit, template=ready.template, format="choice",
                      question="q", params_key=ready.params_key)
        draft = Problem(status="draft", **common)
        dup = Problem(status="rejected", reject_code="duplicate", **common)
        bad = Problem(status="rejected", reject_code="solver_mismatch", **common)
        s.add_all([draft, dup, bad])
    for p in (draft, dup):
        assert client.get(f"/problems/{p.id}").status_code == 404
    assert client.get(f"/problems/{bad.id}").status_code == 200
    assert f"/problems/{dup.id}" not in client.get("/status/quality?days=0").text


def test_review_level_filter_includes_problems_from_before_levels(client, settings, sessions, sandbox):
    stock_up(settings, sessions, sandbox, scope="tmpl:m2-int-area", count=1)
    with sessions.begin() as s:
        for p in s.execute(select(Problem)).scalars():
            p.level = ""  # 前の版で作った問題（レベルの列が空）
    text = client.get("/review?level=common").text
    assert "放物線で囲まれた面積" in text and "共通テスト" in text
    assert "放物線で囲まれた面積" not in client.get("/review?level=basic").text
    w = Worker(settings, sessions, OracleLLM(sessions), sandbox)
    w.recover()  # worker の起動時にレベルを書き込む
    assert {p.level for p in all_rows(sessions, Problem)} == {"common"}


def test_review_tools_for_existing_problems(client, settings, sessions, sandbox):
    """前に作った問題の表記をまとめて直し、直せないものを一覧にして、番号でまとめて削除できる。"""
    stock_up(settings, sessions, sandbox, scope="tmpl:m1-trig-area", count=3)
    a, b, c = [p.id for p in all_rows(sessions, Problem)]
    with sessions.begin() as s:
        s.get(Problem, a).question = "$A = 120^\\top$、$CA = 3$ のとき面積を求めよ。"
        s.get(Problem, b).hint = "$3回$ と $\\root{2}$"
    client.post("/review/normalize")
    with sessions() as s:
        assert s.get(Problem, a).question.startswith("$A = 120^\\circ$")
    text = client.get("/review?status=issues").text
    assert f"#{b}" in text and f"#{a}" not in text and "日本語" in text
    md = client.get("/review/export.md?status=issues").text
    assert f"#{b}" in md and f"#{c}" not in md
    client.post("/review/delete-ids", data={"ids": f"{a}, {c} 9999"})
    with sessions() as s:
        assert [s.get(Problem, i).status for i in (a, b, c)] == ["deleted", "ready", "deleted"]
    client.post("/review/delete-issues")
    with sessions() as s:
        assert s.get(Problem, b).status == "deleted"


def test_explanation_review_setting(client, sessions):
    r = client.post("/status/models", data={"review": "off"}, follow_redirects=True)
    assert "見直し <strong>しない" in r.text
    client.post("/generate", data={"subject": "math", "format": "choice", "count": "1"})
    assert all_rows(sessions, Job)[-1].explanation_review is False
    client.post("/generate", data={"subject": "math", "format": "choice", "count": "1", "review_shown": "1",
                                   "review": "on"})
    assert all_rows(sessions, Job)[-1].explanation_review is True


def test_templates_have_no_nested_forms():
    """<form> の中に <form> を書くと、ブラウザは内側を無視する（テストの POST では気づけない）。"""
    from html.parser import HTMLParser

    class Depth(HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = self.max = 0

        def handle_starttag(self, tag, attrs):
            if tag == "form":
                self.depth += 1
                self.max = max(self.max, self.depth)

        def handle_endtag(self, tag):
            if tag == "form":
                self.depth -= 1

    for path in (Path(__file__).resolve().parents[1] / "app/quizapp/templates").glob("*.html"):
        parser = Depth()
        parser.feed(path.read_text())
        assert parser.max <= 1, path.name


def test_review_filters_by_date_and_id_and_export_shows_creation_date(client, settings, sessions, sandbox):
    """最近作った問題だけを書き出せる（作った日・管理番号）。書き出しに作成日時が入る。"""
    from datetime import datetime

    stock_up(settings, sessions, sandbox, count=3)
    a, b, c = [p.id for p in all_rows(sessions, Problem)]
    with sessions.begin() as s:
        s.get(Problem, a).created_at = datetime(2026, 10, 10, 3, 0)   # 日本時間 10/10 12:00
        s.get(Problem, b).created_at = datetime(2026, 10, 20, 16, 0)  # 日本時間 10/21 01:00
        s.get(Problem, c).created_at = datetime(2026, 10, 22, 3, 0)
    md = client.get("/review/export.md?date_from=2026-10-21&date_to=2026-10-21").text
    assert f"#{b}" in md and f"#{a}" not in md and f"#{c}" not in md
    assert "作成日時：2026/10/21 01:00" in md and "作成日: 2026-10-21〜2026-10-21" in md
    md = client.get(f"/review/export.md?id_from={b}").text
    assert f"#{b}）" in md and f"#{c}）" in md and f"#{a}）" not in md
    text = client.get(f"/review?id_from={b}&id_to={b}").text
    assert f"#{b}" in text and "作成 2026/10/21 01:00" in text and f"#{c}<" not in text


def test_api_section_limits_and_models(client, settings, sessions):
    from quizapp.db import dumps, kv_set
    from quizapp.runtime import api_limits, month_key

    assert "API は使えるように設定されていません" in client.get("/status/models").text
    with sessions.begin() as s:
        kv_set(s, "api", dumps({"configured": True, "models": ["anthropic/claude-haiku-5-5"]}))
        kv_set(s, "api_spend", dumps({month_key(settings): 1.25}))
    text = client.get("/status/models").text
    assert "$1.25" in text and "$5.00" in text and "anthropic/claude-haiku-5-5" in text
    client.post("/status/api", data={"monthly": "10", "per_job": "0.5"})
    with sessions() as s:
        assert api_limits(settings, s) == (10.0, 0.5)
    assert 'value="anthropic/claude-haiku-5-5"' in client.get("/generate").text  # 作る画面で選べる
    assert "今月 $1.25" in client.get("/status").text
