"""英語の部品（共通テストレベルの読解・語彙）。

読解: 作成役が本文と設問（選択肢・正解・根拠の文・解説）を書く → 根拠の文が本文にそのまま入っているか、
      英文に日本語が混ざっていないか、選択肢が重複していないかなどを Python で点検（落ちたら 1 回だけ直させる）
      → 作成役に本文を見せずに設問だけを解かせ、正解できた設問は「常識で解ける」として外す
語彙: 正解の語と誤答の語は Python が単語リスト（words.py）から選ぶ。作成役は例文・訳・ヒント・解説だけを書く
検証: 検証役に本文と設問と選択肢を見せ（正解は見せない。選択肢の順番は毎回入れ替える）、記号で答えさせる。
      スペルは日本語の意味と例文から英単語を答えさせ、完全一致で確かめる
採点: 選択式は選んだ選択肢、スペルは英単語の完全一致（大文字・小文字は区別しない）。LLM も sandbox も使わない

数学と違い、検証役に選択肢を見せる（読解の答えは文章なので、選択肢なしでは機械的に突き合わせられない）。
英語だけの決めごとの詳細は docs/english.md。
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import random
import re
import unicodedata
from typing import Any

from ..base import LLM, Draft, GenContext, Grade, Rejected, Sandbox, Subject, Subunit, Unit, VerifyResult
from . import check, prompts
from .catalog import MISTAKES, READING_MISTAKES, READINGS, TOPICS, UNITS, VOCABS
from .jhs_words import JHS_WORDS
from .words import POS, SPELLING_EXCLUDE, WORDS, Word

FIX_ROUNDS = 1  # 点検で落ちたとき、直しを頼む回数
LETTERS = "ABCD"
MAX_ROLLS = 30


def _params_key(template: str, params: dict[str, Any]) -> str:
    raw = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    return template + ":" + hashlib.sha1(raw.encode()).hexdigest()[:16]


@functools.cache
def _units() -> list[Unit]:
    out = []
    for uid, label, ids in UNITS:
        subs = []
        for sid in ids:
            if sid in READINGS:
                r = READINGS[sid]
                subs.append(Subunit(sid, r.label, ("choice",), r.level))
            else:
                v = VOCABS[sid]
                subs.append(Subunit(sid, v.label, (v.fmt,), v.level))
        out.append(Unit(uid, label, tuple(subs)))
    return out


def _chat(ctx: GenContext, system: str, user: str, schema: dict[str, Any], model: str = "") -> dict[str, Any]:
    """作成役（model を渡せばそのモデル）を呼ぶ。出力が読めなかったときは、作り直しの回数を使わずに 1 回だけ呼び直す。"""
    from ...llm import LLMError

    model = model or ctx.gen_model
    try:
        return ctx.llm.chat_json(model, system, user, schema, temperature=0.8)
    except LLMError:
        return ctx.llm.chat_json(model, system, user, schema, temperature=0.8)


def _verdict(out: dict[str, Any]) -> tuple[str, str]:
    """見直しの結果（1 行目が「問題なし／問題あり」で始まるか。指示を書き写したときに誤って弾かない）。"""
    verdict = next((ln.strip(" 　。.*「」") for ln in str(out.get("verdict") or "").splitlines() if ln.strip()), "")
    return verdict, str(out.get("issues") or "").strip()


def _set_label(llm: Any, label: str) -> None:
    if hasattr(llm, "label"):
        llm.label = label


# --- 読解の設問の欄を読む ---------------------------------------------------------

_FIELD_RE = re.compile(r"^\s*[*_#>-]*\s*\(?(Question|Answer|Wrong|Evidence|Hint|Explanation|[A-D])(?:\s*\d+)?\)?"
                       r"\s*[*_]*\s*[:：.)]\s*[*_]*\s*(.*)$", re.I)


def parse_question(text: str) -> dict[str, Any]:
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
    evidence = check.strip_quotes(fields.get("evidence", ""))
    return {"question": fields.get("question", ""), "options": [fields.get(c, "") for c in LETTERS],
            "answer": LETTERS.index(answer.group()) if answer else -1, "wrong": wrong, "evidence": evidence,
            "hint": fields.get("hint", ""), "explanation": fields.get("explanation", "")}


def _question_issues(q: dict[str, Any], passage: str, *, check_paragraph: bool = True) -> list[str]:
    """設問の点検。check_paragraph：ヒントの「第N段落」を根拠の文のある段落と照らす（お知らせ・広告は
    見出しや箇条書きで段落の数え方が決まらないので照らさない）。"""
    out = check.english_issues(q["question"], "設問")
    if q["answer"] < 0:
        return out + ["正解の記号（Answer: A〜D）がない"]
    out += check.choice_issues(q["options"], q["answer"])
    wrong = [c for i, c in enumerate(LETTERS) if i != q["answer"]]
    if any(q["wrong"].get(c) not in READING_MISTAKES for c in wrong):
        out.append("誤答の種類（Wrong: B=not_stated のような行）が足りない・種類の記号が違う")
    for c in wrong:  # 「本文に書かれていない」なのに、中心の語が本文にある（第 5 回のレビュー C-3）
        option = q["options"][LETTERS.index(c)]
        if q["wrong"].get(c) == "not_stated" and check.stated_in_passage(option, passage):
            out.append(f"誤答「{option[:60]}」の種類が not_stated だが、中心の語が本文にある"
                       "（本文と食い違うなら contradicts、一部だけ違うなら partial、細部の違いなら detail に直す）")
    if not q["evidence"]:
        out.append("根拠の文（Evidence:）がない")
    elif not check.contains(passage, q["evidence"]):
        out.append(f"根拠の文「{q['evidence'][:80]}」が本文にそのまま入っていない（本文から一字一句写す）")
    out += check.japanese_issues(q["hint"], "ヒント")
    out += check.japanese_issues(q["explanation"], "解説", min_len=30)
    for field in ("hint", "explanation"):
        if check.letter_reference(q[field]):
            out.append(("ヒント" if field == "hint" else "解説") + "が選択肢を A・B などの記号で呼んでいる（選択肢は並べ替えるので中身で呼ぶ）")
    correct = q["options"][q["answer"]]
    if len(correct) >= 12 and check.flat(correct) and check.flat(correct) in check.flat(q["hint"]):
        out.append("ヒントに正解の選択肢がそのまま書かれている")
    # ヒントが解説の 1 文目とほぼ同じ・ヒントに根拠の文がそのまま入っている（答えが見えてしまう。A-4）
    if check.similar(q["hint"], check.first_sentence(q["explanation"])):
        out.append("ヒントが解説の 1 文目とほぼ同じ（ヒントは本文のどこを読むかだけを示す）")
    if q["evidence"] and check.word_count(q["evidence"]) >= 4 and check.contains(q["hint"], q["evidence"]):
        out.append("ヒントに根拠の文がそのまま書かれている")
    for field, label in (("hint", "ヒント"), ("explanation", "解説")):
        if check.article_typo(q[field]):
            out.append(f"{label}で段落を「第N条」と書いている（「第N段落」）")
    if check.main_vs_one(q["question"], q["evidence"]):
        out.append("設問は main reason / main idea などを問うのに、根拠の文は「One reason」など理由の 1 つにすぎない"
                   "（設問を「one reason」にするか、主な理由を述べた文を根拠にする）")
    # ヒントの「第N段落」が、根拠の文のある段落と合っているか（A-5）
    if check_paragraph and q["evidence"]:
        where = check.paragraph_of(passage, q["evidence"])
        said = check.paragraph_numbers(q["hint"])
        if where and said and where not in said:
            out.append(f"ヒントの「第{said[0]}段落」が、根拠の文のある段落（第{where}段落）と違う")
    return out


# --- 語彙の誤答 -------------------------------------------------------------------

def _kanji(meaning: str) -> set[str]:
    """意味の重なりを見るための字（漢字・カタカナの 2 字の並び）。「提案」と「提案する」などを同じとみなす。"""
    core = re.sub(r"[぀-ゟ〜]", " ", meaning)
    out = set()
    for part in core.split():
        out |= {part[i:i + 2] for i in range(len(part) - 1)} or {part}
    return out


def _all_kanji(word: Word) -> set[str]:
    out: set[str] = set()
    for m in word.meanings:
        out |= _kanji(m)
    return out


def pick_distractors(word: Word, rng: random.Random, n: int = 3, *, need_label: bool = False) -> list[Word]:
    """同じ品詞・同じレベルで、意味が重ならない語を n 個（意味が近い語は 2 つとも正解になりうるので避ける）。

    中学の語は使わない（D-1）。need_label：意味を選択肢に出すので、2 文字以上の意味がある語だけ（D-2）。
    意味の重なりは、並べたすべての意味で見る（D-3）。
    """
    pool = [w for w in WORDS if w.pos == word.pos and w.level == word.level and w.word != word.word
            and w.word not in JHS_WORDS and (w.label or not need_label)]
    rng.shuffle(pool)
    chosen: list[Word] = []
    seen = _all_kanji(word)
    for w in pool:
        k = _all_kanji(w)
        if k & seen or set(w.meanings) & set(word.meanings) or any(w.word == c.word for c in chosen):
            continue
        chosen.append(w)
        seen |= k
        if len(chosen) == n:
            break
    return chosen


def _blank(sentence: str, word: str, repl: str) -> str:
    return re.sub(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", repl, sentence, count=1, flags=re.I)


def spelling_pattern(word: str) -> str:
    return word[0] + " _" * (len(word) - 1)


def spelling_hint(word: str) -> str:
    """スペルの問題のヒント（コードで作る。LLM のヒントは位置の説明の誤りが多かった。第 4 回のレビュー D-4）。

    問題文に最初の文字と文字数は出ているので、最後の文字（7 文字以上なら 2 文字目も）を見せる。
    """
    hint = f"最後の文字は「{word[-1]}」です。"
    if len(word) >= 7:
        hint += f"2 文字目は「{word[1]}」です。"
    return hint


CANDIDATES = 12  # 誤答の候補の数（作成役がここから 3 つ選ぶ。D-5）


def chosen_meaning(word: Word, text: str) -> str:
    """作成役が選んだ意味（並べた意味のどれか）。読めなければ中心の意味。"""
    t = (text or "").strip().strip("「」『』\"' 。")
    first = t.splitlines()[0].strip() if t else ""
    return next((m for m in word.meanings if m == first), None) or next(
        (m for m in word.meanings if m and m in t), word.meaning)


def chosen_others(text: str, candidates: list[Word], n: int = 3) -> tuple[list[Word], bool]:
    """作成役が候補から選んだ誤答の語。足りなければ候補の順に足す。(語, 作成役が 3 つとも正しく選んだか)。"""
    by_word = {w.word.lower(): w for w in candidates}
    picked: list[Word] = []
    for token in re.findall(r"[A-Za-z]+", text or ""):
        w = by_word.get(token.lower())
        if w is not None and w not in picked:
            picked.append(w)
    ok = len(picked) >= n
    for w in candidates:
        if len(picked) >= n:
            break
        if w not in picked:
            picked.append(w)
    return picked[:n], ok


def normalize_word(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").strip().strip("\"'“”‘’「」").rstrip(".。").strip().lower()
    return t.replace("’", "'")


_LETTER_RE = re.compile(r"(?<![A-Za-z])([A-D])(?![A-Za-z])")  # 小文字の a は冠詞と区別できないので見ない


def read_letter(answer: str, work: str, n: int) -> int | None:
    """検証役の答えの記号。answer 欄の最初の記号、なければ work の最後の「答え: B」などの記号。"""
    letters = "ABCDEFGH"[:n]
    head = (answer or "").strip().strip("*()（）[]【】「」.。 ").upper()
    if head and head[0] in letters and (len(head) == 1 or not head[1:2].isalpha()):
        return letters.index(head[0])
    m = _LETTER_RE.search(answer or "")
    if m and m.group(1).upper() in letters:
        return letters.index(m.group(1).upper())
    tail = re.findall(r"(?:答え|answer)\s*(?:は|is)?\s*[:：]?\s*\(?([A-Da-d])\)?", work or "", re.I)
    if tail and tail[-1].upper() in letters:
        return letters.index(tail[-1].upper())
    return None


# 検証で、正解そのものが合わないことを表す確認（不合格の理由は solver_mismatch。ほかの確認の不合格は正解が複数）
ANSWER_CHECKS = ("正解の裏付け", "正解が合うか")
# 理由・目的を問う設問（作り直した D を、まずこれだけに使う。第 8 回のレビュー 2-2。#860 は理由を問う設問）
REASON_RE = re.compile(r"\b(why|reasons?|because|purpose|in order to|so that|causes?d?)\b", re.I)
# スペルの問題文の空所「( c _ _ _ _ _ )」
SPELL_BLANK = r"\(\s*[A-Za-z](?:\s*_)+\s*\)"


def _kanji_pairs(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)
            if all("\u4e00" <= ch <= "\u9fff" for ch in text[i:i + 2])}


def spelling_rivals(word: Any) -> list[str]:
    """スペルの問題で答えが 2 つになりうる語（単語リストのうち、同じ最初の文字・文字数・品詞で、意味が同じか
    漢字 2 字が重なるもの。例：severe と strict の「厳しい」）。意味の近さは文字列で見る暫定の方法
    （第 7 回のレビュー H。リストにない語＝method に対する manner は、検証で条件に合う語をすべて挙げさせて拾う）。"""
    mine = {m.replace("〜", "") for m in word.meanings}
    pairs = set().union(*(_kanji_pairs(m) for m in mine)) if mine else set()
    out = []
    for w in WORDS:
        if w.word == word.word or w.word[:1] != word.word[:1] or len(w.word) != len(word.word) or w.pos != word.pos:
            continue
        theirs = {m.replace("〜", "") for m in w.meanings}
        if mine & theirs or pairs & set().union(*(_kanji_pairs(m) for m in theirs)):
            out.append(w.word)
    return out


def example_sentence(problem: Any) -> str:
    """語彙・スペルの例文に答えを入れた文（例文の筋の確認に使う）。空所は正解の語、[ ] の印は外す。"""
    sentence = problem.question.split("\n\n")[-1].strip()
    if problem.template == "en-vocab-blank":
        right = next((c["text"] for c in problem.choices if c.get("correct")), "")
        return sentence.replace("(　　　　)", right) if right else ""
    if problem.template == "en-vocab-meaning":
        return re.sub(r"\[\s*([^\]]+?)\s*\]", r"\1", sentence)
    if problem.template == "en-spelling":
        word = str(problem.answer.get("word", ""))
        return re.sub(SPELL_BLANK, word, sentence) if word else ""
    return ""


def read_none_fit(answer: str, options: list[str]) -> int | None:
    """正解を抜いた判定の答え（番号・「なし」・選択肢の言葉）を、options の番号（0 から）にする。最後が「どれも合わない」。
    読めなければ None。選択肢ごとに説明を書いた長い返事から、最初の数字を拾って取り違えないようにする（第 7 回のレビュー A）。"""
    text = unicodedata.normalize("NFKC", answer or "").strip()
    none = len(options) - 1
    flat = re.sub(r"[\s*「」『』\"'`.。、:：()（）\[\]]", "", text)
    if not flat:
        return None
    if flat in ("なし", "無し", "ない", "該当なし", "none", "None", "NONE", "0", "どれも合わない") or \
            flat.startswith(("なし", "どれも合わない")):
        return none
    m = re.search(r"(?:答え|answer|番号|選択|正解)\s*(?:は|[:：])?\s*\(?([0-9]+)", text, re.I)
    first = text.splitlines()[0]
    if m is None and len(re.sub(r"\s", "", first)) <= 12:  # 「4」「4: どれも合わない」「(2)」（見出しなしで理由が続くときも）
        if re.sub(r"[\s*「」:：.]", "", first) in ("なし", "どれも合わない"):
            return none
        m = re.match(r"^[\s*(（\[]*([0-9]+)[\s*)）\].:：]*(.*)$", first)
        # 番号のあとに書いてよいのは、その選択肢の言葉だけ（「1: 集まる→合わない」のような選択肢ごとの説明は読まない）
        if m is not None and m.group(2).strip() and not (
                0 < int(m.group(1)) <= len(options) and m.group(2).strip(" *") == options[int(m.group(1)) - 1]):
            m = None
    if m is not None:
        k = int(m.group(1)) - 1
        return k if 0 <= k < len(options) else (none if k == -1 else None)
    hits = [i for i, o in enumerate(options) if o and o in text]
    return hits[0] if len(hits) == 1 else None


@contextlib.contextmanager
def _thinking(llm: Any, choice: str) -> Any:
    """この間だけ思考モードを決める（"on" / "off"。"" はモデルの設定のまま）。読解の選択肢の判定だけ思考ありで
    試すため（第 7 回のレビュー E）。"""
    if choice not in ("on", "off") or not hasattr(llm, "think_for"):
        yield
        return
    old = llm.think_for
    llm.think_for = lambda _m: choice == "on"
    try:
        yield
    finally:
        llm.think_for = old


def _differs(reason: str) -> str:
    """判定の理由の中の「違う点: …」（選択肢のどこが本文と違うか）。なければ空。"""
    m = re.search(r"違う点\s*[:：]\s*(.+?)(?:\s+[—\-ー]{1,2}\s+|$)", reason or "")
    return m.group(1).strip() if m else ""


def _strict_missing(got: dict[int, tuple[str, str]], right: int, choices: list[dict[str, Any]]) -> list[str]:
    """作り直した D（第 8 回のレビュー 2-2）で、根拠の書き方が足りない誤答。「本文と食い違う」は本文の文の引用、
    ほかの誤答（書かれていない・一部だけ・細部）は「違う点」を書く。「裏付けあり」は別の確認（正解が複数）で見る。"""
    out = []
    for i, (lb, reason) in got.items():
        if i == right or lb == "裏付けあり" or i >= len(choices):
            continue
        if lb == "本文と食い違う" and not _quote(reason):
            out.append(f"「{choices[i]['text'][:50]}」食い違う本文の文の引用がない")
        elif lb != "本文と食い違う" and not _differs(reason):
            out.append(f"「{choices[i]['text'][:50]}」違う点が書かれていない")
    return out


def _quote(reason: str) -> str:
    """判定の理由の中の「引用: "…"」。なければ空。"""
    m = re.search(r"引用\s*[:：]\s*[\"“「『]([^\"”」』]+)[\"”」』]", reason or "")
    return m.group(1).strip() if m else ""


class EnglishSubject(Subject):
    id = "english"
    name = "英語"
    formats = ("choice", "input")
    mistakes = MISTAKES
    math_text = False  # 「$20」のような値段を数式として表示しない
    # 読解の本文を書く役割と、本文なしの確認をする役割（どちらも設定がなければ作成役のモデル）。
    # 本文なしの確認は作成役が弱いと効かないので、検証役・見直し役のモデルにも変えられる（第 4 回のレビュー A-8）
    model_roles = {"passage": "本文作成", "blind": "本文なしの確認"}
    reject_codes = {
        "en_text": "英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）",
        "en_no_passage": "本文を読まなくても解けた（常識で答えられる設問）",
        "en_passage_review": "本文の見直しで誤りを指摘された（時系列・因果・矛盾・英文の誤り）",
        "en_review_unread": "見直しの結果を読めなかった（やり直しても読めない確認があった）",
        "en_review": "見直しで誤りを指摘された（解説・ヒント・訳の誤り、例文・本文の筋）",
        "en_verify_multiple": "検証で、正解のほかにも合う選択肢・語があった（正解が 1 つに決まらない）",
    }
    test_format = "choice"  # 検証のテスト：正解の印を誤答に移した問題を流す
    verify_split_check = True  # 2 回の判定が割れたら要確認（第 6 回のレビュー 2-4）
    # 検証のやり方の切り替え（第 7 回のレビュー C・D・E。見本集で並べて測れる）。既定は見本集でいちばん成績の
    # 良かった #129 の形（語彙は 2 段階・誤答の根拠は書かせない。第 8 回のレビュー 1 章）
    verify_options = {
        "fit3": {"label": "語彙の判定", "default": "off",
                 "choices": {"off": "2 段階（通る／通らない）", "on": "3 段階（自然に入る／不自然／入らない）"}},
        "reading_strict": {"label": "読解の誤答の根拠", "default": "off",
                           "choices": {"off": "書かせない", "reason": "理由・目的を問う設問だけ書かせる",
                                       "all": "すべての設問で書かせる"}},
        "reading_think": {"label": "読解の選択肢の判定の思考モード", "default": "model",
                          "choices": {"model": "モデルの設定のまま", "on": "あり", "off": "なし"}},
    }
    # 英語の既定のモデル（第 8 回のレビュー 1 章。見本集の #129。26b は本文の破綻を見逃したので見直しは 12b）。
    # 本文作成・本文なしの確認は、設定がなければ作成役と同じ
    default_models = {"gen": "gemma4:26b-a4b-it-qat", "verify": "gemma4:e4b-it-qat", "review": "gemma4:12b-it-qat"}

    def units(self) -> list[Unit]:
        return _units()

    # --- 作成 ---------------------------------------------------------------

    def generate(self, ctx: GenContext, template: str, fmt: str) -> Draft | list[Draft]:
        if template in READINGS:
            return self._reading(ctx, template)
        return self._vocab(ctx, template, fmt)

    def _reading(self, ctx: GenContext, template: str) -> list[Draft]:
        """読解：本文作成役が本文を書く（または、設問の足りない本文を使い回す）→ 作成役が設問を書く。"""
        spec = READINGS[template]
        n = spec.questions
        existing = ctx.existing_keys(template)
        stored = ctx.passages(template)

        def key(topic: str, i: int) -> str:
            return _params_key(template, {"topic": topic, "q": i})

        # 見直しを通った本文のうち、設問が足りないもの（設問が不合格になった本文）に設問を足す。
        # 不合格が重なる本文（設問の数の 2 倍）は使わない。数えるのは今の作成役が作った設問の不合格だけ
        # （弱い作成役で設問が落ち続けた本文にも、別の作成役なら設問を足せる。第 4 回のレビュー E-5）
        reusable = [p for p in stored if p.get("status") == "ok" and p.get("topic") and len(p["questions"]) < n
                    and p.get("rejected_by", {}).get(ctx.gen_model, 0) < 2 * n]
        src = self._reusable_passage(ctx, reusable) if reusable else None
        if src is not None:
            topic, body, have = src["topic"], src["body"], list(src["questions"])
            source: dict[str, Any] = {"id": src["id"]}
            log: list[dict[str, Any]] = [{
                "step": "本文", "ok": True,
                "summary": f"前に作った本文 #{src['id']}（題材「{topic}」、{src.get('gen_model') or '—'} が作成）に設問を足す"
                           f"（今 {len(have)} 問）"}]
            passage_model = src.get("gen_model", "")
            pre = [e for e in src.get("log", []) if e.get("step") == "使い回す前の本文の見直し"]
            if pre:
                log.append({**pre[-1]})
        else:
            used = {p.get("topic") for p in stored if p.get("status") != "rejected"}  # 落ちた本文の題材はまた使える
            topics = [t for t in TOPICS[template] if t not in used and key(t, 1) not in existing]
            if not topics:
                raise Rejected("duplicate", "この小単元の題材をすべて使いました（catalog.TOPICS に題材を足すと作れます）")
            topic = ctx.rng.choice(topics)
            title, passage, log = self._write_passage(ctx, spec, template, topic)
            body = f"{title}\n\n{passage}" if title else passage
            passage_model = ctx.model("passage")
            source = {"title": title, "body": body, "source_title": f"{passage_model} が書いた英文", "source_url": "",
                      "template": template, "topic": topic, "status": "ok", "gen_model": passage_model,
                      "log": list(log)}  # 本文の記録だけ（このあと設問の記録が log に足されるので、ここで写す）
            have = []
        # 設問の番号（重複の判定に使う）。不合格になった設問の番号とも重ならないよう、不合格の数も足す
        start = len(have) + (src.get("rejected", 0) if reusable else 0) + 1
        params = {"topic": topic, "seed": ctx.rng.randrange(1 << 30), "test": ctx.is_test,
                  "passage_model": passage_model}
        partial: dict[str, Any] = {"unit": "en-reading", "template": template, "format": "choice", "params": params}
        qs, q_issues = self._write_questions(ctx, spec, body, have, n - len(have), log, partial)

        drafts = []
        for i, (q, problems) in enumerate(zip(qs, q_issues), start):
            qlog = list(log)
            draft_partial = {**partial, "params": {**params, "q": i}, "question": q["question"],
                             "explanation": q["explanation"], "hint": q["hint"]}
            if 0 <= q["answer"] < len(q["options"]):  # 不合格でも選択肢を残す（書き出しで見られるように。E-3）
                draft_partial["choices"] = [{"text": t, "correct": j == q["answer"],
                                             "mistake": None if j == q["answer"] else q["wrong"].get(LETTERS[j])}
                                            for j, t in enumerate(q["options"])]
            if problems:
                summary = "設問の点検で落ちた: " + "／".join(dict.fromkeys(problems))
                drafts.append(self._rejected_draft(template, "en_text", summary, qlog, draft_partial, source))
                continue
            qlog.append({"step": "英文の点検", "ok": True,
                         "summary": f"根拠の文が本文にある・選択肢 4 つが重複しない・英文に日本語がない（本文 {check.word_count(body)} 語）"})
            choices = [{"text": t, "correct": j == q["answer"], "mistake": None if j == q["answer"]
                        else q["wrong"].get(LETTERS[j])} for j, t in enumerate(q["options"])]
            if not ctx.is_test:
                blind = self._blind_check(ctx, q["question"], q["options"], q["answer"])
                qlog.append(blind[1])
                if not blind[0]:
                    drafts.append(self._rejected_draft(template, "en_no_passage", blind[1]["summary"], qlog,
                                                       {**draft_partial, "choices": choices}, source))
                    continue
            ctx.rng.shuffle(choices)
            if ctx.is_test:
                _tamper(choices, ctx.rng)
            correct = next(c for c in choices if c["correct"])
            explanation = q["explanation"].strip() + f"\n\n本文の根拠: \u201c{q['evidence']}\u201d"
            drafts.append(Draft(
                unit="en-reading", template=template, format="choice", question=q["question"],
                answer={"kind": "choice"}, answer_display=correct["text"], explanation=explanation, hint=q["hint"],
                choices=choices,
                source=f"本文 {passage_model or '—'}・設問 {ctx.gen_model}（題材「{topic}」）。正解の根拠の文は本文と照合済み",
                params={**params, "q": i, "evidence": q["evidence"]}, params_key=key(topic, i), log=qlog, passage=source,
            ))
        return drafts

    def _write_passage(self, ctx: GenContext, spec: Any, template: str, topic: str) -> tuple[str, str, list[dict[str, Any]]]:
        """本文作成役が題名と本文を書く → 機械的な点検（落ちたら 1 回だけ直させる）→ 本文の見直し（見直し役）。"""
        model = ctx.model("passage")
        user = prompts.passage_user(spec.genre, topic, spec.words)
        log: list[dict[str, Any]] = [{
            "step": "題材", "ok": True, "model": model,
            "summary": f"題材「{topic}」（{spec.words[0]}〜{spec.words[1]} 語・設問 {spec.questions} 問）。本文作成役 {model}"
                       + ("。検証のテストのため、正解の印を誤答に移します" if ctx.is_test else ""),
            "detail": "本文作成役に送った文章:\n" + user + "\n\n（システムの指示）\n" + prompts.PASSAGE_SYSTEM,
        }]
        partial: dict[str, Any] = {"unit": "en-reading", "template": template, "format": "choice",
                                   "params": {"topic": topic, "passage_model": model}}
        rejected_passage: dict[str, Any] = {"template": template, "topic": topic, "status": "rejected",
                                            "gen_model": model, "source_title": f"{model} が書いた英文"}
        _set_label(ctx.llm, "本文作成")
        out = _chat(ctx, prompts.PASSAGE_SYSTEM, user, prompts.PASSAGE_SCHEMA, model)
        for round_no in range(FIX_ROUNDS + 1):
            title = check.normalize(str(out.get("title") or ""))
            passage = check.normalize(str(out.get("passage") or ""))
            partial.update(question=f"（本文の段階で不合格）{title}", explanation="",
                           passage={**rejected_passage, "title": title, "body": f"{title}\n\n{passage}" if title else passage})
            issues = self._passage_issues(spec, title, passage)
            if not issues:
                break
            summary = "／".join(dict.fromkeys(issues))
            last = round_no == FIX_ROUNDS
            log.append({"step": "本文の点検", "ok": False, "summary": summary + ("" if last else "（直しを依頼します）")})
            if last:
                raise Rejected("en_text", "本文の点検で落ちた: " + summary, log, partial)
            _set_label(ctx.llm, "直し")
            out = _chat(ctx, prompts.PASSAGE_SYSTEM,
                        prompts.fix_user(user, out, summary, list(prompts.PASSAGE_SCHEMA["properties"])),
                        prompts.PASSAGE_SCHEMA, model)
        log.append({"step": "本文の点検", "ok": True,
                    "summary": f"本文 {check.word_count(passage)} 語・英文に日本語がない"})
        body = f"{title}\n\n{passage}" if title else passage
        if ctx.review:
            log.append(self._review_passage(ctx, body, log, partial))
        return title, passage, log

    @staticmethod
    def _passage_check(llm: LLM, model: str, body: str) -> VerifyResult:
        """本文の見直し（時系列・因果・矛盾・言葉と内容の食い違い・架空のものを本物扱い・英文の誤り）。
        読めなければ 1 回だけやり直し、それでも読めなければ ok=False・code="en_review_unread"（A-2）。"""
        from ...llm import LLMError

        verdict, issues, raw = "", "", ""
        for _ in range(2):
            try:
                out = llm.chat_json(model, prompts.PASSAGE_REVIEW_SYSTEM, prompts.passage_review_user(body),
                                    prompts.REVIEW_SCHEMA, temperature=0.2)
            except LLMError as exc:
                raw = f"読めない出力（{exc}）\n{getattr(exc, 'raw', '')}"
                continue
            verdict, issues = _verdict(out)
            raw = f"[[verdict]]\n{verdict}\n[[issues]]\n{issues}"
            if verdict.startswith(("問題あり", "問題なし")):
                break
        if verdict.startswith("問題あり"):
            return VerifyResult(False, f"{model}: 本文の見直しで誤りを指摘された（{issues[:300] or '理由なし'}）", issues,
                                "en_passage_review", raw=raw[:4000])
        if verdict.startswith("問題なし"):
            return VerifyResult(True, f"{model}: 本文に誤りは見つからなかった", issues, raw=raw[:4000])
        return VerifyResult(False, f"{model}: 本文の見直しの結果を読めなかった", "", "en_review_unread", raw=raw[:4000])

    def review_passage(self, llm: LLM, model: str, passage: Any) -> VerifyResult | None:
        """見直しだけをやり直す依頼で「本文の見直しもする」を選んだとき（第 5 回のレビュー B-2）。"""
        return self._passage_check(llm, model, passage.body)

    def _review_passage(self, ctx: GenContext, body: str, log: list[dict[str, Any]],
                        partial: dict[str, Any]) -> dict[str, Any]:
        """作成のときの本文の見直し（見直し役）。誤りの指摘・読めないときは本文ごと不合格（本文は状態 rejected で残す）。"""
        model = ctx.model("review")
        _set_label(ctx.llm, "本文の見直し")
        res = self._passage_check(ctx.llm, model, body)
        entry = {"step": "本文の見直し", "model": model, "ok": res.ok, "summary": res.summary, "raw": res.raw}
        if not res.ok:
            raise Rejected(res.code, res.summary, log + [entry], partial)
        return entry

    def _reusable_passage(self, ctx: GenContext, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        """使い回す本文を選ぶ。今の本文作成役以外が書いた本文は、今の見直し役の見直しを通ったものだけ
        （まだなら今見直して、結果を本文に残す。誤りがあれば使えない印を付ける。第 5 回のレビュー B-1：
        bonsai・qwen の古い本文が使い回されていた）。見直しをしない依頼では、ほかのモデルの本文は使わない。"""
        writer, reviewer = ctx.model("passage"), ctx.model("review")
        for src in candidates:
            if src.get("gen_model") == writer:
                return src
            checked = [e for e in src.get("log", []) if e.get("step") in ("本文の見直し", "使い回す前の本文の見直し",
                                                                           "本文の見直しのやり直し")
                       and e.get("model") == reviewer]
            if checked:
                if checked[-1].get("ok"):
                    return src
                continue
            if not ctx.review:
                continue
            _set_label(ctx.llm, "本文の見直し")
            res = self._passage_check(ctx.llm, reviewer, src["body"])
            entry = {"step": "使い回す前の本文の見直し", "model": reviewer, "ok": res.ok, "summary": res.summary,
                     "raw": res.raw}
            ctx.update_passage(src["id"], {"log_add": [entry], **({} if res.ok else {"status": "rejected"})})
            src["log"] = src.get("log", []) + [entry]
            if res.ok:
                return src
        return None

    def _write_questions(self, ctx: GenContext, spec: Any, body: str, have: list[str], n: int,
                         log: list[dict[str, Any]], partial: dict[str, Any]) -> tuple[list[dict[str, Any]], list[list[str]]]:
        """作成役が確定した本文に設問を n 問書く → 点検（落ちたら 1 回だけ直させる）。(設問, 設問ごとの指摘)。"""
        schema = prompts.question_schema(n)
        user = prompts.question_user(body, n, spec.question_style, have)
        log.append({"step": "設問の作成", "ok": True, "model": ctx.gen_model,
                    "summary": f"作成役 {ctx.gen_model} が設問を {n} 問書く",
                    "detail": "作成役に送った文章:\n" + user + "\n\n（システムの指示）\n" + prompts.QUESTION_SYSTEM})
        _set_label(ctx.llm, "作成")
        out = _chat(ctx, prompts.QUESTION_SYSTEM, user, schema)
        qs: list[dict[str, Any]] = []
        q_issues: list[list[str]] = []
        for round_no in range(FIX_ROUNDS + 1):
            qs = [parse_question(check.normalize(str(out.get(f"q{i}") or ""))) for i in range(1, n + 1)]
            q_issues = [_question_issues(q, body, check_paragraph=spec.id != "en-notice") for q in qs]
            partial.update(question=qs[0]["question"] if qs else "", explanation=body)
            all_issues = [f"設問 {i}: {x}" for i, xs in enumerate(q_issues, 1) for x in xs]
            if not all_issues:
                break
            summary = "／".join(dict.fromkeys(all_issues))
            last = round_no == FIX_ROUNDS
            log.append({"step": "設問の点検", "ok": False, "summary": summary + ("" if last else "（直しを依頼します）")})
            if last:
                break  # 落ちた設問だけを不合格にし、ほかの設問は先へ進める
            _set_label(ctx.llm, "直し")
            out = _chat(ctx, prompts.QUESTION_SYSTEM, prompts.fix_user(user, out, summary, list(schema["properties"])),
                        schema)
        return qs, q_issues

    @staticmethod
    def _passage_issues(spec: Any, title: str, passage: str) -> list[str]:
        out = check.english_issues(passage, "本文") + (check.english_issues(title, "題名") if title else [])
        n = check.word_count(passage)
        lo, hi = spec.words
        if n < lo * 0.8 or n > hi * 1.3:
            out.append(f"本文の長さが {n} 語（{lo}〜{hi} 語にする）")
        return out

    @staticmethod
    def _rejected_draft(template: str, code: str, summary: str, log: list[dict[str, Any]],
                        partial: dict[str, Any], passage: dict[str, Any] | None = None) -> Draft:
        """同じ本文のほかの設問は検証に進めるので、この設問だけを不合格として返す（worker が残す）。

        本文も渡す（設問がすべて不合格でも本文は残り、後の依頼で設問を足せる）。"""
        log = log + [{"step": "設問", "ok": False, "summary": summary}]
        return Draft(unit="en-reading", template=template, format="choice", question=partial.get("question", ""),
                     answer={}, answer_display="", explanation=partial.get("explanation", ""),
                     hint=partial.get("hint", ""), log=log, passage=passage,
                     rejected=Rejected(code, summary, log, partial))

    def _blind_check(self, ctx: GenContext, question: str, options: list[str], answer: int) -> tuple[bool, dict[str, Any]]:
        """本文なしの確認役（設定がなければ作成役）に本文を見せずに設問だけを解かせる。正解できたら、本文を読まなくても解ける設問とみなす。"""
        from ...llm import LLMError

        model = ctx.model("blind")
        _set_label(ctx.llm, "本文なしの確認")
        user = prompts.solve_user(question, options)
        out: dict[str, Any] = {}
        for attempt in range(2):  # 読めなかったら 1 回だけやり直す
            try:
                out = ctx.llm.chat_json(model, prompts.BLIND_SYSTEM, user, prompts.BLIND_SCHEMA, temperature=0.2)
                break
            except LLMError as exc:
                if attempt == 1:  # 確かめられないときは通す（見直しの「正解が 1 つか」で拾う）
                    return True, {"step": "本文なしの確認", "ok": True, "summary": f"確かめられなかった（{exc}）",
                                  "model": model, "raw": getattr(exc, "raw", "")[:500]}
        raw = str(out.get("answer") or "")
        got = read_letter(raw, "", len(options))
        entry = {"step": "本文なしの確認", "model": model, "raw": raw[:500]}
        if got == answer:
            return False, {**entry, "ok": False,
                           "summary": "本文を見せずに解かせたら正解した（常識で答えられる設問なので使わない）"}
        shown = "X（本文がないと分からない）" if got is None else f"{LETTERS[got]}（不正解）"
        return True, {**entry, "ok": True, "summary": f"本文を見せずに解かせた答えは {shown}"}

    def _vocab(self, ctx: GenContext, template: str, fmt: str) -> Draft:
        spec = VOCABS[template]
        existing = ctx.existing_keys(template)
        pool = [w for w in WORDS if w.level == spec.word_level and w.word not in JHS_WORDS
                and (fmt != "input" or (w.word not in SPELLING_EXCLUDE and len(w.word) >= 4))]
        pool = [w for w in pool if _params_key(template, {"word": w.word, "pos": w.pos}) not in existing]
        if fmt == "input":  # 同じ最初の文字・文字数・近い意味の語がある語は出さない（答えが 2 つになる。第 7 回のレビュー H）
            pool = [w for w in pool if not spelling_rivals(w)]
        if not pool:
            raise Rejected("duplicate", "単語リストの語をすべて使いました（words.py に語を足すと作れます）")
        word = ctx.rng.choice(pool)
        choice = template != "en-spelling"
        # 誤答の候補（同じ品詞・レベルで意味が重ならない語）を作成役に渡し、文法上は入るが意味が合わない語を 3 つ選ばせる（D-5）
        candidates = pick_distractors(word, ctx.rng, CANDIDATES, need_label=template == "en-vocab-meaning") if choice else []
        if choice and len(candidates) < 3:
            raise Rejected("choices", f"「{word.word}」と意味が重ならない誤答の語を 3 つ選べません")
        params: dict[str, Any] = {"word": word.word, "pos": word.pos, "seed": ctx.rng.randrange(1 << 30),
                                  "test": ctx.is_test}
        key = _params_key(template, {"word": word.word, "pos": word.pos})
        schema = prompts.vocab_schema(template, multi=len(word.meanings) > 1)
        system = prompts.vocab_system(template)
        user = prompts.vocab_user(word.word, POS[word.pos], list(word.meanings), [w.word for w in candidates])
        log: list[dict[str, Any]] = [{
            "step": "単語", "ok": True,
            "summary": f"単語リストから「{word.word}」（{POS[word.pos]}・{'／'.join(word.meanings)}）を選んだ"
                       + (f"。誤答の候補 {len(candidates)} 語" if choice else "")
                       + ("。検証のテストのため、正解の印を誤答に移します" if ctx.is_test else ""),
            "detail": "作成役に送った文章:\n" + user + "\n\n（システムの指示）\n" + system,
        }]
        partial: dict[str, Any] = {"unit": "en-vocab", "template": template, "format": fmt, "params": params}
        out = _chat(ctx, system, user, schema)
        others: list[Word] = []
        picked_ok = True
        for round_no in range(FIX_ROUNDS + 1):
            fields = {k: check.normalize(str(out.get(k) or "")) for k in schema["properties"]}
            # 作成役が例文の語に付けた括弧（[leak]・【leak】）は外す（意味の問題で [[ leak ]] になった。A-3）
            fields["sentence"] = re.sub(rf"[\[【［]\s*({re.escape(word.word)})\s*[\]】］]", r"\1",
                                        fields["sentence"], flags=re.I)
            meaning = chosen_meaning(word, fields.get("meaning", ""))
            if choice:
                others, picked_ok = chosen_others(fields.get("others", ""), candidates)
            if not choice:
                fields["hint"] = spelling_hint(word.word)  # スペルのヒントはコードで作る（D-4）
            partial.update(question=fields["sentence"], explanation=fields["explanation"], hint=fields["hint"])
            issues = self._vocab_issues(word, others if template == "en-vocab-blank" else [], fields, template)
            if not issues:
                break
            summary = "／".join(dict.fromkeys(issues))
            last = round_no == FIX_ROUNDS
            log.append({"step": "例文の点検", "ok": False, "summary": summary + ("" if last else "（直しを依頼します）")})
            if last:
                raise Rejected("en_text", summary, log, partial)
            _set_label(ctx.llm, "直し")
            out = _chat(ctx, system, prompts.fix_user(user, out, summary, list(schema["properties"])), schema)
        params.update(meaning=meaning, others=[w.word for w in others])
        log.append({"step": "例文の点検", "ok": True,
                    "summary": f"例文に「{word.word}」がちょうど 1 回、ほかの選択肢の語は入っていない、訳・解説は日本語"
                               + (f"。意味は「{meaning}」を使う" if len(word.meanings) > 1 else "")})
        if choice:
            log.append({"step": "誤答", "ok": True,
                        "summary": ("作成役が候補から選んだ誤答の語: " if picked_ok else
                                    "作成役が候補から 3 つ選べなかったので、候補から足した誤答の語: ")
                                   + ", ".join(w.word for w in others)})
        sentence = fields["sentence"]
        explanation = f"訳: {fields['translation']}\n\n{fields['explanation']}"
        hint = fields["hint"]
        choices: list[dict[str, Any]] = []
        input_hint = ""
        if template == "en-vocab-blank":
            question = "次の英文の空所（　　）に入る最も適切な語を選べ。\n\n" + _blank(sentence, word.word, "(　　　　)")
            choices = [{"text": word.word, "correct": True, "mistake": None}]
            choices += [{"text": w.word, "correct": False, "mistake": "other_word"} for w in others]
            answer_display = word.word
            explanation += f"\n\n答え: {word.word}（{meaning}）"
        elif template == "en-vocab-meaning":
            question = ("次の英文の [ ] で示した語の意味として最も適切なものを選べ。\n\n"
                        + _blank(sentence, word.word, f"[ {word.word} ]"))
            choices = [{"text": meaning, "correct": True, "mistake": None}]
            choices += [{"text": w.label, "correct": False, "mistake": "other_meaning"} for w in others]
            answer_display = meaning
            explanation += f"\n\n答え: {word.word} ＝「{meaning}」"
        else:
            question = (f"日本語の意味に合う英単語を書け（{len(word.word)} 文字。最初の文字は示してある）。\n\n"
                        f"意味: {meaning}（{POS[word.pos]}）\n\n"
                        + _blank(sentence, word.word, f"( {spelling_pattern(word.word)} )"))
            answer_display = word.word
            input_hint = "英単語を 1 語、半角の英字で入力（大文字・小文字は区別しません）"
            explanation += f"\n\n答え: {word.word}"
        if choices:
            ctx.rng.shuffle(choices)
            if ctx.is_test:
                _tamper(choices, ctx.rng)
            answer_display = next(c["text"] for c in choices if c["correct"])
        return Draft(
            unit="en-vocab", template=template, format=fmt, question=question,
            answer={"kind": "choice"} if choices else {"kind": "word", "word": word.word},
            answer_display=answer_display, explanation=explanation, hint=hint, choices=choices,
            input_hint=input_hint, source=f"単語リスト（words.py）の「{word.word}」。例文は {ctx.gen_model}",
            params=params, params_key=key, log=log,
        )

    @staticmethod
    def _vocab_issues(word: Word, others: list[Word], fields: dict[str, str], template: str = "") -> list[str]:
        sentence = fields["sentence"]
        out = check.english_issues(sentence, "例文")
        if "\n" in sentence.strip():
            out.append("例文が 1 文になっていない（改行がある）")
        if re.search(r"[\[\]【】［］]", sentence):
            out.append("例文に [ ] などの括弧がある（語を示す括弧はこちらで付ける）")
        n_words = check.word_count(sentence)
        if not 6 <= n_words <= 30:
            out.append(f"例文の長さが {n_words} 語（8〜20 語にする）")
        times = check.count_word(sentence, word.word)
        if times != 1:
            out.append(f"例文に「{word.word}」がそのままの形で {times} 回（ちょうど 1 回、語尾を変えずに使う）")
        used = [w.word for w in others if check.count_word(sentence, w.word)]
        if used:
            out.append("例文に、選択肢に並ぶ別の語（" + ", ".join(used) + "）が使われている")
        if re.search(rf"(?<![A-Za-z])an?\s+{re.escape(word.word)}(?![A-Za-z])", sentence, re.I):
            out.append(f"「{word.word}」の直前に a / an がある（答えの手がかりになる）")
        out += check.japanese_issues(fields["translation"], "和訳")
        out += check.japanese_issues(fields["hint"], "ヒント")
        out += check.japanese_issues(fields["explanation"], "解説", min_len=20)
        if check.count_word(fields["hint"], word.word):
            out.append("ヒントに答えの単語が書かれている")
        # 意味を答える・空所に入れる問題で、ヒントに正解の語の意味が書かれている（スペルは問題文に意味を出すので見ない。A-4）
        if template != "en-spelling":
            for m in word.meanings:
                core = m.replace("〜", "").strip()
                if len(core) >= 2 and core in fields["hint"]:
                    out.append(f"ヒントに正解の語の意味「{m}」が書かれている")
                    break
        if check.similar(fields["hint"], check.first_sentence(fields["explanation"])):
            out.append("ヒントが解説の 1 文目とほぼ同じ")
        stray = check.stray_english(fields["explanation"], [sentence, word.word, *[w.word for w in others]])
        if stray:
            out.append(f"解説に、例文にも選択肢にもない英単語（{', '.join(stray[:4])}）がある（選択肢にない語の説明はしない）")
        # 意味の問題の例文には空所がない（A-7）
        if template == "en-vocab-meaning":
            for field, label in (("hint", "ヒント"), ("explanation", "解説")):
                if check.blank_words(fields[field]):
                    out.append(f"{label}に「空所」「入る」などの言葉がある（この問題の例文に空所はない）")
        return out

    # --- 検証 ---------------------------------------------------------------

    def verify(self, llm: LLM, sandbox: Sandbox, model: str, problem: Any,
               options: dict[str, str] | None = None) -> VerifyResult:
        """検証（第 6 回のレビュー 2 章）。解かせて正解と突き合わせるのをやめ、正解が 1 つに決まるかを確かめる。

        読解：選択肢ごとに「裏付けあり／本文と食い違う／本文に書かれていない…」を、裏付けに使った本文の文の引用とともに
        判定させる（引用は本文と機械的に照らし、本文にない引用の判定はやり直させる）。正解が裏付けあり以外・誤答が
        裏付けありなら不合格（2-1）
        空所・意味：選択肢ごとに文で通るかを判定させ（2-1）、さらに正解を抜いた 3 つと「どれも合わない」から選ばせる（2-2）
        スペル：最初の文字・文字数・意味に合う語をすべて挙げさせ、正解のほかに例文に入る語があれば不合格（2-3）
        選択肢は毎回並べ替える。検証の回数（2 回）のどちらかで不合格なら不合格、割れたら要確認（worker。2-4）。
        options（verify_options。第 7 回のレビュー）：fit3＝語彙の判定を 3 段階に（C）、reading_strict＝読解の誤答ごとに
        否定の根拠を書かせる（D）、reading_think＝読解の判定だけ思考モードを決める（E）。
        """
        opts = {k: spec["default"] for k, spec in self.verify_options.items()} | (options or {})
        three = opts.get("fit3") == "on"
        if problem.format != "choice":
            return self._verify_spelling(llm, model, problem, three)
        checks: list[tuple[str, bool | None, str]] = []
        raws: list[str] = []
        updates: dict[str, Any] = {}
        choices = list(problem.choices)
        random.shuffle(choices)  # 毎回並べ替える（記号の位置で決まらないように）
        sentence = problem.question.split("\n\n")[-1]
        if problem.template in READINGS:
            think = opts.get("reading_think", "model")
            if think in ("on", "off"):
                _set_label(llm, f"検証（読解・思考{'あり' if think == 'on' else 'なし'}）")  # 所要時間を比べる（E）
            strict_opt = opts.get("reading_strict", "off")
            strict = strict_opt in ("all", "on") or (strict_opt == "reason" and bool(REASON_RE.search(problem.question)))
            with _thinking(llm, think):
                self._judge_choices(llm, model, problem, choices, checks, raws, updates, quote=True, strict=strict)
        elif problem.template == "en-vocab-blank":
            sentences = [sentence.replace("(　　　　)", c["text"]) for c in choices]
            self._judge_fit(llm, model, prompts.FIT3_JUDGE_SYSTEM if three else prompts.FIT_JUDGE_SYSTEM,
                            prompts.fit_judge_user(sentences), choices, "空所に選択肢の語を入れた文", checks, raws,
                            three=three)
            self._judge_without_answer(llm, model, prompts.NONE_FIT_BLANK_SYSTEM, sentence, choices, checks, raws)
        elif problem.template == "en-vocab-meaning":
            self._judge_fit(llm, model, prompts.MEANING3_JUDGE_SYSTEM if three else prompts.MEANING_JUDGE_SYSTEM,
                            prompts.meaning_judge_user(sentence, [c["text"] for c in choices]), choices,
                            "語の意味の選択肢", checks, raws, three=three)
            self._judge_without_answer(llm, model, prompts.NONE_FIT_MEANING_SYSTEM, sentence, choices, checks, raws)
        return self._verdict_of(model, checks, raws, updates)

    @staticmethod
    def _verdict_of(model: str, checks: list[tuple[str, bool | None, str]], raws: list[str],
                    updates: dict[str, Any] | None = None) -> VerifyResult:
        """検証の確認の結果をまとめる。正解が合わない＝solver_mismatch、正解のほかにも合う＝en_verify_multiple、
        読めない・引用が本文にない判定が残った＝solver_unreadable。"""
        lines = [f"{'○' if ok else '×' if ok is False else '？'} {name}：{text}" for name, ok, text in checks]
        detail, raw = "\n".join(lines), "\n\n".join(raws)[:4000]
        failed = [(name, text) for name, ok, text in checks if ok is False]
        if failed:
            code = "solver_mismatch" if any(name in ANSWER_CHECKS for name, _t in failed) else "en_verify_multiple"
            summary = "／".join(f"{name}（{text[:150]}）" for name, text in failed)
            return VerifyResult(False, f"{model}: {summary[:400]}", detail, code, raw=raw)
        unread = [f"{name}：{text[:120]}" for name, ok, text in checks if ok is None]
        if unread:
            return VerifyResult(False, f"{model}: 判定を読めなかった（{'／'.join(unread)}）", detail,
                                "solver_unreadable", raw=raw)
        relabel = "。誤答の種類を付け直した" if updates else ""
        return VerifyResult(True, f"{model}: 正解は 1 つに決まる（{len(checks)} つの確認）{relabel}", detail, raw=raw,
                            updates=updates or {})

    def _judge_without_answer(self, llm: LLM, model: str, system: str, sentence: str,
                              choices: list[dict[str, Any]], checks: list[tuple[str, bool | None, str]],
                              raws: list[str]) -> None:
        """正解を抜いた選択肢と「どれも合わない」から選ばせる（2-2。#911 の「探す」のような 2 つ目の正解を拾う）。
        読めなければ形を示して 1 回だけやり直す（第 7 回のレビュー A）。"""
        wrong = [c["text"] for c in choices if not c.get("correct")]
        options = wrong + [prompts.NONE_FIT]
        user = prompts.none_fit_user(sentence, options)
        for attempt in range(2):
            again = ("\n\n前の出力は形が違って読めませんでした。[[answer]] の下に番号 1 つ（または「なし」）だけを書いてください。"
                     if attempt else "")
            out, raw = self._ask(llm, model, system, user + again, prompts.NONE_FIT_SCHEMA)
            raws.append("【正解を抜いた判定】\n" + raw)
            k = read_none_fit(str(out.get("answer") or ""), options)
            if k is not None:
                reason = str(out.get("reason") or "").strip()
                if k == len(options) - 1:
                    checks.append(("正解を抜いた判定", True, "ほかの選択肢はどれも合わない"))
                else:
                    checks.append(("正解を抜いた判定", False, f"正解を抜くと「{options[k]}」を選んだ（正解が 2 つの疑い）"
                                   + (f"：{reason[:100]}" if reason else "")))
                return
        checks.append(("正解を抜いた判定", None, "判定を読めなかった"))

    def _verify_spelling(self, llm: LLM, model: str, problem: Any, three: bool = True) -> VerifyResult:
        """スペル（2-3）：最初の文字・文字数・意味に合う語をすべて挙げさせる。正解が挙がらなければ不一致、
        正解のほかに挙がった語が例文に自然に入れば不合格（thick と tough）。最初の文字・文字数に合わない語や、
        正解の語形違い（rose と rise）だけのときは検証役の誤りとして聞き直す（2 回まで。05 A-5）。"""
        expected = problem.answer.get("word", "").lower()
        pattern = f"「{expected[:1]}」で始まる {len(expected)} 文字の語"
        user = prompts.spell_list_user(problem.question, pattern)
        raws: list[str] = []
        tried: list[str] = []
        fit: list[str] = []
        for attempt in range(3):
            note = (f"\n前に挙げた「{'」「'.join(tried[-4:])}」は、最初の文字・文字数・語形のどれかが合っていません。"
                    if tried else "")
            out, raw = self._ask(llm, model, prompts.SPELL_LIST_SYSTEM, user + note, prompts.SPELL_LIST_SCHEMA)
            raws.append(raw)
            if not out:
                return VerifyResult(False, f"{model}: 出力を読めません", "", "llm_output", raw=raw[:4000])
            words = [normalize_word(w) for w in re.split(r"[,\s、，/／;]+", str(out.get("words") or ""))]
            words = [w for w in dict.fromkeys(words) if re.fullmatch(r"[a-z][a-z'-]*", w)]
            fit = [w for w in words if w[:1] == expected[:1] and len(w) == len(expected)]
            if expected in fit:
                break
            if fit and not any(check._stem(w) == check._stem(expected) for w in fit):
                return VerifyResult(False, f"{model}: {', '.join(fit)}（正解 {expected} が挙がらない）",
                                    f"挙げた語：{', '.join(words)}", "solver_mismatch", raw="\n\n".join(raws)[:4000])
            tried += words or ["（なし）"]
        else:
            return VerifyResult(False, f"{model}: 最初の文字・文字数に合わない答えが続いた（{'」「'.join(tried[:6])}）",
                                "", "solver_unreadable", raw="\n\n".join(raws)[:4000])
        others = [w for w in fit if w != expected]
        note = f"（合わない語「{'」「'.join(tried[:4])}」は聞き直した）" if tried else ""
        if not others:
            return VerifyResult(True, f"{model}: 条件に合う語は {expected} だけ{note}", f"挙げた語：{', '.join(fit)}",
                                raw="\n\n".join(raws)[:4000])
        sentence = re.sub(SPELL_BLANK, "{}", problem.question.split("\n\n")[-1])
        cands = [{"text": w, "correct": False} for w in others]
        checks: list[tuple[str, bool | None, str]] = []
        # スペルは、意味と最初の文字・文字数に合う別の語が例文に入れば、不自然でも答えが 2 つになる
        # （#1006 の method と manner。第 7 回のレビュー H）。3 段階なら「入らない」以外は不合格
        self._judge_fit(llm, model, prompts.FIT3_JUDGE_SYSTEM if three else prompts.FIT_JUDGE_SYSTEM,
                        prompts.fit_judge_user([sentence.replace("{}", w) for w in others]),
                        cands, "ほかの語を入れた例文", checks, raws, has_answer=False, three=three, strict_others=True)
        res = self._verdict_of(model, checks, raws)
        if res.ok:
            res.summary = f"{model}: {expected} のほかに挙がった {', '.join(others)} は例文に入らない{note}"
        return res

    def review(self, llm: LLM, model: str, problem: Any) -> VerifyResult | None:
        """見直し：解説・ヒント・訳と、例文・本文の筋（第 6 回のレビュー 4-3）。正解が 1 つに決まるかは検証で
        選択肢ごとに判定するので、見直しでは聞かない。読めないときは 1 回やり直し、それでも読めなければ不合格（05 A-2）。
        """
        checks: list[tuple[str, bool | None, str]] = []  # (確かめたこと, 結果（None は読めなかった）, 内容)
        raws: list[str] = []
        if problem.template in VOCABS:
            self._judge_coherence(llm, model, problem, checks, raws)
        self._judge_explanation(llm, model, problem, checks, raws)
        failed = [(name, text) for name, ok, text in checks if ok is False]
        lines = [f"{'○' if ok else '×' if ok is False else '？'} {name}：{text}" for name, ok, text in checks]
        detail = "\n".join(lines)
        raw = "\n\n".join(raws)[:4000]
        if failed:
            summary = "／".join(f"{name}（{text[:150]}）" for name, text in failed)
            return VerifyResult(False, f"{model}: 誤りを指摘：{summary[:400]}", detail, "en_review", raw=raw)
        unread = [name for name, ok, _t in checks if ok is None]
        if unread:
            return VerifyResult(False, f"{model}: 見直しの結果を読めなかった（{'・'.join(unread)}）", detail,
                                "en_review_unread", raw=raw)
        return VerifyResult(True, f"{model}: 誤りは見つからなかった", detail, raw=raw)

    def rereview(self, llm: LLM, model: str, problem: Any,
                 options: dict[str, str] | None = None) -> VerifyResult | None:
        """見直しのやり直し：前に作った問題は、選択肢ごとの判定（今の検証。1 回）と解説の見直しの両方で点検し直す
        （正解が 1 つに決まるかは第 6 回のレビューで検証に移したので、見直しだけでは拾えないため）。"""
        parts = [("検証の判定", self.verify(llm, None, model, problem, options=options)),
                 ("解説の見直し", self.review(llm, model, problem))]
        bad = [(name, r) for name, r in parts if r is not None and not r.ok]
        detail = "\n".join(f"【{name}】\n{r.detail}" for name, r in parts if r is not None and r.detail)
        raw = "\n\n".join(r.raw for _n, r in parts if r is not None and r.raw)[:4000]
        updates = parts[0][1].updates if parts[0][1] is not None else {}
        if bad:
            summary = "／".join(r.summary for _n, r in bad)
            return VerifyResult(False, summary[:500], detail, bad[0][1].code, raw=raw, updates=updates)
        return VerifyResult(True, "／".join(r.summary for _n, r in parts if r is not None)[:500], detail, raw=raw,
                            updates=updates)

    @staticmethod
    def _ask(llm: LLM, model: str, system: str, user: str, schema: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """見直し役を呼ぶ（読めなければ 1 回だけ呼び直す）。(出力, 生の出力)。読めなければ ({}, 理由)。"""
        from ...llm import LLMError

        last = ""
        for _ in range(2):
            try:
                out = llm.chat_json(model, system, user, schema, temperature=0.2)
                return out, "\n".join(f"[[{k}]]\n{v}" for k, v in out.items())
            except LLMError as exc:
                last = f"読めない出力（{exc}）\n{getattr(exc, 'raw', '')}"
        return {}, last

    @staticmethod
    def _judgments(text: str, labels: dict[str, Any], n: int) -> dict[int, tuple[str, str]]:
        """「番号: 判定 — 理由」の行を読む。{番号（0 から）: (判定, 理由)}。長いラベルから先に照らす。"""
        names = sorted(labels, key=len, reverse=True)
        out: dict[int, tuple[str, str]] = {}
        for line in (text or "").splitlines():
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

    def _judge_choices(self, llm: LLM, model: str, problem: Any, choices: list[dict[str, Any]],
                       checks: list[tuple[str, bool | None, str]], raws: list[str], updates: dict[str, Any],
                       quote: bool = False, strict: bool = False) -> None:
        """読解の選択肢ごとの判定。quote=True（検証）は、判定に使った本文の文を引用させ、本文にそのまま
        入っているかを照らす（本文にない引用の判定は無効にしてやり直させる。第 6 回のレビュー 2-1）。"""
        passage = problem.passage.body if problem.passage is not None else ""
        user = prompts.choice_judge_user(passage, problem.question, [c["text"] for c in choices])
        system = (prompts.CHOICE_STRICT_SYSTEM if strict else prompts.CHOICE_VERIFY_SYSTEM) if quote \
            else prompts.CHOICE_JUDGE_SYSTEM
        got: dict[int, tuple[str, str]] = {}
        bad: list[str] = []
        asked_again = False  # 作り直した D の根拠の書き方が足りないときの聞き直し（1 回だけ）
        right_i = next((i for i, c in enumerate(choices) if c.get("correct")), -1)
        note = ""
        for _ in range(3 if quote else 2):  # 読めない・引用が本文にないときはやり直す（A-2）
            out, raw = self._ask(llm, model, system, user + note, prompts.JUDGMENTS_SCHEMA)
            raws.append("【選択肢の判定】\n" + raw)
            got = self._judgments(str(out.get("judgments") or ""), prompts.CHOICE_LABELS, len(choices))
            bad = [q for lb, reason in got.values() for q in [_quote(reason)]
                   if quote and lb != "本文に書かれていない" and (q and not check.contains(passage, q)
                                                                  or not q and lb == "裏付けあり")]
            short = _strict_missing(got, right_i, choices) if strict and len(got) >= len(choices) else []
            if len(got) >= len(choices) and not bad and (not short or asked_again):
                break
            if bad:
                note = (f"\n\n前の判定の引用「{'」「'.join(q[:60] for q in bad[:2])}」は本文にありません。"
                        "本文からそのまま引用してください。")
            elif short:
                asked_again = True
                note = ("\n\n前の判定は形が足りませんでした。「本文と食い違う」には「引用: \"本文の文\"」を、"
                        "ほかの誤答には「違う点: 」のあとに、選択肢のどこが本文と違うか（人・理由・時期など）を 1 行で書いてください。")
            else:
                note = ""
        if len(got) < len(choices):
            checks.append(("選択肢の判定", None, "判定を読めなかった"))
            return
        if bad:
            checks.append(("選択肢の判定", None, f"本文にない文を引用した・引用のない判定が残った（{bad[0][:60] or '引用なし'}）"))
            return
        right = next(i for i, c in enumerate(choices) if c.get("correct"))
        label, reason = got[right]
        checks.append(("正解の裏付け", label == "裏付けあり",
                       f"正解「{choices[right]['text'][:60]}」は{label}" + (f"（{reason[:100]}）" if reason else "")))
        also = [i for i, (lb, _r) in got.items() if i != right and lb == "裏付けあり"]
        checks.append(("正解が 1 つか", not also,
                       "ほかに裏付けのある選択肢はない" if not also
                       else "「" + "」「".join(choices[i]["text"][:60] for i in also) + "」も本文で裏付けられる（正解が複数）"))
        if strict:
            # 誤答ごとの根拠（作り直した D。第 8 回のレビュー 2-2）：書き方が足りない誤答は 1 回聞き直したうえで記録に
            # 残すだけ（不合格・要確認にしない。言葉の照合では誤答の正しさを決められないため、機械的な照合はやめた）
            short = _strict_missing(got, right, choices)
            checks.append(("誤答の根拠", True, "どの誤答にも根拠がある" if not short
                           else "根拠の書き方が足りない誤答：" + "／".join(short)))
        # 誤答の種類：見直し役の判定はモデルごとにばらつく（第 5 回のレビュー C-1）ので、判定が違うだけでは付け直さない。
        # 付け直すのは機械的な確認が見直し役を支持するときだけ（作成役の「本文に書かれていない」なのに中心の語が本文にある）。
        # それ以外の食い違いは記録に残し（label_doubt）、書き出しに出す（C-3）
        fixed, doubts, new_choices = [], [], []
        for i, c in enumerate(choices):
            code = prompts.CHOICE_LABELS.get(got[i][0], "")
            mine = c.get("mistake")
            if c.get("correct") or not code or code == mine:
                new_choices.append({k: v for k, v in c.items() if k != "label_doubt"})
                continue
            stated = check.stated_in_passage(c["text"], passage)
            if mine == "not_stated" and stated and code != "not_stated":
                fixed.append(f"「{c['text'][:40]}」{MISTAKES.get(str(mine), mine)}→{MISTAKES[code]}")
                c = {**c, "mistake": code}
                c.pop("label_doubt", None)
            else:
                doubts.append(f"「{c['text'][:40]}」作成役 {MISTAKES.get(str(mine), mine)}／見直し {MISTAKES[code]}")
                c = {**c, "label_doubt": code}
            new_choices.append(c)
        if fixed or doubts:
            updates["choices"] = new_choices
            text = "／".join(x for x in ("付け直した：" + "、".join(fixed) if fixed else "",
                                         "判定が分かれた（そのまま）：" + "、".join(doubts) if doubts else "") if x)
            checks.append(("誤答の種類", True, text))

    def _judge_fit(self, llm: LLM, model: str, system: str, user: str, choices: list[dict[str, Any]], what: str,
                   checks: list[tuple[str, bool | None, str]], raws: list[str], has_answer: bool = True,
                   three: bool = False, strict_others: bool = False) -> None:
        """選択肢ごとに文で通るかを判定させる。has_answer=False（スペルのほかの語）は、どれも入らないことだけを見る。

        three=True（第 7 回のレビュー C）：「自然に入る／文法的には入るが不自然／入らない」。不合格にするのは、正解が
        「入らない」・正解以外に「自然に入る」が付いたときだけ（「不自然」は記録に残す）。strict_others=True（スペル）は
        正解以外の「不自然」も不合格。"""
        labels = prompts.FIT3_LABELS if three else prompts.FIT_LABELS
        got: dict[int, tuple[str, str]] = {}
        for _ in range(2):  # 判定が読めなかったら 1 回だけやり直す（A-2）
            out, raw = self._ask(llm, model, system, user, prompts.JUDGMENTS_SCHEMA)
            raws.append(f"【{what}の判定】\n" + raw)
            got = self._judgments(str(out.get("judgments") or ""), labels, len(choices))
            if len(got) >= len(choices):
                break
        if len(got) < len(choices):
            checks.append((f"{what}の判定", None, "判定を読めなかった"))
            return
        # 判定を natural / awkward / no にそろえる（2 段階の「通る」は natural、「通らない」は no）
        cat = {i: ("natural" if lb in ("通る", "自然に入る") else "awkward" if "不自然" in lb else "no")
               for i, (lb, _r) in got.items()}
        bad_other = ("natural", "awkward") if strict_others else ("natural",)
        awkward = [i for i in cat if cat[i] == "awkward" and choices[i].get("correct") is not True]
        note = ("（文法的には入るが不自然：「" + "」「".join(choices[i]["text"] for i in awkward) + "」）"
                if awkward and not strict_others else "")
        if not has_answer:
            also = [i for i in cat if cat[i] in bad_other]
            checks.append(("正解が 1 つか", not also, "ほかの語は入らない" if not also
                           else "「" + "」「".join(choices[i]["text"] for i in also) + "」も入る（正解が複数）"))
            return
        right = next(i for i, c in enumerate(choices) if c.get("correct"))
        ok_right = cat[right] != "no" if three else cat[right] == "natural"
        checks.append(("正解が合うか", ok_right,
                       f"正解「{choices[right]['text']}」は{got[right][0]}" + (f"（{got[right][1][:100]}）" if got[right][1] else "")))
        also = [i for i in cat if i != right and cat[i] in bad_other]
        checks.append(("正解が 1 つか", not also,
                       ("ほかの選択肢は合わない" + note) if not also
                       else "「" + "」「".join(choices[i]["text"] for i in also) + "」も合う（正解が複数）"))

    def _judge_coherence(self, llm: LLM, model: str, problem: Any,
                         checks: list[tuple[str, bool | None, str]], raws: list[str]) -> None:
        """語彙・スペルの例文の筋だけを確かめる（第 8 回のレビュー 3-1）。答えを入れた例文を「自然／不自然」の 2 択で。
        読めなければ 1 回だけやり直し、それでも読めなければ「読めなかった」（見直しの不合格）。"""
        sentence = example_sentence(problem)
        if not sentence:
            return
        for _ in range(2):
            out, raw = self._ask(llm, model, prompts.COHERENCE_SYSTEM, prompts.coherence_user(sentence),
                                 prompts.COHERENCE_SCHEMA)
            raws.append("【例文の筋】\n" + raw)
            verdict = re.sub(r"[\s*「」:：。]", "", str(out.get("verdict") or "").splitlines()[0] if out.get("verdict") else "")
            reason = str(out.get("reason") or "").strip()
            if verdict.startswith("不自然"):
                checks.append(("例文の筋", False, reason[:200] or "前半と後半がつながらない"))
                return
            if verdict.startswith("自然"):
                checks.append(("例文の筋", True, "前半と後半が自然につながる"))
                return
        checks.append(("例文の筋", None, "結果を読めなかった"))

    def _judge_explanation(self, llm: LLM, model: str, problem: Any,
                           checks: list[tuple[str, bool | None, str]], raws: list[str]) -> None:
        passage = problem.passage.body if problem.passage is not None else ""
        user = prompts.review_user(problem.question, [(c["text"], bool(c.get("correct"))) for c in problem.choices],
                                   problem.answer_display, problem.explanation, problem.hint, passage)
        verdict, issues = "", ""
        for _ in range(2):  # 結果が読めなかったら 1 回だけやり直す（A-2）
            out, raw = self._ask(llm, model, prompts.REVIEW_SYSTEM, user, prompts.REVIEW_SCHEMA)
            raws.append("【解説の点検】\n" + raw)
            verdict, issues = _verdict(out)
            if verdict.startswith(("問題あり", "問題なし")):
                break
        if verdict.startswith("問題あり"):
            checks.append(("解説・例文", False, issues[:300] or "理由なし"))
        elif verdict.startswith("問題なし"):
            checks.append(("解説・例文", True, "誤りは見つからなかった"))
        else:
            checks.append(("解説・例文", None, f"結果を読めなかった（{verdict[:60]}）"))

    # --- 問題検証モード -----------------------------------------------------

    def text_issues(self, problem: Any) -> list[str]:
        out = []
        if problem.template in READINGS:
            out += check.english_issues(problem.question, "設問")
        out += check.japanese_issues(problem.hint, "ヒント") + check.japanese_issues(problem.explanation, "解説")
        if problem.format == "choice":
            texts = [c.get("text", "") for c in problem.choices]
            correct = next((i for i, c in enumerate(problem.choices) if c.get("correct")), 0)
            out += check.choice_issues(texts, correct, english=problem.template != "en-vocab-meaning")
            if check.letter_reference(problem.explanation) or check.letter_reference(problem.hint):
                out.append("解説・ヒントが選択肢を記号で呼んでいる")
        if problem.template in VOCABS:
            allowed = [problem.question, *[c.get("text", "") for c in problem.choices],
                       str(problem.params.get("word", "")), *problem.params.get("others", [])]
            body = problem.explanation.split("\n\n答え:")[0]
            stray = check.stray_english(body, allowed)
            if stray:
                out.append(f"解説に、例文にも選択肢にもない英単語（{', '.join(stray[:4])}）がある")
        if problem.template in READINGS:
            if check.article_typo(problem.hint) or check.article_typo(problem.explanation):
                out.append("ヒント・解説で段落を「第N条」と書いている")
            if check.main_vs_one(problem.question, str(problem.params.get("evidence", ""))):
                out.append("設問は main reason などを問うのに、根拠の文は理由の 1 つにすぎない")
        # 誤答の種類の付け方の食い違い（「本文に書かれていない」なのに中心の語が本文にある）は、致命的な欠陥では
        # ないので点検に入れない（見本集の通すべき問題 4 問が引っかかった。作成のときの直しと検証の付け直しで扱う）
        if problem.template == "en-vocab-meaning" and (check.blank_words(problem.hint)
                                                         or check.blank_words(problem.explanation)):
            out.append("解説・ヒントに「空所」「入る」などの言葉がある（この問題の例文に空所はない）")
        if problem.passage is not None:
            out += check.english_issues(problem.passage.body, "本文")
        return list(dict.fromkeys(out))

    def normalize_text(self, text: str) -> str:
        return check.normalize(text)

    # --- 採点 ---------------------------------------------------------------

    def grade(self, sandbox: Sandbox | None, problem: Any, response: str) -> Grade:
        if problem.format == "choice":
            return super().grade(sandbox, problem, response)
        word = normalize_word(response)
        if not word:
            return Grade(False, retry_message="答えを入力してください")
        if not re.fullmatch(r"[a-z][a-z'-]*", word):
            return Grade(False, retry_message="英単語を 1 語、半角の英字で入力してください")
        return Grade(word == str(problem.answer.get("word", "")).lower(), response_display=word)


def _tamper(choices: list[dict[str, Any]], rng: random.Random) -> None:
    """検証のテスト用に、正解の印を誤答の 1 つに移す（検証役が本当の正解を選べば不一致で弾かれる）。"""
    right = next(i for i, c in enumerate(choices) if c["correct"])
    wrong = rng.choice([i for i in range(len(choices)) if i != right])
    choices[right] = {**choices[right], "correct": False, "mistake": "not_stated"}
    choices[wrong] = {**choices[wrong], "correct": True, "mistake": None}
