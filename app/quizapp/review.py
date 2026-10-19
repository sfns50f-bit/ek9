"""問題検証モード（親向け）の部品：パスワード、絞り込み、品質チェック用の Markdown の書き出し。

web が使う。LLM は使わない。
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Problem
from .subjects import get_subject
from .subjects.base import FORMATS, LEVELS

COOKIE = "review"
# 書き出しの冒頭に付ける依頼文（Claude などのチャットにそのまま貼って評価してもらう）
EXPORT_REQUEST = "高校生向けの問題として、正確さ・分かりやすさ・難易度の適切さを評価してください。"
STATUSES = {"ready": "ストック（検証に合格）", "deleted": "削除した問題", "rejected": "検証で不合格"}


def token(password: str) -> str:
    """ログインの cookie に入れる値（パスワードそのものは入れない）。"""
    return hmac.new(password.encode(), b"ai-quiz-review", hashlib.sha256).hexdigest()


def allowed(password: str, cookie: str | None) -> bool:
    """パスワードが設定されていなければ誰でも入れる。"""
    return not password or hmac.compare_digest(cookie or "", token(password))


def query(s: Session, *, subject: str = "", unit: str = "", level: str = "", gen_model: str = "",
          status: str = "ready", limit: int = 200, offset: int = 0) -> list[Problem]:
    q = select(Problem).where(Problem.status == (status if status in STATUSES else "ready"))
    if subject:
        q = q.where(Problem.subject == subject)
    if unit:
        q = q.where(Problem.unit == unit)
    if level:
        q = q.where(Problem.level == level)
    if gen_model:
        q = q.where(Problem.gen_model == gen_model)
    return list(s.execute(q.order_by(Problem.id.desc()).offset(offset).limit(limit)).scalars())


def labels(problem: Problem) -> dict[str, str]:
    sub = get_subject(problem.subject)
    unit_label = subunit_label = problem.template
    if sub:
        found = sub.subunit_map().get(problem.template)
        if found:
            unit_label, subunit_label = found[0].label, found[1].label
    return {"subject": sub.name if sub else problem.subject, "unit": unit_label, "subunit": subunit_label,
            "level": LEVELS.get(problem.level, "—"), "format": FORMATS.get(problem.format, problem.format)}


def mistake_label(problem: Problem, key: str | None) -> str:
    sub = get_subject(problem.subject)
    return (sub.mistakes.get(key or "", key or "") if sub else key or "") or "—"


def to_markdown(problems: list[Problem], filters: dict[str, str]) -> str:
    """品質チェック用の Markdown。冒頭に依頼文、各問に問題文・選択肢・正解・解説・ヒント・モデル。"""
    lines = [EXPORT_REQUEST, ""]
    shown = [f"{k}: {v}" for k, v in filters.items() if v]
    lines += [f"（絞り込み：{'、'.join(shown) if shown else 'なし'}／{len(problems)} 問）", ""]
    lines += ["数式は LaTeX（$…$）で書かれています。", ""]
    for n, p in enumerate(problems, 1):
        lab = labels(p)
        lines += [f"## 問題 {n}（#{p.id}）{lab['subject']}／{lab['unit']}／{lab['subunit']}",
                  "", f"- レベル：{lab['level']}／形式：{lab['format']}",
                  f"- 作成役：{p.gen_model or '—'}／検証役：{p.verify_models or '—'}", ""]
        if p.passage is not None and p.passage.body:
            lines += ["### 本文", "", p.passage.body.strip(), ""]
        lines += ["### 問題文", "", p.question.strip(), ""]
        if p.choices:
            lines += ["### 選択肢", ""]
            for i, c in enumerate(p.choices):
                mark = "（正解）" if c.get("correct") else f"（誤答：{mistake_label(p, c.get('mistake'))}）"
                lines.append(f"{'ABCDEFGH'[i]}. {c.get('text', '')} {mark}")
            lines.append("")
        lines += ["### 正解", "", p.answer_display or "—", ""]
        lines += ["### ヒント", "", (p.hint or "—").strip(), ""]
        lines += ["### 解説", "", (p.explanation or "—").strip(), "", "---", ""]
    return "\n".join(lines)


def filter_options(s: Session) -> dict[str, Any]:
    """絞り込みの選択肢（作成役のモデルは、記録のあるものから）。"""
    models = sorted({m for m in s.execute(select(Problem.gen_model).distinct()).scalars() if m})
    return {"gen_models": models, "levels": LEVELS, "statuses": STATUSES}
