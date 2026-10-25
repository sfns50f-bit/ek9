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
            "needs_check": "要確認（検証の判定が割れた問題。確かめてストックに入れるか決める）",
            "deleted": "削除した問題", "rejected": "検証で不合格"}
REREVIEW_STEP = "見直しのやり直し"  # worker.REREVIEW_STEP と同じ（web は worker を読み込まない）
PASSAGE_REREVIEW_STEP = "本文の見直しのやり直し"
# 本文の記録（passages.log）から書き出しに出す段階（B-3）
_PASSAGE_STEPS = ("本文の点検", "本文の見直し", "使い回す前の本文の見直し", PASSAGE_REREVIEW_STEP)
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
    if status == "needs_check":
        q = select(Problem).where(Problem.status == "rejected", Problem.reject_code == "needs_check")
    else:
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
    return [e for e in problem.verification if e.get("step") in (REREVIEW_STEP, PASSAGE_REREVIEW_STEP)]


def reflagged(problem: Problem) -> bool:
    """見直しのやり直しのどれかで誤りを指摘された（出力が読めなかっただけのものは除く）。"""
    return any(not e.get("ok") and e.get("code") not in ("llm_output", "en_review_unread", "solver_unreadable")
               for e in rereviews(problem))


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
        # 本文は、本文を作ったときの記録だけを使う（使い回した本文に、設問を作ったときの設定を出さない。E-2）
        roles.append(("本文作成", passage.gen_model, passage.think or "none"))
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
            parts.append(f"{label} {THINK_RECORD.get(state, '記録なし')}")  # "none" も「記録なし」
    return "／".join(parts)


# 書き出しに出す記録の段階（作成の途中の細かい記録は出さない）
_EXPORT_STEPS = ("文章", "使い回す前の本文の見直し", PASSAGE_REREVIEW_STEP, "本文の点検", "本文の見直し", "英文の点検", "設問の点検", "例文の点検", "本文なしの確認",
                 "検証", "解説の見直し", REREVIEW_STEP, "設問", "要確認からストックへ")


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
    # 本文の記録（本文を作ったとき・使い回す前・やり直しの見直し）。設問の記録と同じものは出さない（B-3）
    passage = problem.passage
    if passage is not None:
        mine = {(e.get("step"), e.get("summary")) for e in problem.verification}
        for e in passage.log:
            if e.get("step") in _PASSAGE_STEPS and (e.get("step"), e.get("summary")) not in mine:
                model = f"（{e['model']}）" if e.get("model") else ""
                out.append(f"- {'○' if e.get('ok') else '×'} 本文：{e.get('step')}{model}："
                           f"{str(e.get('summary', '')).strip()[:300]}")
                shown += 1
    for e in problem.verification:
        if e.get("step") not in _EXPORT_STEPS:
            continue
        mark = "○" if e.get("ok") else "×"
        model = f"（{e['model']}）" if e.get("model") else ""
        out.append(f"- {mark} {e.get('step')}{model}：{str(e.get('summary', '')).strip()[:300]}")
        if e.get("step") in ("解説の見直し", REREVIEW_STEP) and e.get("detail"):
            out += [f"  - {ln.strip()[:300]}" for ln in str(e["detail"]).splitlines() if ln.strip()][:8]
        elif e.get("step") == "検証" and e.get("detail"):
            # 英語の検証は選択肢ごとの判定の結果（第 6 回のレビュー 2-1。見本集と照らすため）。数学の考え方は出さない
            out += [f"  - {ln.strip()[:300]}" for ln in str(e["detail"]).splitlines()
                    if ln.strip()[:1] in ("○", "×", "？")][:8]
        if not e.get("ok") and e.get("raw") and (e.get("step") == "文章" or e.get("code") == "llm_output"):
            # 読めなかった出力そのもの（E-4。思考の部分が混ざっていないかなどを見る）
            out += ["", "  ```", *[f"  {ln}" for ln in str(e["raw"])[:800].splitlines()], "  ```", ""]
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
        question = p.question.strip() or "（作成の途中で不合格になったので、問題文はありません。下の記録に作成役の出力があります）"
        lines += ["### 問題文", "", question, ""]
        if p.choices:
            lines += ["### 選択肢", ""]
            for i, c in enumerate(p.choices):
                doubt = f"／見直しでは「{mistake_label(p, c['label_doubt'])}」" if c.get("label_doubt") else ""
                mark = "（正解）" if c.get("correct") else f"（誤答：{mistake_label(p, c.get('mistake'))}{doubt}）"
                lines.append(f"{'ABCDEFGH'[i]}. {c.get('text', '')} {mark}")
            lines.append("")
        lines += ["### 正解", "", p.answer_display or "—", ""]
        lines += ["### ヒント", "", (p.hint or "—").strip(), ""]
        lines += ["### 解説", "", (p.explanation or "—").strip(), ""]
        lines += verification_lines(p) + ["---", ""]
    return "\n".join(lines)


def quality_markdown(stats: dict[str, Any], job_rows: list[dict[str, Any]], rejected: list[Problem],
                     codes: dict[str, str], period: str,
                     when: Callable[[datetime], str] = lambda dt: dt.strftime("%Y/%m/%d %H:%M")) -> str:
    """「検証の成績」の内容（集計表・依頼ごとの比較・不合格の一覧と生の出力）を Markdown に（第 5 回のレビュー E-1）。"""
    def label(code: str) -> str:
        return codes.get(code, code)

    out = ["# 検証の成績", "", f"期間：{period}（検証のテストは含めない）", "",
           f"作成 {stats['total']} 問のうち合格 {stats['passed']} 問（{stats['pass_rate']}%）", "",
           "## 作成役のモデルごと", "", "| 作成役 | 作った数 | 合格 | 不合格の内訳 |", "|---|---|---|---|"]
    out += [f"| {g['model']} | {g['total']} | {g['passed']}（{g['rate']}%） | "
            + ("、".join(f"{label(c)} {n}" for c, n in g["codes"]) or "—") + " |" for g in stats["by_gen"]]
    out += ["", "## 検証役のモデルごと", "", "| 検証役 | 検証した数 | 弾いた数 | 弾いた割合 |", "|---|---|---|---|"]
    out += [f"| {m['model']} | {m['reached']} | {m['rejected']} | {m['rate']}% |" for m in stats["by_model"]]
    out += ["", "## 不合格の理由", "", "| 理由 | 数 |", "|---|---|"]
    out += [f"| {label(c)} | {n} |" for c, n in stats["by_code"]]
    out += ["", "## 依頼ごとの比較", "", "| 依頼 | 作成役 | 検証役 | 合格／作成 | 作成 1 問（秒） | 検証 1 問（秒） | 読み込み（秒） | API（ドル） | 不合格 |",
            "|---|---|---|---|---|---|---|---|---|"]
    for r in job_rows:
        j = r["job"]
        gen = j.gen_model or (r["old_models"][0] if r.get("old_models") else "—")
        ver = (f"{j.verify_model} ×{j.verify_count}" if j.verify_model else
               (r["old_models"][1] if r.get("old_models") else "—"))
        rate = f"（{r['pass_rate']}%）" if r.get("pass_rate") is not None else ""
        out.append(f"| #{j.id} | {gen} | {ver} | {r['made']}／{r['tried']}{rate} | {r['gen_per'] if r['gen_per'] is not None else '—'} "
                   f"| {r['verify_per'] if r['verify_per'] is not None else '—'} | {r['load_s']} | "
                   f"{'%.3f' % j.api_cost_usd if j.api_cost_usd else '—'} | "
                   + ("、".join(f"{label(c)} {n}" for c, n in r["codes"]) or "—") + " |")
    out += ["", "## 不合格の一覧", ""]
    for p in rejected:
        fail = next((e for e in reversed(p.verification) if not e.get("ok")), {})
        out += [f"### #{p.id} {label(p.reject_code or '')}（{when(p.created_at) if p.created_at else '—'}）", "",
                f"- 作成役：{p.gen_model or '—'}／落ちた段階：{fail.get('step', '—')}（{fail.get('model', '—')}）",
                f"- 理由：{(p.reject_reason or '').strip()[:400]}"]
        if p.question:
            out.append(f"- 問題文：{p.question.strip()[:300]}")
        if fail.get("raw"):
            out += ["", "```", str(fail["raw"])[:1500], "```"]
        out.append("")
    return "\n".join(out)


def rereview_table(problems: list[Problem]) -> str:
    """見直しのやり直しの結果を、問題 × 依頼（見直し役）の表に（○ 誤りなし・× 誤りあり・？ 読めなかった。E-2）。"""
    cols: dict[tuple[int, str], None] = {}
    cells: dict[tuple[int, int, str], str] = {}
    for p in problems:
        for e in rereviews(p):
            key = (int(e.get("job") or 0), str(e.get("model") or ""))
            cols.setdefault(key, None)
            unread = e.get("code") in ("llm_output", "en_review_unread", "solver_unreadable")
            mark = "？" if unread else ("○" if e.get("ok") else "×")
            tag = "本文" if e.get("step") == PASSAGE_REREVIEW_STEP else "設問"
            cells[(p.id, key[0], tag)] = mark
    keys = sorted(cols)
    out = ["# 見直しのやり直しの比較", "", "○ 誤りなし／× 誤りあり／？ 読めなかった（本文の見直しもしたときは「設問 本文」の順）", ""]
    out.append("| 問題 | 小単元 | " + " | ".join(f"#{j} {m}" for j, m in keys) + " |")
    out.append("|---|---|" + "---|" * len(keys))
    for p in problems:
        if not any((p.id, j, "設問") in cells for j, _m in keys):
            continue
        row = []
        for j, _m in keys:
            mark = cells.get((p.id, j, "設問"), "—")
            if (p.id, j, "本文") in cells:
                mark += " " + cells[(p.id, j, "本文")]
            row.append(mark)
        out.append(f"| #{p.id} | {labels(p)['subunit']} | " + " | ".join(row) + " |")
    out += ["", "## 誤りありの指摘", ""]
    for p in problems:
        for e in rereviews(p):
            if not e.get("ok"):
                out.append(f"- #{p.id}（#{e.get('job')} {e.get('model')}）{e.get('step')}：{str(e.get('summary', ''))[:300]}")
    return "\n".join(out)


def filter_options(s: Session) -> dict[str, Any]:
    """絞り込みの選択肢（作成役のモデルは、記録のあるものから）。"""
    models = sorted({m for m in s.execute(select(Problem.gen_model).distinct()).scalars() if m})
    return {"gen_models": models, "levels": LEVELS, "statuses": STATUSES}
