"""ストック（検証に合格した未出題の問題）の数え方と、解き始めのセットの組み方。web が使う。

ここでは LLM を呼ばない。データベースを読むだけ。
"""

from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import Attempt, Problem, utcnow
from .subjects import all_subjects
from .subjects.base import DEVICES, FORMATS, Subject


def _servable():
    return (Problem.status == "ready", Problem.reported.is_(False))


def _seen_ids(user_id: int):
    return select(Attempt.problem_id).where(Attempt.user_id == user_id)


def unseen_counts(s: Session, user_id: int) -> dict[tuple[str, str, str], int]:
    """(科目, 小単元, 形式) → まだ解いていないストックの数。"""
    rows = s.execute(
        select(Problem.subject, Problem.template, Problem.format, func.count())
        .where(*_servable(), Problem.id.not_in(_seen_ids(user_id)))
        .group_by(Problem.subject, Problem.template, Problem.format)
    ).all()
    return {(sub, t, f): n for sub, t, f, n in rows}


def stock_summary(s: Session, user_id: int, low: int) -> list[dict[str, Any]]:
    """科目 × 端末ごとの未出題ストック数と「残りが少ない」かどうか。"""
    counts = unseen_counts(s, user_id)
    out = []
    for subject in all_subjects():
        by_format = {f: sum(v for (sub, _t, ff), v in counts.items() if sub == subject.id and ff == f)
                     for f in FORMATS}
        for device, (label, formats) in DEVICES.items():
            # キーボードの端末では選択式も出すので、その端末で出せる数は形式の合計になる
            n = sum(by_format[f] for f in formats)
            detail = "・".join(f"{FORMATS[f]} {by_format[f]}" for f in formats) if len(formats) > 1 else ""
            out.append({"subject": subject.id, "subject_name": subject.name, "device": device,
                        "device_label": label, "count": n, "low": n < low, "detail": detail,
                        "formats": "＋".join(FORMATS[f] for f in formats)})
    return out


def wrong_problem_ids(s: Session, user_id: int, subject: str, templates: list[str],
                      formats: tuple[str, ...]) -> list[int]:
    """最後の回答が不正解のままの問題（古い順）。報告された問題は除く。"""
    latest = (
        select(Attempt.problem_id, func.max(Attempt.id).label("last_id"))
        .where(Attempt.user_id == user_id).group_by(Attempt.problem_id).subquery()
    )
    rows = s.execute(
        select(Attempt.problem_id)
        .join(latest, Attempt.id == latest.c.last_id)
        .join(Problem, Problem.id == Attempt.problem_id)
        .where(Attempt.is_correct.is_(False), *_servable(), Problem.subject == subject,
               Problem.template.in_(templates), Problem.format.in_(formats))
        .order_by(Attempt.created_at)
    ).scalars().all()
    return list(rows)


def build_session(s: Session, user_id: int, subject: Subject, scope: str, device: str, count: int,
                  retry_percent: int, rng: random.Random | None = None) -> list[int]:
    """未出題のストックに、間違えた問題の再出題を混ぜて count 問のセットを作る。

    再出題は count の retry_percent % まで。未出題が足りなければ、残りの間違えた問題で埋め、
    それでも足りなければある分だけで始める。
    """
    rng = rng or random.Random()
    formats = DEVICES[device][1]
    templates = subject.templates_for(scope)
    wrong = wrong_problem_ids(s, user_id, subject.id, templates, formats)
    fresh = list(s.execute(
        select(Problem.id).where(*_servable(), Problem.subject == subject.id, Problem.template.in_(templates),
                                 Problem.format.in_(formats), Problem.id.not_in(_seen_ids(user_id)))
    ).scalars())
    rng.shuffle(fresh)
    n_retry = min(len(wrong), count * retry_percent // 100)
    picked = wrong[:n_retry] + fresh[:count - n_retry]
    if len(picked) < count:
        picked += wrong[n_retry:n_retry + count - len(picked)]
    rng.shuffle(picked)
    return picked


# ---------------------------------------------------------------------------
# /status 用の集計
# ---------------------------------------------------------------------------

def template_stock(s: Session, user_id: int) -> list[dict[str, Any]]:
    unseen = unseen_counts(s, user_id)
    ready = {(sub, t, f): n for sub, t, f, n in s.execute(
        select(Problem.subject, Problem.template, Problem.format, func.count())
        .where(*_servable()).group_by(Problem.subject, Problem.template, Problem.format))}
    rows = []
    for subject in all_subjects():
        for unit in subject.units():
            for su in unit.subunits:
                rows.append({
                    "subject": subject.name, "unit": unit.label, "label": su.label, "id": su.id,
                    "choice_unseen": unseen.get((subject.id, su.id, "choice"), 0),
                    "input_unseen": unseen.get((subject.id, su.id, "input"), 0),
                    "ready": sum(ready.get((subject.id, su.id, f), 0) for f in ("choice", "input")),
                })
    return rows


def verify_stats(s: Session, days: int = 30) -> dict[str, Any]:
    """直近 days 日の作成・検証の成績。

    by_model: 検証役のモデルごとに、検証にかけた問題数と、そのモデルが弾いた数
    by_code: 不合格の理由の種類ごとの数（作成段階の不合格も含む）
    """
    since = utcnow() - timedelta(days=days)
    rows = s.execute(
        select(Problem.status, Problem.reject_code, Problem.verification_json, Problem.created_at)
        .where(Problem.created_at >= since, Problem.status.in_(("ready", "rejected")))
    ).all()
    by_code: Counter[str] = Counter()
    reached: Counter[str] = Counter()
    rejected_by: Counter[str] = Counter()
    total = passed = 0
    for status, code, vjson, _created in rows:
        total += 1
        if status == "ready":
            passed += 1
        else:
            by_code[code or "unknown"] += 1
        try:
            entries = json.loads(vjson or "[]")
        except ValueError:
            entries = []
        models_seen = []
        for e in entries:
            if e.get("step") != "検証" or not e.get("model"):
                continue
            if e["model"] not in models_seen:
                models_seen.append(e["model"])
            if not e.get("ok"):
                rejected_by[e["model"]] += 1
        for m in models_seen:
            reached[m] += 1
    by_model = [{"model": m, "reached": n, "rejected": rejected_by[m],
                 "rate": round(100 * rejected_by[m] / n) if n else 0} for m, n in reached.most_common()]
    return {"days": days, "total": total, "passed": passed,
            "pass_rate": round(100 * passed / total) if total else 0,
            "by_code": by_code.most_common(), "by_model": by_model}


def per_template_rejects(s: Session, days: int = 30) -> dict[str, dict[str, int]]:
    since = utcnow() - timedelta(days=days)
    out: dict[str, dict[str, int]] = defaultdict(lambda: {"ready": 0, "rejected": 0})
    for template, status, n in s.execute(
        select(Problem.template, Problem.status, func.count())
        .where(Problem.created_at >= since, Problem.status.in_(("ready", "rejected")))
        .group_by(Problem.template, Problem.status)
    ):
        out[template][status] += n
    return dict(out)
