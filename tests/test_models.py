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
    # 設定のない役割（本文なしの確認）は、この依頼で選んだ作成役と同じ
    assert j.extra_models == {"passage": "anthropic/claude-haiku-5-5", "blind": "qwen3.5:9b"}
    # 打ち込んだ名前が優先
    client.post("/generate", data={"subject": "english", "format": "choice", "count": "2",
                                   "m_english_role_passage": "gemma4:12b-it-qat",
                                   "m_english_role_passage_text": "anthropic/claude-sonnet-5-5"})
    assert jobs(sessions)[-1].extra_models["passage"] == "anthropic/claude-sonnet-5-5"
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
                    "blind": {"gemma4:12b-it-qat"}, "solve": {"gemma4:e4b"}, "review": {"review-model"},
                    "judge": {"review-model"}}
    j = jobs(sessions)[-1]
    assert j.extra_models["passage"] == "passage-model" and j.review_model == "review-model"
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


def test_think_mode_is_recorded_and_exported(settings, sessions, sandbox, client):
    """処理したときのモデルごとの思考モードを依頼に残し、問題検証モードと書き出しに出す（前の版の問題は「記録なし」）。"""
    from quizapp.db import Problem

    with sessions.begin() as s:
        runtime.save_think_setting(s, "gemma4:12b-it-qat", "off")
        runtime.save_subject_settings(s, "english", {"passage": "anthropic/claude-haiku-5-5"})
    add_job(sessions, subject="english", scope="tmpl:en-notice", count=2)
    make_worker(settings, sessions, sandbox, EnglishOracle(sessions)).run_once()
    j = jobs(sessions)[-1]
    assert j.think == {"anthropic/claude-haiku-5-5": "api", "gemma4:12b-it-qat": "off", "gemma4:e4b": "off"}
    with sessions() as s:
        p = s.scalars(select(Problem).where(Problem.status == "ready")).first()
        assert p.passage.think == "api"
    md = client.get("/review/export.md?subject=english").text
    assert "- 思考モード：本文作成 API／作成 なし／検証 なし／見直し なし" in md
    assert "思考モード: 本文作成 API／作成 なし" in client.get("/review?subject=english").text
    # 記録のない前の版の問題
    with sessions.begin() as s:
        s.get(Job, j.id).think_json = "{}"
    assert "- 思考モード：記録なし" in client.get("/review/export.md?subject=english").text


def test_think_mode_is_fixed_when_the_job_is_requested(settings, sessions, sandbox, client):
    """思考モードは依頼したときの設定で最後まで生成する（後で設定を変えても、その依頼は変わらない）。"""
    with sessions.begin() as s:
        runtime.save_think_setting(s, "gemma4:12b-it-qat", "off")
    client.post("/generate", data={"subject": "math", "format": "choice", "count": "1",
                                   "m_math_gen_model": "gemma4:12b-it-qat", "m_math_verify_model": "gemma4:e4b"})
    j = jobs(sessions)[-1]
    assert j.think == {"gemma4:12b-it-qat": "off", "gemma4:e4b": "off"}
    with sessions.begin() as s:  # 依頼した後で設定を変える
        runtime.save_think_setting(s, "gemma4:12b-it-qat", "on")
    llm = OracleLLM(sessions)
    llm.think_for = None
    make_worker(settings, sessions, sandbox, llm).run_once()
    assert llm.think_for("gemma4:12b-it-qat") is False and llm.think_for("gemma4:e4b") is False
    assert jobs(sessions)[-1].think == {"gemma4:12b-it-qat": "off", "gemma4:e4b": "off"}
    # 記録のない前の版の依頼は、取りかかった時点の設定を記録して使う
    old = add_job(sessions, count=1)
    make_worker(settings, sessions, sandbox, llm).run_once()
    assert job(sessions, old).think["gemma4:12b-it-qat"] == "on" and llm.think_for("gemma4:12b-it-qat") is True


def test_think_mode_is_only_on_or_off(client, sessions):
    """思考モードは「あり」「なし」の 2 つだけ（「自動」「モデルの既定」は選べない）。前の版の記録は分かるように出す。"""
    from quizapp.db import dumps, kv_set

    assert runtime.THINK_CHOICES == {"off": "なし", "on": "あり"}
    with sessions.begin() as s:
        kv_set(s, "ollama", dumps({"ok": True, "models": [{"name": "gemma4:12b-it-qat"}]}))
        kv_set(s, "think_settings", dumps({"qwen3.5:9b": "auto"}))  # 前の版の値は読まない
        assert runtime.think_settings(s) == {}
    page = client.get("/status/models").text.split('id="think"')[1].split("</section>")[0]
    assert "（未設定＝なし）" in page and "自動" not in page and "既定" not in page
    client.post("/status/think", data={"model": "gemma4:12b-it-qat", "choice": "auto"})
    with sessions() as s:
        assert runtime.think_settings(s) == {}
    assert runtime.THINK_RECORD["default"] == "モデルの既定（前の版）"


def test_rereview_job_records_results_without_changing_status(settings, sessions, sandbox, client):
    """見直しだけをやり直す依頼：選んだモデルで見直し、結果を記録に足すだけ（合否は変えない）。書き出しで比べられる。"""
    from quizapp.db import Problem

    add_job(sessions, subject="english", scope="tmpl:en-expository", count=3)
    make_worker(settings, sessions, sandbox, EnglishOracle(sessions)).run_once()
    ready = problems(sessions, status="ready")
    assert len(ready) == 3
    r = client.post("/review/rereview", data={"subject": "english", "status": "ready", "model": "",
                                              "model_text": "anthropic/claude-haiku-5-5", "limit": "2"},
                    follow_redirects=True)
    assert "2 問の見直しのやり直しを順番待ちに入れました" in r.text
    j = jobs(sessions)[-1]
    assert j.kind == "review" and j.count == 2 and j.review_model in ("", "anthropic/claude-haiku-5-5")
    assert j.target_ids == [p.id for p in ready][::-1][:2]  # 新しい順
    assert "見直しのやり直し・2問" in client.get("/generate").text
    # 「正解が複数」と判定する見直し役で見直す
    llm = EnglishOracle(sessions, judge="multiple")
    w = make_worker(settings, sessions, sandbox, llm)
    w.run_once()
    j = job(sessions, j.id)
    assert j.status == "done" and j.made == 2 and "誤りの指摘 2 問" in j.error
    assert {m for r, m in llm.calls} == {"anthropic/claude-haiku-5-5"}
    with sessions() as s:
        rows = [s.get(Problem, pid) for pid in j.target_ids]
        assert all(p.status == "ready" for p in rows)  # 合否は変えない
        entries = [e for p in rows for e in p.verification if e["step"] == "見直しのやり直し"]
    assert len(entries) == 2 and all(not e["ok"] and e["job"] == j.id for e in entries)
    # 続きから処理しても、同じ依頼で 2 回見直さない
    w._rereview(j.id, "anthropic/claude-haiku-5-5")
    with sessions() as s:
        assert sum(1 for pid in j.target_ids for e in s.get(Problem, pid).verification
                   if e["step"] == "見直しのやり直し") == 2
    # 「見直しのやり直しで誤りを指摘された問題」で絞れる・一覧と書き出しに出る
    page = client.get("/review?subject=english&status=reflagged").text
    assert page.count("見直しのやり直し（anthropic/claude-haiku-5-5）: 誤りあり") == 2
    md = client.get("/review/export.md?subject=english&status=reflagged").text
    assert "### 検証と見直しの記録" in md and "× 見直しのやり直し（anthropic/claude-haiku-5-5）" in md
    assert "× 正解が 1 つか" in md and "○ 検証（gemma4:e4b）" in md


def test_rereview_works_for_math_and_export_shows_rejections(settings, sessions, sandbox, client):
    """見直しのやり直しは数学でも使える（科目の review をそのまま使う）。不合格の問題は理由も書き出す。"""
    add_job(sessions, count=2)
    make_worker(settings, sessions, sandbox, OracleLLM(sessions)).run_once()
    client.post("/review/rereview", data={"subject": "math", "status": "ready", "model": "gemma4:12b-it-qat"})
    llm = OracleLLM(sessions)
    make_worker(settings, sessions, sandbox, llm).run_once()
    j = jobs(sessions)[-1]
    assert j.kind == "review" and j.status == "done" and j.made == 2 and llm.reviews == ["gemma4:12b-it-qat"] * 2
    md = client.get("/review/export.md?subject=math").text
    assert "○ 見直しのやり直し（gemma4:12b-it-qat）" in md
    # 不合格の問題
    add_job(sessions, count=1)
    make_worker(settings, sessions, sandbox, OracleLLM(sessions, solve_answer="0")).run_once()
    md = client.get("/review/export.md?subject=math&status=rejected").text
    assert "- **不合格**：検証役の答えは読めたが、数値が正解と違う" in md and "× 検証（gemma4:e4b）" in md
    # 絞り込みに当てはまる問題がないとき
    r = client.post("/review/rereview", data={"subject": "english", "status": "ready", "model": "x"},
                    follow_redirects=True)
    assert "見直しのやり直しは頼みませんでした" in r.text


def test_quality_and_rereview_exports(settings, sessions, sandbox, client):
    """「検証の成績」の Markdown（E-1）と、見直しのやり直しの比較表（E-2）。不合格の問題も同じ形で書き出す（E-3）。"""
    add_job(sessions, subject="english", scope="tmpl:en-expository", count=3)
    make_worker(settings, sessions, sandbox, EnglishOracle(sessions, blind_correct={2})).run_once()
    md = client.get("/status/quality/export.md?days=0").text
    assert md.startswith("# 検証の成績") and "## 作成役のモデルごと" in md and "## 依頼ごとの比較" in md
    assert "## 不合格の一覧" in md and "本文を読まなくても解けた" in md
    assert "attachment" in client.get("/status/quality/export.md?days=0&download=1").headers["content-disposition"]
    # 見直しのやり直しを 2 つのモデルで
    for model, judge in (("model-a", "right"), ("model-b", "multiple")):
        client.post("/review/rereview", data={"subject": "english", "status": "ready", "model": "",
                                              "model_text": model, "limit": "5"})
        make_worker(settings, sessions, sandbox, EnglishOracle(sessions, judge=judge)).run_once()
    table = client.get("/review/rereview.md?subject=english").text
    head = next(ln for ln in table.splitlines() if ln.startswith("| 問題 |"))
    assert "model-a" in head and "model-b" in head
    rows = [ln for ln in table.splitlines() if ln.startswith("| #")]
    assert rows and all(ln.rstrip(" |").endswith("×") and "○" in ln for ln in rows)
    # 不合格の問題（本文なしで解けた設問）にも選択肢が残り、同じ形で書き出せる
    rejected_md = client.get("/review/export.md?subject=english&status=rejected").text
    assert "### 選択肢" in rejected_md and "### 検証と見直しの記録" in rejected_md
