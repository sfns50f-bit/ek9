"""英語の文章の機械的な点検（数学の textcheck / latexfix の代わり）。sympy なし（web の問題検証モードでも使う）。

作成のたびに点検し、引っかかったら作成役に 1 回だけ直させる。問題検証モードの「表記の点検で引っかかる問題」にも使う。
"""

from __future__ import annotations

import re
import unicodedata

from ... import latexfix

_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿！-～]")
_JA_RE = re.compile(r"[぀-ヿ㐀-鿿]")
_WORD_RE = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
# 解説が選択肢を記号で呼んでいる（選択肢は画面で並べ替えるので、記号が合わなくなる）
_LETTER_REF_RE = re.compile(r"選択肢\s*[A-DＡ-Ｄ]|[（(]\s*[A-DＡ-Ｄ]\s*[)）]|(?<![A-Za-z])[A-DＡ-Ｄ]\s*(?:は|が|を|の選択肢|も|と|、)")
_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "—": "-", "–": "-"})


def words(text: str) -> list[str]:
    return _WORD_RE.findall(text or "")


def word_count(text: str) -> int:
    return len(words(text))


def flat(text: str) -> str:
    """比べるための形：全角を半角に、引用符をそろえ、小文字にして、英数字以外を 1 つの空白にする。"""
    t = unicodedata.normalize("NFKC", text or "").translate(_QUOTES).lower()
    return " ".join(re.findall(r"[a-z0-9']+", t))


def contains(passage: str, quote: str) -> bool:
    """根拠の文が本文にそのまま入っているか（大文字・小文字、空白、記号の違いは無視する）。"""
    q = flat(quote)
    return bool(q) and f" {q} " in f" {flat(passage)} "


def count_word(sentence: str, word: str) -> int:
    return sum(1 for w in words(sentence) if w.lower() == word.lower())


def normalize(text: str) -> str:
    """意味を変えずに直せる表記の揺れ：文字どおりの \\n を改行に、日本語の文の簡体字を日本の字に、前後の空白。"""
    t = (text or "").replace("\r\n", "\n").replace("\\n", "\n")
    t = t.translate(latexfix._ZH_TO_JA)
    return t.strip()


def english_issues(text: str, field: str) -> list[str]:
    """英文（本文・設問・選択肢・例文）の点検。"""
    out = []
    if not (text or "").strip():
        return [f"{field}が空"]
    if _CJK_RE.search(text):
        out.append(f"{field}に日本語・全角の文字が入っている")
    if "[[" in text or "]]" in text:
        out.append(f"{field}に見出し [[…]] が残っている")
    return out


def japanese_issues(text: str, field: str, *, min_len: int = 0) -> list[str]:
    """日本語の文（ヒント・解説・訳）の点検。"""
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
    if latexfix._META_RE.search(body):
        out.append(f"{field}が作成の指示に触れている")
    if "[[" in body or "]]" in body:
        out.append(f"{field}に見出し [[…]] が残っている")
    return out


def letter_reference(text: str) -> bool:
    return bool(_LETTER_REF_RE.search(text or ""))


def choice_issues(texts: list[str], correct: int, *, english: bool = True) -> list[str]:
    """選択肢 4 つの点検：空・重複・日本語の混入・正解だけが極端に長い。"""
    out = []
    if len(texts) != 4 or any(not t.strip() for t in texts):
        return ["選択肢が 4 つそろっていない"]
    keys = [flat(t) if english else unicodedata.normalize("NFKC", t).strip() for t in texts]
    if len(set(keys)) < 4:
        out.append("選択肢に同じものがある")
    if english:
        for i, t in enumerate(texts):
            out += english_issues(t, f"選択肢 {'ABCD'[i]}")
    others = [len(t) for i, t in enumerate(texts) if i != correct]
    if english and len(texts[correct]) > 1.6 * max(others) and len(texts[correct]) - max(others) > 20:
        out.append("正解の選択肢だけが極端に長い（長さで正解が分かってしまう）")
    return out
