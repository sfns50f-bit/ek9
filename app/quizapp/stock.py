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


# 選ぶ単位：1 問（問題の ID）か、同じ本文の設問の塊（ID のタプル。設問の順）
Pick = tuple[Any, str]


def _size(key: Any) -> int:
    return len(key) if isinstance(key, tuple) else 1


def _blocks(rows: list[tuple[int, str, int | None]]) -> list[Pick]:
    """(ID, 小単元, 本文の ID) を選ぶ単位にまとめる。同じ本文の設問は 1 つの塊（最初に出てきた位置に置く）。

    本文つきの科目（英語の読解・公共）の設問は、同じ本文のものを続けて出す（本文を何度も読み直さないように）。
    """
    out: list[Pick] = []
    where: dict[int, int] = {}
    for pid, t, passage_id in rows:
        if passage_id is None:
            out.append((pid, t))
            continue
        if passage_id in where:
            i = where[passage_id]
            out[i] = (tuple(sorted(out[i][0] + (pid,))), t)
        else:
            where[passage_id] = len(out)
            out.append(((pid,), t))
    return out


def spread_pick(items: list[Pick], n: int, rng: random.Random) -> list[Pick]:
    """小単元ごとに順番に 1 つずつ取り、なるべく均等に n 問選ぶ（同じ小単元の中は items の順）。

    本文の塊は設問の数で数える。残りの数より大きい塊は、前の設問から残りの数だけ使う。
    """
    groups: dict[str, list[Pick]] = defaultdict(list)
    for item in items:
        groups[item[1]].append(item)
    keys = list(groups)
    rng.shuffle(keys)
    out: list[Pick] = []
    got = 0
    while got < n and keys:
        for k in list(keys):
            if got >= n:
                break
            key, t = groups[k].pop(0)
            if isinstance(key, tuple) and len(key) > n - got:
                key = key[:n - got]
            out.append((key, t))
            got += _size(key)
            if not groups[k]:
                keys.remove(k)
    return out


def spread_order(items: list[Pick], rng: random.Random) -> list[int]:
    """同じ小単元の問題が続かないように並べる（本文の塊の中の設問は続けて、設問の順に出す）。

    毎回「直前と違う小単元のうち、残りが最も多いもの」から 1 つ出す（残りが多いものを先に減らすと、
    最後に同じ小単元だけが残って続いてしまうことが起きにくい）。1 つの小単元しかなければ続くのは避けられない。
    """
    groups: dict[str, list[Any]] = defaultdict(list)
    for key, t in items:
        groups[t].append(key)
    for keys in groups.values():
        rng.shuffle(keys)
    out: list[int] = []
    last = None
    while groups:
        cands = [t for t in groups if t != last] or list(groups)
        most = max(len(groups[t]) for t in cands)
        t = rng.choice([c for c in cands if len(groups[c]) == most])
        key = groups[t].pop()
        out.extend(key if isinstance(key, tuple) else (key,))
        if not groups[t]:
            del groups[t]
        last = t
    return out


def build_session(s: Session, user_id: int, subject: Subject, scope: str, device: str, count: int,
                  retry_percent: int, rng: random.Random | None = None, levels: str = "") -> list[int]:
    """未出題のストックに、間違えた問題の再出題を混ぜて count 問のセットを作る。

    再出題は count の retry_percent % まで。未出題が足りなければ、残りの間違えた問題で埋め、
    それでも足りなければある分だけで始める。選んだ範囲の小単元からなるべく均等に選び、
    同じ小単元の問題が続かないように並べる。同じ本文の設問はまとめて続けて出す。
    """
    rng = rng or random.Random()
    formats = DEVICES[device][1]
    templates = subject.templates_for(scope, levels=levels)
    wrong = wrong_problem_ids(s, user_id, subject.id, templates, formats)
    fresh = [(pid, t, passage) for pid, t, passage in s.execute(
        select(Problem.id, Problem.template, Problem.passage_id).where(
            *_servable(), Problem.subject == subject.id, Problem.template.in_(templates),
            Problem.format.in_(formats), Problem.id.not_in(_seen_ids(user_id)))
    ).all()]
    rng.shuffle(fresh)
    passage_of = dict(s.execute(select(Problem.id, Problem.passage_id).where(
        Problem.id.in_([pid for pid, _t in wrong]))).all()) if wrong else {}
    wrong_blocks = _blocks([(pid, t, passage_of.get(pid)) for pid, t in wrong])
    n_retry = min(len(wrong), count * retry_percent // 100)
    picked = spread_pick(wrong_blocks, n_retry, rng)
    picked += spread_pick(_blocks(fresh), count - sum(_size(k) for k, _t in picked), rng)
    have = sum(_size(k) for k, _t in picked)
    if have < count:
        used = {pid for k, _t in picked for pid in (k if isinstance(k, tuple) else (k,))}
        rest = _blocks([(pid, t, passage_of.get(pid)) for pid, t in wrong if pid not in used])
        picked += spread_pick(rest, count - have, rng)
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
        if status == "rejected" and code != "duplicate":
            by_code[code or "unknown"] += 1
            g["codes"][code or "unknown"] += 1
        else:  # 合格（後で削除したもの、同じ数値の問題がすでにあってストックに入れなかったものも、検証には合格している）
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
    models: dict[int, tuple[str, str]] = {}
    if jobs:
        # 前の版の依頼はモデルの記録がないので、作った問題の記録から出す
        for jid, gen, ver in s.execute(
            select(Problem.job_id, Problem.gen_model, Problem.verify_models)
            .where(Problem.job_id.in_([j.id for j in jobs if not j.gen_model]))
        ):
            if gen and (jid not in models or (ver and not models[jid][1])):
                models[jid] = (gen, ver or "")
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
            "job": j, "made": made, "tried": tried, "old_models": models.get(j.id),
            "pass_rate": round(100 * made / tried) if tried else None,
            "gen_per": round(j.gen_eval_s / j.gen_attempts, 1) if j.gen_attempts and j.gen_calls else None,
            "verify_per": round(j.verify_eval_s / j.verified, 1) if j.verified and j.verify_calls else None,
            "load_s": round(j.gen_load_s + j.verify_load_s, 1),
            "codes": codes[j.id].most_common(),
        })
    return out


def recent_rejected(s: Session, days: int = 30, from_job: int = 0, limit: int = 30) -> list[Problem]:
    # 同じ数値の問題がストックにある（duplicate）ものは、ストックの問題の答えが見えるので出さない
    return list(s.execute(select(Problem).where(*_period(days, from_job), Problem.status == "rejected",
                                                Problem.reject_code != "duplicate")
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
