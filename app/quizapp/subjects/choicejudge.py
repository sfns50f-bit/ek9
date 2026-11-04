"""本文つきの選択式の科目で共通に使う部品（英語の読解で作り、公共でも使う。フェーズ3で英語から切り出した）。

- 作成役・見直し役の呼び出し（読めなければ 1 回だけ呼び直す）
- 設問の欄（Question: / A: … / Answer: / Wrong: / Evidence: / Hint: / Explanation:）の読み取り
- 選択肢ごとの判定「番号: 判定 — 引用: "本文の文" — 理由」の読み取り、引用の取り出し、足りない判定の聞き直し
- 見直しの「問題なし／問題あり」の読み取り、直しを頼む文章

科目に特有の言葉（判定の名前・プロンプト）は科目の側に置く。web からも読まれうるので、llm は関数の中で読む
（CLAUDE.md のルール 1）。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable

LETTERS = "ABCD"

# 判定の並び（番号ごとの判定）を書かせるときの欄
JUDGMENTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"judgments": {"type": "string", "description": "番号ごとの判定（1 行に 1 つ。「番号: 判定 — 理由」）"}},
    "required": ["judgments"],
}

# 見直し（「問題なし／問題あり」）の欄
REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "description": "「問題なし」または「問題あり」"},
        "issues": {"type": "string", "description": "問題があればその内容（なければ「なし」）"},
    },
    "required": ["verdict"],
}


def set_label(llm: Any, label: str) -> None:
    """LLM の呼び出しの記録に付ける種類（作成・直し・検証・見直し…）。"""
    if hasattr(llm, "label"):
        llm.label = label


def chat_retry(llm: Any, model: str, system: str, user: str, schema: dict[str, Any], *,
               temperature: float = 0.8) -> dict[str, Any]:
    """作成役を呼ぶ。出力が読めなかったときは、作り直しの回数を使わずに 1 回だけ呼び直す。"""
    from ..llm import LLMError

    try:
        return llm.chat_json(model, system, user, schema, temperature=temperature)
    except LLMError:
        return llm.chat_json(model, system, user, schema, temperature=temperature)


def ask(llm: Any, model: str, system: str, user: str, schema: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """検証役・見直し役を呼ぶ（読めなければ 1 回だけ呼び直す）。(出力, 生の出力)。読めなければ ({}, 理由)。"""
    from ..llm import LLMError

    last = ""
    for _ in range(2):
        try:
            out = llm.chat_json(model, system, user, schema, temperature=0.2)
            return out, "\n".join(f"[[{k}]]\n{v}" for k, v in out.items())
        except LLMError as exc:
            last = f"読めない出力（{exc}）\n{getattr(exc, 'raw', '')}"
    return {}, last


def verdict_line(out: dict[str, Any]) -> tuple[str, str]:
    """見直しの結果（1 行目が「問題なし／問題あり」で始まるか。指示を書き写したときに誤って弾かない）。"""
    verdict = next((ln.strip(" 　。.*「」") for ln in str(out.get("verdict") or "").splitlines() if ln.strip()), "")
    return verdict, str(out.get("issues") or "").strip()


def fix_user(original: str, previous: dict[str, Any], issues: str, keys: list[str]) -> str:
    """前回の出力を同じ見出しの形で見せて、指摘した点だけを直させる。"""
    shown = "\n".join(f"[[{k}]]\n{str(previous.get(k) or '').strip()}" for k in keys)
    return "\n".join([
        original, "", "前回の出力:", shown, "",
        "直してほしい点: " + issues,
        "指摘した点だけを直し、ほかはできるだけそのままにして、同じ見出しの形ですべて書き直してください。",
    ])


# --- 設問の欄 ---------------------------------------------------------------------

_FIELD_RE = re.compile(r"^\s*[*_#>-]*\s*\(?(Question|Answer|Wrong|Evidence|Hint|Explanation|[A-D])(?:\s*\d+)?\)?"
                       r"\s*[*_]*\s*[:：.)]\s*[*_]*\s*(.*)$", re.I)


def _strip_outer_quotes(text: str) -> str:
    t = (text or "").strip()
    pairs = {'"': '"', "“": "”", "「": "」", "『": "』"}
    if len(t) >= 2 and t[0] in pairs and t[-1] == pairs[t[0]]:
        return t[1:-1].strip()
    return t


def parse_question(text: str, strip_quotes: Callable[[str], str] = _strip_outer_quotes) -> dict[str, Any]:
    """[[q1]] の中身（Question: … / A: … / Answer: … の各行）を読む。Explanation より後は全部解説。"""
    fields: dict[str, str] = {}
    current = ""
    for line in (text or "").splitlines():
        m = _FIELD_RE.match(line) if current != "explanation" else None
        if m:
            name = m.group(1)
            current = name.upper() if len(name) == 1 else name.lower()
            fields[current] = m.group(2).strip()
        elif current:
            fields[current] = (fields[current] + "\n" + line).strip()
    answer = re.search(r"[A-D]", fields.get("answer", "").upper())
    wrong = {k.upper(): v.lower() for k, v in re.findall(r"([A-Da-d])\s*[=:：]\s*([a-z_]+)", fields.get("wrong", ""))}
    evidence = strip_quotes(fields.get("evidence", ""))
    return {"question": fields.get("question", ""), "options": [fields.get(c, "") for c in LETTERS],
            "answer": LETTERS.index(answer.group()) if answer else -1, "wrong": wrong, "evidence": evidence,
            "hint": fields.get("hint", ""), "explanation": fields.get("explanation", "")}


# --- 選択肢ごとの判定 -----------------------------------------------------------------

# 一部の選択肢しか答えなかったときの聞き直し（英語の第 12 回のレビュー 3-2）
MISSING_HEAD = "\n\n前の判定には"


def missing_note(got: dict[int, Any], n: int) -> str:
    missing = [str(i + 1) for i in range(n) if i not in got]
    return (f"{MISSING_HEAD}選択肢 {'・'.join(missing)} の判定がありませんでした。"
            f"1〜{n} のすべての選択肢について、1 行ずつ「番号: 判定 — 理由」の形で書いてください。")


def parse_judgments(text: str, labels: dict[str, Any], n: int) -> dict[int, tuple[str, str]]:
    """「番号: 判定 — 理由」の行を読む。{番号（0 から）: (判定, 理由)}。長いラベルから先に照らす。"""
    names = sorted(labels, key=len, reverse=True)
    out: dict[int, tuple[str, str]] = {}
    for line in (text or "").splitlines():
        # 思考なしの小さいモデルは行を「[[1: 通る — …]]」や「[[」「]]」で囲むことがある（英語の第 10 回のレビュー 4-5）
        line = re.sub(r"\s*\]\]\s*$", "", re.sub(r"^\s*\[\[\s*", "", line))
        m = re.match(r"^\s*[*#-]*\s*\(?([0-9０-９]+)\)?\s*[:：.)、]\s*[*「]*(.*)$", line)
        if not m:
            continue
        k = int(unicodedata.normalize("NFKC", m.group(1))) - 1
        rest = m.group(2)
        label = next((name for name in names if rest.startswith(name) or rest.lstrip("*「 ").startswith(name)), None)
        if label is None:
            label = next((name for name in names if name in rest[:12]), None)
        if 0 <= k < n and label is not None and k not in out:
            reason = re.sub(r"^[\s—\-ー:：、。*」]+", "", rest.split(label, 1)[1]) if label in rest else ""
            out[k] = (label, reason.strip())
    return out


def quote_of(reason: str, min_len: int = 12) -> str:
    """判定の理由の中の「引用: "…"」。「引用:」を付けずに判定のすぐ後ろに "…" と書いた形も読む（英語の第 12 回の
    レビュー 5-1）。なければ空。min_len：「引用:」のない形で引用とみなす最短の長さ。"""
    m = re.search(r"引用\s*[:：]\s*[\"“「『]([^\"”」』]+)[\"”」』]", reason or "") \
        or re.match(r"^[\s—\-ー:：]*[\"“「『]([^\"”」』]{%d,})[\"”」』]" % min_len, reason or "")
    return m.group(1).strip() if m else ""


def check_lines(checks: list[tuple[str, bool | None, str]]) -> str:
    """検証・見直しの確認の並びを、記録の行にする（○ 合格・× 不合格・？ 読めなかった）。"""
    return "\n".join(f"{'○' if ok else '×' if ok is False else '？'} {name}：{text}" for name, ok, text in checks)
