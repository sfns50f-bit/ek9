"""作成依頼を処理するワーカー。LLM を呼ぶのはここだけ。

1 回の依頼は次の順に処理する（GPU 8GB でモデルの入れ替えを減らすため）:
  1. 作成フェーズ：必要な数の下書き（問題文・解説・ヒント）を作成用モデルで全部作る
  2. 検証フェーズ：検証役のモデルで、全部の下書きをまとめて解かせる（モデルごとに続けて）
  3. 足りなければ、作り直しの上限まで 1〜2 を繰り返す
下書きはデータベースに保存しながら進むので、途中で止まっても続きから再開できる。

Ollama につながらないときは落ちずに待ち、OLLAMA_RETRY_SECONDS ごとに再試行する。
あわせて、Ollama の状態の確認（/status 用）と定期バックアップもここで行う。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload, sessionmaker

from . import backup
from .config import Settings
from .datadir import ensure_or_exit
from .db import Job, Passage, Problem, dumps, init_db, kv_json, kv_set, utcnow
from .llm import (
    LLMBudgetExceeded,
    LLMCancelled,
    LLMError,
    LLMModelMissing,
    LLMRouter,
    LLMUnavailable,
    OllamaClient,
)
from .runtime import (
    ModelPlan,
    add_api_daily,
    add_api_spend,
    api_daily,
    api_limits,
    api_spend,
    apply_plan,
    daily_limits,
    day_key,
    month_key,
    plan_for_job,
    provider_of,
    resolve_think,
    think_settings,
)
from .sandbox_client import SandboxClient, SandboxUnavailable
from .subjects import get_subject
from .subjects.base import Draft, GenContext, Rejected, Subject, Subunit, VerifyResult

log = logging.getLogger("worker")

OPEN_STATUSES = ("pending", "running", "waiting")
CANCEL_STATUSES = ("cancelling", "cancelled")
OLLAMA_CHECK_SECONDS = 60
HEARTBEAT_SECONDS = 60
LLM_CALLS_KEEP = 40
VERIFY_STEP = "検証"
REVIEW_STEP = "解説の見直し"
# Ollama の依頼と API の依頼は別のスレッドで同時に処理する。kv の読み書き（呼び出しの記録・利用額）は 1 つずつ
_KV_LOCK = threading.Lock()
# 順番待ちの分け方：ollama＝作成役・検証役のどれかが Ollama のモデル、api＝全部 API のモデル、None＝両方（テスト用）
LANES = ("ollama", "api")
TEST_ATTEMPTS = 3  # 検証のテストで、検証の段階まで届く下書きを作るまでに試す回数


class Cancelled(Exception):
    pass


class Worker:
    def __init__(self, settings: Settings, sessions: sessionmaker[Session], llm: Any,
                 sandbox: SandboxClient, rng: random.Random | None = None, lane: str | None = None) -> None:
        self.lane = lane
        self.last_subject = ""  # 直前に処理した依頼の科目（同じ科目の依頼を続けて処理する）
        self.status_key = "worker_api" if lane == "api" else "worker"
        self.settings = settings
        self.sessions = sessions
        self.llm = llm
        self.sandbox = sandbox
        self.rng = rng or random.Random()
        self.unavailable_until = 0.0
        self.last_ollama_check = -1e9
        self.last_backup_check = -1e9
        self._last_status: tuple[str, int | None] | None = None
        self._last_status_at = -1e9

    # --- 状態表示 -----------------------------------------------------------

    def set_status(self, text: str, job_id: int | None = None) -> None:
        """状態を kv に書く。HDD への書き込みを減らすため、変わったときと 1 分ごとの生存確認だけ書く。"""
        now = time.monotonic()
        if (text, job_id) == self._last_status and now - self._last_status_at < HEARTBEAT_SECONDS:
            return
        self._last_status, self._last_status_at = (text, job_id), now
        with self.sessions.begin() as s:
            kv_set(s, self.status_key, dumps({"text": text, "job_id": job_id, "at": utcnow().isoformat()}))

    def check_ollama(self, force: bool = False) -> bool:
        """Ollama の状態とダウンロード済みモデルを kv に記録する（web はこれを表示するだけ）。"""
        now = time.monotonic()
        if not force:
            if now < self.unavailable_until:
                return False  # つながらなかった直後は、待ち時間が過ぎるまで試さない
            if now - self.last_ollama_check < OLLAMA_CHECK_SECONDS:
                return True
        self.last_ollama_check = now
        try:
            models = self.llm.list_models()
            info = {"ok": True, "models": models, "error": ""}
            self.unavailable_until = 0.0
        except LLMUnavailable as exc:
            info = {"ok": False, "models": [], "error": str(exc)}
            self.unavailable_until = now + self.settings.retry_seconds
        info["checked_at"] = utcnow().isoformat()
        with self.sessions.begin() as s:
            kv_set(s, "ollama", dumps(info))
        return bool(info["ok"])

    def maybe_backup(self) -> None:
        now = time.monotonic()
        if now - self.last_backup_check < 600:
            return
        self.last_backup_check = now
        if not backup.due(self.settings.backup_dir, self.settings.backup_interval_hours):
            return
        try:
            path = backup.make_backup(self.settings.database_path, self.settings.backup_dir,
                                      self.settings.backup_keep, tz=self.settings.timezone)
            log.info("backup written: %s", path)
            info = {"ok": True, "path": path, "error": ""}
        except Exception as exc:  # バックアップに失敗しても作成は続ける
            log.exception("backup failed")
            info = {"ok": False, "path": "", "error": str(exc)}
        info["at"] = utcnow().isoformat()
        with self.sessions.begin() as s:
            kv_set(s, "backup", dumps(info))

    def _label(self, text: str) -> None:
        if hasattr(self.llm, "label"):
            self.llm.label = text

    def _save_llm_calls(self, job_id: int | None = None) -> None:
        """LLM の呼び出しの時間の内訳を、新しいものから LLM_CALLS_KEEP 件まで kv に残す。

        job_id があれば、その依頼の作成・検証の時間（出力 eval と読み込み load を分けて）にも足す。
        """
        calls = self.llm.pop_calls() if hasattr(self.llm, "pop_calls") else []
        if not calls:
            return
        cost = sum(float(c.get("cost_usd") or 0) for c in calls)
        with _KV_LOCK, self.sessions.begin() as s:
            kept = (kv_json(s, "llm_calls", []) or []) + calls
            kv_set(s, "llm_calls", dumps(kept[-LLM_CALLS_KEEP:]))
            if cost:
                add_api_spend(self.settings, s, cost)  # 月ごとの API の利用額
            job = s.get(Job, job_id) if job_id is not None else None
            if job is None:
                return
            api_calls = sum(1 for c in calls if provider_of(str(c.get("model", "")))[0] != "ollama")
            if api_calls or cost:  # 科目ごと・日ごとの API の利用（科目の 1 日の上限に使う）
                add_api_daily(self.settings, s, job.subject, cost, api_calls)
            job.api_cost_usd = round((job.api_cost_usd or 0.0) + cost, 6)
            for c in calls:
                # 解説・本文の見直しは検証の時間に数える
                role = "verify" if c.get("label") in ("検証", "見直し", "本文の見直し") else "gen"
                setattr(job, f"{role}_calls", (getattr(job, f"{role}_calls") or 0) + 1)
                setattr(job, f"{role}_eval_s", (getattr(job, f"{role}_eval_s") or 0.0) + float(c.get("eval_s") or 0))
                setattr(job, f"{role}_load_s", (getattr(job, f"{role}_load_s") or 0.0) + float(c.get("load_s") or 0))

    # --- 依頼の読み書き ------------------------------------------------------

    def recover(self) -> None:
        """前回の停止時に処理中だった依頼を順番待ちに戻す（下書きは残っているので続きから）。

        取り消し中のまま止まった依頼は、取り消し済みにする（検証待ちの下書きは捨てる）。
        """
        with self.sessions.begin() as s:
            s.execute(update(Job).where(Job.status == "running").values(status="pending"))
            for job in s.execute(select(Job).where(Job.status == "cancelling")).scalars():
                self._discard_drafts(s, job.id)
                job.status, job.progress, job.finished_at = "cancelled", "", utcnow()
            # 前の版で作った問題には、レベル・関連する単元を小単元から書き込む（列を足しただけでは空のまま）
            for p in s.execute(select(Problem).where(Problem.level == "")).scalars():
                su = self._subunit(p.subject, p.template)
                if su is not None and su.level:
                    p.level, p.related_units = su.level, ",".join(su.related)

    def next_job_id(self, ollama_ok: bool = True) -> int | None:
        """次に処理する依頼。Ollama につながらないときは、API のモデルだけを使う依頼を先に進める。

        直前に処理した依頼と同じ科目の依頼があれば、そちらを先にする（科目を交互に作ると、科目ごとに
        モデルが違うときにローカルのモデルの入れ替えが増えるため）。なければ古い順。
        """
        with self.sessions() as s:
            ready: list[Job] = []
            for job in s.execute(select(Job).where(Job.status.in_(OPEN_STATUSES))
                                 .order_by(Job.created_at, Job.id)).scalars():
                needs = plan_for_job(self.settings, s, job).needs_ollama
                if self.lane == "api" and needs or self.lane == "ollama" and not needs:
                    continue  # もう一方の順番待ちの依頼
                if ollama_ok or not needs:
                    ready.append(job)
            if not ready:
                return None
            same = [j for j in ready if j.subject == self.last_subject]
            return (same or ready)[0].id

    def _job(self, job_id: int) -> Job:
        with self.sessions() as s:
            return s.get(Job, job_id)

    def _update_job(self, job_id: int, **values: object) -> None:
        """依頼を書き換える。取り消し中・取り消し済みの依頼の状態は変えない（進み具合などは書く）。"""
        with self.sessions.begin() as s:
            job = s.get(Job, job_id)
            if job.status in CANCEL_STATUSES:
                values.pop("status", None)
                if job.status == "cancelled":
                    return
            for key, value in values.items():
                setattr(job, key, value)

    def is_cancelled(self, job_id: int) -> bool:
        with self.sessions() as s:
            job = s.get(Job, job_id)
            return job is None or job.status in CANCEL_STATUSES

    def _check_cancel(self, job_id: int) -> None:
        if self.is_cancelled(job_id):
            raise Cancelled

    @staticmethod
    def _discard_drafts(s: Session, job_id: int) -> int:
        """検証が終わっていない下書きを捨てる（取り消したとき）。"""
        drafts = list(s.execute(select(Problem).where(Problem.job_id == job_id, Problem.status == "draft")).scalars())
        for p in drafts:
            s.delete(p)
        return len(drafts)

    def finish_cancel(self, job_id: int) -> None:
        """取り消しを終える。検証に合格した問題はストックに残し、検証待ちの下書きは捨てる。"""
        with self.sessions.begin() as s:
            self._discard_drafts(s, job_id)
            job = s.get(Job, job_id)
            made = s.execute(select(func.count()).select_from(Problem).where(
                Problem.job_id == job_id, Problem.status == "ready")).scalar_one()
            job.made = made
            job.status, job.progress, job.finished_at = "cancelled", "", utcnow()
            job.error = f"取り消しました（それまでに検証に合格した {made} 問はストックに残しました）" if made else "取り消しました"

    def _count(self, job_id: int, statuses: tuple[str, ...]) -> int:
        with self.sessions() as s:
            return s.execute(select(func.count()).select_from(Problem).where(
                Problem.job_id == job_id, Problem.status.in_(statuses))).scalar_one()

    def _drafts(self, job_id: int) -> list[Problem]:
        with self.sessions() as s:
            return list(s.execute(select(Problem).options(selectinload(Problem.passage))
                                  .where(Problem.job_id == job_id, Problem.status == "draft")
                                  .order_by(Problem.id)).scalars())

    def existing_keys(self, template: str) -> set[str]:
        with self.sessions() as s:
            return set(s.execute(select(Problem.params_key).where(
                Problem.template == template, Problem.status.in_(("ready", "draft", "test")))).scalars())

    # --- 保存 ---------------------------------------------------------------

    @staticmethod
    def _subunit(subject_id: str, template: str) -> Subunit | None:
        subject = get_subject(subject_id)
        found = subject.subunit_map().get(template) if subject else None
        return found[1] if found else None

    def save_drafts(self, job: Job, drafts: list[Draft], plan: ModelPlan,
                    cache: dict[int, Passage] | None = None) -> None:
        with self.sessions.begin() as s:
            passages = cache if cache is not None else {}
            for draft in drafts:
                self._add_draft(s, job, draft, plan, passages)

    def passages_for(self, template: str) -> list[dict[str, Any]]:
        """小単元の本文と、それについている設問（不合格・削除のものを除く）。本文の使い回しに使う。"""
        with self.sessions() as s:
            rows = list(s.execute(select(Passage).where(Passage.template == template).order_by(Passage.id)).scalars())
            questions: dict[int, list[str]] = {}
            rejected: dict[int, int] = {}
            for pid, q, status in s.execute(select(Problem.passage_id, Problem.question, Problem.status).where(
                    Problem.passage_id.in_([r.id for r in rows]))):
                if status in ("ready", "draft"):
                    questions.setdefault(pid, []).append(q)
                elif status == "rejected":
                    rejected[pid] = rejected.get(pid, 0) + 1
            return [{"id": r.id, "title": r.title, "body": r.body, "topic": r.topic, "status": r.status,
                     "gen_model": r.gen_model, "questions": questions.get(r.id, []), "rejected": rejected.get(r.id, 0)}
                    for r in rows]

    @staticmethod
    def _passage_row(s: Session, job: Job, p: dict[str, Any] | None, cache: dict[int, Passage]) -> Passage | None:
        """下書きの本文（dict）の行。同じ dict は 1 つの行にまとめ、{"id": …} はすでにある本文を使う。"""
        if p is None:
            return None
        passage = cache.get(id(p))
        if passage is not None and passage.id is not None and passage not in s:
            passage = s.get(Passage, passage.id)  # 前の書き込み（不合格の設問）で保存した本文
        if passage is None and p.get("id"):
            passage = s.get(Passage, int(p["id"]))
        if passage is None:
            passage = Passage(subject=job.subject, title=str(p.get("title", "")), body=str(p.get("body", "")),
                              source_title=str(p.get("source_title", "")), source_url=str(p.get("source_url", "")),
                              template=str(p.get("template", "")), topic=str(p.get("topic", "")),
                              status=str(p.get("status", "")), gen_model=str(p.get("gen_model", "")),
                              log_json=dumps(p.get("log", [])))
            s.add(passage)
        cache[id(p)] = passage
        return passage

    def _add_draft(self, s: Session, job: Job, draft: Draft, plan: ModelPlan, passages: dict[int, Passage]) -> None:
        passage = self._passage_row(s, job, draft.passage, passages)
        su = self._subunit(job.subject, draft.template)
        s.add(Problem(
            subject=job.subject, unit=draft.unit, template=draft.template, format=draft.format,
            level=su.level if su else "", related_units=",".join(su.related) if su else "",
            status="draft", job_id=job.id, passage=passage, question=draft.question,
            choices_json=dumps(draft.choices), answer_json=dumps(draft.answer), answer_display=draft.answer_display,
            input_hint=draft.input_hint, explanation=draft.explanation, hint=draft.hint, source=draft.source,
            params_json=dumps(draft.params), params_key=draft.params_key,
            verification_json=dumps(draft.log), gen_model=plan.gen_model,
        ))

    def save_rejected(self, job: Job, subject: Subject, template: str, exc: Rejected, plan: ModelPlan,
                      passage: dict[str, Any] | None = None, cache: dict[int, Passage] | None = None) -> None:
        """不合格を残す。本文つきの設問なら本文にも結び付ける（本文は後で設問を足すのに使い回せる）。"""
        p = exc.partial
        unit, su = subject.subunit_map().get(template, (None, None))
        with self.sessions.begin() as s:
            row = self._passage_row(s, job, passage, cache if cache is not None else {})
            s.add(Problem(passage=row,
                subject=job.subject, unit=unit.id if unit else "", template=template, format=job.format,
                level=su.level if su else "", related_units=",".join(su.related) if su else "",
                status="rejected", job_id=job.id, question=str(p.get("question", "")),
                explanation=str(p.get("explanation", "")), hint=str(p.get("hint", "")),
                choices_json=dumps(p.get("choices", [])), params_json=dumps(p.get("params", {})), params_key="",
                verification_json=dumps(exc.log), reject_code=exc.code, reject_reason=str(exc),
                gen_model=plan.gen_model,
            ))

    # --- 処理 ---------------------------------------------------------------

    def process(self, job_id: int) -> None:
        job = self._job(job_id)
        subject = get_subject(job.subject)
        if subject is None:
            self._finish(job_id, "failed", f"未知の科目です: {job.subject}")
            return
        templates = subject.templates_for(job.scope, job.format, job.levels)
        if not templates:
            self._finish(job_id, "failed", "この範囲・形式・レベルに対応する小単元がありません")
            return
        with self.sessions.begin() as s:
            row = s.get(Job, job_id)
            plan = plan_for_job(self.settings, s, row)
            if not row.gen_model:
                apply_plan(row, plan)
            choices = think_settings(s)
        if hasattr(self.llm, "think_for"):
            env_think = self.settings.think
            self.llm.think_for = lambda m: resolve_think(m, choices, env_think)
        budget = TEST_ATTEMPTS if job.is_test else job.count * self.settings.max_attempts
        done_statuses = ("test",) if job.is_test else ("ready",)
        self._check_cancel(job_id)
        self._update_job(job_id, status="running", error="")
        label = f"依頼 #{job_id} {subject.scope_label(job.scope)}"

        while True:
            job = self._job(job_id)
            made = self._count(job_id, done_statuses)
            drafts = self._drafts(job_id)
            need = job.count - made - len(drafts)
            if need > 0 and job.gen_attempts < budget:
                self._generate_phase(job, subject, templates, min(need, budget - job.gen_attempts), plan, label)
                drafts = self._drafts(job_id)
            self._check_cancel(job_id)  # 作成と検証の間でも確かめる
            if drafts:
                self._verify_phase(job, subject, drafts, plan, label)
            made = self._count(job_id, done_statuses)
            self._update_job(job_id, made=made)
            job = self._job(job_id)
            if made >= job.count or job.gen_attempts >= budget or (job.is_test and self._test_caught(job_id)):
                break

        if job.is_test:
            if made:
                self._finish(job_id, "failed", "検証のテスト：正解をずらした問題が検証に合格してしまいました。"
                                               "検証役のモデルや回数を見直してください")
            elif self._test_caught(job_id):
                self._finish(job_id, "done", "検証のテスト：正解をずらした問題は、期待どおり検証で弾かれました")
            else:
                with self.sessions() as s:
                    codes = sorted(set(s.execute(select(Problem.reject_code).where(Problem.job_id == job_id))
                                       .scalars()))
                self._finish(job_id, "failed", "検証のテスト：判定できませんでした。下書きが検証の段階まで届きませんでした"
                                               f"（理由: {'、'.join(c for c in codes if c) or '不明'}）。もう一度流してください")
        elif made >= job.count:
            self._finish(job_id, "done", "")
        elif made:
            self._finish(job_id, "partial", f"{job.count} 問中 {made} 問だけ検証に合格しました（作り直しの上限に達しました）")
        else:
            self._finish(job_id, "failed", "検証に合格した問題がありませんでした。/status の不合格の理由を確認してください")

    def _test_caught(self, job_id: int) -> bool:
        """検証のテストの下書きが、検証役との突き合わせで弾かれたか（文章の確認などで落ちたものは数えない）。"""
        with self.sessions() as s:
            return s.execute(select(func.count()).select_from(Problem).where(
                Problem.job_id == job_id, Problem.status == "rejected",
                Problem.reject_code.in_(("solver_mismatch", "solver_unreadable")))).scalar_one() > 0

    def _finish(self, job_id: int, status: str, message: str) -> None:
        self._update_job(job_id, status=status, error=message, progress="", finished_at=utcnow())

    def _generate_phase(self, job: Job, subject: Subject, templates: list[str], n: int,
                        plan: ModelPlan, label: str) -> None:
        ctx = GenContext(llm=self.llm, sandbox=self.sandbox, gen_model=plan.gen_model, rng=self.rng,
                         is_test=job.is_test, existing_keys=self.existing_keys, models=plan.models,
                         review=plan.review and not job.is_test, passages=self.passages_for)
        order = list(templates)
        self.rng.shuffle(order)
        produced = 0
        for i in range(n):
            if produced >= n:  # 1 回で複数問（同じ本文の設問）を作る科目もある
                break
            self._check_cancel(job.id)
            template = order[(job.gen_attempts + i) % len(order)]
            text = f"作成中 {i + 1}/{n}（{plan.gen_model}）"
            self._update_job(job.id, progress=text)
            self.set_status(f"{label} — {text}", job.id)
            # 先に回数を数える（Ollama が落ちて再開しても、上限を超えて作り続けない）
            with self.sessions.begin() as s:
                s.get(Job, job.id).gen_attempts += 1
            self._label("作成")
            try:
                made = subject.generate(ctx, template, job.format)
            except Rejected as exc:
                self.save_rejected(job, subject, template, exc, plan)
                continue
            except LLMError as exc:
                entry = {"step": "文章", "ok": False, "summary": f"作成役の出力を読めません（{exc}）",
                         "model": plan.gen_model, "raw": getattr(exc, "raw", "")[:4000]}
                self.save_rejected(job, subject, template, Rejected("llm_output", str(exc), [entry]), plan)
                continue
            except LLMUnavailable:
                with self.sessions.begin() as s:  # 作れなかった分は数えない
                    s.get(Job, job.id).gen_attempts -= 1
                raise
            finally:
                self._save_llm_calls(job.id)
            drafts = made if isinstance(made, list) else [made]
            # 同じ本文の設問のうち、作成の段階で使えないと分かったものは不合格として残す（ほかは検証へ）。
            # 本文は同じ行にまとめる（不合格の設問だけの本文も残し、後で設問を足せるようにする）
            cache: dict[int, Passage] = {}
            for d in drafts:
                if d.rejected is not None:
                    self.save_rejected(job, subject, d.template, d.rejected, plan, d.passage, cache)
            drafts = [d for d in drafts if d.rejected is None]
            self.save_drafts(job, drafts, plan, cache)
            produced += len(drafts)

    def _verify_phase(self, job: Job, subject: Subject, drafts: list[Problem], plan: ModelPlan,
                      label: str) -> None:
        """下書きをまとめて検証する。取り消し・Ollama の停止で途中で抜けても、
        それまでに全回合格した下書きはストックに入れ、不合格が決まったものは不合格にする。"""
        logs = {d.id: [e for e in d.verification if e.get("step") not in (VERIFY_STEP, REVIEW_STEP)] for d in drafts}
        alive = {d.id: d for d in drafts}
        failed: dict[int, tuple[str, str]] = {}
        # 検証役に解かせる回数分 → 全回一致したものだけ、検証役のモデルで解説の見直しを 1 回（検証のテストではしない）
        steps = [(model, VERIFY_STEP) for model in plan.verify_runs]
        if plan.review and not job.is_test:
            steps.append((plan.reviewer, REVIEW_STEP))
        done = {d.id: 0 for d in drafts}  # 終わった段階の数
        try:
            for r, (model, step) in enumerate(steps, 1):
                for k, (pid, problem) in enumerate(list(alive.items()), 1):
                    self._check_cancel(job.id)
                    what = "解説の見直し" if step == REVIEW_STEP else f"検証中 {r}/{len(plan.verify_runs)} 回目"
                    text = f"{what} {k}/{len(alive)}（{model}）"
                    self._update_job(job.id, progress=text)
                    self.set_status(f"{label} — {text}", job.id)
                    self._label("見直し" if step == REVIEW_STEP else "検証")
                    try:
                        if step == REVIEW_STEP:
                            res = subject.review(self.llm, model, problem) or VerifyResult(True, "見直しなし")
                        else:
                            res = subject.verify(self.llm, self.sandbox, model, problem)
                        ok, summary, detail, code, raw = res.ok, res.summary, res.detail, res.code, res.raw
                    except LLMError as exc:
                        ok, summary, detail, code = False, f"{model}: 出力を読めません（{exc}）", "", "llm_output"
                        raw = getattr(exc, "raw", "")
                    finally:
                        self._save_llm_calls(job.id)
                    logs[pid].append({"step": step, "ok": ok, "summary": summary, "detail": detail,
                                      "model": model, "run": r, "code": "" if ok else code, "raw": raw[:4000]})
                    done[pid] += 1
                    if not ok:
                        failed[pid] = (code, summary)
                        del alive[pid]
        finally:
            self._finalize_verified(job, drafts, logs, failed, done, len(steps), plan)

    def _finalize_verified(self, job: Job, drafts: list[Problem], logs: dict[int, list[dict[str, Any]]],
                           failed: dict[int, tuple[str, str]], done: dict[int, int], n_runs: int,
                           plan: ModelPlan) -> None:
        final = "test" if job.is_test else "ready"
        with self.sessions.begin() as s:
            for d in drafts:
                passed = d.id not in failed and done[d.id] >= n_runs
                if d.id not in failed and not passed:
                    continue  # 検証の途中（取り消し・Ollama の停止）。下書きのまま残す
                row = s.get(Problem, d.id)
                row.verification_json = dumps(logs[d.id])
                row.verify_models = plan.verify_label
                row.verified_at = utcnow()
                if d.id in failed:
                    row.status = "rejected"
                    row.reject_code, row.reject_reason = failed[d.id]
                elif final == "ready" and row.params_key and s.execute(select(func.count()).select_from(Problem).where(
                        Problem.params_key == row.params_key, Problem.status == "ready",
                        Problem.id != row.id)).scalar_one():
                    # テンプレートと数値が同じ問題がすでにストックにある
                    row.status, row.reject_code = "rejected", "duplicate"
                    row.reject_reason = "テンプレートと数値が同じ問題がすでにストックにあります"
                else:
                    row.status = final
            s.get(Job, job.id).verified += sum(1 for d in drafts if d.id in failed or done[d.id] >= n_runs)

    def run_once(self) -> bool:
        """1 件の依頼を処理する。仕事がなければ False。"""
        if self.lane == "api" and time.monotonic() < self.unavailable_until:
            # API が混み合っている・sandbox が止まっているときは、待ち時間が過ぎるまで次の依頼に手をつけない
            # （すぐに取りかかると、料金のかかる呼び出しを繰り返してしまう）
            return False
        if self.lane != "api":  # API の順番待ちは Ollama の状態を見ない。バックアップも Ollama 側でだけ取る
            self.maybe_backup()
        job_id = self.next_job_id(self.check_ollama() if self.lane != "api" else True)
        if job_id is None:
            return False
        if hasattr(self.llm, "should_abort"):
            # 呼び出し中でも取り消しを確かめる（1 秒に 1 回まで。接続を切ると Ollama も生成をやめる）
            self.llm.should_abort = self._throttled_cancel_check(job_id)
        if hasattr(self.llm, "budget_check"):
            self.llm.budget_check = lambda: self.check_api_budget(job_id)
        self.last_subject = self._job(job_id).subject
        try:
            self.process(job_id)
        except (Cancelled, LLMCancelled):
            log.info("job %s cancelled", job_id)
            self.finish_cancel(job_id)
            self.set_status(f"依頼 #{job_id} を取り消しました", job_id)
        except (LLMModelMissing, LLMBudgetExceeded) as exc:  # 待っても直らないので失敗で止める
            log.warning("job %s failed: %s", job_id, exc)
            with self.sessions.begin() as s:  # 検証できない下書きは捨てる（合格済みの問題は残す）
                self._discard_drafts(s, job_id)
                s.get(Job, job_id).made = s.execute(select(func.count()).select_from(Problem).where(
                    Problem.job_id == job_id, Problem.status == "ready")).scalar_one()
            self._finish(job_id, "failed", str(exc))
        except (LLMUnavailable, SandboxUnavailable) as exc:
            log.warning("job %s waiting: %s", job_id, exc)
            self.unavailable_until = time.monotonic() + self.settings.retry_seconds
            self.last_ollama_check = -1e9  # 次は状態を確かめ直してから再開する
            self._update_job(job_id, status="waiting",
                             progress=f"待機中：{exc}（{self.settings.retry_seconds} 秒ごとに再試行）")
            self.set_status(f"待機中：{exc}", job_id)
        except Exception as exc:  # 想定外の失敗でもワーカーは止めない
            log.exception("job %s failed", job_id)
            self._finish(job_id, "failed", f"内部エラー: {exc}")
        finally:
            if hasattr(self.llm, "should_abort"):
                self.llm.should_abort = None
        return True

    def check_api_budget(self, job_id: int) -> None:
        """API を呼ぶ前に、今月の利用額と、この依頼の利用額が上限に達していないかを確かめる。"""
        pending = self.llm.pending_cost() if hasattr(self.llm, "pending_cost") else 0.0  # まだ集計していない分
        with self.sessions() as s:
            monthly, per_job = api_limits(self.settings, s)
            spent = api_spend(s).get(month_key(self.settings), 0.0) + pending
            job = s.get(Job, job_id)
            job_cost = (job.api_cost_usd or 0.0) + pending
            day_usd, day_calls = daily_limits(s, job.subject)
            today = api_daily(s).get(day_key(self.settings), {}).get(job.subject, {})
        sub = get_subject(job.subject)
        name = sub.name if sub else job.subject
        if day_usd > 0 and today.get("usd", 0.0) + pending >= day_usd:
            raise LLMBudgetExceeded(f"{name}の今日の API の利用額が上限（${day_usd:.2f}）に達しました"
                                    f"（${today.get('usd', 0.0) + pending:.2f}）。「状態 → モデルと思考モード」の科目ごとの設定で変えられます")
        if day_calls > 0 and today.get("calls", 0) >= day_calls:
            raise LLMBudgetExceeded(f"{name}の今日の API の呼び出しが上限（{day_calls} 回）に達しました。"
                                    "「状態 → モデルと思考モード」の科目ごとの設定で変えられます")
        if spent >= monthly:
            raise LLMBudgetExceeded(f"今月の API の利用額が上限（${monthly:.2f}）に達しました（${spent:.2f}）。"
                                    "「状態 → モデルと思考モード」で上限を変えられます")
        if per_job > 0 and job_cost >= per_job:
            raise LLMBudgetExceeded(f"この依頼の API の利用額が上限（${per_job:.2f}）に達しました（${job_cost:.2f}）")

    def _throttled_cancel_check(self, job_id: int):  # noqa: ANN202
        last = {"at": 0.0, "value": False}

        def check() -> bool:
            now = time.monotonic()
            if now - last["at"] >= 1.0:
                last["at"], last["value"] = now, self.is_cancelled(job_id)
            return last["value"]

        return check

    def run_forever(self, recover: bool = True) -> None:
        if recover:
            self.recover()
        log.info("worker started (lane=%s)", self.lane or "all")
        while True:
            try:
                worked = self.run_once()
            except Exception:
                log.exception("worker loop error")
                worked = False
            if not worked:
                with self.sessions() as s:
                    waiting = s.execute(select(func.count()).select_from(Job)
                                        .where(Job.status.in_(OPEN_STATUSES))).scalar_one()
                if waiting and time.monotonic() < self.unavailable_until:
                    what = "API（または sandbox）" if self.lane == "api" else "Ollama"
                    self.set_status(f"{what}につながるのを待っています（順番待ち {waiting} 件）")
                else:
                    self.set_status("待機中（依頼なし）")
                time.sleep(3)


def make_llm(settings: Settings) -> LLMRouter:
    """Ollama と、設定されていれば API の提供元を登録したクライアント（順番待ちごとに 1 つずつ作る）。"""
    ollama = OllamaClient(settings.ollama_base_url, timeout=settings.llm_timeout, num_ctx=settings.num_ctx,
                          num_predict=settings.num_predict, keep_alive=settings.keep_alive, think=settings.think)
    providers: dict[str, Any] = {}
    if settings.anthropic_api_key:  # API の提供元を足すときはここに登録する
        from .llm_anthropic import AnthropicClient, parse_prices

        providers["anthropic"] = AnthropicClient(
            settings.anthropic_api_key, models=[m.strip() for m in settings.anthropic_models.split(",") if m.strip()],
            prices=parse_prices(settings.anthropic_prices), effort=settings.anthropic_effort,
            max_tokens=settings.anthropic_max_tokens, timeout=settings.llm_timeout)
    return LLMRouter(ollama, providers)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    ensure_or_exit(settings.data_dir, skip=settings.skip_data_check)
    sessions = init_db(settings.database_path)
    sandbox = SandboxClient(settings.sandbox_url)
    workers = {lane: Worker(settings, sessions, make_llm(settings), sandbox, lane=lane) for lane in LANES}
    workers["ollama"].recover()
    # 画面に出す API の情報（キーがあるか、選べるモデル）。キーそのものは書かない
    with sessions.begin() as s:
        kv_set(s, "api", dumps({"configured": bool(settings.anthropic_api_key),
                                "models": workers["api"].llm.api_models()}))
    # Ollama の依頼と API の依頼は、処理する頭が別なので同時に進める
    threading.Thread(target=workers["api"].run_forever, kwargs={"recover": False}, daemon=True,
                     name="worker-api").start()
    workers["ollama"].run_forever(recover=False)


if __name__ == "__main__":
    main()
