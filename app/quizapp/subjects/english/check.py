"""英語の文章の機械的な点検（数学の textcheck / latexfix の代わり）。sympy なし（web の問題検証モードでも使う）。

作成のたびに点検し、引っかかったら作成役に 1 回だけ直させる。問題検証モードの「表記の点検で引っかかる問題」にも使う。
"""

from __future__ import annotations

import difflib
import re
import unicodedata

from ... import latexfix

_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uff01-\uff5e\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]")
# ハングル（第 5 回のレビュー A-4：解説に「到着하자」が混ざった）
_HANGUL_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]+")
# 二重の括弧「[[ leak ]]」「[ [leak] ]」（作成役が例文の語に付けた括弧に、Python がさらに付けた。A-3）
_DOUBLE_BRACKET_RE = re.compile(r"\[\s*\[\s*([^\[\]\n]+?)\s*\]\s*\]")
_JA_RE = re.compile(r"[぀-ヿ㐀-鿿]")
_WORD_RE = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
# 解説が選択肢を記号で呼んでいる（選択肢は画面で並べ替えるので、記号が合わなくなる）。
# 「選択肢A」「(C)」「A は」「C の」「A 選択肢」「「B」」など（第 4 回のレビュー A-1：「A 選択肢」「C の」を拾えていなかった）
_LETTER_REF_RE = re.compile(
    r"選択肢\s*[A-DＡ-Ｄ](?![A-Za-z])|[（(「『]\s*[A-DＡ-Ｄ]\s*[)）」』]"
    r"|(?<![A-Za-z.'’])[A-DＡ-Ｄ](?![A-Za-z.'’])\s*(?:の|は|が|を|も|と|、|選択肢|：|:(?!\S*//))")
# アプリの内部の言葉・作成の指示への言及（第 4 回のレビュー A-2）。生徒が読む文章には出さない
# （「与えられた」は「生徒に与えられた課題」のような本文の内容の説明もあるので、作成の材料を指すものだけ）
_INTERNAL_RE = re.compile(r"入らない語|入らない単語|与えられた(?:語|単語|意味|指示|条件|情報|例文|選択肢|品詞|正解)|"
                          r"指示(?:に従|どおり|通り|された)|要点に(?:従|沿)|使う単語|誤答の語|"
                          r"指定された(?:語|単語|意味|形|品詞)|作成の(?:指示|材料)")
# 語彙の意味の問題（例文に空所がない）で空所補充の言い回し（A-7）
_BLANK_WORDS_RE = re.compile(r"空所|空欄|(?<!気に)(?<!手に)(?<!頭に)入る|入れる|当てはまる語")
# URL・メールアドレス（A-3）。ドメインは example.com / example.org / example.net とそのサブドメインだけ許す
_URL_HOST_RE = re.compile(r"(?:https?://|www\.)([A-Za-z0-9.-]+)|[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+)"
                          r"|(?<![@\w.-])((?:[A-Za-z0-9-]+\.)+(?:com|org|net|jp|info|io|edu|gov|biz|co|us|uk))(?![\w-])",
                          re.I)
_ALLOWED_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*example\.(?:com|org|net)$", re.I)
_QUOTE_CHARS = "\"'“”‘’「」"
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
    """意味を変えずに直せる表記の揺れ：文字どおりの \\n を改行に、日本語の文の簡体字を日本の字に、前後の空白。
    Markdown の強調（**太字**・*斜体*・__太字__）の記号を外し、行頭の「* 」の箇条書きを「- 」にそろえる
    （画面は Markdown を表示しないので、記号がそのまま見えていた。第 10 回のレビュー 4-5。#1114・#1130）。"""
    t = (text or "").replace("\r\n", "\n").replace("\\n", "\n")
    t = t.translate(latexfix._ZH_TO_JA)
    t = _DOUBLE_BRACKET_RE.sub(r"[ \1 ]", t)
    t = re.sub(r"^([ \t]*)[*+][ \t]+", r"\1- ", t, flags=re.M)
    t = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), t)
    t = re.sub(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"\1", t)
    return t.strip()


def english_issues(text: str, field: str) -> list[str]:
    """英文（本文・設問・選択肢・例文）の点検。"""
    out = []
    if not (text or "").strip():
        return [f"{field}が空"]
    if _CJK_RE.search(text):
        out.append(f"{field}に日本語・全角・韓国語の文字が入っている")
    if "[[" in text or "]]" in text:
        out.append(f"{field}に見出し [[…]] が残っている")
    hosts = bad_hosts(text)
    if hosts:
        out.append(f"{field}の URL・メールのドメイン（{'、'.join(hosts[:3])}）が例示用のもの（example.org など）でない")
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
    ko = _HANGUL_RE.findall(body)
    if ko:
        out.append(f"{field}に韓国語の文字（{''.join(ko)[:8]}）")
    if latexfix._META_RE.search(body):
        out.append(f"{field}が作成の指示に触れている")
    inside = internal_words(body)
    if inside:
        out.append(f"{field}にアプリの内部の言葉（{'・'.join(inside[:3])}）がある（生徒が読む文章に書かない）")
    if "[[" in body or "]]" in body:
        out.append(f"{field}に見出し [[…]] が残っている")
    hosts = bad_hosts(body)
    if hosts:
        out.append(f"{field}の URL・メールのドメイン（{'、'.join(hosts[:3])}）が例示用のもの（example.org など）でない")
    return out


def letter_reference(text: str) -> bool:
    return bool(_LETTER_REF_RE.search(text or ""))


def replace_letters(text: str, options: list[str]) -> str:
    """選択肢を A・B の記号で呼んでいる所を、選択肢の文（「…」）に置き換える（直しを頼んでも残ったとき。
    第 11 回のレビュー 2-2）。options は作成役が書いた順（A〜D）。"""
    def repl(m: re.Match[str]) -> str:
        s = m.group(0)
        letter = re.search(r"[A-DＡ-Ｄ]", s)
        if letter is None:
            return s
        i = "ABCD".index(unicodedata.normalize("NFKC", letter.group()))
        if i >= len(options) or not options[i].strip():
            return s
        quoted = f"「{options[i].strip()}」"
        if s.startswith("選択肢") or s[0] in "（(「『":
            return quoted
        return quoted + s[letter.end():].lstrip()
    return _LETTER_REF_RE.sub(repl, text or "")


def internal_words(text: str) -> list[str]:
    """アプリの内部の言葉・指示への言及（「入らない語」「与えられた」など）。"""
    return list(dict.fromkeys(m.group(0) for m in _INTERNAL_RE.finditer(text or "")))


def blank_words(text: str) -> bool:
    return bool(_BLANK_WORDS_RE.search(text or ""))


def bad_hosts(text: str) -> list[str]:
    """例示用のドメイン（example.com・example.org・example.net とそのサブドメイン）以外の URL・メールのドメイン。

    「greenleafcafe-example.org」のようにハイフンでつないだ形は example.org のサブドメインではないので弾く。
    """
    out = []
    for m in _URL_HOST_RE.finditer(text or ""):
        host = (m.group(1) or m.group(2) or m.group(3) or "").strip(".-").lower()
        if host.startswith("www."):
            host = host[4:]
        if host and "." in host and not _ALLOWED_HOST_RE.match(host):
            out.append(host)
    return list(dict.fromkeys(out))


_STOP = frozenset("""
about above after again also although among another because been before being below between both could
does doing down during each even every from have having here into just like many more most much must only
other over same should since some such than that their them then there these they this those through very
want were what when where which while will with would your yours the and for are was not but can did had
has her him his its our out own she who why how all any few nor off too yet one two three four five
""".split())


def _stem(w: str) -> str:
    """語形の違いをそろえる（decide・decided・decides・deciding → decid）。"""
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            w = w[: -len(suf)]
            break
    return w[:-1] if len(w) > 4 and w.endswith("e") else w


def content_coverage(option: str, passage: str) -> tuple[float, list[str]]:
    """選択肢の中心の語（4 文字以上で、ありふれた語でないもの）のうち、本文にある語の割合と、その語。

    「本文に書かれていない」という誤答の種類が正しいかを機械的に確かめるのに使う（第 5 回のレビュー C-3）。
    中心の語が 2 つ未満なら (0, [])（判断しない）。"""
    have = {_stem(w.lower()) for w in words(passage)}
    keys = [w.lower() for w in words(option) if len(w) >= 4 and w.lower() not in _STOP]
    keys = list(dict.fromkeys(keys))
    if len(keys) < 2:
        return 0.0, []
    found = [w for w in keys if _stem(w) in have]
    return len(found) / len(keys), found


def stated_in_passage(option: str, passage: str) -> bool:
    """選択肢の中心の語のほとんど（6 割以上・2 語以上）が本文にある（＝「本文に書かれていない」ではない）。"""
    share, found = content_coverage(option, passage)
    return share >= 0.6 and len(found) >= 2


def _sentences(text: str) -> list[str]:
    return [s.strip() for para in (text or "").split("\n") for s in re.split(r"(?<=[.!?])\s+", para) if s.strip()]


def _content(text: str) -> list[str]:
    """内容語（4 文字以上で、ありふれた語でないもの）の語幹。"""
    return list(dict.fromkeys(_stem(w.lower()) for w in words(text) if len(w) >= 4 and w.lower() not in _STOP))


def evidence_window(passage: str, evidence: str, answer: str) -> str:
    """根拠の文とその前後 1 文（第 9 回のレビュー 1-1）。根拠の文が分からないとき（見本集の問題など）は、正解と内容語が
    いちばん重なる本文の文を根拠とみなす。"""
    sents = _sentences(passage)
    if not sents:
        return ""
    idx = next((i for i, s in enumerate(sents) if evidence and contains(s, evidence) or evidence and contains(evidence, s)),
               None)
    if idx is None:
        want = set(_content(evidence or answer))
        idx = max(range(len(sents)), key=lambda i: len(want & set(_content(sents[i]))))
    return " ".join(sents[max(0, idx - 1):idx + 2])


def overlap_share(option: str, window: str) -> float:
    """誤答の内容語のうち、根拠の文と前後 1 文にある割合（どの誤答を 1 対 1 で確かめるかを選ぶだけに使う）。
    内容語が 2 つ未満なら 0。"""
    keys = _content(option)
    if len(keys) < 2:
        return 0.0
    have = set(_content(window))
    return sum(1 for k in keys if k in have) / len(keys)


_NARRATOR_RE = re.compile(r"\b(the\s+)?(writer|author|narrator|speaker)\b", re.I)
_GENDERED_RE = re.compile(r"\b(he|his|him|himself|she|her|hers|herself)\b", re.I)
_NARRATOR_SAYS_RE = re.compile(
    r"\bI(?:'m|\s+am|\s+was|\s+became)\s+(?:a|an|the)?\s*(?:\w+\s+)?(boy|girl|man|woman|son|daughter|father|mother|"
    r"husband|wife|brother|sister|grandfather|grandmother)\b", re.I)


def narrator_pronoun(question: str, options: list[str], passage: str) -> bool:
    """本文が一人称（I）で語られ、語り手の性別が本文から分からないのに、設問・選択肢で語り手（the writer など）を
    he・she などで呼んでいる（第 6 回のレビュー 3-4、第 10 回の #1100）。語り手を問う設問で、設問に he・she が
    あるか、選択肢の 2 つ以上が He・She で始まるときに真。"""
    if len(re.findall(r"\bI\b", passage or "")) < 2 or _NARRATOR_SAYS_RE.search(passage or ""):
        return False
    m = _NARRATOR_RE.search(question or "")
    if not m:
        return False
    # 設問では、語り手の語のあとで、ほかの人物（大文字で始まる名前）を挟まずに出てくる he・she だけを見る
    # （「the writer learn from Mr. Tanaka about his past」の his は Mr. Tanaka）
    after = (question or "")[m.end():]
    pron = _GENDERED_RE.search(after)
    in_question = bool(pron) and not re.search(r"\b(?!I\b)[A-Z][a-z]+", after[:pron.start()])
    starts = sum(1 for o in options if re.match(r"\s*(he|she|his|her)\b", o or "", re.I))
    return in_question or starts >= 2


def stray_english(explanation: str, allowed_texts: list[str]) -> list[str]:
    """語彙の問題の解説に出てくる英単語のうち、例文にも選択肢にもないもの（第 5 回のレビュー D-1：
    選択肢にない語の説明をしていた）。語形の違い（-s・-ed・-ing）と、ありふれた語は見ない。"""
    have = {_stem(w.lower()) for t in allowed_texts for w in words(t)}
    out = []
    for w in words(explanation):
        lw = w.lower()
        if len(lw) < 3 or lw in _STOP or _stem(lw) in have:
            continue
        out.append(w)
    return list(dict.fromkeys(out))


_ARTICLE_RE = re.compile(r"第\s*[0-9０-９一二三四五六七八九十]+\s*条")
_MAIN_Q_RE = re.compile(r"\b(main|most important|biggest|primary)\s+(reason|idea|purpose|point|cause)\b", re.I)
_ONE_OF_RE = re.compile(r"\b(one|another|a|second|third)\s+(reason|idea|purpose|point|cause)\b|\bone of the\b", re.I)


def article_typo(text: str) -> bool:
    """段落を「第N条」と書いている（第 5 回のレビュー D-3）。"""
    return bool(_ARTICLE_RE.search(text or ""))


def main_vs_one(question: str, evidence: str) -> bool:
    """「main reason」などを問うのに、根拠の文が「One reason」など 1 つの理由にすぎない（D-4）。"""
    return bool(_MAIN_Q_RE.search(question or "")) and bool(_ONE_OF_RE.search(evidence or ""))


def first_sentence(text: str) -> str:
    body = (text or "").strip()
    return re.split(r"(?<=[。．！？!?])", body, maxsplit=1)[0].strip()


def similar(a: str, b: str) -> bool:
    """2 つの文がほぼ同じか（ヒントが解説の 1 文目と同じ、など）。空白・句読点の違いは無視する。"""
    def key(t: str) -> str:
        return re.sub(r"[\s、。，．,.!！?？「」『』()（）]", "", unicodedata.normalize("NFKC", t or ""))

    ka, kb = key(a), key(b)
    if len(ka) < 8 or len(kb) < 8:
        return False
    return ka == kb or difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.85


def strip_quotes(text: str) -> str:
    """根拠の文の外側の引用符だけを外す（A-6：文が内側の引用符で終わるとき、その閉じの引用符を消さない）。

    例：「"She said, "Have a cup of tea.""」→「She said, "Have a cup of tea."」
    """
    t = (text or "").strip()
    if len(t) >= 2 and t[0] in _QUOTE_CHARS and t[-1] in _QUOTE_CHARS:
        inner = t[1:-1].strip()
        opens = inner.count("“") + inner.count("「") + inner.count("‘")
        closes = inner.count("”") + inner.count("」") + inner.count("’")
        plain_odd = inner.count('"') % 2 == 1
        # 内側の引用符が閉じていなければ、最後の文字は内側の閉じの引用符（外側には閉じがなかった）
        if opens > closes or plain_odd:
            return (inner + t[-1]).strip()
        return inner
    if t and t[0] in _QUOTE_CHARS and t[-1] not in _QUOTE_CHARS:
        return t[1:].strip()  # 開きだけの引用符
    return t


def paragraphs(body: str) -> list[str]:
    """本文の段落（空行で区切る）。最初のかたまりが 1 行で文の終わりの記号がなければ題名とみなして除く。"""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", body or "") if b.strip()]
    if len(blocks) > 1 and "\n" not in blocks[0] and not re.search(r"[.!?。]\s*$", blocks[0]):
        blocks = blocks[1:]
    return blocks


def paragraph_of(body: str, quote: str) -> int | None:
    """根拠の文がある段落の番号（1 から）。見つからなければ None。"""
    for i, para in enumerate(paragraphs(body), 1):
        if contains(para, quote):
            return i
    return None


_PARA_RE = re.compile(r"第\s*([0-9０-９一二三四五六七八九]+)\s*段落|([0-9０-９一二三四五六七八九]+)\s*段落目")
_KANJI_NUM = {c: i for i, c in enumerate("一二三四五六七八九", 1)}


def paragraph_numbers(text: str) -> list[int]:
    """ヒントに書かれた「第N段落」「N段落目」の番号。"""
    out = []
    for m in _PARA_RE.finditer(text or ""):
        raw = unicodedata.normalize("NFKC", m.group(1) or m.group(2))
        out.append(int(raw) if raw.isdigit() else _KANJI_NUM.get(raw, 0))
    return [n for n in out if n]


def fix_paragraph(hint: str, where: int) -> str:
    """ヒントの「第N段落」「N段落目」を、根拠の文のある段落の番号に直す（第 11 回のレビュー 2-2）。"""
    def repl(m: re.Match[str]) -> str:
        return f"第{where}段落" if m.group(1) else f"{where}段落目"
    return _PARA_RE.sub(repl, hint or "")


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
