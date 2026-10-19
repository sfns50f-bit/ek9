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
import time
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload, sessionmaker

from . import backup
from .config import Settings
from .datadir import ensure_or_exit
from .db import Job, Passage, Problem, dumps, init_db, kv_json, kv_set, utcnow
from .llm import LLMCancelled, LLMError, LLMModelMissing, LLMRouter, LLMUnavailable, OllamaClient
from .runtime import ModelPlan, apply_plan, plan_for_job, resolve_think, think_settings
from .sandbox_client import SandboxClient, SandboxUnavailable
from .subjects import get_subject
from .subjects.base import Draft, GenContext, Rejected, Subject, Subunit

log = logging.getLogger("worker")

OPEN_STATUSES = ("pending", "running", "waiting")
CANCEL_STATUSES = ("cancelling", "cancelled")
OLLAMA_CHECK_SECONDS = 60
HEARTBEAT_SECONDS = 60
LLM_CALLS_KEEP = 40
VERIFY_STEP = "検証"
TEST_ATTEMPTS = 3  # 検証のテストで、検証の段階まで届く下書きを作るまでに試す回数


class Cancelled(Exception):
    pass


class Worker:
    def __init__(self, settings: Settings, sessions: sessionmaker[Session], llm: Any,
                 sandbox: SandboxClient, rng: random.Random | None = None) -> None:
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
            kv_set(s, "worker", dumps({"text": text, "job_id": job_id, "at": utcnow().isoformat()}))

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
        with self.sessions.begin() as s:
            kept = (kv_json(s, "llm_calls", []) or []) + calls
            kv_set(s, "llm_calls", dumps(kept[-LLM_CALLS_KEEP:]))
            job = s.get(Job, job_id) if job_id is not None else None
            if job is None:
                return
            for c in calls:
                role = "verify" if c.get("label") == "検証" else "gen"
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

    def next_job_id(self, ollama_ok: bool = True) -> int | None:
        """次に処理する依頼。Ollama につながらないときは、API のモデルだけを使う依頼を先に進める。"""
        with self.sessions() as s:
            for job in s.execute(select(Job).where(Job.status.in_(OPEN_STATUSES))
                                 .order_by(Job.created_at, Job.id)).scalars():
                if ollama_ok or not plan_for_job(self.settings, s, job).needs_ollama:
                    return job.id
        return None

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

    def save_drafts(self, job: Job, drafts: list[Draft], plan: ModelPlan) -> None:
        with self.sessions.begin() as s:
            passages: dict[int, Passage] = {}
            for draft in drafts:
                self._add_draft(s, job, draft, plan, passages)

    def _add_draft(self, s: Session, job: Job, draft: Draft, plan: ModelPlan, passages: dict[int, Passage]) -> None:
        passage = None
        if draft.passage is not None:
            # 同じ本文（同じ dict）の問題は 1 つの Passage にまとめる
            passage = passages.get(id(draft.passage))
            if passage is None:
                p = draft.passage
                passage = Passage(subject=job.subject, title=str(p.get("title", "")), body=str(p.get("body", "")),
                                  source_title=str(p.get("source_title", "")),
                                  source_url=str(p.get("source_url", "")))
                s.add(passage)
                passages[id(draft.passage)] = passage
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

    def save_rejected(self, job: Job, subject: Subject, template: str, exc: Rejected, plan: ModelPlan) -> None:
        p = exc.partial
        unit, su = subject.subunit_map().get(template, (None, None))
        with self.sessions.begin() as s:
            s.add(Problem(
                subject=job.subject, unit=unit.id if unit else "", template=template, format=job.format,
                level=su.level if su else "", related_units=",".join(su.related) if su else "",
                status="rejected", job_id=job.id, question=str(p.get("question", "")),
                explanation=str(p.get("explanation", "")), hint=str(p.get("hint", "")),
                params_json=dumps(p.get("params", {})), params_key="",
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
                         is_test=job.is_test, existing_keys=self.existing_keys)
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
            self.save_drafts(job, drafts, plan)
            produced += len(drafts)

    def _verify_phase(self, job: Job, subject: Subject, drafts: list[Problem], plan: ModelPlan,
                      label: str) -> None:
        """下書きをまとめて検証する。取り消し・Ollama の停止で途中で抜けても、
        それまでに全回合格した下書きはストックに入れ、不合格が決まったものは不合格にする。"""
        logs = {d.id: [e for e in d.verification if e.get("step") != VERIFY_STEP] for d in drafts}
        alive = {d.id: d for d in drafts}
        failed: dict[int, tuple[str, str]] = {}
        runs = plan.verify_runs
        done = {d.id: 0 for d in drafts}  # 終わった検証の回数
        try:
            for r, model in enumerate(runs, 1):
                for k, (pid, problem) in enumerate(list(alive.items()), 1):
                    self._check_cancel(job.id)
                    text = f"検証中 {r}/{len(runs)} 回目 {k}/{len(alive)}（{model}）"
                    self._update_job(job.id, progress=text)
                    self.set_status(f"{label} — {text}", job.id)
                    self._label("検証")
                    try:
                        res = subject.verify(self.llm, self.sandbox, model, problem)
                        ok, summary, detail, code, raw = res.ok, res.summary, res.detail, res.code, res.raw
                    except LLMError as exc:
                        ok, summary, detail, code = False, f"{model}: 出力を読めません（{exc}）", "", "llm_output"
                        raw = getattr(exc, "raw", "")
                    finally:
                        self._save_llm_calls(job.id)
                    logs[pid].append({"step": VERIFY_STEP, "ok": ok, "summary": summary, "detail": detail,
                                      "model": model, "run": r, "code": "" if ok else code, "raw": raw[:4000]})
                    done[pid] += 1
                    if not ok:
                        failed[pid] = (code, summary)
                        del alive[pid]
        finally:
            self._finalize_verified(job, drafts, logs, failed, done, len(runs), plan)

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
        self.maybe_backup()
        job_id = self.next_job_id(self.check_ollama())
        if job_id is None:
            return False
        if hasattr(self.llm, "should_abort"):
            # 呼び出し中でも取り消しを確かめる（1 秒に 1 回まで。接続を切ると Ollama も生成をやめる）
            self.llm.should_abort = self._throttled_cancel_check(job_id)
        try:
            self.process(job_id)
        except (Cancelled, LLMCancelled):
            log.info("job %s cancelled", job_id)
            self.finish_cancel(job_id)
            self.set_status(f"依頼 #{job_id} を取り消しました", job_id)
        except LLMModelMissing as exc:  # 待っても直らないので失敗で止める
            log.warning("job %s failed: %s", job_id, exc)
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

    def _throttled_cancel_check(self, job_id: int):  # noqa: ANN202
        last = {"at": 0.0, "value": False}

        def check() -> bool:
            now = time.monotonic()
            if now - last["at"] >= 1.0:
                last["at"], last["value"] = now, self.is_cancelled(job_id)
            return last["value"]

        return check

    def run_forever(self) -> None:
        self.recover()
        log.info("worker started")
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
                    self.set_status(f"Ollama につながるのを待っています（順番待ち {waiting} 件）")
                else:
                    self.set_status("待機中（依頼なし）")
                time.sleep(3)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    ensure_or_exit(settings.data_dir, skip=settings.skip_data_check)
    sessions = init_db(settings.database_path)
    ollama = OllamaClient(settings.ollama_base_url, timeout=settings.llm_timeout, num_ctx=settings.num_ctx,
                          num_predict=settings.num_predict, keep_alive=settings.keep_alive, think=settings.think)
    llm = LLMRouter(ollama)  # API の提供元を足すときはここに登録する
    Worker(settings, sessions, llm, SandboxClient(settings.sandbox_url)).run_forever()


if __name__ == "__main__":
    main()
