"""公共の資料の取得（worker だけが使う。web からは読まない）。

取りに行く先はここに書いた 2 つだけ（`ALLOWED_HOSTS`）。sandbox は外に出さない（CLAUDE.md のルール 7）。
- e-Gov 法令 API（v2）：条文ごとに取る（`/law_data/{法令ID}?law_full_text_format=xml&elm=MainProvision-Article_{条}`）。
  応答は JSON で、条文は法令標準 XML を base64 にしたもの。版は `revision_info.law_revision_id`
- Wikipedia 日本語版：記事の HTML（MediaWiki の API の `action=parse`）を受け取り、表・脚注・注記を除いて文章にする
  （小見出しは「【見出し】」の行で残す。第 14 回のレビュー 3 章 A）。版は `revid`
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


class SourceRefused(SourceMissing):
    """404・403・429 以外の 4xx（要求の形の誤りなど）、応答の XML が壊れているなど。作成ではこの資料を飛ばすが、
    鮮度の調査では「資料が見つからない」とせず「調べられない」にする（第 3 フェーズのルール 10 のレビュー 3・4）。"""


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
        if status in (403, 429) or status >= 500:  # 403：Wikimedia が一時的に断る（名乗り・利用の制限）
            raise SourceUnavailable(f"{host} が混み合っている・一時的に断られました（{status}）")
        if status >= 400:
            raise SourceRefused(f"{host} が断りました（{status}）: {text[:200]}")
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
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise SourceRefused(f"条文の XML を読めません（{exc}）") from exc
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
        if not isinstance(d, dict):
            raise SourceRefused(f"{src.label} の応答の形が違います")
        rev = d.get("revision_info") or {}
        if (rev.get("repeal_status") or "None") not in ("None", ""):
            raise SourceMissing(f"{src.label} は廃止されています（{rev.get('repeal_status')}）")
        info_rev = rev.get("law_revision_id") or info_rev
        law_title = src.law_title or rev.get("law_title") or law_title
        enforced = rev.get("amendment_enforcement_date") or enforced
        raw = d.get("law_full_text") if isinstance(d, dict) else None
        if not isinstance(raw, str) or not raw:
            raise SourceRefused(f"{src.label} の条文が応答にありません")
        try:
            xml = base64.b64decode(raw).decode("utf-8") if not raw.lstrip().startswith("<") else raw
        except (ValueError, UnicodeDecodeError) as exc:
            raise SourceRefused(f"{src.label} の条文を読めません") from exc
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
# 記事は HTML（action=parse）で受け取り、ここで文章にする（第 14 回のレビュー 3 章 A：本文だけの API
# （TextExtracts）では表のセルが見出しなしの文として混ざり、主語のない文ができて正解の取り違えを招いた）。
# 表・脚注・注記・画像・目次は入れない。小見出し（h3 以下）と定義の語（dt）は「【見出し】」の行で残す。

_HEAD_RE = re.compile(r"^(=+)\s*(.+?)\s*\1\s*$")
_VOID = {"br", "img", "hr", "meta", "link", "wbr", "input", "area", "col", "source", "track", "embed", "param"}
_SKIP_TAGS = {"table", "style", "script", "figure", "noscript", "math", "svg", "audio", "video", "map", "caption"}
_SKIP_CLASSES = {"reference", "references", "reflist", "mw-references-wrap", "mw-editsection", "mw-cite-backlink",
                 "hatnote", "dablink", "rellink", "toc", "navbox", "vertical-navbox", "noprint", "metadata", "infobox",
                 "thumb", "mw-empty-elt", "ambox", "mbox-small", "sistersitebox", "shortdescription", "gallery",
                 "mw-kartographer-maplink", "mwe-math-element", "plainlinks"}
_BLOCK = {"p", "div", "li", "dd", "dt", "ul", "ol", "dl", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "section",
          "pre", "center"}


class _HTMLText:
    """MediaWiki の記事の HTML を、節の見出し（「== 見出し ==」の行）・小見出し（「【見出し】」）・段落の行にする。"""

    def __init__(self) -> None:
        from html.parser import HTMLParser

        outer = self

        class P(HTMLParser):
            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                outer.start(tag, dict(attrs))

            def handle_endtag(self, tag: str) -> None:
                outer.end(tag)

            def handle_data(self, data: str) -> None:
                outer.data(data)

        self.parser = P(convert_charrefs=True)
        self.lines: list[str] = []
        self.buf: list[str] = []
        self.skip: list[str] = []  # 飛ばしている要素のタグ（入れ子を数える）
        self.kind = ""  # 今の行の種類（h2 / h3〜h6 / li / dt）

    def flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        self.buf = []
        kind, self.kind = self.kind, ""
        if not text:
            return
        if kind == "h2":
            self.lines.append(f"== {text} ==")
        elif kind in ("h3", "h4", "h5", "h6", "dt"):
            self.lines.append(f"【{text}】")
        elif kind == "li":
            self.lines.append("・" + text)
        else:
            self.lines.append(text)

    def start(self, tag: str, attrs: dict[str, str | None]) -> None:
        if tag in _VOID:
            if tag == "br" and not self.skip:
                self.buf.append(" ")
            return
        if self.skip:
            self.skip.append(tag)
            return
        classes = set((attrs.get("class") or "").split())
        if tag in _SKIP_TAGS or classes & _SKIP_CLASSES or (attrs.get("role") == "navigation") \
                or (tag == "sup" and "reference" in (attrs.get("class") or "")) \
                or "display:none" in (attrs.get("style") or "").replace(" ", ""):
            self.skip.append(tag)
            return
        if tag in _BLOCK:
            self.flush()
            if tag in ("h2", "h3", "h4", "h5", "h6", "li", "dt"):
                self.kind = tag

    def end(self, tag: str) -> None:
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            elif tag in self.skip:  # 閉じ忘れのある HTML
                while self.skip and self.skip.pop() != tag:
                    pass
            return
        if tag in _BLOCK:
            self.flush()

    def data(self, data: str) -> None:
        if not self.skip:
            self.buf.append(data)

    def text(self, html: str) -> str:
        self.parser.feed(html or "")
        self.parser.close()
        self.flush()
        return "\n".join(self.lines)


def html_to_text(html: str) -> str:
    return _HTMLText().text(html)


def wiki_sections(text: str) -> list[tuple[str, str]]:
    """記事の文章（html_to_text の出力）を (節の名前, 文章) に分ける。最初の節（導入）の名前は ""。"""
    out: list[tuple[str, list[str]]] = [("", [])]
    for line in (text or "").splitlines():
        m = _HEAD_RE.match(line.strip())
        if m:
            out.append((m.group(2).strip(), []))
        elif line.strip():
            out[-1][1].append(line.strip())
    return [(name, "\n".join(lines)) for name, lines in out if lines]


LEAD_ENOUGH = 400  # 導入がこの字数以上なら導入だけを本文にする（後の節は導入の言い直しが多い。第 13 回のレビュー）
_SENTENCE_END = ("。", "」", "）", ")", "！", "？", "』")


def drop_trailing_headings(text: str) -> str:
    """末尾の、文で終わらない行（見出し・定義の語だけの行）を落とす（第 13 回のレビュー：本文が見出し
    「付随的違憲審査制（司法裁判所型、アメリカ型）」で終わっていた。括弧で終わる見出しも、短い行は落とす）。"""
    lines = text.rstrip().split("\n")
    # 箇条書きの行（「・」）は見出しではないので落とさない（第 16 回のレビュー A-2：「マズローの欲求段階説」の
    # 5 つの箇条書きのうち、短い下の 2 つだけが落ちていた）
    while lines and not lines[-1].startswith("・") and (
            not lines[-1].rstrip().endswith(_SENTENCE_END)
            or ("。" not in lines[-1] and len(lines[-1].strip()) <= 40)):
        lines.pop()
    return "\n".join(lines).strip()


# 「以下の 3 段階を定義している。」「次のとおりである。」のように、後ろに箇条書き・表が続くはずの文
_LEAD_IN_RE = re.compile(r"(以下|次|下記)(の|に)[^。]{0,40}(定義している|ある|挙げられる|行っている|示す|掲げる|分けられる"
                         r"|分類される|大別される|記載する|とおり|通り|説明する)[^。]{0,6}。$")


def tidy(text: str, cut: bool = False) -> str:
    """取り込みで欠けた所を、文章を書き換えずに行ごと落とす（第 16 回のレビュー A-2）。
    - 途中で切れた箇条書き（cut＝長くて切ったとき、末尾が「・」の行なら、その箇条書きを丸ごと）
    - 「以下の〜。」で終わるのに、後ろに箇条書きが続かない段落（表が落ちた・切れた）
    - 中身のなくなった見出し（すぐ後ろが別の見出し・末尾）"""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if cut:
        while lines and lines[-1].startswith("・"):
            lines.pop()
    out: list[str] = []
    dropped = False  # 直前の行を落としたか
    for i, ln in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if not ln.startswith(("・", "【")) and _LEAD_IN_RE.search(ln.rstrip()) and not nxt.startswith("・"):
            dropped = True
            continue
        if dropped and ln.startswith("【") and out and out[-1].startswith("【"):
            out.pop()  # 中身を落として空になった見出し
        dropped = False
        out.append(ln)
    while out and out[-1].startswith("【"):
        out.pop()
    return "\n".join(out).strip()


def wiki_excerpt(text: str, section: str = "") -> str:
    """本文にする部分：section を指定すればその節から、なければ導入から、末尾の節（脚注など）の手前まで。
    導入だけで LEAD_ENOUGH 字以上あれば導入だけ。続けて入れる節には「【節の名前】」の行を付ける（見出しを落とさない）。"""
    secs = wiki_sections(text)
    if not section and secs and secs[0][0] == "" and len(secs[0][1]) >= LEAD_ENOUGH:
        return secs[0][1]
    start = 0
    if section:
        start = next((i for i, (name, _t) in enumerate(secs) if name == section), -1)
        if start < 0:
            raise SourceMissing(f"節「{section}」がありません")
    out: list[str] = []
    for k, (name, body) in enumerate(secs[start:]):
        if name.startswith(_WIKI_SKIP):
            break
        out.append(body if k == 0 or not name else f"【{name}】\n{body}")
        if len("\n".join(out)) >= MAX_CHARS:
            break
    return "\n".join(out)


def _wiki_page(f: Fetcher, title: str) -> dict[str, Any]:
    """記事の HTML を受け取り、{"title", "revid", "text"（html_to_text の文章）} にする。"""
    url = (f"{WIKI_API}?action=parse&format=json&formatversion=2&redirects=1&prop=text%7Crevid"
           f"&disabletoc=1&disableeditsection=1&page={quote(title)}")
    d = f.json(url)
    if not isinstance(d, dict):
        raise SourceRefused("Wikipedia の応答の形が違います")
    err = d.get("error") or {}
    if err:
        if err.get("code") in ("missingtitle", "invalidtitle"):
            raise SourceMissing(f"Wikipedia に「{title}」の記事がありません")
        raise SourceRefused(f"Wikipedia が断りました（{err.get('code')}: {str(err.get('info', ''))[:120]}）")
    page = d.get("parse") or {}
    html = page.get("text")
    if isinstance(html, dict):  # formatversion=1 の形 {"*": "…"}
        html = html.get("*")
    if not isinstance(html, str):
        raise SourceRefused("Wikipedia の応答に記事の本文がありません")
    return {"title": page.get("title") or title, "revid": str(page.get("revid") or ""), "text": html_to_text(html)}


def fetch_wiki(f: Fetcher, src: Source) -> Fetched:
    page = _wiki_page(f, src.key)
    title, revid = page["title"], page["revid"]
    text = wiki_excerpt(page["text"], src.section)
    body, cut = clip(text)
    body = tidy(drop_trailing_headings(body), cut)
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
              "fetched_at": now_iso(), "clipped": cut, "license": WIKI_LICENSE[0], "license_url": WIKI_LICENSE[1],
              "format": "html"},
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
            page = _wiki_page(f, str(info.get("key")))
            rev = page["revid"]
            if rev and rev == info.get("revision"):
                return {"check": "same", "current_revision": rev, "detail": "記事の版が同じ"}
            if info.get("format") != "html":  # 前の版（本文だけの API）で取った本文は、行の分け方が違うので文字だけで照らす
                # （前の版の本文に入っていた表のセルは今の取り込みでは入らないので、「変わった」になる＝点検の対象に出る）
                now = flat(re.sub(r"[【】・]", "", page["text"]))
                body = re.sub(r"[【】・]", "", body)
            else:
                now = flat(page["text"])
            # 抜き出しの最後に足した「（以下略）」は記事にないので外して照らす
            kept = all(flat(p) in now for p in body.replace("（以下略）", "").split("\n") if flat(p))
            return {"check": "same" if kept else "changed", "current_revision": rev,
                    "detail": "記事は編集されたが、使った部分は同じ" if kept else "使った部分の文章が今の記事にない（書き換えられた）"}
    except SourceRefused as exc:  # 断られた・読めない：見つからないとは言えないので、次の回にまた調べる
        return {"check": "unknown", "detail": str(exc)}
    except SourceMissing as exc:
        return {"check": "missing", "detail": str(exc)}
    return {"check": "unknown", "detail": f"知らない資料の種類: {kind}"}
