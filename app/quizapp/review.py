"""問題検証モード（親向け）の部品：パスワード、絞り込み、品質チェック用の Markdown の書き出し。

web が使う。LLM は使わない。
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Problem, dumps
from .subjects import all_subjects, get_subject
from .subjects.base import FORMATS, LEVELS

COOKIE = "review"
# 書き出しの冒頭に付ける依頼文（Claude などのチャットにそのまま貼って評価してもらう）
EXPORT_REQUEST = "高校生向けの問題として、正確さ・分かりやすさ・難易度の適切さを評価してください。"
STATUSES = {"ready": "ストック（検証に合格）", "issues": "ストックのうち表記の点検で引っかかる問題",
            "reflagged": "ストックのうち見直しのやり直しで誤りを指摘された問題",
            "deleted": "削除した問題", "rejected": "検証で不合格"}
REREVIEW_STEP = "見直しのやり直し"  # worker.REREVIEW_STEP と同じ（web は worker を読み込まない）
MAX_REREVIEW = 200  # 1 回の見直しのやり直しで見る問題の数の上限
TEXT_FIELDS = ("question", "hint", "explanation")


def token(password: str) -> str:
    """ログインの cookie に入れる値（パスワードそのものは入れない）。"""
    return hmac.new(password.encode(), b"ai-quiz-review", hashlib.sha256).hexdigest()


def allowed(password: str, cookie: str | None) -> bool:
    """パスワードが設定されていなければ誰でも入れる。"""
    return not password or hmac.compare_digest(cookie or "", token(password))


def query(s: Session, *, subject: str = "", unit: str = "", level: str = "", gen_model: str = "",
          status: str = "ready", since: datetime | None = None, until: datetime | None = None,
          id_from: int = 0, id_to: int = 0, limit: int = 200, offset: int = 0) -> list[Problem]:
    """絞り込み。since / until は作った日時（UTC。until は含まない）、id_from / id_to は管理番号（両端を含む）。"""
    status = status if status in STATUSES else "ready"
    q = select(Problem).where(Problem.status == ("ready" if status in ("issues", "reflagged") else status))
    if since is not None:
        q = q.where(Problem.created_at >= since)
    if until is not None:
        q = q.where(Problem.created_at < until)
    if id_from > 0:
        q = q.where(Problem.id >= id_from)
    if id_to > 0:
        q = q.where(Problem.id <= id_to)
    if subject:
        q = q.where(Problem.subject == subject)
    if unit:
        q = q.where(Problem.unit == unit)
    if level:
        # レベルは小単元で決まるので、小単元で絞る（前の版で作った、レベルの列が空の問題も入るように）
        templates = [t for sub in all_subjects() for t in sub.templates_for("", levels=level)]
        q = q.where(Problem.template.in_(templates))
    if gen_model:
        q = q.where(Problem.gen_model == gen_model)
    q = q.order_by(Problem.id.desc())
    if status in ("issues", "reflagged"):  # 表記の点検・記録の中身は Python で見るので、全部読んでから絞る
        check = text_issues if status == "issues" else reflagged
        hits = [p for p in s.execute(q).scalars() if check(p)]
        return hits[offset:offset + limit]
    return list(s.execute(q.offset(offset).limit(limit)).scalars())


def rereviews(problem: Problem) -> list[dict[str, Any]]:
    """見直しのやり直しの記録（古い順）。"""
    return [e for e in problem.verification if e.get("step") == REREVIEW_STEP]


def reflagged(problem: Problem) -> bool:
    """見直しのやり直しのどれかで誤りを指摘された（出力が読めなかっただけのものは除く）。"""
    return any(not e.get("ok") and e.get("code") != "llm_output" for e in rereviews(problem))


def text_issues(problem: Problem) -> list[str]:
    """問題文・ヒント・解説の表記の崩れ。作成のときと同じ点検（中身は科目ごと。数学は latexfix）。"""
    sub = get_subject(problem.subject)
    return sub.text_issues(problem) if sub else []


def normalize_all(s: Session) -> int:
    """ストックと削除した問題の問題文・ヒント・解説に、作成のときと同じ表記の直し（latexfix.normalize）をかける。
    直した問題の数を返す。数式の意味は変えない（度の記号の揺れ・\\sin・文字どおりの \\n など）。"""
    changed = 0
    for p in s.execute(select(Problem).where(Problem.status.in_(("ready", "deleted")))).scalars():
        sub = get_subject(p.subject)
        if sub is None:
            continue
        new = {f: sub.normalize_text(getattr(p, f) or "") for f in TEXT_FIELDS}
        before = {f: getattr(p, f) or "" for f in TEXT_FIELDS if new[f] != (getattr(p, f) or "")}
        if before:
            # 直す前の文章は記録に残す（問題の「作り方と検証の記録」で見られる）
            p.verification_json = dumps(p.verification + [{
                "step": "表記の直し", "ok": True, "summary": "問題検証モードで表記をまとめて直した（直す前の文章は「詳しく」）",
                "detail": "\n\n".join(f"[{f}]\n{v}" for f, v in before.items())}])
            for f, v in new.items():
                setattr(p, f, v)
            changed += 1
    return changed


def delete_ids(s: Session, text: str) -> list[int]:
    """「12, 34 56」のように書いた番号の問題（ストックのものだけ）を削除にする。削除した番号を返す。"""
    import re

    done = []
    for pid in dict.fromkeys(int(n) for n in re.findall(r"\d+", text or "")):
        p = s.get(Problem, pid)
        if p is not None and p.status == "ready":
            p.status = "deleted"
            done.append(pid)
    return done


def labels(problem: Problem) -> dict[str, str]:
    sub = get_subject(problem.subject)
    unit_label = subunit_label = problem.template
    level = problem.level
    if sub:
        found = sub.subunit_map().get(problem.template)
        if found:
            unit_label, subunit_label = found[0].label, found[1].label
            level = level or found[1].level
    return {"subject": sub.name if sub else problem.subject, "unit": unit_label, "subunit": subunit_label,
            "level": LEVELS.get(level, "—"), "format": FORMATS.get(problem.format, problem.format)}


def think_text(problem: Problem) -> str:
    """役割ごとのモデルの思考モード（作成したときの記録）。前の版の問題は「記録なし」。"""
    from .runtime import THINK_RECORD

    job = problem.job
    think = job.think if job is not None else {}
    if not think:
        return "記録なし"
    roles: list[tuple[str, str, str]] = []
    passage = problem.passage
    if passage is not None and passage.gen_model:
        roles.append(("本文作成", passage.gen_model, passage.think or think.get(passage.gen_model, "")))
    roles.append(("作成", problem.gen_model or job.gen_model, ""))
    roles.append(("検証", job.verify_model, ""))
    if job.verify_model_2:
        roles.append(("別系統の検証", job.verify_model_2, ""))
    if job.explanation_review:
        roles.append(("見直し", job.review_model or job.verify_model, ""))
    parts = []
    for label, model, state in roles:
        if model:
            state = state or think.get(model, "")
            parts.append(f"{label} {THINK_RECORD.get(state, '記録なし')}")
    return "／".join(parts)


# 書き出しに出す記録の段階（作成の途中の細かい記録は出さない）
_EXPORT_STEPS = ("本文の点検", "本文の見直し", "英文の点検", "設問の点検", "例文の点検", "本文なしの確認",
                 "検証", "解説の見直し", REREVIEW_STEP, "設問")


def verification_lines(problem: Problem) -> list[str]:
    """書き出し用の「検証と見直しの記録」（第 4 回のレビュー E-3：検証役・見直し役の良し悪しを比べるため）。

    不合格の理由、検証役の答え（回ごと）、見直しの判定と確認ごとの結果、見直しのやり直しの結果。
    """
    sub = get_subject(problem.subject)
    codes = sub.all_reject_codes() if sub else {}
    out = ["### 検証と見直しの記録", ""]
    if problem.status == "rejected":
        label = codes.get(problem.reject_code or "", problem.reject_code or "—")
        out.append(f"- **不合格**：{label}（{(problem.reject_reason or '').strip()[:300]}）")
    shown = 0
    for e in problem.verification:
        if e.get("step") not in _EXPORT_STEPS:
            continue
        mark = "○" if e.get("ok") else "×"
        model = f"（{e['model']}）" if e.get("model") else ""
        out.append(f"- {mark} {e.get('step')}{model}：{str(e.get('summary', '')).strip()[:300]}")
        if e.get("step") in ("解説の見直し", REREVIEW_STEP) and e.get("detail"):
            out += [f"  - {ln.strip()[:300]}" for ln in str(e["detail"]).splitlines() if ln.strip()][:8]
        shown += 1
    if not shown and problem.status != "rejected":
        out.append("- 記録なし")
    return out + [""]


def mistake_label(problem: Problem, key: str | None) -> str:
    sub = get_subject(problem.subject)
    return (sub.mistakes.get(key or "", key or "") if sub else key or "") or "—"


def to_markdown(problems: list[Problem], filters: dict[str, str],
                when: Callable[[datetime], str] = lambda dt: dt.strftime("%Y/%m/%d %H:%M")) -> str:
    """品質チェック用の Markdown。冒頭に依頼文、各問に問題文・選択肢・正解・解説・ヒント・モデル。"""
    lines = [EXPORT_REQUEST, ""]
    shown = [f"{k}: {v}" for k, v in filters.items() if v]
    lines += [f"（絞り込み：{'、'.join(shown) if shown else 'なし'}／{len(problems)} 問）", ""]
    lines += ["数式は LaTeX（$…$）で書かれています。", ""]
    for n, p in enumerate(problems, 1):
        lab = labels(p)
        lines += [f"## 問題 {n}（#{p.id}）{lab['subject']}／{lab['unit']}／{lab['subunit']}",
                  "", f"- 作成日時：{when(p.created_at) if p.created_at else '—'}／管理番号：#{p.id}",
                  f"- レベル：{lab['level']}／形式：{lab['format']}",
                  f"- {'本文作成役：' + (p.passage.gen_model or '—') + '／' if p.passage is not None else ''}"
                  f"作成役：{p.gen_model or '—'}／検証役：{p.verify_models or '—'}",
                  f"- 思考モード：{think_text(p)}", ""]
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
        lines += ["### 解説", "", (p.explanation or "—").strip(), ""]
        lines += verification_lines(p) + ["---", ""]
    return "\n".join(lines)


def filter_options(s: Session) -> dict[str, Any]:
    """絞り込みの選択肢（作成役のモデルは、記録のあるものから）。"""
    models = sorted({m for m in s.execute(select(Problem.gen_model).distinct()).scalars() if m})
    return {"gen_models": models, "levels": LEVELS, "statuses": STATUSES}
