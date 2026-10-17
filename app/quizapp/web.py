"""画面（FastAPI）。

設計の原則：ここでは LLM（Ollama）を一切呼ばない。問題はストックから出し、採点は
選択式なら選んだ選択肢、キーボード入力なら sandbox の sympy で行う。Ollama の状態は
worker が kv 表に書いたものを表示するだけ。デスクトップの電源が切れていても全部の画面が動く。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from . import backup, runtime, stock
from .config import Settings
from .datadir import ensure_or_exit
from .db import (
    DEFAULT_USER_ID,
    Attempt,
    Job,
    Problem,
    StudySession,
    dumps,
    init_db,
    kv_json,
    kv_updated_at,
    utcnow,
)
from .sandbox_client import SandboxClient
from .subjects import all_subjects, get_subject
from .subjects.base import DEVICES, FORMATS, Grade, Subject

BASE = Path(__file__).resolve().parent
MAX_SESSION = 50
MAX_JOB = 50
MAX_DURATION_MS = 3 * 60 * 60 * 1000


def _int(value: str, default: int) -> int:
    try:
        return int(str(value).strip())
    except ValueError:
        return default


def _ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)


def create_app(settings: Settings | None = None, sessions: sessionmaker[Session] | None = None,
               sandbox: Any = None) -> FastAPI:
    settings = settings or Settings()
    if sessions is None:
        ensure_or_exit(settings.data_dir, skip=settings.skip_data_check)
        sessions = init_db(settings.database_path)
    sandbox = sandbox if sandbox is not None else SandboxClient(settings.sandbox_url)
    tz = ZoneInfo(settings.timezone)

    app = FastAPI(title="ai-quiz", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    templates = Jinja2Templates(directory=BASE / "templates")

    def localtime(dt: datetime | None, fmt: str = "%m/%d %H:%M") -> str:
        if dt is None:
            return ""
        return dt.replace(tzinfo=timezone.utc).astimezone(tz).strftime(fmt)

    def seconds(ms: int | None) -> str:
        if ms is None:
            return ""
        sec = ms // 1000
        return f"{sec // 60}分{sec % 60:02d}秒" if sec >= 60 else f"{sec}秒"

    templates.env.filters["localtime"] = localtime
    templates.env.filters["seconds"] = seconds
    templates.env.filters["isolocal"] = lambda text: localtime(datetime.fromisoformat(text)) if text else ""
    templates.env.globals.update(FORMATS=FORMATS, DEVICES=DEVICES, subjects=all_subjects,
                                 get_subject=get_subject)

    def render(request: Request, name: str, status_code: int = 200, **ctx: Any) -> Response:
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def redirect(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    def subject_or_404(subject_id: str) -> Subject:
        subject = get_subject(subject_id)
        if subject is None:
            raise HTTPException(404, "科目が見つかりません")
        return subject

    def problem_or_404(s: Session, problem_id: int) -> Problem:
        problem = s.get(Problem, problem_id)
        if problem is None:
            raise HTTPException(404, "問題が見つかりません")
        return problem

    def user_id(_request: Request) -> int:
        return DEFAULT_USER_ID  # 利用者の切り替え画面は後のフェーズ

    def scope_options(subject: Subject) -> list[dict[str, Any]]:
        return [{"id": u.id, "label": u.label,
                 "subunits": [{"id": su.id, "label": su.label, "formats": list(su.formats)} for su in u.subunits]}
                for u in subject.units()]

    def scope_valid(subject: Subject, scope: str) -> bool:
        return scope == "" or bool(subject.templates_for(scope))

    # --- ホーム（解き始め） --------------------------------------------------

    @app.get("/")
    def home(request: Request, msg: str = "") -> Response:
        uid = user_id(request)
        with sessions() as s:
            summary = stock.stock_summary(s, uid, settings.low_stock)
            counts = stock.unseen_counts(s, uid)
        data = {
            sub.id: {"name": sub.name, "units": scope_options(sub),
                     "counts": {f"{t}|{f}": n for (sid, t, f), n in counts.items() if sid == sub.id}}
            for sub in all_subjects()
        }
        return render(request, "home.html", summary=summary, data=data,
                      device=request.cookies.get("device", "phone"), msg=msg)

    @app.post("/start")
    def start(request: Request, subject: str = Form(...), scope: str = Form(""), device: str = Form("phone"),
              count: str = Form("20")) -> Response:
        sub = subject_or_404(subject)
        if device not in DEVICES or not scope_valid(sub, scope):
            raise HTTPException(400, "選び方が正しくありません")
        count_n = max(1, min(MAX_SESSION, _int(count, 20)))
        with sessions.begin() as s:
            ids = stock.build_session(s, user_id(request), sub, scope, device, count_n, settings.retry_percent)
            if not ids:
                return redirect("/?msg=empty")
            row = StudySession(user_id=user_id(request), subject=sub.id, scope=scope, device=device,
                               problem_ids_json=dumps(ids), requested=count_n)
            s.add(row)
            s.flush()
            sid = row.id
        resp = redirect(f"/play/{sid}")
        resp.set_cookie("device", device, max_age=365 * 24 * 3600, samesite="lax")
        return resp

    # --- 解く ---------------------------------------------------------------

    def load_session(s: Session, sid: int) -> StudySession:
        row = s.get(StudySession, sid)
        if row is None:
            raise HTTPException(404, "セットが見つかりません")
        return row

    def answered_ids(s: Session, sid: int) -> set[int]:
        return set(s.execute(select(Attempt.problem_id).where(Attempt.session_id == sid)).scalars())

    @app.get("/play/{sid}")
    def play(request: Request, sid: int, msg: str = "") -> Response:
        with sessions.begin() as s:
            row = load_session(s, sid)
            done = answered_ids(s, sid)
            ids = row.problem_ids
            for pos, pid in enumerate(ids):
                if pid in done:
                    continue
                problem = s.get(Problem, pid)
                if problem is None or problem.reported or problem.status != "ready":
                    continue  # 「おかしい？」が押された問題は飛ばす
                if row.current_problem_id != pid:  # 初めて表示した時刻を残す（読み込み直しでは更新しない）
                    row.current_problem_id, row.current_shown_at = pid, utcnow()
                _ = problem.passage
                return render(request, "play.html", session=row, problem=problem, pos=pos + 1,
                              total=len(ids), shown_at=_ms(row.current_shown_at), msg=msg, value="",
                              subject=get_subject(problem.subject), retry_message="", hint_viewed=0)
            attempts = list(s.execute(select(Attempt).where(Attempt.session_id == sid)
                                      .order_by(Attempt.id)).scalars())
        correct = sum(1 for a in attempts if a.is_correct)
        return render(request, "summary.html", session=row, attempts=attempts, correct=correct,
                      subject=get_subject(row.subject))

    @app.post("/play/{sid}/answer")
    def answer(request: Request, sid: int, problem_id: int = Form(...), response: str = Form(""),
               shown_at: int = Form(0), hint_viewed: int = Form(0), action: str = Form("answer")) -> Response:
        uid = user_id(request)
        now = utcnow()
        with sessions() as s:
            row = load_session(s, sid)
            if problem_id not in row.problem_ids:
                raise HTTPException(400, "このセットの問題ではありません")
            if problem_id in answered_ids(s, sid):
                return redirect(f"/play/{sid}")
            problem = problem_or_404(s, problem_id)
            subject = subject_or_404(problem.subject)
            gave_up = action == "giveup"
            if gave_up:
                grade = Grade(False, response_display="（わからない）")
            else:
                grade = subject.grade(sandbox, problem, response)
            if grade.retry_message:
                ids = row.problem_ids
                return render(request, "play.html", status_code=422, session=row, problem=problem,
                              pos=ids.index(problem_id) + 1, total=len(ids), shown_at=shown_at, msg="",
                              value=response, subject=subject, retry_message=grade.retry_message,
                              hint_viewed=hint_viewed)
            prev = s.execute(select(func.count()).select_from(Attempt).where(
                Attempt.user_id == uid, Attempt.problem_id == problem_id)).scalar_one()
        # かかった時間はサーバーの時計で測る（最初に表示した時刻から）。古いページから送られたときは画面の値を使う
        if row.current_problem_id == problem_id and row.current_shown_at is not None:
            shown = row.current_shown_at
        else:
            shown = _from_ms(shown_at) if shown_at > 0 else None
        duration = None
        if shown is not None:
            duration = int((now - shown).total_seconds() * 1000)
            duration = duration if 0 <= duration <= MAX_DURATION_MS else None
        try:
            aid = save_attempt(uid, problem, sid, row, prev, grade, gave_up, hint_viewed, response, shown,
                               duration, now)
        except IntegrityError:  # 二重送信。最初の回答の結果を見せる
            with sessions() as s:
                aid = s.execute(select(Attempt.id).where(Attempt.session_id == sid,
                                                         Attempt.problem_id == problem_id)).scalar_one()
        return redirect(f"/play/{sid}/result/{aid}")

    def save_attempt(uid: int, problem: Problem, sid: int, row: StudySession, prev: int, grade: Grade,
                     gave_up: bool, hint_viewed: int, response: str, shown: datetime | None,
                     duration: int | None, now: datetime) -> int:
        with sessions.begin() as s:
            att = Attempt(
                user_id=uid, problem_id=problem.id, session_id=sid, subject=problem.subject, unit=problem.unit,
                template=problem.template, format=problem.format, device=row.device, attempt_no=prev + 1,
                is_correct=grade.correct, gave_up=gave_up, hint_viewed=bool(hint_viewed),
                explanation_viewed=gave_up, response="" if gave_up else response.strip()[:500],
                response_display=grade.response_display[:1000], chosen_index=grade.chosen_index,
                chosen_mistake=grade.chosen_mistake, shown_at=shown, duration_ms=duration, created_at=now,
            )
            s.add(att)
            s.flush()
            return att.id

    @app.get("/play/{sid}/result/{aid}")
    def result(request: Request, sid: int, aid: int, msg: str = "") -> Response:
        with sessions() as s:
            row = load_session(s, sid)
            att = s.get(Attempt, aid)
            if att is None or att.session_id != sid:
                raise HTTPException(404, "回答が見つかりません")
            problem = att.problem
            ids = row.problem_ids
            remaining = len(ids) - len(answered_ids(s, sid))
        return render(request, "result.html", session=row, attempt=att, problem=problem,
                      pos=ids.index(problem.id) + 1 if problem.id in ids else 0, total=len(ids),
                      remaining=remaining, subject=get_subject(problem.subject), msg=msg)

    # --- 見返し・再挑戦・報告 -----------------------------------------------

    @app.get("/history")
    def history(request: Request, filter: str = "all", subject: str = "") -> Response:  # noqa: A002
        uid = user_id(request)
        with sessions() as s:
            q = (select(Attempt.problem_id, func.max(Attempt.id), func.count())
                 .where(Attempt.user_id == uid).group_by(Attempt.problem_id))
            rows = []
            for _pid, last_id, n in s.execute(q):
                last = s.get(Attempt, last_id)
                problem = last.problem
                if subject and problem.subject != subject:
                    continue
                if filter == "wrong" and (last.is_correct or problem.reported):
                    continue
                rows.append({"problem": problem, "last": last, "count": n})
        rows.sort(key=lambda r: r["last"].created_at, reverse=True)
        return render(request, "history.html", rows=rows[:300], filter=filter, subject=subject)

    @app.get("/problems/{pid}")
    def problem_page(request: Request, pid: int, msg: str = "") -> Response:
        with sessions() as s:
            problem = problem_or_404(s, pid)
            attempts = list(s.execute(select(Attempt).where(Attempt.problem_id == pid,
                                                            Attempt.user_id == user_id(request))
                                      .order_by(Attempt.id.desc())).scalars())
        return render(request, "problem.html", problem=problem, attempts=attempts,
                      subject=get_subject(problem.subject), msg=msg)

    @app.post("/problems/{pid}/retry")
    def retry(request: Request, pid: int) -> Response:
        with sessions.begin() as s:
            problem = problem_or_404(s, pid)
            if problem.status != "ready" or problem.reported:
                return redirect(f"/problems/{pid}?msg=unavailable")
            device = "keyboard" if problem.format == "input" else request.cookies.get("device", "phone")
            row = StudySession(user_id=user_id(request), subject=problem.subject, device=device, kind="retry",
                               problem_ids_json=dumps([pid]), requested=1)
            s.add(row)
            s.flush()
            sid = row.id
        return redirect(f"/play/{sid}")

    @app.post("/problems/{pid}/report")
    def report(pid: int, note: str = Form(""), back: str = Form("")) -> Response:
        with sessions.begin() as s:
            problem = problem_or_404(s, pid)
            problem.reported = True
            problem.report_note = note.strip()[:1000]
            problem.reported_at = utcnow()
        target = back if back.startswith("/") and not back.startswith("//") else f"/problems/{pid}"
        sep = "&" if "?" in target else "?"
        return redirect(f"{target}{sep}msg=reported")

    @app.post("/problems/{pid}/unreport")
    def unreport(pid: int) -> Response:
        with sessions.begin() as s:
            problem = problem_or_404(s, pid)
            problem.reported = False
            problem.reported_at = None
        return redirect("/status#reported")

    @app.post("/api/preview")
    async def preview(request: Request) -> JSONResponse:
        """キーボード入力の読み取り結果を数式で見せる（sandbox の sympy。LLM は使わない）。"""
        try:
            body = await request.json()
            pid = int(body.get("problem_id"))
            text = str(body.get("text", ""))[:500]
        except (ValueError, TypeError, AttributeError):
            return JSONResponse({"ok": False, "error": "不正なリクエストです"}, status_code=400)
        with sessions() as s:
            problem = s.get(Problem, pid)
            if problem is None:
                return JSONResponse({"ok": False, "error": "問題が見つかりません"}, status_code=404)
            ans = problem.answer
        if not text.strip() or "kind" not in ans:
            return JSONResponse({"ok": False, "error": ""})
        try:
            res = sandbox.preview(text, ans["kind"], ans.get("var", "x"))
        except Exception:
            return JSONResponse({"ok": False, "error": "プレビューを表示できません"})
        return JSONResponse({"ok": bool(res.get("ok")), "latex": res.get("latex", ""), "error": res.get("error", "")})

    # --- 作成依頼 -----------------------------------------------------------

    @app.get("/generate")
    def generate_page(request: Request, msg: str = "") -> Response:
        with sessions() as s:
            jobs = list(s.execute(select(Job).order_by(Job.id.desc()).limit(30)).scalars())
            counts = stock.unseen_counts(s, user_id(request))
        data = {sub.id: {"name": sub.name, "units": scope_options(sub), "formats": list(sub.formats),
                         "counts": {f"{t}|{f}": n for (sid, t, f), n in counts.items() if sid == sub.id}}
                for sub in all_subjects()}
        return render(request, "generate.html", jobs=jobs, data=data, msg=msg)

    @app.post("/generate")
    def generate(subject: str = Form(...), scope: str = Form(""), format: str = Form("choice"),  # noqa: A002
                 count: int = Form(10)) -> Response:
        sub = subject_or_404(subject)
        if format not in sub.formats or not scope_valid(sub, scope) or not sub.templates_for(scope, format):
            raise HTTPException(400, "選び方が正しくありません")
        with sessions.begin() as s:
            s.add(Job(subject=sub.id, scope=scope, format=format, count=max(1, min(MAX_JOB, count))))
        return redirect("/generate?msg=queued")

    @app.post("/jobs/{job_id}/cancel")
    def cancel(job_id: int) -> Response:
        with sessions.begin() as s:
            job = s.get(Job, job_id)
            if job and job.status in ("pending", "running", "waiting"):
                job.status = "cancelled"
                job.finished_at = utcnow()
        return redirect("/generate")

    @app.get("/jobs.json")
    def jobs_json() -> dict[str, Any]:
        with sessions() as s:
            jobs = s.execute(select(Job).where(Job.status.in_(("pending", "running", "waiting")))).scalars()
            return {"jobs": [{"id": j.id, "status": j.status, "progress": j.progress, "made": j.made,
                              "count": j.count} for j in jobs]}

    # --- 状態 ---------------------------------------------------------------

    def status_data(s: Session, uid: int) -> dict[str, Any]:
        worker = kv_json(s, "worker", {}) or {}
        worker_at = kv_updated_at(s, "worker")
        ollama = kv_json(s, "ollama", {}) or {}
        last_backup = backup.latest_time(settings.backup_dir)
        return {
            "worker": worker,
            # LLM の 1 回の呼び出しは最大 LLM_TIMEOUT 秒かかるので、処理中はその分だけ待つ
            "worker_alive": bool(worker_at and utcnow() - worker_at < timedelta(
                seconds=180 + (settings.llm_timeout if worker.get("job_id") else 0))),
            "worker_at": worker_at,
            "ollama": ollama,
            "stock": stock.stock_summary(s, uid, settings.low_stock),
            "jobs_open": s.execute(select(func.count()).select_from(Job)
                                   .where(Job.status.in_(("pending", "running", "waiting")))).scalar_one(),
            "backup_last": localtime(last_backup, "%Y/%m/%d %H:%M") if last_backup else None,
            "backup_count": len(backup.list_backups(settings.backup_dir)),
            "backup_info": kv_json(s, "backup", {}) or {},
        }

    @app.get("/status")
    def status(request: Request, msg: str = "") -> Response:
        uid = user_id(request)
        with sessions() as s:
            data = status_data(s, uid)
            plan = runtime.model_plan(settings, s)
            overrides = runtime.overrides(s)
            jobs = list(s.execute(select(Job).order_by(Job.id.desc()).limit(15)).scalars())
            stats = stock.verify_stats(s)
            per_template = stock.per_template_rejects(s)
            templ = stock.template_stock(s, uid)
            rejected = list(s.execute(select(Problem).where(Problem.status == "rejected")
                                      .order_by(Problem.id.desc()).limit(20)).scalars())
            reported = list(s.execute(select(Problem).where(Problem.reported.is_(True))
                                      .order_by(Problem.reported_at.desc())).scalars())
            tests = list(s.execute(select(Job).where(Job.is_test.is_(True)).order_by(Job.id.desc()).limit(5))
                         .scalars())
        sandbox_ok = sandbox.healthy()
        model_names = [m["name"] for m in data["ollama"].get("models", [])]
        codes = {}
        for sub in all_subjects():
            codes.update(sub.all_reject_codes())
        return render(request, "status.html", **data, plan=plan, overrides=overrides, jobs=jobs, stats=stats,
                      per_template=per_template, templ=templ, rejected=rejected, reported=reported,
                      tests=tests, sandbox_ok=sandbox_ok, model_names=model_names, codes=codes,
                      settings=settings, max_verify=runtime.MAX_VERIFY_COUNT, msg=msg)

    @app.get("/status.json")
    def status_json(request: Request) -> dict[str, Any]:
        with sessions() as s:
            data = status_data(s, user_id(request))
            data["verify"] = stock.verify_stats(s)
            data["models"] = runtime.model_plan(settings, s).__dict__
        data["worker_at"] = data["worker_at"].isoformat() if data["worker_at"] else None
        data["sandbox_ok"] = sandbox.healthy()
        return json.loads(json.dumps(data, default=str))

    @app.post("/status/models")
    def save_models(gen_model: str = Form(""), verify_model: str = Form(""), verify_count: str = Form(""),
                    verify_model_2: str = Form("")) -> Response:
        with sessions.begin() as s:
            runtime.save_overrides(s, {"gen_model": gen_model, "verify_model": verify_model,
                                       "verify_count": verify_count, "verify_model_2": verify_model_2})
        return redirect("/status?msg=saved#models")

    @app.post("/status/test")
    def verify_test(target: str = Form("")) -> Response:
        """検証のテスト：わざと正解をずらした問題を 1 問流す（ストックには入らない）。

        target は "<科目ID>" か "<科目ID>|<小単元ID>"。テストに対応する科目（test_format あり）だけ。
        """
        subject_id, _, template = target.partition("|")
        sub = get_subject(subject_id)
        scope = f"tmpl:{template}" if template else ""
        if sub is None or not sub.test_format or not scope_valid(sub, scope):
            raise HTTPException(400, "選び方が正しくありません")
        with sessions.begin() as s:
            s.add(Job(subject=sub.id, scope=scope, format=sub.test_format, count=1, is_test=True))
        return redirect("/status?msg=test#tests")

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        if request.url.path.endswith(".json") or request.url.path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)
        return render(request, "error.html", status_code=exc.status_code, message=exc.detail)

    app.state.sessions = sessions
    app.state.started = time.time()
    return app
