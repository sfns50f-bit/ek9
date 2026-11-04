"""公共の資料の取得（worker だけが使う。web からは読まない）。

取りに行く先はここに書いた 2 つだけ（`ALLOWED_HOSTS`）。sandbox は外に出さない（CLAUDE.md のルール 7）。
- e-Gov 法令 API（v2）：条文ごとに取る（`/law_data/{法令ID}?law_full_text_format=xml&elm=MainProvision-Article_{条}`）。
  応答は JSON で、条文は法令標準 XML を base64 にしたもの。版は `revision_info.law_revision_id`
- Wikipedia 日本語版：本文は MediaWiki の API の TextExtracts（`prop=extracts&explaintext=1`）。版は `revisions[0].revid`。
  鮮度の調査では本文を取り直して、前に取った抜き出しが今の記事にそのまま残っているかを見る

本文は取ってきた文章を「手を加えずに」抜き出す（LLM に書き直させない。正解の根拠の文が本文にそのまま
入っているかを機械で確かめるため。開発者と決めたこと）。長すぎるときは段落・文の切れ目で切る。
"""

from __future__ import annotations

import base64
import json
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from .catalog import Source

EGOV_API = "https://laws.e-gov.go.jp/api/2"
WIKI_API = "https://ja.wikipedia.org/w/api.php"
ALLOWED_HOSTS = {"laws.e-gov.go.jp", "ja.wikipedia.org"}
# Wikimedia は名乗りのない取得を断る（https://meta.wikimedia.org/wiki/User-Agent_policy）。連絡先はリポジトリ
USER_AGENT = "ai-quiz/1.0 (+https://github.com/sfns50f-bit/ek9; home study app for a high school student)"
TIMEOUT = 30.0
MIN_INTERVAL = 1.0  # 同じ先へは 1 秒に 1 回まで（Wikimedia・e-Gov に負担をかけない）

MIN_CHARS = 120   # これより短い本文は使わない（設問が作れない）
MAX_CHARS = 1300  # これより長い本文は段落・文の切れ目で切る（スマホで読む長さ）
WIKI_LICENSE = ("CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/deed.ja")
# Wikipedia の記事の末尾の、本文にしない節
_WIKI_SKIP = ("脚注", "注釈", "出典", "参考文献", "関連項目", "外部リンク", "関連書籍", "参考資料", "関連文献")
_KANSUJI = "〇一二三四五六七八九"


class SourceUnavailable(Exception):
    """つながらない・混んでいる（429・5xx）。待ってからやり直せば直るもの。"""


class SourceMissing(Exception):
    """記事・法令・条がない、本文が短すぎるなど。この題材は使えない（別の題材を選ぶ）。"""


@dataclass
class Fetched:
    """取ってきた本文。info は passages.source_info に入れる（鮮度の調査に使う）。"""

    title: str
    body: str
    source_title: str
    source_url: str
    info: dict[str, Any] = field(default_factory=dict)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()  # db.utcnow と同じ形（UTC・タイムゾーンなし）


class Fetcher:
    """HTTP の取得（テストでは get を差し替える）。get(url) -> (状態コード, 本文の文字列)。"""

    def __init__(self, get: Callable[[str], tuple[int, str]] | None = None, *, min_interval: float = MIN_INTERVAL,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._get = get or _httpx_get
        self.min_interval = min_interval
        self.sleep = sleep
        self._last: dict[str, float] = {}

    def json(self, url: str) -> Any:
        host = urlsplit(url).hostname or ""
        if host not in ALLOWED_HOSTS:  # 書いた先にしか行かない
            raise SourceMissing(f"取得してよい先ではありません: {host}")
        wait = self._last.get(host, -1e9) + self.min_interval - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        self._last[host] = time.monotonic()
        try:
            status, text = self._get(url)
        except SourceUnavailable:
            raise
        except Exception as exc:  # 接続できない・時間切れ
            raise SourceUnavailable(f"{host} につながりません（{type(exc).__name__}: {exc}）") from exc
        if status == 404:
            raise SourceMissing(f"{host} にありません（404）")
        if status == 429 or status >= 500:
            raise SourceUnavailable(f"{host} が混み合っています（{status}）")
        if status >= 400:
            raise SourceMissing(f"{host} が断りました（{status}）: {text[:200]}")
        try:
            return json.loads(text)
        except ValueError as exc:
            # Wikimedia は制限に当たると JSON でない文章を返す（「You are making too many requests」）
            if "too many requests" in text.lower():
                raise SourceUnavailable(f"{host} の利用の制限に当たりました。しばらく待ちます") from exc
            raise SourceUnavailable(f"{host} の応答を読めません: {text[:120]}") from exc


def _httpx_get(url: str) -> tuple[int, str]:
    import httpx

    r = httpx.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=TIMEOUT,
                  follow_redirects=False)
    return r.status_code, r.text


# --- 文章の切り出し ------------------------------------------------------------------

def clip(text: str, max_chars: int = MAX_CHARS) -> tuple[str, bool]:
    """長ければ段落の切れ目、なければ文の切れ目（。）で切る。(切った本文, 切ったか)。"""
    text = text.strip()
    if len(text) <= max_chars:
        return text, False
    paras = text.split("\n")
    out: list[str] = []
    for p in paras:
        if len("\n".join(out + [p])) > max_chars:
            break
        out.append(p)
    if out and len("\n".join(out)) >= max_chars * 0.5:
        return "\n".join(out).strip(), True
    head = text[:max_chars]
    cut = head.rfind("。")
    return (head[:cut + 1] if cut > 0 else head).strip(), True


def kansuji(n: int) -> str:
    """条の番号を漢数字に（25 → 二十五）。e-Gov の「第二十五条」と同じ書き方。"""
    if n <= 0:
        return str(n)
    out = ""
    for unit, name in ((1000, "千"), (100, "百"), (10, "十")):
        d, n = divmod(n, unit)
        if d:
            out += ("" if d == 1 else _KANSUJI[d]) + name
    return out + (_KANSUJI[n] if n else "")


def article_label(num: str) -> str:
    """"25" → 「第二十五条」、"1_2" → 「第一条の二」。"""
    parts = num.split("_")
    head = f"第{kansuji(int(parts[0]))}条"
    return head + "".join(f"の{kansuji(int(p))}" for p in parts[1:])


# --- e-Gov 法令 -----------------------------------------------------------------------

def _sentences(el: ET.Element | None) -> str:
    """Sentence をつなぐ（Column の間は全角の空白。e-Gov の表示と同じ）。"""
    if el is None:
        return ""
    cols = el.findall("Column")
    if cols:
        return "　".join(_sentences(c) for c in cols)
    return "".join("".join(s.itertext()).strip() for s in el.iter("Sentence"))


def _items(el: ET.Element, depth: int = 0) -> list[str]:
    """号（Item）・その下の細分（Subitem1, Subitem2, …）を「一　…」の行にする。"""
    lines = []
    child_tag = "Item" if depth == 0 else f"Subitem{depth}"
    for item in el.findall(child_tag):
        title_tag = "ItemTitle" if depth == 0 else f"Subitem{depth}Title"
        sent_tag = "ItemSentence" if depth == 0 else f"Subitem{depth}Sentence"
        title = (item.findtext(title_tag) or "").strip()
        lines.append(("　" * depth) + f"{title}　{_sentences(item.find(sent_tag))}".strip())
        lines += _items(item, depth + 1)
    return lines


def article_text(xml: str) -> tuple[str, str]:
    """条の XML を、e-Gov の表示に近い文章にする。(見出し「第二十五条（見出し）」, 本文)。

    項が 2 つ以上なら 2 項目から「２　」を付ける（古い法令は XML に項の番号がない。設問で「第 2 項」と呼べるように）。"""
    root = ET.fromstring(xml)
    art = root if root.tag == "Article" else root.find(".//Article")
    if art is None:
        raise SourceMissing("条文が見つかりません")
    caption = (art.findtext("ArticleCaption") or "").strip()
    title = (art.findtext("ArticleTitle") or "").strip()
    lines = []
    paras = art.findall("Paragraph")
    for i, para in enumerate(paras, 1):
        num = (para.findtext("ParagraphNum") or "").strip()
        if not num and i > 1:
            num = unicodedata.normalize("NFKC", str(i)).translate(str.maketrans("0123456789", "０１２３４５６７８９"))
        text = _sentences(para.find("ParagraphSentence"))
        lines.append(f"{num}　{text}" if num else text)
        lines += _items(para)
    return f"{title}{caption}", "\n".join(x for x in lines if x.strip())


def fetch_law(f: Fetcher, src: Source) -> Fetched:
    """法令の条文（src.parts の条）を取ってきて 1 つの本文にする。"""
    pieces, info_rev, law_title, enforced = [], "", "", ""
    for num in src.parts:
        d = f.json(f"{EGOV_API}/law_data/{quote(src.key)}?law_full_text_format=xml"
                   f"&elm=MainProvision-Article_{quote(num)}")
        rev = d.get("revision_info") or {}
        if (rev.get("repeal_status") or "None") not in ("None", ""):
            raise SourceMissing(f"{src.label} は廃止されています（{rev.get('repeal_status')}）")
        info_rev = rev.get("law_revision_id") or info_rev
        law_title = src.law_title or rev.get("law_title") or law_title
        enforced = rev.get("amendment_enforcement_date") or enforced
        raw = d.get("law_full_text") or ""
        try:
            xml = base64.b64decode(raw).decode("utf-8") if not raw.lstrip().startswith("<") else raw
        except (ValueError, UnicodeDecodeError) as exc:
            raise SourceMissing(f"{src.label} の条文を読めません") from exc
        head, text = article_text(xml)
        if not text:
            raise SourceMissing(f"{src.label} の{article_label(num)}が空です")
        pieces.append(f"{head}\n{text}")
    body, cut = clip("\n\n".join(pieces))
    if cut:
        body += "\n（以下略）"
    if len(body) < MIN_CHARS // 2:
        raise SourceMissing(f"{src.label} の条文が短すぎます（{len(body)} 字）")
    parts = "・".join(article_label(n) for n in src.parts)
    when = f"（{enforced} 施行の版）" if enforced else ""
    return Fetched(
        title=f"{law_title}　{parts}",
        body=body,
        source_title=f"e-Gov 法令検索「{law_title}」{parts}{when}",
        source_url=f"https://laws.e-gov.go.jp/law/{src.key}",
        info={"kind": "law", "key": src.key, "parts": list(src.parts), "revision": info_rev,
              "enforced": enforced, "fetched_at": now_iso(), "clipped": cut},
    )


def law_revision(f: Fetcher, law_id: str) -> str:
    """今の版の番号（鮮度の調査で、版が変わっていなければ条文を取り直さない）。"""
    d = f.json(f"{EGOV_API}/laws?law_id={quote(law_id)}")
    laws = d.get("laws") or []
    if not laws:
        raise SourceMissing(f"法令 {law_id} が見つかりません")
    rev = laws[0].get("current_revision_info") or laws[0].get("revision_info") or {}
    return str(rev.get("law_revision_id") or "")


# --- Wikipedia ----------------------------------------------------------------------

_HEAD_RE = re.compile(r"^(=+)\s*(.+?)\s*\1\s*$")


def wiki_sections(extract: str) -> list[tuple[str, str]]:
    """explaintext の記事を (節の名前, 文章) に分ける。最初の節（導入）の名前は ""。"""
    out: list[tuple[str, list[str]]] = [("", [])]
    for line in (extract or "").splitlines():
        m = _HEAD_RE.match(line.strip())
        if m:
            out.append((m.group(2).strip(), []))
        elif line.strip():
            out[-1][1].append(line.strip())
    return [(name, "\n".join(lines)) for name, lines in out if lines]


def wiki_excerpt(extract: str, section: str = "") -> str:
    """本文にする部分：section を指定すればその節から、なければ導入から、末尾の節（脚注など）の手前まで。"""
    secs = wiki_sections(extract)
    start = 0
    if section:
        start = next((i for i, (name, _t) in enumerate(secs) if name == section), -1)
        if start < 0:
            raise SourceMissing(f"節「{section}」がありません")
    text = []
    for name, body in secs[start:]:
        if name.startswith(_WIKI_SKIP):
            break
        text.append(body)
        if len("\n".join(text)) >= MAX_CHARS:
            break
    return "\n".join(text)


def _wiki_query(f: Fetcher, title: str, extract: bool) -> dict[str, Any]:
    props = "extracts|revisions|info" if extract else "revisions|info"
    url = (f"{WIKI_API}?action=query&format=json&formatversion=2&redirects=1&prop={quote(props)}"
           f"&rvprop=ids%7Ctimestamp&inprop=url&titles={quote(title)}")
    if extract:
        url += "&explaintext=1&exsectionformat=wiki"
    d = f.json(url)
    pages = (d.get("query") or {}).get("pages") or []
    if not pages or pages[0].get("missing") or pages[0].get("invalid"):
        raise SourceMissing(f"Wikipedia に「{title}」の記事がありません")
    return pages[0]


def fetch_wiki(f: Fetcher, src: Source) -> Fetched:
    page = _wiki_query(f, src.key, extract=True)
    title = page.get("title") or src.key
    revs = page.get("revisions") or [{}]
    revid = str(revs[0].get("revid") or "")
    text = wiki_excerpt(page.get("extract") or "", src.section)
    body, cut = clip(text)
    if len(body) < MIN_CHARS:
        raise SourceMissing(f"Wikipedia「{title}」の本文が短すぎます（{len(body)} 字）")
    where = f"「{title}」" + (f"の節「{src.section}」" if src.section else "")
    return Fetched(
        title=title + (f"（{src.section}）" if src.section else ""),
        body=body,
        source_title=f"Wikipedia 日本語版{where}（版 {revid}）",
        # 取った版への固定のリンク（CC BY-SA の表示に、どの版かを示す）
        source_url=f"https://ja.wikipedia.org/w/index.php?title={quote(title)}&oldid={revid}" if revid
        else f"https://ja.wikipedia.org/wiki/{quote(title)}",
        info={"kind": "wiki", "key": src.key, "section": src.section, "page_title": title, "revision": revid,
              "fetched_at": now_iso(), "clipped": cut, "license": WIKI_LICENSE[0], "license_url": WIKI_LICENSE[1]},
    )


def fetch(f: Fetcher, src: Source) -> Fetched:
    if src.kind == "law":
        return fetch_law(f, src)
    if src.kind == "wiki":
        return fetch_wiki(f, src)
    raise SourceMissing(f"知らない資料の種類: {src.kind}")


# --- 鮮度の調査 ---------------------------------------------------------------------

def flat(text: str) -> str:
    """照らし合わせ用：NFKC・空白をすべて除く。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def check(f: Fetcher, info: dict[str, Any], body: str) -> dict[str, Any]:
    """前に取った本文が、今の資料にそのまま残っているか。

    返す値：{"check": "same"（変わっていない）/ "changed"（本文が変わった）/ "missing"（記事・法令がなくなった）,
    "current_revision": 今の版, "detail": 説明}。つながらなければ SourceUnavailable。
    法令は版の番号が同じなら取り直さない。版が変わっても、使った条文が同じなら same（ほかの条の改正）。
    Wikipedia は版がよく変わる（細かな編集）ので、本文が今の記事に残っているかで見る。"""
    kind = info.get("kind")
    try:
        if kind == "law":
            rev = law_revision(f, str(info.get("key")))
            if rev and rev == info.get("revision"):
                return {"check": "same", "current_revision": rev, "detail": "法令の版が同じ"}
            src = Source("law", str(info.get("key")), tuple(info.get("parts") or ()), label=str(info.get("key")))
            new = fetch_law(f, src)
            same = flat(new.body) == flat(body)
            return {"check": "same" if same else "changed", "current_revision": new.info.get("revision", rev),
                    "current_enforced": new.info.get("enforced", ""),
                    "detail": "法令の版が変わったが、使った条文は同じ" if same
                    else f"条文が変わった（{new.info.get('enforced') or '日付不明'} 施行の版）"}
        if kind == "wiki":
            page = _wiki_query(f, str(info.get("key")), extract=True)
            rev = str((page.get("revisions") or [{}])[0].get("revid") or "")
            if rev and rev == info.get("revision"):
                return {"check": "same", "current_revision": rev, "detail": "記事の版が同じ"}
            now = flat(page.get("extract") or "")
            # 抜き出しの最後に足した「（以下略）」は記事にないので外して照らす
            kept = all(flat(p) in now for p in body.replace("（以下略）", "").split("\n") if flat(p))
            return {"check": "same" if kept else "changed", "current_revision": rev,
                    "detail": "記事は編集されたが、使った部分は同じ" if kept else "使った部分の文章が今の記事にない（書き換えられた）"}
    except SourceMissing as exc:
        return {"check": "missing", "detail": str(exc)}
    return {"check": "unknown", "detail": f"知らない資料の種類: {kind}"}
