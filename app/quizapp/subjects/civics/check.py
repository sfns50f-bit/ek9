"""公共の設問の機械的な点検（数学の textcheck・英語の check の代わり）。sympy なし・ネットワークなし（web も読む）。"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any

from ... import latexfix

_JA_RE = re.compile(r"[぀-ヿ一-鿿]")
_HANGUL_RE = re.compile(r"[가-힯]")
# 「誤っているもの」を選ばせる設問（選択肢ごとの判定は「合うものが 1 つ」を確かめるので作らせない）
_NEGATIVE_RE = re.compile(r"誤って|誤りを含む|適当でない|適切でない|当てはまらない|正しくない|合わない|含まれない|ないもの")
# 意見・価値判断を問う設問（意見が分かれるテーマは制度や立場を問う形にする。仕様書）
_OPINION_RE = re.compile(r"べきだと(思|考え)|べきか|あなた(は|の考え)|賛成|反対で|望ましいと(思|考え)|どちらが正しい|良いと思")
# 選択肢を記号で呼ぶ（画面で並べ替えるので使えない）
_LETTER_REF_RE = re.compile(r"選択肢\s*[A-DＡ-Ｄ①-④1-4１-４]|(?<![A-Za-zＡ-Ｚａ-ｚ])[A-DＡ-Ｄ]\s*(は|が|の選択肢|も)")
# 生徒に見せる文章に書かせない言葉（アプリの内部の言葉・作成の指示）
_INTERNAL = ("与えられた", "指示", "指定された", "作成役", "検証役", "Evidence", "Wrong:", "[[")


def flat(text: str) -> str:
    """照らし合わせ用：NFKC・空白と引用符を除く。"""
    t = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"[\s「」『』\"“”]", "", t)


def contains(passage: str, quote: str) -> bool:
    """根拠の文が本文にそのまま入っているか（空白・全角半角・かぎかっこの違いは無視する）。"""
    q = flat(quote)
    return len(q) >= 6 and q in flat(passage)


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[。！？])|\n", text or "") if flat(s)]


def near_quote(passage: str, quote: str, ratio: float = 0.9) -> bool:
    """検証役の引用が本文にあるか。そのまま入っているか、本文の 1 文と 9 割以上の字が同じ並びで合えば同じ文とみなす
    （送り仮名・句読点の 1 字違いなど。作成役の根拠の文は contains で一字一句照らす）。"""
    if contains(passage, quote):
        return True
    q = flat(quote)
    if len(q) < 10:
        return False
    for s in sentences(passage):
        sm = difflib.SequenceMatcher(None, q, flat(s), autojunk=False)
        if sum(b.size for b in sm.get_matching_blocks()) >= ratio * len(q):
            return True
    return False


# 語句として見る字の並び（漢字・カタカナ 3 字以上）
_TERM_RE = re.compile(r"[\u4e00-\u9fffー\u30a0-\u30ff]{3,}")


# 年・数値・順位（正解を決める値。第 14 回のレビュー 3 章 C-2・C-3）
_NUM_RE = re.compile(r"\d{4}年|\d+(?:\.\d+)?(?:%|位|倍|か国|ヵ国|カ国|歳|人|円|社)")


def numbers(text: str) -> list[str]:
    return list(dict.fromkeys(_NUM_RE.findall(flat(text))))


def answer_terms_in_hint(question: str, options: list[str], answer: int, hint: str) -> list[str]:
    """正解の選択肢にだけある語句（設問・誤答にない漢字・カタカナ 3 字以上と、年・数値）のうち、ヒントに書かれているもの
    （第 13 回のレビュー：ヒントが「ナチズムの台頭と…を確認」と正解の語句を指していた）。"""
    others = flat(question) + "".join(flat(o) for i, o in enumerate(options) if i != answer)
    terms = {t for t in _TERM_RE.findall(unicodedata.normalize("NFKC", options[answer])) if t not in others}
    terms |= {t for t in numbers(options[answer]) if t not in others}
    h = flat(hint)
    return sorted(t for t in terms if t in h)


def best_sentence(passage: str, quote: str, ratio: float = 0.9) -> str:
    """引用に最も近い資料の 1 文（9 割以上の字が同じ並びで合うもの）。なければ空。"""
    q = flat(quote)
    if len(q) < 10:
        return ""
    best, score = "", 0.0
    for s in sentences(passage):
        fs = flat(s)
        sm = difflib.SequenceMatcher(None, q, fs, autojunk=False)
        hit = sum(b.size for b in sm.get_matching_blocks())
        rate = min(hit / len(q), hit / max(len(fs), 1))  # 引用と文の両方の 9 割が合う（一部だけの文を選ばない）
        if rate > score:
            best, score = s.strip(), rate
    return best if score >= ratio else ""


def fix_evidence(q: dict[str, Any], passage: str) -> str:
    """根拠の文が資料にそのまま入っていないとき、9 割以上合う資料の文があれば、根拠をその原文に差し替える
    （第 14 回のレビュー 3 章 C-1：かぎかっこ 1 字の欠けなどで落ちていた）。直したことの説明（なければ空）。"""
    ev = q.get("evidence") or ""
    if not ev or contains(passage, ev):
        return ""
    best = best_sentence(passage, ev)
    if not best:
        return ""
    q["evidence"] = best
    return f"根拠の文を資料の原文に差し替えた（「{ev[:30]}…」→「{best[:30]}…」）"


def similar(a: str, b: str, ratio: float = 0.85) -> bool:
    fa, fb = flat(a), flat(b)
    return bool(fa and fb) and difflib.SequenceMatcher(None, fa, fb, autojunk=False).ratio() >= ratio


def same_wrongs(mine: list[str], other: list[str]) -> list[str]:
    """2 つの設問の誤答のうち、ほぼ同じもの（第 14 回のレビュー 3 章 C-4：#1443 と #1445）。"""
    return [a for a in mine if any(similar(a, b) for b in other)]


def build_explanation(choices: list[dict[str, Any]], evidence: str) -> str:
    """選択肢ごとの理由（choices[*]["why"]）から解説を組み立てる（第 14 回のレビュー 3 章 B）。正解の理由 → 資料の根拠
    → 誤答の理由（選択肢の並びの順。選択肢の文を見出しにする）。"""
    right = next((c for c in choices if c.get("correct")), None)
    lines = []
    if right is not None:
        lines += [f"正解：「{right['text']}」", str(right.get("why", "")).strip()]
    if evidence:
        lines.append(f"資料の根拠：「{evidence}」")
    wrong = [c for c in choices if not c.get("correct")]
    if wrong:
        lines += ["", "ほかの選択肢："] + [f"・「{c['text']}」：{str(c.get('why', '')).strip()}" for c in wrong]
    return "\n".join(x for x in lines if x is not None).strip()


def japanese_issues(text: str, field: str, *, min_len: int = 0) -> list[str]:
    out = []
    body = (text or "").strip()
    if not body:
        return [f"{field}が空"]
    if not _JA_RE.search(body):
        out.append(f"{field}が日本語で書かれていない")
    if min_len and len(re.sub(r"\s", "", body)) < min_len:
        out.append(f"{field}が短すぎる")
    zh = latexfix._simplified_chinese(body)
    if zh:
        out.append(f"{field}に中国語の字（{''.join(zh[:5])}）")
    if _HANGUL_RE.search(body):
        out.append(f"{field}に韓国語の文字")
    if latexfix._META_RE.search(body):
        out.append(f"{field}が作成の指示に触れている")
    inside = [w for w in _INTERNAL if w in body]
    if inside:
        out.append(f"{field}にアプリの内部の言葉（{'・'.join(inside[:3])}）がある")
    return out


def normalize(text: str) -> str:
    """意味を変えずに直せる表記の揺れ：文字どおりの \\n、簡体字、Markdown の強調の記号、前後の空白。"""
    t = (text or "").replace("\r\n", "\n").replace("\\n", "\n")
    t = t.translate(latexfix._ZH_TO_JA)
    t = re.sub(r"^([ \t]*)[*+][ \t]+", r"\1- ", t, flags=re.M)
    t = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), t)
    return t.strip()


def question_issues(q: dict[str, Any], passage: str, mistakes: dict[str, str], *, why: bool = False) -> list[str]:
    """設問 1 つの点検。q は choicejudge.parse_question の形。why=True（作成のとき）は選択肢ごとの理由（Why A〜D）を
    点検する。False のときも理由があれば理由を、なければ解説（前の版の問題）を点検する。"""
    out = []
    opts = [o.strip() for o in q["options"]]
    if not q["question"].strip():
        out.append("設問が空")
    else:
        out += [x for x in japanese_issues(q["question"], "設問") if not x.endswith("が空")]
        if _NEGATIVE_RE.search(q["question"]):
            out.append("「誤っているもの・適当でないもの」を選ばせる設問（「資料の内容に合うもの」を選ばせる形にする）")
        if _OPINION_RE.search(q["question"]):
            out.append("意見・価値判断を問う設問（制度のしくみや、それぞれの立場の主張を問う形にする）")
    if any(not o for o in opts):
        out.append("選択肢が 4 つそろっていない")
    elif len({flat(o) for o in opts}) < len(opts):
        out.append("選択肢が重複している")
    if not 0 <= q["answer"] < len(opts):
        out.append("正解の記号が読めない")
        return out
    for i, o in enumerate(opts):
        if i != q["answer"] and q["wrong"].get("ABCD"[i]) not in mistakes:
            out.append(f"誤答 {'ABCD'[i]} に間違いの種類（{'・'.join(mistakes)}）がない")
        if o and not _JA_RE.search(o):
            out.append(f"選択肢 {'ABCD'[i]} が日本語で書かれていない")
    if not q["evidence"]:
        out.append("根拠の文がない")
    elif not contains(passage, q["evidence"]):
        out.append("根拠の文が資料にそのまま入っていない（資料から一字一句写す）")
    elif flat(opts[q["answer"]]) and flat(opts[q["answer"]]) == flat(q["evidence"]):
        out.append("正解の選択肢が根拠の文の丸写し（言い換える）")
    else:
        missing = [n for n in numbers(opts[q["answer"]]) if n not in flat(q["evidence"])]
        if missing:
            out.append(f"正解の「{'・'.join(missing[:3])}」が根拠の文にない（正解を裏付ける、その年・数値の書かれた文を Evidence に写す）")
    out += japanese_issues(q["hint"], "ヒント")
    whys = q.get("why") or {}
    if why or whys:
        for i, c in enumerate("ABCD"[:len(opts)]):
            text = whys.get(c, "")
            if not text.strip():
                out.append(f"選択肢 {c} の理由（Why {c}）がない")
                continue
            out += japanese_issues(text, f"選択肢 {c} の理由", min_len=15 if i == q["answer"] else 8)
            if _LETTER_REF_RE.search(text):
                out.append(f"選択肢 {c} の理由が選択肢を記号（A・B など）で呼んでいる（中身で呼ぶ）")
    else:
        out += japanese_issues(q["explanation"], "解説", min_len=30)
    right = flat(opts[q["answer"]])
    if right and len(right) >= 6 and right in flat(q["hint"]):
        out.append("ヒントに正解の選択肢がそのまま書かれている")
    leaked = answer_terms_in_hint(q["question"], opts, q["answer"], q["hint"])
    if leaked:
        out.append(f"ヒントに正解の選択肢だけにある語句（{'・'.join(leaked[:3])}）がある（読む場所だけを示す）")
    if _LETTER_REF_RE.search(q["hint"]) or (not (why or whys) and _LETTER_REF_RE.search(q["explanation"])):
        out.append("ヒント・解説が選択肢を記号（A・B など）で呼んでいる（画面で並べ替えるので、中身で呼ぶ）")
    return out
