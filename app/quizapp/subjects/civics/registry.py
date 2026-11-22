"""公共の資料の一覧の運用（2026-11-22。開発者と決めた形）。web も読む（LLM・ネットワークなし）。

- 原文はいつも worker が e-Gov・Wikipedia から取る（sources.py）。ここで扱うのは「どの資料を使うか」だけ
- 資料の一覧 ＝ catalog.SOURCES ＋ 取り込んだ追加（add）− 外したもの（remove）。法令は、一覧を使い切ったら
  条の範囲（auto。既定は catalog.AUTO_LAWS）から、まだ使っていない条を自動で選ぶ
- 開発者は問題検証モードの「資料の一覧」で、今の一覧と鮮度の調査の結果を書き出し（JSON・Markdown）、
  クラウドの AI（開発のセッション）に確かめさせ、返ってきた取り込み用の JSON を読み込む（git は使わない）
- 取り込んだデータは kv の subject_data["civics"]（runtime.subject_data）
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from . import catalog
from .catalog import SPECS, Source

VERSION = 1
LAW_ID_RE = re.compile(r"^[0-9A-Za-z_]{6,40}$")
PART_RE = re.compile(r"^[0-9]{1,4}(_[0-9]{1,3})*$")
MAX_PER = 5
WARN_REMAINING = 10  # 小単元ごとに、あと作れる問題がこれより少なければ警告する


# --- 一覧の組み立て ------------------------------------------------------------------

def _source_of(d: dict[str, Any]) -> Source:
    if d.get("kind") == "law":
        return Source("law", str(d["key"]), tuple(str(p) for p in d.get("parts") or ()),
                      label=str(d.get("label") or ""), law_title=str(d.get("title") or ""))
    return Source("wiki", str(d["key"]), section=str(d.get("section") or ""),
                  label=str(d.get("label") or ""), drop=tuple(str(x) for x in d.get("drop") or ()))


def _source_dict(template: str, s: Source) -> dict[str, Any]:
    if s.kind == "law":
        return {"template": template, "kind": "law", "key": s.key, "parts": list(s.parts), "title": s.law_title,
                "label": s.label}
    return {"template": template, "kind": "wiki", "key": s.key, "section": s.section, "drop": list(s.drop),
            "label": s.label}


def listed(template: str, data: dict[str, Any] | None) -> list[Source]:
    """小単元の資料の一覧（catalog ＋ 取り込んだ追加 − 外したもの）。"""
    data = data or {}
    removed = set(data.get("remove") or ())
    out = [s for s in catalog.SOURCES.get(template, []) if s.id not in removed]
    seen = {s.id for s in out}
    for d in data.get("add") or ():
        if d.get("template") != template:
            continue
        s = _source_of(d)
        if s.id not in removed and s.id not in seen:
            out.append(s)
            seen.add(s.id)
    return out


def auto_laws(template: str, data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """小単元の条文の自動選びの範囲（取り込んだ auto があればそれ、なければ catalog.AUTO_LAWS）。"""
    data = data or {}
    rows = data["auto"] if isinstance(data.get("auto"), list) else [a.as_dict() for a in catalog.AUTO_LAWS]
    return [r for r in rows if r.get("template") == template]


def _parts_of(topic: str) -> tuple[str, list[str]]:
    """'law:<法令ID>:1,2' → (法令ID, ['1', '2'])。法令でなければ ('', [])。"""
    if not topic.startswith("law:"):
        return "", []
    _k, key, parts = (topic.split(":", 2) + ["", ""])[:3]
    return key, [p for p in parts.split(",") if p]


def used_articles(topics: set[str]) -> dict[str, set[str]]:
    """使った本文の題材から、法令ごとに使った条。"""
    out: dict[str, set[str]] = {}
    for t in topics:
        key, parts = _parts_of(t)
        if key:
            out.setdefault(key, set()).update(parts)
    return out


def auto_candidates(rule: dict[str, Any], used: dict[str, set[str]]) -> list[str]:
    """範囲のうち、まだ使っていない条の番号（前から順）。"""
    skip = {str(x) for x in rule.get("skip") or ()} | used.get(str(rule.get("law_id")), set())
    return [str(n) for n in range(int(rule["from"]), int(rule["to"]) + 1) if str(n) not in skip]


def allowed_topic(template: str, topic: str, data: dict[str, Any] | None) -> bool:
    """この小単元の資料として、今も使ってよい題材か（外した資料・一覧にない資料の本文には設問を足さない）。"""
    data = data or {}
    if topic in set(data.get("remove") or ()):
        return False
    if topic in {s.id for s in listed(template, data)}:
        return True
    key, parts = _parts_of(topic)
    if not key or not parts:
        return False
    for rule in auto_laws(template, data):
        if str(rule.get("law_id")) == key and all(
                p.isdigit() and int(rule["from"]) <= int(p) <= int(rule["to"]) for p in parts):
            return True
    return False


def remaining(template: str, used_topics: set[str], data: dict[str, Any] | None) -> int:
    """あと作れる問題の目安：一覧のまだ使っていない資料 × 2 ＋ 条文の自動選びの残りの条 ÷ 1 資料の条の数 × 2。"""
    left = [s for s in listed(template, data) if s.id not in used_topics]
    used = used_articles(used_topics)
    auto = 0
    for rule in auto_laws(template, data):
        n = len(auto_candidates(rule, used))
        auto += n // max(1, int(rule.get("per") or 2))
    return (len(left) + auto) * 2


# --- 書き出し ------------------------------------------------------------------------

def report(data: dict[str, Any], passages: list[dict[str, Any]], problems: list[dict[str, Any]]) -> dict[str, Any]:
    """資料の一覧の書き出し（JSON）。小単元ごとに、資料と、使った本文・問題の数・鮮度の調査の結果。"""
    by_passage: dict[int, Counter[str]] = {}
    for p in problems:
        if p.get("passage_id"):
            by_passage.setdefault(int(p["passage_id"]), Counter())[str(p.get("status"))] += 1
    changed_problems = []
    passage_check = {int(p["id"]): (p.get("source_info") or {}).get("check", "") for p in passages}
    for p in problems:
        pid = p.get("passage_id")
        if pid and passage_check.get(int(pid)) in ("changed", "missing") and p.get("status") == "ready":
            changed_problems.append({"id": p["id"], "template": p.get("template"), "passage_id": pid,
                                     "check": passage_check[int(pid)], "question": p.get("question", "")})
    templates = []
    for t, spec in SPECS.items():
        mine = [p for p in passages if p.get("template") == t]
        topics_used = {p.get("topic") for p in mine if p.get("status") != "rejected" and p.get("topic")}
        rows = []
        listed_ids = set()
        for s in listed(t, data):
            listed_ids.add(s.id)
            use = [p for p in mine if p.get("topic") == s.id]
            rows.append(_row(t, s.id, "list", s.name, use, by_passage, source=_source_dict(t, s)))
        # 一覧にない題材の本文（自動で選んだ条・外した資料・前の版の資料）
        for topic in sorted({p.get("topic") for p in mine if p.get("topic")} - listed_ids):
            use = [p for p in mine if p.get("topic") == topic]
            origin = "auto" if allowed_topic(t, topic, data) else "removed"
            rows.append(_row(t, topic, origin, use[0].get("title", topic) if use else topic, use, by_passage))
        ready = sum(r["problems"].get("ready", 0) for r in rows)
        templates.append({"template": t, "label": spec.label, "level": spec.level,
                          "remaining": remaining(t, topics_used, data), "ready": ready,
                          "auto": [{**r, "unused": len(auto_candidates(r, used_articles(topics_used)))}
                                   for r in auto_laws(t, data)],
                          "sources": rows})
    return {"subject": "civics", "version": VERSION,
            "exported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "data": current(data),
            "templates": templates,
            "changed_problems": changed_problems}


def _row(template: str, topic: str, origin: str, name: str, use: list[dict[str, Any]],
         by_passage: dict[int, Counter[str]], source: dict[str, Any] | None = None) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    for p in use:
        counts.update(by_passage.get(int(p["id"]), Counter()))
    infos = [p.get("source_info") or {} for p in use]
    last = infos[-1] if infos else {}
    return {"topic": topic, "origin": origin, "name": name, "source": source,
            "passages": [int(p["id"]) for p in use],
            "passage_status": [str(p.get("status", "")) for p in use],
            "fetched_at": str(last.get("fetched_at", ""))[:10], "revision": str(last.get("revision", "")),
            "check": str(last.get("check", "")), "checked_at": str(last.get("checked_at", ""))[:10],
            "check_detail": str(last.get("check_detail", "") or last.get("detail", ""))[:200],
            "source_url": str(use[-1].get("source_url", "")) if use else "",
            "problems": dict(counts)}


def current(data: dict[str, Any] | None) -> dict[str, Any]:
    """今の取り込みのデータ（auto は今効いている範囲を必ず入れる。取り込み用の JSON の元にする）。"""
    data = data or {}
    return {"version": VERSION, "updated_at": data.get("updated_at", ""), "note": data.get("note", ""),
            "add": list(data.get("add") or []), "remove": list(data.get("remove") or []),
            "auto": data["auto"] if isinstance(data.get("auto"), list) else [a.as_dict() for a in catalog.AUTO_LAWS]}


ORIGINS = {"list": "一覧", "auto": "条文の自動選び", "removed": "外した・一覧にない"}
CHECKS = {"same": "同じ", "changed": "**変わった**", "missing": "**見つからない**", "unknown": "調べられなかった", "": "未調査"}


def markdown(rep: dict[str, Any]) -> str:
    """クラウドの AI（開発のセッション）に渡す Markdown。確かめ方と取り込み用の JSON の形を冒頭に書く。"""
    lines = [
        "# 公共の資料の一覧（確かめと更新の依頼）",
        "",
        f"書き出した日時（UTC）：{rep['exported_at']}",
        "",
        "## お願いしたいこと",
        "",
        "これは高校の「公共」の問題を作るアプリが使っている資料の一覧です。原文はアプリが問題を作るときに e-Gov 法令 API と"
        " Wikipedia 日本語版から取ります。次のことを確かめて、**取り込み用の JSON** を 1 つ返してください。",
        "",
        "1. 鮮度の調査で「変わった」「見つからない」とされた資料：今の原文（e-Gov なら最新の条文）を確かめ、"
        "その資料から作った問題（下の「資料が変わった問題」）を、今の原文でも正しいか見る。誤りになった問題は `delete_problems` に番号を入れる",
        "2. 不正確・一方的・古い内容の Wikipedia の資料は `remove` に題材（topic）を入れる。高校の公共に向かない資料も外す",
        "3. あと作れる問題（remaining）が少ない小単元（目安 10 問未満）には、`add` に資料を足す。法令は e-Gov の法令 ID と条の番号"
        "（「第一条の二」は \"1_2\"）を、e-Gov で取れることを確かめてから書く。Wikipedia は記事名（必要なら節）",
        "4. 条文の自動選び（`auto`）の範囲：罰則・附則・手続きの細目の章を含まないように。法令が改正されて条の番号が変わっていたら直す",
        "",
        "## 取り込み用の JSON の形",
        "",
        "下の「今の取り込みのデータ」をもとに、**全体を書き直した形**で返してください（取り込むと丸ごと置き換わります）。",
        "",
        "```json",
        '{"version": 1, "note": "何を変えたかの短い説明",',
        ' "add": [{"template": "cv-labor", "kind": "law", "key": "322AC0000000049", "parts": ["24"], "title": "労働基準法"},',
        '         {"template": "cv-ethics", "kind": "wiki", "key": "実存主義", "section": "", "drop": []}],',
        ' "remove": ["wiki:政党交付金"],',
        ' "auto": [{"template": "cv-labor", "law_id": "322AC0000000049", "title": "労働基準法", "from": 1, "to": 41, "per": 2, "skip": []}],',
        ' "delete_problems": [1234]}',
        "```",
        "",
        "- `template` は下の小単元の ID。`drop` は、その言葉を含む行を本文に入れない（Wikipedia の不正確な 1 文を落とす）",
        "- `per` は 1 つの資料にまとめる条の数（1〜5）。`skip` は使わない条の番号",
        "- `delete_problems` に入れた問題は削除（出題しない）にする。回答の記録は残る",
        "",
        "## 今の取り込みのデータ",
        "",
        "```json",
    ]
    import json

    lines.append(json.dumps(rep["data"], ensure_ascii=False, indent=1))
    lines += ["```", "", "## 資料が変わった・見つからない資料から作った問題（ストックにあるもの）", ""]
    if rep["changed_problems"]:
        for p in rep["changed_problems"]:
            lines.append(f"- #{p['id']}（{p['template']}・本文 #{p['passage_id']}・{CHECKS.get(p['check'], p['check'])}）{p['question'][:120]}")
    else:
        lines.append("（なし）")
    lines += ["", "## 小単元ごとの資料", ""]
    for t in rep["templates"]:
        warn = "　**残りが少ない**" if t["remaining"] < WARN_REMAINING else ""
        lines += [f"### {t['label']}（{t['template']}）", "",
                  f"あと作れる問題の目安：{t['remaining']} 問{warn}／ストック {t['ready']} 問", ""]
        if t["auto"]:
            lines.append("条文の自動選び：" + "、".join(
                f"{a['title']} 第{a['from']}〜{a['to']}条（{a['per']} 条ずつ・未使用 {a['unused']} 条）" for a in t["auto"]))
            lines.append("")
        lines += ["| 題材（topic） | 種類 | 取った日 | 鮮度 | 問題（ストック／不合格／削除） |", "|---|---|---|---|---|"]
        for r in t["sources"]:
            c = r["problems"]
            used = f"{c.get('ready', 0)}／{c.get('rejected', 0)}／{c.get('deleted', 0)}" if r["passages"] else "未使用"
            check = CHECKS.get(r["check"], r["check"]) if r["passages"] else "—"
            lines.append(f"| `{r['topic']}` {r['name'][:30]} | {ORIGINS.get(r['origin'], r['origin'])} | "
                         f"{r['fetched_at'] or '—'} | {check} | {used} |")
        lines.append("")
    return "\n".join(lines)


# --- 取り込み ------------------------------------------------------------------------

def apply(data: dict[str, Any], payload: dict[str, Any]) -> tuple[dict[str, Any], list[int], list[str]]:
    """取り込み用の JSON を確かめて、新しいデータ・削除する問題の番号・説明の行を返す。形が違えば ValueError。"""
    if not isinstance(payload, dict):
        raise ValueError("JSON の一番外側が {…} ではありません")
    if "data" in payload and isinstance(payload["data"], dict) and "add" not in payload:
        payload = {**payload["data"], "delete_problems": payload.get("delete_problems", [])}  # 書き出しをそのまま戻した
    errors: list[str] = []
    add: list[dict[str, Any]] = []
    for i, d in enumerate(payload.get("add") or []):
        where = f"add[{i}]"
        if not isinstance(d, dict) or d.get("template") not in SPECS:
            errors.append(f"{where}：template が小単元の ID ではありません（{d.get('template') if isinstance(d, dict) else d}）")
            continue
        if d.get("kind") == "law":
            parts = [str(p) for p in d.get("parts") or []]
            if not LAW_ID_RE.match(str(d.get("key", ""))) or not parts or not all(PART_RE.match(p) for p in parts):
                errors.append(f"{where}：法令 ID（key）か条の番号（parts）が正しくありません")
                continue
            add.append({"template": d["template"], "kind": "law", "key": str(d["key"]), "parts": parts,
                        "title": str(d.get("title") or "")[:120], "label": str(d.get("label") or "")[:120]})
        elif d.get("kind") == "wiki":
            key = str(d.get("key") or "").strip()
            if not key or len(key) > 120:
                errors.append(f"{where}：記事名（key）がありません")
                continue
            add.append({"template": d["template"], "kind": "wiki", "key": key,
                        "section": str(d.get("section") or "")[:120],
                        "drop": [str(x)[:120] for x in d.get("drop") or [] if str(x).strip()],
                        "label": str(d.get("label") or "")[:120]})
        else:
            errors.append(f"{where}：kind は law か wiki です")
    remove = [str(x) for x in payload.get("remove") or [] if str(x).startswith(("law:", "wiki:"))]
    auto: list[dict[str, Any]] | None = None
    if "auto" in payload:
        auto = []
        for i, r in enumerate(payload.get("auto") or []):
            where = f"auto[{i}]"
            try:
                start, end, per = int(r["from"]), int(r["to"]), int(r.get("per") or 2)
            except (KeyError, TypeError, ValueError):
                errors.append(f"{where}：from・to・per は数です")
                continue
            if r.get("template") not in SPECS or not LAW_ID_RE.match(str(r.get("law_id", ""))):
                errors.append(f"{where}：template か law_id が正しくありません")
                continue
            if not (1 <= start <= end <= 2000 and end - start <= 300 and 1 <= per <= MAX_PER):
                errors.append(f"{where}：範囲は 1〜2000 の中で 300 条まで、per は 1〜{MAX_PER}")
                continue
            auto.append({"template": r["template"], "law_id": str(r["law_id"]), "title": str(r.get("title") or "")[:120],
                         "from": start, "to": end, "per": per, "skip": [str(x) for x in r.get("skip") or []]})
    delete: list[int] = []
    for x in payload.get("delete_problems") or []:
        try:
            delete.append(int(x))
        except (TypeError, ValueError):
            errors.append(f"delete_problems：番号でないもの（{x}）")
    if errors:
        raise ValueError("／".join(errors[:10]))
    new = {"version": VERSION, "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
           "note": str(payload.get("note") or "")[:500], "add": add, "remove": sorted(set(remove))}
    if auto is not None:
        new["auto"] = auto
    old = current(data)
    notes = [f"資料の追加 {len(add)} 件（前は {len(old['add'])} 件）", f"外す資料 {len(new['remove'])} 件（前は {len(old['remove'])} 件）",
             f"条文の自動選びの範囲 {len(auto if auto is not None else old['auto'])} 件"]
    if delete:
        notes.append(f"削除する問題 {len(delete)} 問")
    return new, delete, notes

