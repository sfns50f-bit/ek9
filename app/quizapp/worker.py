"""問題生成ワーカー。

画面からの依頼（Job）を最優先で処理し、手が空いたら最近使われた単元・難易度の在庫を
POOL_TARGET 問まで補充する。GPU は 1 枚なのでワーカーは 1 つだけ動かす。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .db import Job, Problem, init_db, kv_get, kv_set, utcnow
from .llm import LLMUnavailable, OllamaClient
from .pipeline import Aborted, Candidate, Pipeline, Rejection
from .sandbox_client import SandboxClient, SandboxUnavailable
from .topics import get_topic

log = logging.getLogger("worker")

REFILL_BACKOFF_SECONDS = 30 * 60
UNAVAILABLE_BACKOFF_SECONDS = 60


class Worker:
    def __init__(self, settings: Settings, session_factory: sessionmaker[Session], pipeline: Pipeline) -> None:
        self.settings = settings
        self.sessions = session_factory
        self.pipeline = pipeline
        self.backoff: dict[tuple[str, int], float] = {}
        self.unavailable_until = 0.0

    # --- 状態表示 -----------------------------------------------------------

    def set_status(self, text: str) -> None:
        with self.sessions.begin() as s:
            kv_set(s, "worker_status", text)
            kv_set(s, "worker_heartbeat", utcnow().isoformat())

    # --- Job ----------------------------------------------------------------

    def recover(self) -> None:
        """前回の停止時に処理中だった依頼を待ち状態に戻す。"""
        with self.sessions.begin() as s:
            s.execute(update(Job).where(Job.status == "running").values(status="pending", progress=""))

    def claim_job(self) -> Job | None:
        with self.sessions.begin() as s:
            job = s.execute(
                select(Job).where(Job.status == "pending").order_by(Job.created_at, Job.id).limit(1)
            ).scalar_one_or_none()
            if job is None:
                return None
            job.status = "running"
            job.progress = "開始"
            return job

    def has_pending_jobs(self) -> bool:
        with self.sessions() as s:
            return s.execute(select(func.count()).select_from(Job).where(Job.status == "pending")).scalar_one() > 0

    def job_cancelled(self, job_id: int) -> bool:
        with self.sessions() as s:
            return s.get(Job, job_id).status == "cancelled"

    def _update_job(self, job_id: int, **values: object) -> None:
        with self.sessions.begin() as s:
            job = s.get(Job, job_id)
            if job.status == "cancelled" and values.get("status") not in (None, "cancelled"):
                return
            for key, value in values.items():
                setattr(job, key, value)

    # --- 保存 ---------------------------------------------------------------

    def recent_questions(self, topic_id: str, limit: int = 8) -> list[str]:
        with self.sessions() as s:
            rows = s.execute(
                select(Problem.question)
                .where(Problem.topic_id == topic_id, Problem.status != "rejected")
                .order_by(Problem.created_at.desc()).limit(limit)
            ).scalars().all()
        return list(rows)

    def save_candidate(self, cand: Candidate, *, served: bool) -> int:
        with self.sessions.begin() as s:
            problem = Problem(
                topic_id=cand.topic_id, subtopic=cand.subtopic, difficulty=cand.difficulty,
                question=cand.question, answer_kind=cand.answer_kind, answer_srepr=cand.answer_srepr,
                answer_latex=cand.answer_latex, answer_var=cand.answer_var, explanation=cand.explanation,
                status="ready", verification=json.dumps(cand.log(), ensure_ascii=False), models=cand.models,
                served_at=utcnow() if served else None,
            )
            s.add(problem)
            s.flush()
            return problem.id

    def save_rejections(self, rejections: list[Rejection]) -> None:
        if not rejections:
            return
        with self.sessions.begin() as s:
            for r in rejections:
                s.add(Problem(
                    topic_id=r.topic_id, subtopic=r.subtopic, difficulty=r.difficulty, question=r.question,
                    answer_kind=r.answer_kind, status="rejected", reject_reason=r.reason,
                    verification=json.dumps(r.log(), ensure_ascii=False), models=r.models,
                ))

    # --- 処理 ---------------------------------------------------------------

    def handle_job(self, job: Job) -> None:
        topic = get_topic(job.topic_id)
        if topic is None:
            self._update_job(job.id, status="failed", error=f"未知の単元です: {job.topic_id}")
            return
        label = f"{topic.label}（難易度{job.difficulty}）"

        def progress(text: str) -> None:
            self._update_job(job.id, progress=text)
            self.set_status(f"依頼 #{job.id} {label} — {text}")

        try:
            cand, rejections = self.pipeline.run(
                topic, job.difficulty, self.recent_questions(topic.id),
                progress=progress, should_abort=lambda: self.job_cancelled(job.id),
            )
        except Aborted:
            log.info("job %s cancelled", job.id)
            return
        except (LLMUnavailable, SandboxUnavailable) as exc:
            self.unavailable_until = time.monotonic() + UNAVAILABLE_BACKOFF_SECONDS
            self._update_job(job.id, status="failed", error=str(exc))
            return
        except Exception as exc:  # 想定外の失敗でもワーカーは止めない
            log.exception("job %s failed", job.id)
            self._update_job(job.id, status="failed", error=f"内部エラー: {exc}")
            return
        self.save_rejections(rejections)
        if cand is None:
            last = rejections[-1].reason if rejections else "不明"
            self._update_job(job.id, status="failed",
                             error=f"{len(rejections)} 回作り直しましたが、検証に合格する問題ができませんでした。"
                                   f"最後の理由: {last}")
            return
        # 画面で開かれた時点で出題済みにする（画面を閉じていたら在庫として残る）
        problem_id = self.save_candidate(cand, served=False)
        self._update_job(job.id, status="done", problem_id=problem_id, progress="完了")

    def refill_paused(self) -> bool:
        if not self.settings.refill_enabled:
            return True
        with self.sessions() as s:
            return kv_get(s, "refill_paused") == "1"

    def pick_refill_target(self) -> tuple[str, int] | None:
        """最近使われた（単元, 難易度）のうち在庫が足りないものを 1 つ選ぶ。"""
        since = utcnow() - timedelta(days=self.settings.refill_days)
        with self.sessions() as s:
            demand: dict[tuple[str, int], object] = {}
            for topic_id, diff, last in s.execute(
                select(Problem.topic_id, Problem.difficulty, func.max(Problem.served_at))
                .where(Problem.served_at >= since).group_by(Problem.topic_id, Problem.difficulty)
            ):
                demand[(topic_id, diff)] = last
            for topic_id, diff, last in s.execute(
                select(Job.topic_id, Job.difficulty, func.max(Job.created_at))
                .where(Job.created_at >= since).group_by(Job.topic_id, Job.difficulty)
            ):
                demand[(topic_id, diff)] = max(demand.get((topic_id, diff)) or last, last)
            stock = dict(
                ((t, d), n) for t, d, n in s.execute(
                    select(Problem.topic_id, Problem.difficulty, func.count())
                    .where(Problem.status == "ready", Problem.served_at.is_(None))
                    .group_by(Problem.topic_id, Problem.difficulty)
                )
            )
        now = time.monotonic()
        candidates = [
            (stock.get(key, 0), -last.timestamp(), key)
            for key, last in demand.items()
            if get_topic(key[0]) and stock.get(key, 0) < self.settings.pool_target
            and self.backoff.get(key, 0) <= now
        ]
        if not candidates:
            return None
        return min(candidates)[2]

    def refill(self, target: tuple[str, int]) -> None:
        topic = get_topic(target[0])
        label = f"在庫補充 {topic.label}（難易度{target[1]}）"
        try:
            cand, rejections = self.pipeline.run(
                topic, target[1], self.recent_questions(topic.id),
                progress=lambda text: self.set_status(f"{label} — {text}"),
                should_abort=lambda: self.has_pending_jobs() or self.refill_paused(),
            )
        except Aborted:
            return
        except (LLMUnavailable, SandboxUnavailable) as exc:
            log.warning("refill skipped: %s", exc)
            self.unavailable_until = time.monotonic() + UNAVAILABLE_BACKOFF_SECONDS
            self.set_status(f"待機中（{exc}）")
            return
        self.save_rejections(rejections)
        if cand is None:
            self.backoff[target] = time.monotonic() + REFILL_BACKOFF_SECONDS
            return
        self.save_candidate(cand, served=False)

    def run_once(self) -> bool:
        """1 単位の仕事をする。仕事がなければ False。"""
        if time.monotonic() < self.unavailable_until:
            return False
        job = self.claim_job()
        if job is not None:
            self.handle_job(job)
            return True
        if not self.refill_paused():
            target = self.pick_refill_target()
            if target is not None:
                self.refill(target)
                return True
        return False

    def run_forever(self) -> None:
        self.recover()
        log.info("worker started (models: %s)", ", ".join(self.settings.models))
        while True:
            try:
                worked = self.run_once()
            except Exception:
                log.exception("worker loop error")
                worked = False
            if not worked:
                if time.monotonic() >= self.unavailable_until:
                    self.set_status("待機中" + ("（在庫の自動補充は停止中）" if self.refill_paused() else ""))
                time.sleep(2)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    sessions = init_db(settings.database_path)
    llm = OllamaClient(settings.ollama_base_url, timeout=settings.llm_timeout, num_ctx=settings.num_ctx,
                       num_predict=settings.num_predict, keep_alive=settings.keep_alive, think=settings.think)
    pipeline = Pipeline(llm, SandboxClient(settings.sandbox_url), settings)
    Worker(settings, sessions, pipeline).run_forever()


if __name__ == "__main__":
    main()
