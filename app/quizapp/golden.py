"""見本集（検証・見直しの検出力を測るテスト用の問題集。第 6 回のレビューの 5 章）。

見本集は `docs/reviews/golden_set_<科目>.json`（docker では読み取り専用で /app/golden にバインド）。
各問に expected（reject＝致命的な欠陥あり・弾くべき／pass＝問題なし・通すべき）と欠陥の型を持つ。
`to_add_from_db.ids` の問題は、データベースに残っていれば見本集に加える（書き出しに選択肢がなかった
不合格の下書きなど。削除・不合格でも行は残っているので見つかる）。

測るときは、見本集の問題を通常の問題と同じ形（Sample）にして、科目の verify / review / review_passage と
機械的な点検をかける（worker の `_golden`）。結果は依頼（jobs.results）に残し、ストックや問題の記録は変えない。
web からも読むので、LLM・sympy は読み込まない。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .db import Problem
from .subjects import get_subject

LETTERS = "ABCDEFGH"
OUTCOMES = {"reject": "弾いた", "pass": "通した", "unsure": "要確認"}
# borderline（微妙）は採点に含めないが、結果は表に出す（第 7 回のレビュー G）
EXPECTED = {"reject": "弾くべき", "pass": "通すべき", "borderline": "微妙"}
# 読めなかっただけの結果は「弾いた」に数えず「要確認」にする
UNSURE_CODES = ("llm_output", "solver_unreadable", "en_review_unread", "needs_check")


def golden_dirs() -> list[Path]:
    env = os.environ.get("GOLDEN_DIR", "")
    dirs = [Path(env)] if env else []
    return dirs + [Path("/app/golden"), Path(__file__).resolve().parents[2] / "docs" / "reviews"]


def load(subject: str) -> dict[str, Any] | None:
    """見本集のファイルを読む。なければ None。"""
    for d in golden_dirs():
        path = d / f"golden_set_{subject}.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            data["_path"] = str(path)
            return data
    return None


@dataclass
class GoldenPassage:
    body: str
    gen_model: str = ""
    id: int | None = None
    think: str = ""


@dataclass
class Sample:
    """見本集の 1 問。Problem と同じ名前の属性を持ち、科目の verify / review / text_issues にそのまま渡せる。"""

    id: int
    expected: str
    defect_type: str
    note: str
    subject: str
    unit: str
    template: str
    format: str
    question: str
    choices: list[dict[str, Any]]
    answer: dict[str, Any]
    answer_display: str
    explanation: str
    hint: str
    params: dict[str, Any] = field(default_factory=dict)
    passage: GoldenPassage | None = None
    gen_model: str = ""
    origin: str = "file"  # file（見本集のファイル）/ db（データベースから加えた）
    item: dict[str, Any] = field(default_factory=dict)  # 見本集の形（書き出し用）


def _template_of(subject_id: str, category: str) -> tuple[str, str] | None:
    """「英語／語彙／空所に入る語」から (単元 ID, 小単元 ID)。"""
    sub = get_subject(subject_id)
    parts = category.split("／")
    if sub is None or len(parts) < 3:
        return None
    for u in sub.units():
        for su in u.subunits:
            if u.label == parts[1] and su.label == parts[2]:
                return u.id, su.id
    return None


def _choice(sub: Any, o: dict[str, Any]) -> dict[str, Any]:
    """書き出しの選択肢（間違いの種類は名前。「X／見直しでは「Y」」は判定が分かれたもの）を問題の形に。"""
    by_label = {v: k for k, v in sub.mistakes.items()}
    c: dict[str, Any] = {"text": str(o.get("text", "")), "correct": bool(o.get("correct")), "mistake": None}
    if not c["correct"]:
        label, _sep, doubt = str(o.get("mistake") or "").partition("／見直しでは")
        c["mistake"] = by_label.get(label, label or None)
        if doubt:
            c["label_doubt"] = by_label.get(doubt.strip("「」"), doubt.strip("「」"))
    return c


def sample_from_item(subject_id: str, item: dict[str, Any]) -> Sample | None:
    where = _template_of(subject_id, str(item.get("category", "")))
    sub = get_subject(subject_id)
    if where is None or sub is None:
        return None
    unit, template = where
    choices = [_choice(sub, o) for o in item.get("options") or []]
    answer = str(item.get("answer", ""))
    body = item.get("passage")
    # 見本集のファイルには params がないので、点検に使う値を科目に組み立ててもらう（Subject.golden_params）
    params = {} if body else sub.golden_params(template, str(item.get("question", "")),
                                               str(item.get("explanation", "")), choices, answer)
    return Sample(
        id=int(item["id"]), expected=str(item.get("expected", "")), defect_type=str(item.get("defect_type") or ""),
        note=str(item.get("note") or ""), subject=subject_id, unit=unit, template=template,
        format="choice" if choices else "input", question=str(item.get("question", "")), choices=choices,
        answer={"kind": "choice"} if choices else {"kind": "word", "word": answer}, answer_display=answer,
        explanation=str(item.get("explanation", "")), hint=str(item.get("hint", "")), params=params,
        passage=GoldenPassage(str(body), str(item.get("本文作成役", ""))) if body else None,
        gen_model=str(item.get("作成役", "")), origin="file", item=item)


def item_from_problem(p: Problem, expected: str, defect_type: str, note: str) -> dict[str, Any]:
    """データベースの問題を見本集の形にする（書き出しと、見本集のファイルに足すため）。"""
    sub = get_subject(p.subject)
    unit, su = sub.subunit_map().get(p.template, (None, None)) if sub else (None, None)
    item: dict[str, Any] = {
        "expected": expected, "defect_type": defect_type or None, "note": note or None, "id": p.id,
        "category": f"{sub.name if sub else p.subject}／{unit.label if unit else p.unit}／{su.label if su else p.template}",
        "作成日時": p.created_at.strftime("%Y/%m/%d %H:%M") if p.created_at else "", "作成役": p.gen_model,
    }
    if p.passage is not None:
        item["本文作成役"] = p.passage.gen_model
    item.update({
        "passage": p.passage.body if p.passage is not None else None, "question": p.question,
        "options": [{"key": LETTERS[i], "text": c.get("text", ""), "correct": bool(c.get("correct")),
                     "mistake": None if c.get("correct") else (sub.mistakes.get(c.get("mistake") or "", c.get("mistake"))
                                                               if sub else c.get("mistake"))}
                    for i, c in enumerate(p.choices)],
        "answer": p.answer_display or str(p.answer.get("word", "")), "hint": p.hint, "explanation": p.explanation,
    })
    return item


def sample_from_problem(p: Problem, expected: str, defect_type: str, note: str) -> Sample:
    """データベースの問題から（params・本文の記録もそのまま使う）。"""
    return Sample(
        id=p.id, expected=expected, defect_type=defect_type, note=note, subject=p.subject, unit=p.unit,
        template=p.template, format=p.format, question=p.question, choices=p.choices, answer=p.answer,
        answer_display=p.answer_display, explanation=p.explanation, hint=p.hint, params=p.params,
        passage=GoldenPassage(p.passage.body, p.passage.gen_model or "", p.passage.id) if p.passage is not None else None,
        gen_model=p.gen_model, origin="db", item=item_from_problem(p, expected, defect_type, note))


@dataclass
class GoldenSet:
    subject: str
    path: str
    samples: list[Sample]
    db_wanted: list[int]  # to_add_from_db の番号
    db_found: list[int]  # データベースにあって加えた番号
    db_missing: list[str]  # 加えられなかった番号と理由
    data: dict[str, Any]

    @property
    def counts(self) -> dict[str, int]:
        return {k: sum(1 for x in self.samples if x.expected == k) for k in EXPECTED}

    def by_id(self) -> dict[int, Sample]:
        return {x.id: x for x in self.samples}


def golden_set(s: Session, subject: str) -> GoldenSet | None:
    """見本集のファイルの問題に、to_add_from_db のうちデータベースに残っている問題を加えたもの。"""
    data = load(subject)
    if data is None:
        return None
    samples = [x for x in (sample_from_item(subject, it) for it in data.get("items", [])) if x is not None]
    have = {x.id for x in samples}
    extra = data.get("to_add_from_db") or {}
    wanted = [int(i) for i in extra.get("ids", [])]
    rows = {p.id: p for p in s.execute(select(Problem).options(selectinload(Problem.passage))
                                       .where(Problem.id.in_(wanted))).scalars()} if wanted else {}
    found, missing = [], []
    for pid in wanted:
        p = rows.get(pid)
        if pid in have:
            continue
        if p is None:
            missing.append(f"#{pid}（データベースにない）")
        elif p.subject != subject:
            missing.append(f"#{pid}（ほかの科目）")
        elif p.format == "choice" and not p.choices:
            missing.append(f"#{pid}（選択肢が残っていない）")
        else:
            samples.append(sample_from_problem(p, str(extra.get("expected", "reject")),
                                               str(extra.get("defect_type", "multiple_correct")),
                                               f"{extra.get('note', '')}（不合格の理由：{p.reject_reason[:200]}）"
                                               if p.reject_reason else str(extra.get("note", ""))))
            found.append(pid)
    return GoldenSet(subject, data.get("_path", ""), samples, wanted, found, missing, data)


def export_json(gs: GoldenSet) -> str:
    """データベースから加えた問題も items に入れた見本集（ファイルを差し替えるときに使う）。"""
    data = {k: v for k, v in gs.data.items() if k != "_path"}
    data["items"] = [x.item for x in gs.samples]
    left = [i for i in gs.db_wanted if i not in gs.db_found]
    data["to_add_from_db"] = {**(gs.data.get("to_add_from_db") or {}), "ids": left}
    return json.dumps(data, ensure_ascii=False, indent=1) + "\n"


# --- 結果の集計（web の書き出し） -------------------------------------------

def outcome_of(steps: list[dict[str, Any]]) -> str:
    """段階ごとの結果から、弾いた（reject）／通した（pass）／要確認（unsure）。

    検証は回数分の判定がすべて不合格なら弾いた、一部だけ不合格（判定が割れた）なら要確認（第 6 回のレビュー 2-4）。
    読めなかっただけの不合格も要確認。"""
    verify = [st for st in steps if st.get("step") == "検証"]
    others = [st for st in steps if st.get("step") != "検証"]
    if any(not st.get("ok") and st.get("code") not in UNSURE_CODES for st in others):
        return "reject"
    vfail = [st for st in verify if not st.get("ok")]
    if vfail and len(vfail) == len(verify) and any(st.get("code") not in UNSURE_CODES for st in vfail):
        return "reject"
    if vfail or any(not st.get("ok") for st in others):
        return "unsure"
    return "pass"


def config_label(job: Any) -> str:
    parts = [f"検証 {job.verify_model}×{job.verify_count}" if job.verify_count else "検証なし"]
    parts.append(f"見直し {job.review_model or job.verify_model}" if job.explanation_review else "見直しなし")
    if job.review_passage:
        parts.append("本文も")
    if getattr(job, "options", {}).get("golden_fast") == "on":
        parts.append("検証で弾いた問題の見直しは省略")
    if getattr(job, "options", None):  # 検証のやり方（第 7 回のレビュー C・D・E。今の方式と新しい方式を並べて比べる）
        from .runtime import option_label
        from .subjects import get_subject

        parts.append(option_label(get_subject(job.subject), job.options))
    return "／".join(parts)


def score(results: list[dict[str, Any]], expected: dict[int, str] | None = None) -> dict[str, int]:
    """成績。expected（{番号: 期待}）を渡せば、測ったときの期待ではなく今の見本集の期待で数える
    （見本集を直したあとも、前の測定を同じ物差しで比べられるように）。微妙は数えない。"""
    out = {"reject_total": 0, "reject_caught": 0, "reject_unsure": 0, "pass_total": 0, "pass_wrong": 0,
           "pass_unsure": 0}
    for r in results:
        if r.get("partial"):  # 測っている途中の問題は数えない
            continue
        exp = (expected or {}).get(r.get("id"), r.get("expected"))
        r = {**r, "expected": exp}
        if r.get("expected") == "reject":
            out["reject_total"] += 1
            out["reject_caught"] += r.get("outcome") == "reject"
            out["reject_unsure"] += r.get("outcome") == "unsure"
        elif r.get("expected") == "pass":
            out["pass_total"] += 1
            out["pass_wrong"] += r.get("outcome") == "reject"
            out["pass_unsure"] += r.get("outcome") == "unsure"
    return out


def score_text(sc: dict[str, int]) -> str:
    """成績の文。要確認は公開しない（出題の在庫に入れない）ので、「弾いた＋要確認」を「止めた」としても数える（G）。"""
    return (f"致命的な欠陥あり {sc['reject_caught']}／{sc['reject_total']} 問を弾いた"
            f"（要確認 {sc['reject_unsure']}）・問題なし {sc['pass_wrong']}／{sc['pass_total']} 問を誤って弾いた"
            f"（要確認 {sc['pass_unsure']}）。要確認を公開しない扱いでは、弾くべき問題を "
            f"{sc['reject_caught'] + sc['reject_unsure']}／{sc['reject_total']} 問止め、通すべき問題を "
            f"{sc['pass_wrong'] + sc['pass_unsure']}／{sc['pass_total']} 問止めた")


# これまでの記録から参考として並べる見直しの段階
_RECORD_STEPS = ("解説の見直し", "見直しのやり直し")


def past_reviews(s: Session, ids: list[int]) -> dict[int, dict[str, bool]]:
    """データベースに残っている問題の、これまでの見直しの記録（モデルごとの最後の結果。参考に並べる）。"""
    out: dict[int, dict[str, bool]] = {}
    for p in s.execute(select(Problem).where(Problem.id.in_(ids))).scalars():
        for e in p.verification:
            if e.get("step") in _RECORD_STEPS and e.get("model"):
                out.setdefault(p.id, {})[str(e["model"])] = bool(e.get("ok"))
    return out


def _kind(x: Sample | None) -> str:
    if x is None:
        return "その他"
    return "読解" if x.passage is not None else "スペル" if x.format != "choice" else "語彙"


def timing_text(gs: GoldenSet, results: list[dict[str, Any]]) -> str:
    """段階ごと・種類ごとの 1 問 1 回あたりの平均の秒数（第 8 回のレビュー 4-1。読解だけ思考ありの E を比べる）。
    記録のない前の版の測定は空。同じ本文の結果を写した本文の見直しは数えない。"""
    samples = gs.by_id()
    buckets: dict[str, list[float]] = {}
    for r in results:
        for st in r.get("steps", []):
            if "secs" not in st or st.get("key") == "text" or (st.get("key") == "passage" and not st["secs"]):
                continue
            name = f"検証（{_kind(samples.get(r['id']))}）" if str(st.get("key", "")).startswith("verify") else st["step"]
            buckets.setdefault(name, []).append(float(st["secs"]))
    order = ["検証（読解）", "検証（語彙）", "検証（スペル）", "解説の見直し", "本文の見直し"]
    return "・".join(f"{k} {sum(v) / len(v):.1f} 秒（{len(v)} 回）" for k in sorted(buckets, key=lambda k: (
        order.index(k) if k in order else 99, k)) for v in [buckets[k]])


def to_markdown(gs: GoldenSet, jobs: list[Any], past: dict[int, dict[str, bool]]) -> str:
    """見本集の測定の比べる表（問題 × 構成 → 弾いた／通した／要確認）と、構成ごとの成績・理由。"""
    expected = {x.id: x.expected for x in gs.samples}
    out = ["# 見本集での測定", "",
           f"見本集：{gs.path}（{len(gs.samples)} 問：弾くべき {gs.counts['reject']}・通すべき {gs.counts['pass']}・"
           f"微妙（採点に含めない） {gs.counts['borderline']}。"
           f"データベースから加えた問題 {len(gs.db_found)}／{len(gs.db_wanted)}）", ""]
    if gs.db_missing:
        out += [f"データベースから加えられなかった問題：{'、'.join(gs.db_missing)}", ""]
    out += [f"目標：要確認を公開しない扱いで、弾くべき問題をすべて止める。通すべき問題を誤って止めるのは "
            f"{gs.counts['pass']} 問中 2〜3 問まで。", "",
            "## 構成ごとの成績", ""]
    if not jobs:
        out += ["（まだ測っていません）", ""]
    for j in jobs:
        out.append(f"- 依頼 #{j.id}（{config_label(j)}）：{score_text(score(j.results, expected))}"
                   f"{'' if j.status == 'done' else f'（{j.status}：途中まで）'}")
        timing = timing_text(gs, j.results)
        if timing:
            out.append(f"  - 所要時間：{timing}")
    out.append("")
    models = sorted({m for v in past.values() for m in v})
    head = ["問題", "期待", "欠陥の型"] + [f"#{j.id}" for j in jobs] + [f"記録：{m}" for m in models]
    out += ["## 問題ごと", "", "○ 期待どおり／× 期待と違う／・ 微妙（採点に含めない）。「記録」はデータベースにあるこれまでの見直しの結果（参考）", "",
            "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    types = gs.data.get("defect_types") or {}
    results = [{r["id"]: r for r in j.results} for j in jobs]
    for x in gs.samples:
        cells = [f"#{x.id}{'（DB）' if x.origin == 'db' else ''}", EXPECTED.get(x.expected, x.expected),
                 types.get(x.defect_type, x.defect_type) or "—"]
        for res in results:
            r = res.get(x.id)
            if r is None:
                cells.append("—")
            else:
                mark = ("・" if x.expected == "borderline" else "○" if r["outcome"] == x.expected
                        else "×" if r["outcome"] != "unsure" else "？")
                cells.append(f"{mark} {OUTCOMES[r['outcome']]}")
        for m in models:
            v = past.get(x.id, {}).get(m)
            cells.append("—" if v is None else ("問題なし" if v else "誤りあり"))
        out.append("| " + " | ".join(cells) + " |")
    out.append("")
    for j in jobs:
        out += [f"## 依頼 #{j.id}（{config_label(j)}）の理由", ""]
        for r in j.results:
            x = gs.by_id().get(r["id"])
            bad = [st for st in r.get("steps", []) if not st.get("ok")]
            exp = expected.get(r["id"], r.get("expected", ""))
            mark = "・" if exp == "borderline" else "○" if r["outcome"] == exp else "×"
            line = f"- {mark} #{r['id']}（{EXPECTED.get(exp, '')}・{OUTCOMES[r['outcome']]}）"
            if x is not None and x.note:
                line += f" 見本集のメモ：{x.note[:120]}"
            out.append(line)
            for st in bad:
                out.append(f"  - {st.get('step')}（{st.get('model') or '—'}）：{str(st.get('summary', ''))[:300]}")
                if st.get("code") in UNSURE_CODES and st.get("raw"):  # 読めなかった出力そのもの（原因を確かめる）
                    out += ["", "    ```", *[f"    {ln}" for ln in str(st["raw"])[:700].splitlines()], "    ```", ""]
        out.append("")
    return "\n".join(out)
