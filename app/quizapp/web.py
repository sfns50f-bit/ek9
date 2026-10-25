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
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from . import backup, review, runtime, stock
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
from .subjects.base import DEVICES, FORMATS, LEVELS, Grade, Subject, join_scope, split_scope

BASE = Path(__file__).resolve().parent
MAX_SESSION = 50
MAX_JOB = 50
MAX_DURATION_MS = 3 * 60 * 60 * 1000
JOB_STATUS = {
    "pending": "順番待ち", "running": "処理中", "waiting": "Ollama を待っています", "done": "完了",
    "partial": "一部完了", "failed": "失敗", "cancelling": "取り消し中", "cancelled": "取り消し済み",
}


def _nomath(subject_id: str) -> str:
    """$…$ を数式として表示しない科目（英語の「$20」など）の要素に付ける class（app.js の renderMath が飛ばす）。"""
    sub = get_subject(subject_id)
    return "nomath" if sub is not None and not sub.math_text else ""


def _failure(problem: Problem) -> dict[str, Any]:
    """不合格の問題の、落ちた段階の記録（モデル名と生の出力を含む）。"""
    for entry in reversed(problem.verification):
        if not entry.get("ok"):
            return entry
    return {}


def _date_text(value: str) -> str:
    """「2026-10-21」の形の日付だけを通す（ほかは空）。"""
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").strftime("%Y-%m-%d") if value and value.strip() else ""
    except ValueError:
        return ""


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
    templates.env.filters["epochlocal"] = lambda t: localtime(
        datetime.fromtimestamp(t, tz=timezone.utc).replace(tzinfo=None), "%m/%d %H:%M:%S") if t else ""
    templates.env.filters["isolocal"] = lambda text: localtime(datetime.fromisoformat(text)) if text else ""
    templates.env.globals.update(FORMATS=FORMATS, DEVICES=DEVICES, LEVELS=LEVELS, subjects=all_subjects,
                                 get_subject=get_subject, JOB_STATUS=JOB_STATUS, failure=_failure, nomath=_nomath)

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
                 "subunits": [{"id": su.id, "label": su.label, "formats": list(su.formats), "level": su.level}
                              for su in u.subunits]}
                for u in subject.units()]

    def form_scope(subject: Subject, values: list[str]) -> str:
        """チェックした単元・小単元（"unit:…" / "tmpl:…"）を 1 つの範囲の文字列に。何もなければ科目全体（""）。

        単元を選ぶと画面ではその小単元にもチェックが付くので、選んだ単元に入る小単元は省く。
        """
        parts = [p for v in values for p in split_scope(v)]
        units = {p[5:] for p in parts if p.startswith("unit:")}
        found = subject.subunit_map()
        return join_scope([p for p in parts
                           if not (p.startswith("tmpl:") and p[5:] in found and found[p[5:]][0].id in units)])

    def form_levels(values: list[str]) -> str:
        levels = [v for v in values if v]
        if any(v not in LEVELS for v in levels):
            raise HTTPException(400, "レベルの選び方が正しくありません")
        return ",".join(dict.fromkeys(levels))

    def picker_data(s: Session, uid: int) -> dict[str, Any]:
        """解き始め・作成依頼の範囲の選び方と、小単元 × 形式ごとの未出題のストック数（画面の JavaScript が使う）。"""
        counts = stock.unseen_counts(s, uid)
        return {sub.id: {"name": sub.name, "units": scope_options(sub), "formats": list(sub.formats),
                         "counts": {f"{t}|{f}": n for (sid, t, f), n in counts.items() if sid == sub.id}}
                for sub in all_subjects()}

    def api_info(s: Session) -> dict[str, Any]:
        """API の設定（worker が kv に書いたもの）と、今月の利用額・上限。"""
        info = kv_json(s, "api", {}) or {}
        monthly, per_job = runtime.api_limits(settings, s)
        spent = runtime.api_spend(s).get(runtime.month_key(settings), 0.0)
        return {"configured": bool(info.get("configured")), "models": list(info.get("models") or []),
                "monthly": monthly, "per_job": per_job, "spent": spent, "month": runtime.month_key(settings),
                "worker": kv_json(s, "worker_api", {}) or {}}

    def review_ok(request: Request) -> bool:
        return review.allowed(settings.review_password, request.cookies.get(review.COOKIE))

    # --- ホーム（解き始め） --------------------------------------------------

    @app.get("/")
    def home(request: Request, msg: str = "") -> Response:
        uid = user_id(request)
        with sessions() as s:
            summary = stock.stock_summary(s, uid, settings.low_stock)
            data = picker_data(s, uid)
        return render(request, "home.html", summary=summary, data=data,
                      device=request.cookies.get("device", "phone"), msg=msg)

    @app.post("/start")
    def start(request: Request, subject: str = Form(...), scope: Annotated[list[str], Form()] = [],  # noqa: B006
              levels: Annotated[list[str], Form()] = [], device: str = Form("phone"),  # noqa: B006
              count: str = Form("20")) -> Response:
        sub = subject_or_404(subject)
        scope_s, levels_s = form_scope(sub, scope), form_levels(levels)
        if device not in DEVICES or not sub.scope_valid(scope_s):
            raise HTTPException(400, "選び方が正しくありません")
        count_n = max(1, min(MAX_SESSION, _int(count, 20)))
        with sessions.begin() as s:
            ids = stock.build_session(s, user_id(request), sub, scope_s, device, count_n, settings.retry_percent,
                                      levels=levels_s)
            if not ids:
                return redirect("/?msg=empty")
            row = StudySession(user_id=user_id(request), subject=sub.id, scope=scope_s, levels=levels_s,
                               device=device, problem_ids_json=dumps(ids), requested=count_n)
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
            _ = problem.passage
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
    def problem_page(request: Request, pid: int, msg: str = "", review_mode: int = Query(0, alias="review")) -> Response:
        with sessions() as s:
            problem = problem_or_404(s, pid)
            attempts = list(s.execute(select(Attempt).where(Attempt.problem_id == pid,
                                                            Attempt.user_id == user_id(request))
                                      .order_by(Attempt.id.desc())).scalars())
            # まだ解いていない問題は、正解が見えてしまうので問題検証モードでだけ見せる
            # （問題検証モードの一覧からのリンクには ?review=1 が付く。パスワードがあればログインも要る）。
            # ほかに見せてよいのは出題されない問題：不合格（同じ数値の問題がストックにある duplicate は除く）・
            # 削除・検証のテスト・報告済み。検証待ちの下書きはまもなくストックに入るので見せない
            hidden = problem.reported or (problem.status in ("rejected", "deleted", "test")
                                          and problem.reject_code != "duplicate")
            if not hidden and not attempts and not (review_mode and review_ok(request)):
                if review_mode:
                    return redirect("/review/login")
                raise HTTPException(404, "まだ解いていない問題です（正解つきの一覧は問題検証モードで見られます）")
            _ = problem.passage
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
            data = picker_data(s, user_id(request))
            plans = {sub.id: runtime.model_plan(settings, s, sub) for sub in all_subjects()}
            ollama = kv_json(s, "ollama", {}) or {}
            api = api_info(s)
        # 候補：Ollama にあるモデルと、API のモデル（「anthropic/…」）
        model_names = [m["name"] for m in ollama.get("models", [])] + api["models"]
        return render(request, "generate.html", jobs=jobs, data=data, msg=msg, plans=plans, model_names=model_names,
                      max_verify=runtime.MAX_VERIFY_COUNT, api=api)

    def no_subunit_message(sub: Subject, scope: str, fmt: str, levels: str) -> str:
        """作れる小単元がないときの説明。選んだ範囲の小単元ごとに、使える形式とレベルを並べる。

        例：「スペル：キーボード入力・高校基礎の小単元です（選んだのは 選択式）」"""
        smap = sub.subunit_map()
        wanted = [lv for lv in levels.split(",") if lv]
        lines = []
        for tid in sub.templates_for(scope):
            unit, su = smap[tid]
            reasons = []
            if fmt not in su.formats:
                reasons.append(f"形式は{'・'.join(FORMATS[f] for f in su.formats)}だけ（選んだのは{FORMATS.get(fmt, fmt)}）")
            if wanted and su.level not in wanted:
                reasons.append(f"レベルは{LEVELS.get(su.level, su.level)}（選んだのは{'・'.join(LEVELS.get(w, w) for w in wanted)}）")
            if reasons:
                lines.append(f"{unit.label}／{su.label}：" + "、".join(reasons))
        return ("この範囲・形式・レベルに当てはまる小単元がありません。" + ("\n" + "\n".join(lines[:12]) if lines else ""))

    @app.post("/generate")
    async def generate(request: Request) -> Response:
        form = await request.form()
        subject = str(form.get("subject", ""))
        sub = subject_or_404(subject)
        scope_s = form_scope(sub, [str(v) for v in form.getlist("scope")])
        levels_s = form_levels([str(v) for v in form.getlist("levels")])
        fmt = str(form.get("format", "choice"))
        if fmt not in sub.formats or not sub.scope_valid(scope_s):
            raise HTTPException(400, "選び方が正しくありません")
        if not sub.templates_for(scope_s, fmt, levels_s):
            raise HTTPException(400, no_subunit_message(sub, scope_s, fmt, levels_s))

        def field(name: str) -> str:
            """その科目の欄（m_<科目>_<名前>）。前の版の画面の欄の名前（頭なし）も読む。打ち込んだ名前を優先する"""
            for key in (f"m_{sub.id}_{name}_text", f"{name}_text", f"m_{sub.id}_{name}", name):
                value = str(form.get(key, "")).strip()
                if value:
                    return value[:128]
            return ""

        with sessions.begin() as s:
            # 依頼ごとにモデルを記録する（空の欄は「状態」のその科目の設定）。後で設定を変えても、この依頼はこのモデルで最後まで処理する
            plan = runtime.model_plan(settings, s, sub)
            review_shown = field("review_shown")
            v2 = field("verify_model_2")
            gen_model = field("gen_model") or plan.gen_model
            # 科目だけの役割：欄で選んだモデル → 科目の設定 → この依頼の作成役（「作成役と同じ」）
            extra = {role: field(f"role_{role}") or dict(plan.extra).get(role) or gen_model for role in sub.model_roles}
            verify_model = field("verify_model") or plan.verify_model
            review_model = field("review_model") or plan.review_model
            chosen = runtime.ModelPlan(
                gen_model=gen_model,
                verify_model=verify_model,
                verify_count=max(1, min(runtime.MAX_VERIFY_COUNT, _int(field("verify_count"), plan.verify_count))),
                verify_model_2="" if v2 == "-" else (v2 or plan.verify_model_2),
                # チェックを外すと送られないので、欄を出したこと（review_shown）で見分ける
                review=(field("review") == "on") if review_shown else plan.review,
                review_model="" if review_model == verify_model else review_model,
                extra=tuple(sorted((r, m) for r, m in extra.items() if m)),
            )
            job = Job(subject=sub.id, scope=scope_s, levels=levels_s, format=fmt,
                      count=max(1, min(MAX_JOB, _int(str(form.get("count", "10")), 10))))
            runtime.apply_plan(job, chosen)
            runtime.record_think(settings, s, job, chosen)  # 思考モードも依頼したときの設定で最後まで
            s.add(job)
        return redirect("/generate?msg=queued")

    @app.post("/jobs/{job_id}/cancel")
    def cancel(job_id: int, back: str = Form("/generate")) -> Response:
        with sessions.begin() as s:
            job = s.get(Job, job_id)
            if job and job.status in ("pending", "waiting"):  # まだ始まっていない（止まっている）依頼はすぐ取り消し済み
                job.status, job.progress, job.error, job.finished_at = "cancelled", "", "取り消しました", utcnow()
            elif job and job.status == "running":
                # 処理中の依頼は worker が数秒以内に止める（Ollama への問い合わせも途中で切る）
                job.status, job.progress = "cancelling", "取り消し中（worker が止まるのを待っています）"
        return redirect(back if back.startswith("/") and not back.startswith("//") else "/generate")

    @app.get("/jobs.json")
    def jobs_json() -> dict[str, Any]:
        with sessions() as s:
            jobs = s.execute(select(Job).where(Job.status.in_(("pending", "running", "waiting", "cancelling"))))\
                .scalars()
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
                                   .where(Job.status.in_(("pending", "running", "waiting", "cancelling"))))
            .scalar_one(),
            "backup_last": localtime(last_backup, "%Y/%m/%d %H:%M") if last_backup else None,
            "backup_count": len(backup.list_backups(settings.backup_dir)),
            "backup_info": kv_json(s, "backup", {}) or {},
            "api": api_info(s),
        }

    STATUS_PAGES = {"/status": "まとめ", "/status/models": "モデルと思考モード", "/status/quality": "検証の成績",
                    "/status/llm": "LLM の呼び出し", "/review": "問題検証モード"}
    templates.env.globals["STATUS_PAGES"] = STATUS_PAGES

    @app.get("/status")
    def status(request: Request, msg: str = "") -> Response:
        uid = user_id(request)
        with sessions() as s:
            data = status_data(s, uid)
            plan = runtime.model_plan(settings, s)
            jobs = list(s.execute(select(Job).order_by(Job.id.desc()).limit(15)).scalars())
            per_template = stock.per_template_rejects(s)
            templ = stock.template_stock(s, uid)
            reported = list(s.execute(select(Problem).where(Problem.reported.is_(True))
                                      .order_by(Problem.reported_at.desc())).scalars())
        return render(request, "status.html", page="/status", **data, plan=plan, jobs=jobs,
                      per_template=per_template, templ=templ, reported=reported, sandbox_ok=sandbox.healthy(),
                      settings=settings, msg=msg)

    @app.get("/status/models")
    def status_models(request: Request, msg: str = "") -> Response:
        with sessions() as s:
            plan = runtime.model_plan(settings, s)
            overrides = runtime.overrides(s)
            subject_settings = runtime.subject_settings(s)
            plans = {sub.id: runtime.model_plan(settings, s, sub) for sub in all_subjects()}
            today = runtime.api_daily(s).get(runtime.day_key(settings), {})
            think_choices = runtime.think_settings(s)
            ollama = kv_json(s, "ollama", {}) or {}
            tests = list(s.execute(select(Job).where(Job.is_test.is_(True)).order_by(Job.id.desc()).limit(5))
                         .scalars())
            api = api_info(s)
        ollama_names = [m["name"] for m in ollama.get("models", [])]
        model_names = ollama_names + api["models"]
        # 思考モードの一覧は、Ollama に今あるモデルだけ（保存した設定は消さないので、入れ直せば同じ設定が効く）
        think_rows = []
        for name in ollama_names:
            effective = runtime.resolve_think(name, think_choices, settings.think)
            think_rows.append({"model": name, "choice": "on" if effective else "off",
                               "saved": name in think_choices})
        return render(request, "status_models.html", page="/status/models", plan=plan, overrides=overrides,
                      think_rows=think_rows, think_labels=runtime.THINK_CHOICES, model_names=model_names,
                      subject_settings=subject_settings, plans=plans, today=today, roles=runtime.ROLES,
                      daily_keys=runtime.DAILY_KEYS,
                      ollama=ollama, tests=tests, settings=settings, max_verify=runtime.MAX_VERIFY_COUNT, msg=msg,
                      api=api)

    @app.post("/status/api")
    def save_api(monthly: str = Form(""), per_job: str = Form("")) -> Response:
        """API の利用額の上限（ドル）。空の欄は変えない。"""
        with sessions.begin() as s:
            runtime.save_api_limits(s, monthly.strip(), per_job.strip())
        return redirect("/status/models?msg=api#api")

    @app.get("/status/quality")
    def status_quality(request: Request, days: str = "30", from_job: str = "") -> Response:
        days_n, from_n = max(0, _int(days, 30)), max(0, _int(from_job, 0))
        with sessions() as s:
            stats = stock.verify_stats(s, days_n, from_n)
            job_rows = stock.job_stats(s, days_n, from_n)
            rejected = stock.recent_rejected(s, days_n, from_n)
        codes = {}
        for sub in all_subjects():
            codes.update(sub.all_reject_codes())
        return render(request, "status_quality.html", page="/status/quality", stats=stats, job_rows=job_rows,
                      rejected=rejected, codes=codes, days=days_n, from_job=from_n or "")

    @app.get("/status/quality/export.md")
    def status_quality_export(days: str = "30", from_job: str = "", download: int = 0) -> Response:
        """「検証の成績」を Markdown で書き出す（第 5 回のレビュー E-1）。"""
        days_n, from_n = max(0, _int(days, 30)), max(0, _int(from_job, 0))
        with sessions() as s:
            stats = stock.verify_stats(s, days_n, from_n)
            job_rows = stock.job_stats(s, days_n, from_n)
            rejected = stock.recent_rejected(s, days_n, from_n)
        codes = {}
        for sub in all_subjects():
            codes.update(sub.all_reject_codes())
        period = {1: "直近 24 時間", 7: "7 日", 30: "30 日", 0: "すべて"}.get(days_n, f"{days_n} 日")
        if from_n:
            period += f"・依頼 #{from_n} 以降"
        text = review.quality_markdown(stats, job_rows, rejected, codes, period, when=lambda dt: localtime(dt))
        headers = {"Content-Disposition": 'attachment; filename="quality.md"'} if download else {}
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8", headers=headers)

    @app.get("/review/rereview.md")
    def review_rereview_table(request: Request, subject: str = "", unit: str = "", level: str = "",
                              gen_model: str = "", status: str = "ready", date_from: str = "", date_to: str = "",
                              id_from: str = "", id_to: str = "", download: int = 0) -> Response:
        """見直しのやり直しの比較表（問題 × 見直し役）。絞り込みは問題検証モードと同じ（E-2）。"""
        if not review_ok(request):
            return redirect("/review/login")
        f = review_filters(subject, unit, level, gen_model, status, date_from, date_to, id_from, id_to)
        with sessions() as s:
            rows = review.query(s, **review_query_args(f), limit=500)
            text = review.rereview_table(rows)
        headers = {"Content-Disposition": 'attachment; filename="rereview.md"'} if download else {}
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8", headers=headers)

    @app.get("/status/llm")
    def status_llm(request: Request) -> Response:
        with sessions() as s:
            llm_calls = list(reversed(kv_json(s, "llm_calls", []) or []))
        return render(request, "status_llm.html", page="/status/llm", llm_calls=llm_calls)

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
                    verify_model_2: str = Form(""), review: str = Form("on"), review_model: str = Form("")) -> Response:
        with sessions.begin() as s:
            runtime.save_overrides(s, {"gen_model": gen_model, "verify_model": verify_model,
                                       "verify_count": verify_count, "verify_model_2": verify_model_2,
                                       "review": "off" if review == "off" else "on", "review_model": review_model})
        return redirect("/status/models?msg=saved#models")

    @app.post("/status/subject-models")
    async def save_subject_models(request: Request) -> Response:
        """科目ごとの上書き（役割ごとのモデルと API の 1 日の上限）。空の欄は全体の設定。「既定値に戻す」で全部消す。"""
        form = await request.form()
        sub = subject_or_404(str(form.get("subject", "")))
        keys = [*runtime.ROLES, *sub.model_roles, *runtime.DAILY_KEYS]
        values = {} if form.get("action") == "reset" else {k: str(form.get(k, "")) for k in keys}
        with sessions.begin() as s:
            runtime.save_subject_settings(s, sub.id, values)
        return redirect(f"/status/models?msg=subject#subject-{sub.id}")

    @app.post("/status/think")
    def save_think(model: str = Form(...), choice: str = Form("off")) -> Response:
        if choice not in runtime.THINK_CHOICES:
            raise HTTPException(400, "選び方が正しくありません")
        with sessions.begin() as s:
            runtime.save_think_setting(s, model.strip(), choice)
        return redirect("/status/models?msg=think#think")

    @app.post("/status/test")
    def verify_test(target: str = Form("")) -> Response:
        """検証のテスト：わざと正解をずらした問題を 1 問流す（ストックには入らない）。

        target は "<科目ID>" か "<科目ID>|<小単元ID>"。テストに対応する科目（test_format あり）だけ。
        """
        subject_id, _, template = target.partition("|")
        sub = get_subject(subject_id)
        scope = f"tmpl:{template}" if template else ""
        if sub is None or not sub.test_format or not sub.scope_valid(scope):
            raise HTTPException(400, "選び方が正しくありません")
        with sessions.begin() as s:
            job = Job(subject=sub.id, scope=scope, format=sub.test_format, count=1, is_test=True)
            plan = runtime.model_plan(settings, s, sub)
            runtime.apply_plan(job, plan)
            runtime.record_think(settings, s, job, plan)
            s.add(job)
        return redirect("/status/models?msg=test#tests")

    # --- 問題検証モード（親向け） ---------------------------------------------

    def review_filters(subject: str, unit: str, level: str, gen_model: str, status: str,
                       date_from: str = "", date_to: str = "", id_from: str = "", id_to: str = "") -> dict[str, str]:
        return {"subject": subject, "unit": unit, "level": level, "gen_model": gen_model,
                "status": status if status in review.STATUSES else "ready",
                "date_from": _date_text(date_from), "date_to": _date_text(date_to),
                "id_from": str(max(0, _int(id_from, 0)) or ""), "id_to": str(max(0, _int(id_to, 0)) or "")}

    def review_query_args(f: dict[str, str]) -> dict[str, Any]:
        """画面の絞り込み（日付は日本時間の日）を review.query の引数に。"""
        def utc(day: str, plus: int = 0) -> datetime | None:
            if not day:
                return None
            local = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz) + timedelta(days=plus)
            return local.astimezone(timezone.utc).replace(tzinfo=None)

        return {"subject": f["subject"], "unit": f["unit"], "level": f["level"], "gen_model": f["gen_model"],
                "status": f["status"], "since": utc(f["date_from"]), "until": utc(f["date_to"], 1),
                "id_from": _int(f["id_from"], 0), "id_to": _int(f["id_to"], 0)}

    @app.get("/review")
    def review_page(request: Request, subject: str = "", unit: str = "", level: str = "", gen_model: str = "",
                    status: str = "ready", date_from: str = "", date_to: str = "", id_from: str = "",
                    id_to: str = "", page: int = 1, msg: str = "", n: int = 0) -> Response:
        if not review_ok(request):
            return redirect("/review/login")
        f = review_filters(subject, unit, level, gen_model, status, date_from, date_to, id_from, id_to)
        per = 50
        page = max(1, page)
        with sessions() as s:
            rows = review.query(s, **review_query_args(f), limit=per + 1, offset=(page - 1) * per)
            for p in rows:
                _ = p.passage, p.job
            options = review.filter_options(s)
            ollama = kv_json(s, "ollama", {}) or {}
            model_names = [m["name"] for m in ollama.get("models", [])] + api_info(s)["models"]
            reviewer = runtime.model_plan(settings, s, get_subject(subject)).reviewer
        return render(request, "review.html", page="/review", problems=rows[:per], more=len(rows) > per,
                      page_no=page, f=f, options=options, labels=review.labels, mistake=review.mistake_label,
                      issues=review.text_issues, think=review.think_text, locked=bool(settings.review_password),
                      msg=msg, n=n, model_names=model_names, reviewer=reviewer, rereviews=review.rereviews,
                      max_rereview=review.MAX_REREVIEW)

    @app.post("/review/rereview")
    def review_rereview(request: Request, subject: str = Form(""), unit: str = Form(""), level: str = Form(""),
                        gen_model: str = Form(""), status: str = Form("ready"), date_from: str = Form(""),
                        date_to: str = Form(""), id_from: str = Form(""), id_to: str = Form(""),
                        model: str = Form(""), model_text: str = Form(""), limit: str = Form("20"),
                        with_passage: str = Form("")) -> Response:
        """見直しだけをやり直す依頼：絞り込んだ問題（新しい順に limit 問まで）を、選んだモデルで見直す。

        結果は各問題の記録に足すだけで、合否は変えない（見直し役のモデルを比べる・前に作った問題を点検し直すため）。
        """
        if not review_ok(request):
            return redirect("/review/login")
        f = review_filters(subject, unit, level, gen_model, status, date_from, date_to, id_from, id_to)
        count = max(1, min(review.MAX_REREVIEW, _int(limit, 20)))
        chosen = (model_text.strip() or model.strip())[:128]
        if not chosen:
            raise HTTPException(400, "見直しに使うモデルを選んでください")
        with sessions.begin() as s:
            rows = review.query(s, **review_query_args(f), limit=count)
            if not rows:
                return redirect("/review?msg=rereview_empty")
            subjects_ = [p.subject for p in rows]
            main = max(set(subjects_), key=subjects_.count)  # 依頼の一覧に出す科目（いちばん多いもの）
            plan = runtime.ModelPlan(gen_model=chosen, verify_model=chosen, verify_count=1, verify_model_2="",
                                     review=True, review_model="")
            job = Job(subject=main, scope="", format="", count=len(rows), kind="review",
                      target_ids_json=json.dumps([p.id for p in rows]), review_passage=with_passage == "on")
            runtime.apply_plan(job, plan)
            runtime.record_think(settings, s, job, plan)
            s.add(job)
        return redirect(f"/review?msg=rereview&n={len(rows)}")

    @app.get("/review/export.md")
    def review_export(request: Request, subject: str = "", unit: str = "", level: str = "", gen_model: str = "",
                      status: str = "ready", date_from: str = "", date_to: str = "", id_from: str = "",
                      id_to: str = "", download: int = 0) -> Response:
        if not review_ok(request):
            return redirect("/review/login")
        f = review_filters(subject, unit, level, gen_model, status, date_from, date_to, id_from, id_to)
        with sessions() as s:
            rows = review.query(s, **review_query_args(f), limit=500)
            for p in rows:
                _ = p.passage, p.job
            shown = {"科目": get_subject(subject).name if get_subject(subject) else "",
                     "単元": next((u.label for sub in all_subjects() for u in sub.units() if u.id == unit), unit),
                     "レベル": LEVELS.get(level, ""), "作成役": gen_model,
                     "状態": review.STATUSES[f["status"]] if f["status"] != "ready" else "",
                     "作成日": "〜".join([f["date_from"], f["date_to"]]) if f["date_from"] or f["date_to"] else "",
                     "管理番号": f"#{f['id_from'] or '?'}〜#{f['id_to'] or '?'}" if f["id_from"] or f["id_to"] else ""}
            text = review.to_markdown(rows, shown, lambda dt: localtime(dt, "%Y/%m/%d %H:%M"))
        headers = {"Content-Disposition": 'attachment; filename="problems.md"'} if download else {}
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8", headers=headers)

    @app.post("/review/normalize")
    def review_normalize(request: Request) -> Response:
        """前に作った問題の表記をまとめて直す（作成のときと同じ機械的な直し。LLM は使わない）。"""
        if not review_ok(request):
            return redirect("/review/login")
        with sessions.begin() as s:
            n = review.normalize_all(s)
        return redirect(f"/review?status=issues&msg=normalized&n={n}")

    @app.post("/review/delete-ids")
    def review_delete_ids(request: Request, ids: str = Form("")) -> Response:
        if not review_ok(request):
            return redirect("/review/login")
        with sessions.begin() as s:
            done = review.delete_ids(s, ids)
        return redirect(f"/review?msg=deleted_ids&n={len(done)}")

    @app.post("/review/delete-issues")
    def review_delete_issues(request: Request) -> Response:
        """表記の点検で引っかかるストックの問題をまとめて削除する（作り直しは「作る」から依頼する）。"""
        if not review_ok(request):
            return redirect("/review/login")
        with sessions.begin() as s:
            hits = review.query(s, status="issues", limit=100000)
            for p in hits:
                p.status = "deleted"
        return redirect(f"/review?msg=deleted_ids&n={len(hits)}")

    @app.post("/review/{pid}/delete")
    def review_delete(request: Request, pid: int, back: str = Form("/review")) -> Response:
        """削除は status を deleted にするだけ（出題しない。回答の記録は残す）。"""
        if not review_ok(request):
            return redirect("/review/login")
        with sessions.begin() as s:
            problem = problem_or_404(s, pid)
            if problem.status == "ready":
                problem.status = "deleted"
        target = back if back.startswith("/review") else "/review"
        return redirect(target + ("&" if "?" in target else "?") + "msg=deleted")

    @app.post("/review/{pid}/restore")
    def review_restore(request: Request, pid: int, back: str = Form("/review")) -> Response:
        if not review_ok(request):
            return redirect("/review/login")
        with sessions.begin() as s:
            problem = problem_or_404(s, pid)
            if problem.status == "deleted":
                problem.status = "ready"
        target = back if back.startswith("/review") else "/review"
        return redirect(target + ("&" if "?" in target else "?") + "msg=restored")

    @app.get("/review/login")
    def review_login_page(request: Request, msg: str = "") -> Response:
        if review_ok(request):
            return redirect("/review")
        return render(request, "review_login.html", msg=msg)

    @app.post("/review/login")
    def review_login(password: str = Form("")) -> Response:
        if not review.allowed(settings.review_password, review.token(password)):
            return redirect("/review/login?msg=wrong")
        resp = redirect("/review")
        resp.set_cookie(review.COOKIE, review.token(password), max_age=30 * 24 * 3600, httponly=True,
                        samesite="lax")
        return resp

    @app.post("/review/logout")
    def review_logout() -> Response:
        resp = redirect("/")
        resp.delete_cookie(review.COOKIE)
        return resp

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
