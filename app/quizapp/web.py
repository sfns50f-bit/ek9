"""画面（FastAPI + Jinja2）。起動: uvicorn quizapp.web:create_app --factory"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from .config import MODEL_ROLES, Settings, with_model_overrides
from .db import Attempt, Job, Problem, init_db, kv_get, kv_set, model_overrides, utcnow
from .llm import LLMUnavailable, OllamaClient
from .prompts import KIND_LABELS
from .sandbox_client import SandboxClient, SandboxUnavailable
from .topics import COURSES, DIFFICULTIES, get_topic

BASE = Path(__file__).parent

KIND_HINTS = {
    "value": "例: 3/2　-sqrt(3)/2　2x^2+1　pi/6　log(3)",
    "set": "答えが複数あります（順不同・カンマ区切り）。例: 1, -3　(1±√5)/2",
    "tuple": "問題文の順にカンマ区切りで。例: (2, -1)",
    "matrix": "行を ; で区切ります。例: 1, 2; 3, 4",
    "interval": "例: 1<x<3　x<=-1, 2<x（カンマは「または」）　解なし　すべての実数",
    "antiderivative": "積分定数 C は省略できます。例: x^3/3 + sin(x)",
}
APPROX_FEEDBACK = "惜しい！ 小数の近似値ではなく、分数や √、π を使った厳密な値で答えてください。"


def create_app(settings: Settings | None = None, sessions: sessionmaker[Session] | None = None,
               sandbox: Any = None, llm: Any = None) -> FastAPI:
    settings = settings or Settings()
    sessions = sessions or init_db(settings.database_path)
    sandbox = sandbox or SandboxClient(settings.sandbox_url)
    llm = llm or OllamaClient(settings.ollama_base_url)
    tz = ZoneInfo(settings.timezone)

    app = FastAPI(title="AI問題集", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    templates = Jinja2Templates(directory=BASE / "templates")

    def localtime(dt: datetime | None, fmt: str = "%Y/%m/%d %H:%M") -> str:
        return dt.replace(tzinfo=timezone.utc).astimezone(tz).strftime(fmt) if dt else ""

    templates.env.filters["localtime"] = localtime
    templates.env.globals.update(
        DIFFICULTIES=DIFFICULTIES, KIND_LABELS=KIND_LABELS, KIND_HINTS=KIND_HINTS, get_topic=get_topic,
    )

    def render(request: Request, name: str, status_code: int = 200, **ctx: Any) -> Response:
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def error_page(request: Request, message: str, status_code: int) -> Response:
        return render(request, "error.html", status_code=status_code, message=message)

    def load_problem(s: Session, problem_id: int) -> Problem:
        problem = s.execute(
            select(Problem).where(Problem.id == problem_id).options(selectinload(Problem.attempts))
        ).scalar_one_or_none()
        if problem is None or problem.status == "rejected":
            raise HTTPException(404, "問題が見つかりません")
        return problem

    def redirect(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    # --- ホーム --------------------------------------------------------------

    @app.get("/")
    def index(request: Request) -> Response:
        with sessions() as s:
            stock = {
                f"{t}|{d}": n for t, d, n in s.execute(
                    select(Problem.topic_id, Problem.difficulty, func.count())
                    .where(Problem.status == "ready", Problem.served_at.is_(None))
                    .group_by(Problem.topic_id, Problem.difficulty)
                )
            }
            total = s.execute(select(func.count()).select_from(Attempt)).scalar_one()
            correct = s.execute(
                select(func.count()).select_from(Attempt).where(Attempt.is_correct.is_(True))
            ).scalar_one()
            rows = _history_rows(s)
        courses = [
            {"course": course, "topics": [{"id": t.id, "unit": t.unit, "level": t.level} for t in topics]}
            for course, topics in COURSES
        ]
        return render(
            request, "index.html",
            courses_json=json.dumps(courses, ensure_ascii=False), stock_json=json.dumps(stock),
            total=total, correct=correct, wrong=sum(1 for r in rows if r["wrong"]), recent=rows[:5],
        )

    @app.post("/generate")
    def generate(topic_id: str = Form(...), difficulty: int = Form(2)) -> Response:
        if get_topic(topic_id) is None:
            raise HTTPException(400, "未知の単元です")
        difficulty = min(max(difficulty, 1), 3)
        with sessions.begin() as s:
            problem = s.execute(
                select(Problem).where(
                    Problem.status == "ready", Problem.served_at.is_(None),
                    Problem.topic_id == topic_id, Problem.difficulty == difficulty,
                ).order_by(Problem.created_at).limit(1)
            ).scalar_one_or_none()
            if problem is not None:
                problem.served_at = utcnow()
                return redirect(f"/problems/{problem.id}")
            job = Job(topic_id=topic_id, difficulty=difficulty)
            s.add(job)
            s.flush()
            return redirect(f"/jobs/{job.id}")

    # --- 生成待ち ------------------------------------------------------------

    @app.get("/jobs/{job_id}")
    def job_page(request: Request, job_id: int) -> Response:
        with sessions.begin() as s:
            job = s.get(Job, job_id)
            if job is None:
                raise HTTPException(404, "依頼が見つかりません")
            if job.status == "done" and job.problem_id:
                problem = s.get(Problem, job.problem_id)
                if problem is not None and problem.served_at is None:
                    problem.served_at = utcnow()
                return redirect(f"/problems/{job.problem_id}")
            return render(request, "job.html", job=job, topic=get_topic(job.topic_id))

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: int) -> dict[str, Any]:
        with sessions() as s:
            job = s.get(Job, job_id)
            if job is None:
                raise HTTPException(404)
            ahead = s.execute(
                select(func.count()).select_from(Job).where(Job.status == "pending", Job.id < job.id)
            ).scalar_one()
            worker = kv_get(s, "worker_status")
            return {
                "status": job.status, "progress": job.progress, "problem_id": job.problem_id,
                "error": job.error, "ahead": ahead, "worker": worker,
            }

    @app.post("/jobs/{job_id}/cancel")
    def job_cancel(job_id: int) -> Response:
        with sessions.begin() as s:
            job = s.get(Job, job_id)
            if job is not None and job.status in ("pending", "running"):
                job.status = "cancelled"
        return redirect("/")

    # --- 出題・解答 ----------------------------------------------------------

    @app.get("/problems/{problem_id}")
    def solve_page(request: Request, problem_id: int) -> Response:
        with sessions() as s:
            problem = load_problem(s, problem_id)
            return render(request, "solve.html", problem=problem, topic=get_topic(problem.topic_id),
                          error="", value="")

    @app.post("/problems/{problem_id}/answer")
    def answer(request: Request, problem_id: int, answer: str = Form("")) -> Response:
        text = answer.strip()
        with sessions() as s:
            problem = load_problem(s, problem_id)

            def again(message: str) -> Response:
                return render(request, "solve.html", status_code=422, problem=problem,
                              topic=get_topic(problem.topic_id), error=message, value=text)

            if not text:
                return again("答えを入力してください。")
            try:
                res = sandbox.compare(problem.answer_kind, {"srepr": problem.answer_srepr}, {"text": text},
                                      var=problem.answer_var)
            except SandboxUnavailable as exc:
                return error_page(request, str(exc), 503)
            if not res.get("ok"):
                if res.get("where") == "given":
                    return again(f"答えを読み取れませんでした：{res.get('error')}")
                return error_page(request, f"正答データを読めません：{res.get('error')}", 500)
        feedback = res.get("detail") or (APPROX_FEEDBACK if res.get("approx") else "")
        with sessions.begin() as s:
            attempt = Attempt(problem_id=problem_id, user_input=text, user_latex=res["given"]["latex"],
                              is_correct=bool(res["equal"]), feedback=feedback)
            s.add(attempt)
            s.flush()
            attempt_id = attempt.id
        return redirect(f"/problems/{problem_id}/review?attempt={attempt_id}")

    @app.post("/problems/{problem_id}/giveup")
    def give_up(problem_id: int) -> Response:
        with sessions.begin() as s:
            load_problem(s, problem_id)
            attempt = Attempt(problem_id=problem_id, gave_up=True, is_correct=False, feedback="答えを見ました")
            s.add(attempt)
            s.flush()
            attempt_id = attempt.id
        return redirect(f"/problems/{problem_id}/review?attempt={attempt_id}")

    @app.get("/problems/{problem_id}/review")
    def review(request: Request, problem_id: int, attempt: int | None = None) -> Response:
        with sessions() as s:
            problem = load_problem(s, problem_id)
            current = next((a for a in problem.attempts if a.id == attempt), None)
            return render(request, "review.html", problem=problem, topic=get_topic(problem.topic_id),
                          current=current, log=problem.verification_log)

    @app.post("/problems/{problem_id}/report")
    def report(problem_id: int, note: str = Form("")) -> Response:
        with sessions.begin() as s:
            problem = load_problem(s, problem_id)
            problem.status = "reported"
            problem.report_note = note.strip()[:2000]
        return redirect(f"/problems/{problem_id}/review")

    @app.post("/problems/{problem_id}/unreport")
    def unreport(problem_id: int) -> Response:
        with sessions.begin() as s:
            problem = load_problem(s, problem_id)
            problem.status = "ready"
            problem.report_note = ""
        return redirect(f"/problems/{problem_id}/review")

    @app.post("/api/preview")
    async def preview(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"ok": False, "error": "不正なリクエストです"}, status_code=400)
        text = str(body.get("text") or "").strip()
        kind = str(body.get("kind") or "value")
        if not text:
            return JSONResponse({"ok": False, "error": ""})
        if kind not in KIND_LABELS:
            return JSONResponse({"ok": False, "error": "不正な形式です"}, status_code=400)
        try:
            res = sandbox.preview(text, kind, str(body.get("var") or "x"))
        except SandboxUnavailable as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
        return JSONResponse({"ok": bool(res.get("ok")), "latex": res.get("latex", ""),
                             "error": res.get("error", "")})

    # --- 履歴 ---------------------------------------------------------------

    @app.get("/history")
    def history(request: Request, filter: str = "all") -> Response:  # noqa: A002
        with sessions() as s:
            rows = _history_rows(s)
        if filter == "wrong":
            rows = [r for r in rows if r["wrong"]]
        elif filter == "reported":
            rows = [r for r in rows if r["problem"].status == "reported"]
        return render(request, "history.html", rows=rows, filter=filter)

    # --- 状態 ---------------------------------------------------------------

    @app.get("/status")
    def status(request: Request, saved: int = 0) -> Response:
        try:
            installed = llm.list_models()
            ollama_error = ""
        except LLMUnavailable as exc:
            installed, ollama_error = [], str(exc)
        installed_names = {m["name"] for m in installed} | {m["name"].removesuffix(":latest") for m in installed}
        with sessions() as s:
            overrides = model_overrides(s)
        current = with_model_overrides(settings, overrides)
        missing = [m for m in current.models if m not in installed_names] if not ollama_error else []
        with sessions() as s:
            heartbeat = kv_get(s, "worker_heartbeat")
            worker_status = kv_get(s, "worker_status")
            refill_paused = kv_get(s, "refill_paused") == "1"
            jobs = s.execute(
                select(Job).where(Job.status.in_(("pending", "running"))).order_by(Job.id)
            ).scalars().all()
            stock = s.execute(
                select(Problem.topic_id, Problem.difficulty, func.count())
                .where(Problem.status == "ready", Problem.served_at.is_(None))
                .group_by(Problem.topic_id, Problem.difficulty)
            ).all()
            rejected = s.execute(
                select(Problem).where(Problem.status == "rejected").order_by(Problem.created_at.desc()).limit(20)
            ).scalars().all()
            counts = dict(s.execute(select(Problem.status, func.count()).group_by(Problem.status)).all())
        age = None
        if heartbeat:
            age = int((utcnow() - datetime.fromisoformat(heartbeat)).total_seconds())
        return render(
            request, "status.html", settings=settings, current=current, overrides=overrides,
            roles=MODEL_ROLES, installed=installed, missing=missing, saved=saved, ollama_error=ollama_error,
            sandbox_ok=sandbox.healthy(), heartbeat_age=age, worker_status=worker_status,
            refill_paused=refill_paused, jobs=jobs, stock=stock, rejected=rejected, counts=counts,
        )

    @app.post("/status/models")
    def save_models(request: Request, gen: str = Form(""), solve: str = Form(""),
                    review: str = Form("")) -> Response:
        chosen = {"gen": gen.strip(), "solve": solve.strip(), "review": review.strip()}
        try:
            names = {m["name"] for m in llm.list_models()}
        except LLMUnavailable as exc:
            return error_page(request, f"モデルを変更できません。{exc}", 503)
        unknown = [name for name in chosen.values() if name and name not in names]
        if unknown:
            return error_page(request, f"ダウンロードされていないモデルです: {', '.join(unknown)}", 400)
        with sessions.begin() as s:
            for role, name in chosen.items():
                kv_set(s, f"model_{role}", name)
        return redirect("/status?saved=1")

    @app.post("/status/refill")
    def toggle_refill(paused: str = Form("0")) -> Response:
        with sessions.begin() as s:
            kv_set(s, "refill_paused", "1" if paused == "1" else "0")
        return redirect("/status")

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)
        return error_page(request, str(exc.detail), exc.status_code)

    return app


def _history_rows(s: Session) -> list[dict[str, Any]]:
    """解いたことのある問題を、最後に解いた日時の新しい順に並べる。"""
    attempts = s.execute(
        select(Attempt).options(selectinload(Attempt.problem)).order_by(Attempt.created_at)
    ).scalars().all()
    grouped: dict[int, list[Attempt]] = defaultdict(list)
    for a in attempts:
        grouped[a.problem_id].append(a)
    rows = []
    for items in grouped.values():
        problem = items[0].problem
        latest = items[-1]
        rows.append({
            "problem": problem,
            "topic": get_topic(problem.topic_id),
            "attempts": items,
            "latest": latest,
            "count": len(items),
            "ever_correct": any(a.is_correct for a in items),
            "wrong": not latest.is_correct and problem.status != "reported",
        })
    rows.sort(key=lambda r: r["latest"].created_at, reverse=True)
    return rows
