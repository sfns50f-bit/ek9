"""ストック（検証に合格した未出題の問題）の数え方と、解き始めのセットの組み方。web が使う。

ここでは LLM を呼ばない。データベースを読むだけ。
"""

from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .db import Attempt, Job, Problem, utcnow
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
                      formats: tuple[str, ...]) -> list[tuple[int, str]]:
    """最後の回答が不正解のままの問題の (ID, 小単元)（古い順）。報告された問題は除く。"""
    latest = (
        select(Attempt.problem_id, func.max(Attempt.id).label("last_id"))
        .where(Attempt.user_id == user_id).group_by(Attempt.problem_id).subquery()
    )
    rows = s.execute(
        select(Attempt.problem_id, Problem.template)
        .join(latest, Attempt.id == latest.c.last_id)
        .join(Problem, Problem.id == Attempt.problem_id)
        .where(Attempt.is_correct.is_(False), *_servable(), Problem.subject == subject,
               Problem.template.in_(templates), Problem.format.in_(formats))
        .order_by(Attempt.created_at)
    ).all()
    return [(pid, t) for pid, t in rows]


def spread_pick(items: list[tuple[int, str]], n: int, rng: random.Random) -> list[tuple[int, str]]:
    """小単元ごとに順番に 1 問ずつ取り、なるべく均等に n 問選ぶ（同じ小単元の中は items の順）。"""
    groups: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for item in items:
        groups[item[1]].append(item)
    keys = list(groups)
    rng.shuffle(keys)
    out: list[tuple[int, str]] = []
    while len(out) < n and keys:
        for k in list(keys):
            if len(out) >= n:
                break
            out.append(groups[k].pop(0))
            if not groups[k]:
                keys.remove(k)
    return out


def spread_order(items: list[tuple[int, str]], rng: random.Random) -> list[int]:
    """同じ小単元の問題が続かないように並べる。

    毎回「直前と違う小単元のうち、残りが最も多いもの」から 1 問出す（残りが多いものを先に減らすと、
    最後に同じ小単元だけが残って続いてしまうことが起きにくい）。1 つの小単元しかなければ続くのは避けられない。
    """
    groups: dict[str, list[int]] = defaultdict(list)
    for pid, t in items:
        groups[t].append(pid)
    for ids in groups.values():
        rng.shuffle(ids)
    out: list[int] = []
    last = None
    while groups:
        cands = [t for t in groups if t != last] or list(groups)
        most = max(len(groups[t]) for t in cands)
        t = rng.choice([c for c in cands if len(groups[c]) == most])
        out.append(groups[t].pop())
        if not groups[t]:
            del groups[t]
        last = t
    return out


def build_session(s: Session, user_id: int, subject: Subject, scope: str, device: str, count: int,
                  retry_percent: int, rng: random.Random | None = None, levels: str = "") -> list[int]:
    """未出題のストックに、間違えた問題の再出題を混ぜて count 問のセットを作る。

    再出題は count の retry_percent % まで。未出題が足りなければ、残りの間違えた問題で埋め、
    それでも足りなければある分だけで始める。選んだ範囲の小単元からなるべく均等に選び、
    同じ小単元の問題が続かないように並べる。
    """
    rng = rng or random.Random()
    formats = DEVICES[device][1]
    templates = subject.templates_for(scope, levels=levels)
    wrong = wrong_problem_ids(s, user_id, subject.id, templates, formats)
    fresh = [(pid, t) for pid, t in s.execute(
        select(Problem.id, Problem.template).where(
            *_servable(), Problem.subject == subject.id, Problem.template.in_(templates),
            Problem.format.in_(formats), Problem.id.not_in(_seen_ids(user_id)))
    ).all()]
    rng.shuffle(fresh)
    n_retry = min(len(wrong), count * retry_percent // 100)
    picked = spread_pick(wrong, n_retry, rng)
    picked += spread_pick(fresh, count - len(picked), rng)
    if len(picked) < count:
        rest = [w for w in wrong if w not in picked]
        picked += spread_pick(rest, count - len(picked), rng)
    return spread_order(picked, rng)


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


def _period(days: int, from_job: int):  # noqa: ANN202
    """集計の期間の条件。days=0 はすべて。from_job を指定すればその依頼番号以降（直す前のデータを外すため）。"""
    conds = []
    if days > 0:
        conds.append(Problem.created_at >= utcnow() - timedelta(days=days))
    if from_job > 0:
        conds.append(Problem.job_id >= from_job)
    return conds


def verify_stats(s: Session, days: int = 30, from_job: int = 0) -> dict[str, Any]:
    """期間内の作成・検証の成績（検証のテストは含めない）。

    by_model: 検証役のモデルごとに、検証にかけた問題数と、そのモデルが弾いた数
    by_gen: 作成役のモデルごとに、作った数・合格した数・不合格の理由の内訳
    by_code: 不合格の理由の種類ごとの数（作成段階の不合格も含む）
    """
    rows = s.execute(
        select(Problem.status, Problem.reject_code, Problem.verification_json, Problem.gen_model)
        .where(*_period(days, from_job), Problem.status.in_(("ready", "rejected", "deleted")),
               or_(Problem.job_id.is_(None), Problem.job_id.not_in(select(Job.id).where(Job.is_test.is_(True)))))
    ).all()
    by_code: Counter[str] = Counter()
    reached: Counter[str] = Counter()
    rejected_by: Counter[str] = Counter()
    gen: dict[str, dict[str, Any]] = {}
    total = passed = 0
    for status, code, vjson, gen_model in rows:
        total += 1
        g = gen.setdefault(gen_model or "（記録なし）", {"total": 0, "passed": 0, "codes": Counter()})
        g["total"] += 1
        if status == "rejected":
            by_code[code or "unknown"] += 1
            g["codes"][code or "unknown"] += 1
        else:  # 合格（後で問題検証モードで削除したものも、検証には合格している）
            passed += 1
            g["passed"] += 1
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
    by_gen = [{"model": m, "total": g["total"], "passed": g["passed"],
               "rate": round(100 * g["passed"] / g["total"]) if g["total"] else 0,
               "codes": g["codes"].most_common()}
              for m, g in sorted(gen.items(), key=lambda kv: -kv[1]["total"])]
    return {"days": days, "from_job": from_job, "total": total, "passed": passed,
            "pass_rate": round(100 * passed / total) if total else 0,
            "by_code": by_code.most_common(), "by_model": by_model, "by_gen": by_gen}


def job_stats(s: Session, days: int = 30, from_job: int = 0, limit: int = 50) -> list[dict[str, Any]]:
    """依頼ごとの比較（モデルの比べ用）。

    1 問あたりの作成時間 = 作成役の eval（出力にかかった時間）の合計 ÷ 作った下書きの数（作成の回数）
    1 問あたりの検証時間 = 検証役の eval の合計 ÷ 検証にかけた下書きの数
    モデルの読み込み（load）は混ぜずに、依頼全体の合計を別に出す
    """
    q = select(Job).where(Job.is_test.is_(False)).order_by(Job.id.desc()).limit(limit)
    if days > 0:
        q = q.where(Job.created_at >= utcnow() - timedelta(days=days))
    if from_job > 0:
        q = q.where(Job.id >= from_job)
    jobs = list(s.execute(q).scalars())
    codes: dict[int, Counter[str]] = defaultdict(Counter)
    statuses: dict[int, Counter[str]] = defaultdict(Counter)
    if jobs:
        for jid, status, code, n in s.execute(
            select(Problem.job_id, Problem.status, Problem.reject_code, func.count())
            .where(Problem.job_id.in_([j.id for j in jobs]))
            .group_by(Problem.job_id, Problem.status, Problem.reject_code)
        ):
            statuses[jid][status] += n
            if status == "rejected":
                codes[jid][code or "unknown"] += n
    out = []
    for j in jobs:
        made = statuses[j.id]["ready"] + statuses[j.id]["deleted"]
        tried = sum(statuses[j.id].values()) - statuses[j.id]["draft"]
        out.append({
            "job": j, "made": made, "tried": tried,
            "pass_rate": round(100 * made / tried) if tried else None,
            "gen_per": round(j.gen_eval_s / j.gen_attempts, 1) if j.gen_attempts and j.gen_calls else None,
            "verify_per": round(j.verify_eval_s / j.verified, 1) if j.verified and j.verify_calls else None,
            "load_s": round(j.gen_load_s + j.verify_load_s, 1),
            "codes": codes[j.id].most_common(),
        })
    return out


def recent_rejected(s: Session, days: int = 30, from_job: int = 0, limit: int = 30) -> list[Problem]:
    return list(s.execute(select(Problem).where(*_period(days, from_job), Problem.status == "rejected")
                          .order_by(Problem.id.desc()).limit(limit)).scalars())


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
