"""科目ごと・役割ごとのモデルの設定（docs/reviews/03_english.md の 4 章）のテスト。"""

import dataclasses
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


@pytest.mark.english_defaults
def test_subject_settings_override_the_default(settings, sessions):
    english, math = get_subject("english"), get_subject("math")
    with sessions.begin() as s:
        runtime.save_overrides(s, {"gen_model": "qwen3.5:9b", "verify_model": "gemma4:e4b"})
        runtime.save_subject_settings(s, "english", {"passage": "anthropic/claude-haiku-5-5", "review": "gemma4:12b",
                                                     "gen": ""})
    with sessions() as s:
        m, e = runtime.model_plan(settings, s, math), runtime.model_plan(settings, s, english)
        default = runtime.model_plan(settings, s)
    # 数学は全体の設定のまま。英語は本文作成と見直しを上書き、空の欄は科目の既定（作成 26b・検証 e4b。第 8 回の 1 章）
    assert m == default and m.gen_model == "qwen3.5:9b" and m.reviewer == "gemma4:e4b"
    assert e.gen_model == "gemma4:26b-a4b-it-qat" and e.verify_model == "gemma4:e4b-it-qat" and e.reviewer == "gemma4:12b"
    assert e.model_for("passage") == "anthropic/claude-haiku-5-5" and e.model_for("blind") == "gemma4:26b-a4b-it-qat"
    assert m.model_for("passage") == "qwen3.5:9b"  # 科目だけの役割の設定がなければ作成役のモデル
    # 既定値に戻す
    with sessions.begin() as s:
        runtime.save_subject_settings(s, "english", {})
        assert runtime.subject_settings(s) == {}
        # 英語は科目の既定（作成 26b・検証 e4b・見直し 12b。第 8 回のレビュー 1 章）
        assert runtime.model_plan(settings, s, english) == dataclasses.replace(
            default, gen_model="gemma4:26b-a4b-it-qat", verify_model="gemma4:e4b-it-qat", review_model="gemma4:12b-it-qat")


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


def test_presets_local_api_and_manual(client, sessions):
    """作り方：ローカル生成・API 生成は状態の科目ごとの設定のまま、手動のときだけモデルの欄を読む。"""
    def english_box():
        page = client.get("/generate").text
        return page.split('<div class="models" data-subject="english">', 1)[1].split('<div class="models"', 1)[0]

    with sessions.begin() as s:
        runtime.save_subject_settings(s, "english", {"gen": "gemma4:12b", "verify": "gemma4:e4b"})
    # API 生成の設定がなければ、作る画面に出さず、送っても受け付けない
    box = english_box()
    assert 'value="local" checked' in box and 'value="api"' not in box and 'value="manual"' in box
    assert client.post("/generate", data={"subject": "english", "format": "choice", "count": "1",
                                          "m_english_mode": "api"}).status_code == 400
    # 状態の画面で API 生成の作成役・本文作成役を選ぶ（検証役は選ばない＝ローカル生成と同じ）
    client.post("/status/subject-models", data={"subject": "english", "gen": "gemma4:12b", "verify": "gemma4:e4b",
                                                "api:gen": "anthropic/claude-haiku-5-5",
                                                "api:passage": "anthropic/claude-sonnet-5-5"})
    page = client.get("/status/models").text
    assert 'name="api:gen"' in page and "ローカル生成と同じ（gemma4:e4b）" in page
    box = english_box()
    assert 'value="api"' in box and "作成 anthropic/claude-haiku-5-5" in box
    # ローカル生成：モデルの欄を送っても使わない
    client.post("/generate", data={"subject": "english", "format": "choice", "count": "1",
                                   "m_english_mode": "local", "m_english_gen_model": "qwen3.5:9b"})
    j = jobs(sessions)[-1]
    assert (j.gen_model, j.verify_model) == ("gemma4:12b", "gemma4:e4b") and "passage" not in j.extra_models
    # API 生成：選んだ役割は API、選ばなかった検証役はローカル生成と同じ
    client.post("/generate", data={"subject": "english", "format": "choice", "count": "1",
                                   "m_english_mode": "api", "m_english_gen_model": "qwen3.5:9b"})
    j = jobs(sessions)[-1]
    assert (j.gen_model, j.verify_model) == ("anthropic/claude-haiku-5-5", "gemma4:e4b")
    assert j.extra_models == {"passage": "anthropic/claude-sonnet-5-5"}
    # 手動：モデルの欄で選んだもの
    client.post("/generate", data={"subject": "english", "format": "choice", "count": "1",
                                   "m_english_mode": "manual", "m_english_gen_model": "qwen3.5:9b"})
    assert jobs(sessions)[-1].gen_model == "qwen3.5:9b"
    # 数学は API 生成の設定がないので出ない
    page = client.get("/generate").text
    math = page.split('<div class="models" data-subject="math">', 1)[1]
    assert 'name="m_math_mode" value="api"' not in math


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
                    "blind": {"gemma4:12b-it-qat"}, "review": {"review-model"},
                    "judge": {"gemma4:e4b"}}  # 選択肢ごとの判定は検証役（第 6 回のレビュー 2 章）
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


# --- 見本集での測定（第 6 回のレビューの 5 章） ----------------------------------------

import json as _json
import re as _re

from quizapp import golden
from quizapp.db import Problem


def test_the_golden_set_file_is_read_whole():
    """docs/reviews/golden_set_english.json の問題は、すべて小単元に当てはまり、通常の問題と同じ形で点検できる。"""
    data = golden.load("english")
    samples = [golden.sample_from_item("english", it) for it in data["items"]]
    assert len(samples) == len(data["items"]) >= 37 and all(samples)
    assert {x.expected for x in samples} == {"reject", "pass", "borderline"}
    by_id = {x.id: x.expected for x in samples}
    assert by_id[1006] == "reject" and by_id[993] == by_id[586] == "borderline"  # 第 7 回のレビュー G
    sub = get_subject("english")
    for x in samples:
        assert x.template in sub.subunit_map()
        assert (x.format == "choice") == bool(x.choices)
        if x.choices:
            assert sum(c["correct"] for c in x.choices) == 1
            assert all(c["mistake"] in sub.mistakes for c in x.choices if not c["correct"])
        sub.text_issues(x)  # 点検がそのまま通る（属性が足りている）
    spelling = next(x for x in samples if x.template == "en-spelling")
    assert spelling.answer["word"] and spelling.passage is None
    assert any(x.passage is not None and x.passage.body for x in samples)


class GoldenLLM:
    """見本集の測定のテスト用の LLM。検証の判定は、正解と also_fit の選択肢を「裏付けあり・通る」にする
    （also_fit に入れた誤答があれば、その問題は「正解が複数」で弾かれる）。引用は本文の最初の文。
    スペルは正解の語だけを挙げる。見直しは「問題なし」。"""

    def __init__(self, samples, also_fit=()):
        self.right = {c["text"] for x in samples for c in x.choices if c["correct"]}
        self.words = {_re.search(r"意味: .*", x.question).group(): x.answer.get("word", "")
                      for x in samples if not x.choices}
        self.also_fit = set(also_fit)
        self.calls = []
        self.label = ""

    def _fits(self, text, good):
        return text in good or any(_re.search(rf"\b{_re.escape(w)}\b", text) for w in good if w.isascii())

    def chat_json(self, model, system, user, schema, *, temperature=0.7):
        props = schema["properties"]
        self.calls.append(model)
        if "judgments" in props:
            items = _re.findall(r"^(\d+): (.*)$", user, _re.M)
            good = self.right | self.also_fit
            passage = user.split("本文:\n", 1)[1].split("\n\n設問:", 1)[0] if "本文:\n" in user else ""
            first = _re.split(r"(?<=[.!?])\s+", passage.split("\n\n")[-1].strip())[0] if passage else ""
            lines = []
            for n, text in items:
                fit = self._fits(text, good)
                if "本文と照らして" in system:
                    lines.append(f"{n}: 裏付けあり — 引用: \"{first}\" — 本文" if fit
                                 else f"{n}: 本文に書かれていない — ない内容: zzqx — なし")
                elif "自然に入る" in system:
                    lines.append(f"{n}: {'自然に入る' if fit else '入らない'} — 理由")
                else:
                    lines.append(f"{n}: {'通る' if fit else '通らない'} — 理由")
            return {"judgments": "\n".join(lines)}
        if "words" in props:
            meaning = _re.search(r"意味: .*", user)
            return {"work": "考えた", "words": self.words.get(meaning.group() if meaning else "", "")}
        if "verdict" in props:
            if "筋が通っているかだけ" in system:
                return {"verdict": "自然", "reason": ""}
            return {"verdict": "問題なし", "issues": "なし"}
        if "reason" in props and "成り立つのはどちらか" in system:  # 1 対 1 の判定（正解の方を選ぶ。also_fit なら両方）
            items = dict(_re.findall(r"^([12]): (.*)$", user, _re.M))
            if any(t in self.also_fit for t in items.values()):
                return {"answer": "両方", "reason": "どちらも成り立つ"}
            return {"answer": next(n for n, t in items.items() if t in self.right), "reason": "正解の方"}
        if "reason" in props:  # 正解を抜いた判定
            items = _re.findall(r"^(\d+): (.*)$", user, _re.M)
            pick = next((n for n, t in items if t in self.also_fit), None) or items[-1][0]
            return {"answer": pick, "reason": "理由"}
        raise AssertionError(f"想定していない呼び出し: {list(props)}")

    def list_models(self):
        return [{"name": "gemma4:12b-it-qat", "size_gb": 8.1, "parameter_size": "12B", "quantization": "Q4"}]


def _small_golden(tmp_path, monkeypatch, db_ids):
    data = golden.load("english")
    meaning = next(it for it in data["items"] if it["id"] == 911)  # 弾くべき（誤答「探す」も合う）
    blank = next(it for it in data["items"] if it["expected"] == "pass" and "空所" in it["category"])
    small = {**{k: v for k, v in data.items() if k != "_path"}, "items": [meaning, blank],
             "to_add_from_db": {"note": "テスト", "ids": db_ids}}
    (tmp_path / "golden_set_english.json").write_text(_json.dumps(small, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("GOLDEN_DIR", str(tmp_path))
    return meaning, blank


def test_golden_measurement(settings, sessions, sandbox, client, tmp_path, monkeypatch):
    """見本集の測定：データベースに残っている to_add_from_db の問題を加え、検証・見直しをかけて、
    弾いた／通した／要確認を依頼に残す。ストック・問題の記録は変えない。"""
    meaning, blank = _small_golden(tmp_path, monkeypatch, [990, 991])
    db_item = {**blank, "id": 990}  # データベースに残っている不合格の下書き（991 はない）
    x = golden.sample_from_item("english", db_item)
    with sessions.begin() as s:
        s.add(Problem(id=990, subject="english", unit=x.unit, template=x.template, format="choice", status="rejected",
                      question=x.question, choices_json=_json.dumps(x.choices, ensure_ascii=False),
                      answer_json='{"kind": "choice"}', answer_display=x.answer_display, explanation=x.explanation,
                      hint=x.hint, params_json=_json.dumps(x.params), reject_code="en_review",
                      reject_reason="haiku: 誤答も文に合う",
                      verification_json=_json.dumps([{"step": "解説の見直し", "model": "anthropic/claude-haiku-5-5",
                                                      "ok": False, "summary": "誤答も合う"}])))
    with sessions() as s:
        gs = golden.golden_set(s, "english")
    assert [i.id for i in gs.samples] == [911, blank["id"], 990]
    assert gs.db_found == [990] and gs.db_missing == ["#991（データベースにない）"]
    assert "データベースから加えた問題 1／2 問（加えられなかった問題：#991（データベースにない））" in \
        client.get("/review").text.replace("\n", "").replace("    ", "")
    # 測る（検証 1 回＋見直し）。「探す」も合うと判定させる → #911 だけ弾く
    r = client.post("/review/golden", data={"subject": "english", "verify_model": "gemma4:12b-it-qat",
                                            "verify_count": "1", "with_review": "on", "review_model": "gemma4:e4b"},
                    follow_redirects=False)
    assert r.status_code == 303
    llm = GoldenLLM(gs.samples, also_fit={"探す"})
    make_worker(settings, sessions, sandbox, llm).run_once()
    jb = jobs(sessions)[-1]
    assert jb.kind == "golden" and jb.status == "done" and jb.made == 3, jb.error
    outcome = {r["id"]: r["outcome"] for r in jb.results}
    assert outcome == {911: "reject", blank["id"]: "pass", 990: "pass"}
    assert {"gemma4:12b-it-qat", "gemma4:e4b"} <= set(llm.calls)
    assert "致命的な欠陥あり 1／2 問を弾いた" in jb.error and "問題なし 0／1 問を誤って弾いた" in jb.error
    with sessions() as s:  # 問題の記録・状態は変えない
        p = s.get(Problem, 990)
        assert p.status == "rejected" and len(p.verification) == 1
    md = client.get("/review/golden.md?subject=english").text
    assert f"依頼 #{jb.id}（検証 gemma4:12b-it-qat×1／見直し gemma4:e4b／語彙の判定：2 段階" in md
    assert "| #911 | 弾くべき | 正解のほかにも正しい選択肢がある | ○ 弾いた |" in md
    assert "| #990（DB） | 弾くべき |" in md and "× 通した" in md
    assert "記録：anthropic/claude-haiku-5-5" in md and "誤りあり" in md
    assert "検証（gemma4:12b-it-qat）：" in md and "正解を抜くと「探す」を選んだ" in md
    # データベースから加えた問題も入れた見本集（ファイルの差し替え用）
    exported = _json.loads(client.get("/review/golden.json?subject=english").text)
    assert [it["id"] for it in exported["items"]] == [911, blank["id"], 990]
    assert exported["to_add_from_db"]["ids"] == [991]
    assert exported["items"][2]["options"] == blank["options"]
    # 依頼の一覧と、作成の比較から外れること
    assert "見本集での測定" in client.get("/generate").text
    from quizapp import stock
    with sessions() as s:
        assert all(r["job"].id != jb.id for r in stock.job_stats(s, days=0))


def test_golden_measurement_resumes(settings, sessions, sandbox, client, tmp_path, monkeypatch):
    """待機・再起動のあとは、まだ測っていない問題から続ける。"""
    _small_golden(tmp_path, monkeypatch, [])
    client.post("/review/golden", data={"subject": "english", "verify_model": "gemma4:12b-it-qat",
                                        "verify_count": "1"})
    jb = jobs(sessions)[-1]
    with sessions.begin() as s:
        s.get(Job, jb.id).results_json = _json.dumps([{"id": 911, "expected": "reject", "outcome": "reject",
                                                       "steps": []}])
    with sessions() as s:
        gs = golden.golden_set(s, "english")
    llm = GoldenLLM(gs.samples)
    make_worker(settings, sessions, sandbox, llm).run_once()
    jb = jobs(sessions)[-1]
    assert jb.status == "done" and [r["id"] for r in jb.results] == [911, gs.samples[1].id]
    assert len(llm.calls) == 2  # 測っていない 1 問の検証だけ（選択肢ごとの判定と正解を抜いた判定。見直しなし）


def test_golden_measurement_runs_over_the_whole_file(settings, sessions, sandbox, client):
    """見本集のファイルの全問に、検証・見直し・本文の見直しが最後まで通る（読めない・属性が足りないで落ちない）。"""
    with sessions() as s:
        gs = golden.golden_set(s, "english")
    client.post("/review/golden", data={"subject": "english", "verify_model": "gemma4:12b-it-qat", "verify_count": "1",
                                        "with_review": "on", "review_model": "gemma4:12b-it-qat",
                                        "with_passage": "on"})
    make_worker(settings, sessions, sandbox, GoldenLLM(gs.samples)).run_once()
    jb = jobs(sessions)[-1]
    assert jb.status == "done" and jb.made == len(gs.samples)
    steps = [st for r in jb.results for st in r["steps"]]
    assert {st["step"] for st in steps} == {"機械的な点検", "検証", "解説の見直し", "本文の見直し"}
    assert not [st for st in steps if st["code"] in golden.UNSURE_CODES], \
        [(r["id"], st["summary"]) for r in jb.results for st in r["steps"] if st["code"] in golden.UNSURE_CODES]


def test_golden_outcome_counts_a_split_as_needs_check():
    """見本集の測定：検証の判定が割れたら要確認、全回不合格なら弾いた、読めないだけなら要確認（2-4）。"""
    v = lambda ok, code="": {"step": "検証", "ok": ok, "code": code}  # noqa: E731
    review_ng = {"step": "解説の見直し", "ok": False, "code": "en_review"}
    assert golden.outcome_of([v(True), v(True)]) == "pass"
    assert golden.outcome_of([v(False, "en_verify_multiple"), v(True)]) == "unsure"
    assert golden.outcome_of([v(False, "en_verify_multiple"), v(False, "solver_mismatch")]) == "reject"
    assert golden.outcome_of([v(False, "solver_unreadable"), v(False, "solver_unreadable")]) == "unsure"
    assert golden.outcome_of([v(True), v(False, "en_verify_multiple"), review_ng]) == "reject"


# --- 第 7 回のレビュー B・F・G と検証のやり方の設定 ----------------------------------------

def test_needs_check_list_shows_split_runs_and_can_discard(settings, sessions, sandbox, client):
    """要確認（第 7 回のレビュー B）：公開せず、どの判定で割れたかと理由を一覧に出す。人が通す・捨てるを選び、
    その結果を見本集の形で書き出せる。"""
    add_job(sessions, subject="english", scope="tmpl:en-vocab-blank", count=2)
    make_worker(settings, sessions, sandbox, EnglishOracle(sessions, judge="multiple", multiple_calls=2)).run_once()
    held = [p for p in problems(sessions, status="rejected") if p.reject_code == "needs_check"]
    held = held[:2]
    assert len(held) == 2
    page = client.get("/review?subject=english&status=needs_check").text
    assert "検証 1 回目" in page and "検証 2 回目" in page and "× 正解が 1 つか" in page and "○ 正解を抜いた判定" in page
    client.post(f"/review/{held[0].id}/restore", data={"back": "/review"})
    client.post(f"/review/{held[1].id}/discard", data={"back": "/review", "note": "探すも合う"})
    with sessions() as s:
        a, b = s.get(Problem, held[0].id), s.get(Problem, held[1].id)
        assert a.status == "ready" and b.status == "rejected" and b.reject_code == "checked_rejected"
        assert b.reject_reason.startswith("探すも合う") and b.verification[-1]["step"] == "要確認から不合格へ"
    items = _json.loads(client.get("/review/golden-candidates.json?subject=english").text)["items"]
    assert {(it["id"], it["expected"]) for it in items} == {(held[0].id, "pass"), (held[1].id, "reject")}


@pytest.mark.english_defaults
def test_english_reviewer_defaults_to_12b_and_warns_on_same_model(settings, sessions, client):
    """見直し役の既定は gemma4:12b（第 7 回のレビュー F）。作成役と見直し役が同じなら注意を出す。"""
    with sessions() as s:
        e = runtime.model_plan(settings, s, get_subject("english"))
        m = runtime.model_plan(settings, s, get_subject("math"))
    assert e.reviewer == "gemma4:12b-it-qat" and m.reviewer == m.verify_model
    with sessions.begin() as s:
        runtime.save_subject_settings(s, "english", {"gen": "gemma4:12b-it-qat", "verify": "gemma4:e4b"})
    with sessions() as s:
        assert runtime.model_plan(settings, s, get_subject("english")).same_gen_verify
    assert "作成役と検証役（または見直し役）が同じモデルです" in client.get("/status/models").text


def test_verify_options_are_saved_recorded_and_compared(settings, sessions, sandbox, client):
    """検証のやり方（C・D・E）は科目ごとに保存でき、依頼に記録する。見本集の測定でも選べ、比べる表に出る。"""
    client.post("/status/subject-models", data={"subject": "english", "opt_fit3": "off", "opt_reading_strict": "reason",
                                               "opt_reading_think": "on"})
    with sessions() as s:
        assert runtime.verify_options(s, get_subject("english")).items() >= {
            "fit3": "off", "reading_strict": "reason", "reading_think": "on", "pair_check": "on"}.items()
    page = client.get("/status/models").text
    assert "検証のやり方：語彙の判定" in page and "読解の選択肢の判定の思考モード" in page
    client.post("/generate", data={"subject": "english", "scope": "tmpl:en-vocab-blank", "format": "choice",
                                   "count": "1"})
    assert jobs(sessions)[-1].options.items() >= {"fit3": "off", "reading_strict": "reason", "reading_think": "on"}.items()
    # 見本集：画面で選んだやり方（新しい方式）で測る
    client.post("/review/golden", data={"subject": "english", "verify_model": "gemma4:e4b", "verify_count": "1",
                                        "opt_fit3": "on", "opt_reading_strict": "off", "opt_reading_think": "model"})
    jb = jobs(sessions)[-1]
    assert jb.kind == "golden" and jb.options.items() >= {"fit3": "on", "reading_strict": "off",
                                                          "reading_think": "model"}.items()
    assert "語彙の判定：3 段階" in golden.config_label(jb) and "読解の誤答の根拠：書かせない" in golden.config_label(jb)


def test_golden_borderline_and_stopped_score():
    """微妙（borderline）は採点に含めない。要確認を公開しない扱いの成績（弾いた＋要確認＝止めた）も出す（G）。
    今の見本集の期待で数え直す（前の測定も同じ物差しで比べられる）。"""
    results = [{"id": 1, "expected": "reject", "outcome": "reject"}, {"id": 2, "expected": "reject", "outcome": "unsure"},
               {"id": 3, "expected": "pass", "outcome": "unsure"}, {"id": 4, "expected": "pass", "outcome": "reject"},
               {"id": 5, "expected": "pass", "outcome": "pass"}]
    sc = golden.score(results, {1: "reject", 2: "reject", 3: "pass", 4: "borderline", 5: "reject"})
    assert sc == {"reject_total": 3, "reject_caught": 1, "reject_unsure": 1, "pass_total": 1, "pass_wrong": 0,
                  "pass_unsure": 1}
    text = golden.score_text(sc)
    assert "弾くべき問題を 2／3 問止め" in text and "通すべき問題を 1／1 問止めた" in text


@pytest.mark.parametrize("fast", [False, True])
def test_golden_measurement_runs_phase_by_phase(settings, sessions, sandbox, client, fast):
    """見本集の測定は段階ごとに全問を回す（モデルの入れ替えを減らす）。同じ本文の見直しは 1 回だけ。
    「検証で弾いた問題の見直しを省く」を選ぶと、検証で弾いた問題に見直しをかけない（成績は同じ）。"""
    with sessions() as s:
        gs = golden.golden_set(s, "english")
    data = {"subject": "english", "verify_model": "gemma4:e4b", "verify_count": "2", "with_review": "on",
            "review_model": "gemma4:12b-it-qat", "with_passage": "on"}
    if fast:
        data["fast"] = "on"
    client.post("/review/golden", data=data)
    llm = GoldenLLM(gs.samples, also_fit={"探す"})
    make_worker(settings, sessions, sandbox, llm).run_once()
    jb = jobs(sessions)[-1]
    assert jb.status == "done" and jb.made == len(gs.samples) and not any(r.get("partial") for r in jb.results)
    switches = sum(1 for a, b in zip(llm.calls, llm.calls[1:]) if a != b)
    assert switches == 1  # 検証役（e4b）を全部終えてから見直し役（12b）へ。1 問ずつだと何十回も入れ替わる
    passages = {x.passage.body for x in gs.samples if x.passage is not None}
    reviewed = [st for r in jb.results for st in r["steps"] if st["step"] == "本文の見直し"
                and not st["summary"].startswith("（同じ本文")]
    assert len(reviewed) <= len(passages)
    rejected_by_verify = [r for r in jb.results if r["id"] == 911][0]
    has_review = any(st["step"] == "解説の見直し" for st in rejected_by_verify["steps"])
    assert has_review is (not fast) and rejected_by_verify["outcome"] == "reject"
    if fast:
        assert "検証で弾いた問題の見直しは省略" in golden.config_label(jb)
    # 所要時間（段階・種類ごとの 1 問 1 回あたり。第 8 回のレビュー 4-1）が比べる表に出る
    assert "所要時間：検証（読解）" in client.get("/review/golden.md?subject=english").text


def test_golden_measurement_resumes_in_the_middle_of_a_phase(settings, sessions, sandbox, client, tmp_path, monkeypatch):
    """段階の途中で止まっても、済んだ段階は呼び直さない。"""
    _small_golden(tmp_path, monkeypatch, [])
    client.post("/review/golden", data={"subject": "english", "verify_model": "gemma4:12b-it-qat",
                                        "verify_count": "1", "with_review": "on", "review_model": "gemma4:e4b"})
    jb = jobs(sessions)[-1]
    with sessions.begin() as s:  # 911 の検証まで済んでいる
        s.get(Job, jb.id).results_json = _json.dumps([{"id": 911, "expected": "reject", "partial": True, "steps": [
            {"step": "機械的な点検", "key": "text", "ok": True, "code": ""},
            {"step": "検証", "key": "verify1", "model": "gemma4:12b-it-qat", "ok": True, "code": ""}]}])
    with sessions() as s:
        gs = golden.golden_set(s, "english")
    llm = GoldenLLM(gs.samples)
    make_worker(settings, sessions, sandbox, llm).run_once()
    jb = jobs(sessions)[-1]
    assert jb.status == "done" and jb.made == 2
    assert llm.calls.count("gemma4:12b-it-qat") == 2  # 残りの 1 問の検証（判定と正解を抜いた判定）だけ
    assert llm.calls.count("gemma4:e4b") == 4  # 見直しは 2 問とも（語彙は例文の筋の確認と解説の点検の 2 回）


def test_export_ready_and_needs_check_together(settings, sessions, sandbox, client):
    """新しく作った問題の点検（第 8 回のレビュー 4-2）：合格と要確認をまとめて書き出す。要確認は割れた理由も出る。"""
    add_job(sessions, subject="english", scope="tmpl:en-vocab-blank", count=2)
    make_worker(settings, sessions, sandbox, EnglishOracle(sessions, judge="multiple", multiple_calls=2)).run_once()
    held = [p for p in problems(sessions, status="rejected") if p.reject_code == "needs_check"]
    ready = problems(sessions, status="ready")
    assert held and ready
    md = client.get("/review/export.md?subject=english&status=ready_check").text
    assert all(f"#{p.id}" in md for p in held + ready)
    assert "検証の判定が割れた" in md and "× 正解が 1 つか" in md


@pytest.mark.english_defaults
def test_english_verifier_thinks_unless_set_on_screen(settings, sessions, client):
    """英語の検証役 e4b は、画面で設定していなければ英語の依頼では思考あり（第 10 回のレビュー 4 章：思考なしでは
    判定の形が崩れた）。画面で設定したものが優先。数学の依頼には効かない。"""
    english = get_subject("english")
    with sessions.begin() as s:
        plan = runtime.model_plan(settings, s, english)
        j = Job(subject="english", scope="", format="choice", count=1)
        runtime.apply_plan(j, plan)
        runtime.record_think(settings, s, j, plan)
        m = Job(subject="math", scope="", format="choice", count=1, verify_model="gemma4:e4b-it-qat")
        mplan = dataclasses.replace(runtime.model_plan(settings, s), verify_model="gemma4:e4b-it-qat")
        runtime.record_think(settings, s, m, mplan)
    assert j.think["gemma4:e4b-it-qat"] == "on" and j.think["gemma4:12b-it-qat"] == "off"
    assert m.think["gemma4:e4b-it-qat"] == "off"
    with sessions.begin() as s:
        runtime.save_think_setting(s, "gemma4:e4b-it-qat", "off")
        j2 = Job(subject="english", scope="", format="choice", count=1)
        runtime.record_think(settings, s, j2, plan)
    assert j2.think["gemma4:e4b-it-qat"] == "off"
