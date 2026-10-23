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


def _chat(ctx: GenContext, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
    """作成役を呼ぶ。出力が読めなかったときは、作り直しの回数を使わずに 1 回だけ呼び直す。"""
    from ...llm import LLMError

    try:
        return ctx.llm.chat_json(ctx.gen_model, system, user, schema, temperature=0.8)
    except LLMError:
        return ctx.llm.chat_json(ctx.gen_model, system, user, schema, temperature=0.8)


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
    evidence = fields.get("evidence", "").strip().strip("\"'“”‘’「」")
    return {"question": fields.get("question", ""), "options": [fields.get(c, "") for c in LETTERS],
            "answer": LETTERS.index(answer.group()) if answer else -1, "wrong": wrong, "evidence": evidence,
            "hint": fields.get("hint", ""), "explanation": fields.get("explanation", "")}


def _question_issues(q: dict[str, Any], passage: str) -> list[str]:
    out = check.english_issues(q["question"], "設問")
    if q["answer"] < 0:
        return out + ["正解の記号（Answer: A〜D）がない"]
    out += check.choice_issues(q["options"], q["answer"])
    wrong = [c for i, c in enumerate(LETTERS) if i != q["answer"]]
    if any(q["wrong"].get(c) not in READING_MISTAKES for c in wrong):
        out.append("誤答の種類（Wrong: B=not_stated のような行）が足りない・種類の記号が違う")
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
    return out


# --- 語彙の誤答 -------------------------------------------------------------------

def _kanji(meaning: str) -> set[str]:
    """意味の重なりを見るための字（漢字・カタカナの 2 字の並び）。「提案」と「提案する」などを同じとみなす。"""
    core = re.sub(r"[぀-ゟ〜]", " ", meaning)
    out = set()
    for part in core.split():
        out |= {part[i:i + 2] for i in range(len(part) - 1)} or {part}
    return out


def pick_distractors(word: Word, rng: random.Random, n: int = 3) -> list[Word]:
    """同じ品詞・同じレベルで、意味が重ならない語を n 個（意味が近い語は 2 つとも正解になりうるので避ける）。"""
    pool = [w for w in WORDS if w.pos == word.pos and w.level == word.level and w.word != word.word]
    rng.shuffle(pool)
    chosen: list[Word] = []
    seen = _kanji(word.meaning)
    for w in pool:
        k = _kanji(w.meaning)
        if k & seen or w.meaning == word.meaning or any(w.word == c.word for c in chosen):
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


class EnglishSubject(Subject):
    id = "english"
    name = "英語"
    formats = ("choice", "input")
    mistakes = MISTAKES
    math_text = False  # 「$20」のような値段を数式として表示しない
    reject_codes = {
        "en_text": "英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）",
        "en_no_passage": "本文を読まなくても解けた（常識で答えられる設問）",
        "en_review": "見直しで誤りを指摘された（正解が 1 つに決まらない・英文の誤り・解説の誤り）",
    }
    test_format = "choice"  # 検証のテスト：正解の印を誤答に移した問題を流す

    def units(self) -> list[Unit]:
        return _units()

    # --- 作成 ---------------------------------------------------------------

    def generate(self, ctx: GenContext, template: str, fmt: str) -> Draft | list[Draft]:
        if template in READINGS:
            return self._reading(ctx, template)
        return self._vocab(ctx, template, fmt)

    def _reading(self, ctx: GenContext, template: str) -> list[Draft]:
        spec = READINGS[template]
        n = spec.questions
        existing = ctx.existing_keys(template)

        def keys(topic: str) -> list[str]:
            return [_params_key(template, {"topic": topic, "q": i}) for i in range(1, n + 1)]

        topics = [t for t in TOPICS[template] if not set(keys(t)) & existing]
        if not topics:
            raise Rejected("duplicate", "この小単元の題材をすべて使いました（catalog.TOPICS に題材を足すと作れます）")
        topic = ctx.rng.choice(topics)
        params = {"topic": topic, "seed": ctx.rng.randrange(1 << 30), "test": ctx.is_test}
        partial: dict[str, Any] = {"unit": "en-reading", "template": template, "format": "choice", "params": params}
        schema = prompts.reading_schema(n)
        user = prompts.reading_user(spec.genre, topic, spec.words, n, spec.question_style)
        log: list[dict[str, Any]] = [{
            "step": "題材", "ok": True, "summary": f"題材「{topic}」（{spec.words[0]}〜{spec.words[1]} 語・設問 {n} 問）"
            + ("。検証のテストのため、正解の印を誤答に移します" if ctx.is_test else ""),
            "detail": "作成役に送った文章:\n" + user + "\n\n（システムの指示）\n" + prompts.READING_SYSTEM,
        }]
        out = _chat(ctx, prompts.READING_SYSTEM, user, schema)
        for round_no in range(FIX_ROUNDS + 1):
            title = check.normalize(str(out.get("title") or ""))
            passage = check.normalize(str(out.get("passage") or ""))
            qs = [parse_question(check.normalize(str(out.get(f"q{i}") or ""))) for i in range(1, n + 1)]
            partial.update(question=qs[0]["question"] if qs else "", explanation=passage)
            issues = self._passage_issues(spec, title, passage)
            q_issues = [_question_issues(q, passage) for q in qs]
            all_issues = issues + [f"設問 {i}: {x}" for i, xs in enumerate(q_issues, 1) for x in xs]
            if not all_issues:
                break
            summary = "／".join(dict.fromkeys(all_issues))
            last = round_no == FIX_ROUNDS
            log.append({"step": "英文の点検", "ok": False, "summary": summary + ("" if last else "（直しを依頼します）")})
            if last:
                if issues:  # 本文がだめなら、設問もすべて使えない
                    raise Rejected("en_text", summary, log, partial)
                break
            _set_label(ctx.llm, "直し")
            out = _chat(ctx, prompts.READING_SYSTEM, prompts.fix_user(user, out, summary, list(schema["properties"])),
                        schema)

        body = f"{title}\n\n{passage}" if title else passage
        source = {"title": title, "body": body, "source_title": f"{ctx.gen_model} が書いた英文", "source_url": ""}
        drafts = []
        for i, (q, problems) in enumerate(zip(qs, q_issues), 1):
            qlog = list(log)
            draft_partial = {**partial, "question": q["question"], "explanation": q["explanation"], "hint": q["hint"]}
            if problems:
                summary = "設問の点検で落ちた: " + "／".join(dict.fromkeys(problems))
                drafts.append(self._rejected_draft(template, "en_text", summary, qlog, draft_partial))
                continue
            qlog.append({"step": "英文の点検", "ok": True,
                         "summary": f"根拠の文が本文にある・選択肢 4 つが重複しない・英文に日本語がない（本文 {check.word_count(passage)} 語）"})
            choices = [{"text": t, "correct": j == q["answer"], "mistake": None if j == q["answer"]
                        else q["wrong"].get(LETTERS[j])} for j, t in enumerate(q["options"])]
            if not ctx.is_test:
                blind = self._blind_check(ctx, q["question"], q["options"], q["answer"])
                qlog.append(blind[1])
                if not blind[0]:
                    drafts.append(self._rejected_draft(template, "en_no_passage", blind[1]["summary"], qlog,
                                                       {**draft_partial, "choices": choices}))
                    continue
            ctx.rng.shuffle(choices)
            if ctx.is_test:
                _tamper(choices, ctx.rng)
            correct = next(c for c in choices if c["correct"])
            explanation = q["explanation"].strip() + f"\n\n本文の根拠: “{q['evidence']}”"
            drafts.append(Draft(
                unit="en-reading", template=template, format="choice", question=q["question"],
                answer={"kind": "choice"}, answer_display=correct["text"], explanation=explanation, hint=q["hint"],
                choices=choices, source=f"{ctx.gen_model} が書いた英文（題材「{topic}」）。正解の根拠の文は本文と照合済み",
                params={**params, "q": i}, params_key=keys(topic)[i - 1], log=qlog, passage=source,
            ))
        return drafts

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
                        partial: dict[str, Any]) -> Draft:
        """同じ本文のほかの設問は検証に進めるので、この設問だけを不合格として返す（worker が残す）。"""
        log = log + [{"step": "設問", "ok": False, "summary": summary}]
        return Draft(unit="en-reading", template=template, format="choice", question=partial.get("question", ""),
                     answer={}, answer_display="", explanation=partial.get("explanation", ""),
                     hint=partial.get("hint", ""), log=log, rejected=Rejected(code, summary, log, partial))

    def _blind_check(self, ctx: GenContext, question: str, options: list[str], answer: int) -> tuple[bool, dict[str, Any]]:
        """作成役に本文を見せずに設問だけを解かせる。正解できたら、本文を読まなくても解ける設問とみなす。"""
        from ...llm import LLMError

        _set_label(ctx.llm, "本文なしの確認")
        user = prompts.solve_user(question, options)
        try:
            out = ctx.llm.chat_json(ctx.gen_model, prompts.BLIND_SYSTEM, user, prompts.BLIND_SCHEMA, temperature=0.2)
        except LLMError as exc:  # 確かめられないときは通す（検証役と見直しで拾う）
            return True, {"step": "本文なしの確認", "ok": True, "summary": f"確かめられなかった（{exc}）",
                          "model": ctx.gen_model}
        raw = str(out.get("answer") or "")
        got = read_letter(raw, "", len(options))
        entry = {"step": "本文なしの確認", "model": ctx.gen_model, "raw": raw[:500]}
        if got == answer:
            return False, {**entry, "ok": False,
                           "summary": "本文を見せずに解かせたら正解した（常識で答えられる設問なので使わない）"}
        shown = "X（本文がないと分からない）" if got is None else f"{LETTERS[got]}（不正解）"
        return True, {**entry, "ok": True, "summary": f"本文を見せずに解かせた答えは {shown}"}

    def _vocab(self, ctx: GenContext, template: str, fmt: str) -> Draft:
        spec = VOCABS[template]
        existing = ctx.existing_keys(template)
        pool = [w for w in WORDS if w.level == spec.word_level
                and (fmt != "input" or (w.word not in SPELLING_EXCLUDE and len(w.word) >= 4))]
        pool = [w for w in pool if _params_key(template, {"word": w.word, "pos": w.pos}) not in existing]
        if not pool:
            raise Rejected("duplicate", "単語リストの語をすべて使いました（words.py に語を足すと作れます）")
        word = ctx.rng.choice(pool)
        others = pick_distractors(word, ctx.rng)
        if len(others) < 3:
            raise Rejected("choices", f"「{word.word}」と意味が重ならない誤答の語を 3 つ選べません")
        params = {"word": word.word, "pos": word.pos, "others": [w.word for w in others],
                  "seed": ctx.rng.randrange(1 << 30), "test": ctx.is_test}
        key = _params_key(template, {"word": word.word, "pos": word.pos})
        user = prompts.vocab_user(word.word, POS[word.pos], word.meaning,
                                  [w.word for w in others] if template == "en-vocab-blank" else [])
        log: list[dict[str, Any]] = [{
            "step": "単語", "ok": True,
            "summary": f"単語リストから「{word.word}」（{POS[word.pos]}・{word.meaning}）を選んだ"
                       + (f"。誤答の語: {', '.join(w.word for w in others)}" if fmt == "choice" else "")
                       + ("。検証のテストのため、正解の印を誤答に移します" if ctx.is_test else ""),
            "detail": "作成役に送った文章:\n" + user + "\n\n（システムの指示）\n" + prompts.VOCAB_SYSTEM,
        }]
        partial: dict[str, Any] = {"unit": "en-vocab", "template": template, "format": fmt, "params": params}
        schema = prompts.VOCAB_SCHEMA
        out = _chat(ctx, prompts.VOCAB_SYSTEM, user, schema)
        for round_no in range(FIX_ROUNDS + 1):
            fields = {k: check.normalize(str(out.get(k) or "")) for k in schema["properties"]}
            partial.update(question=fields["sentence"], explanation=fields["explanation"], hint=fields["hint"])
            issues = self._vocab_issues(word, others if template == "en-vocab-blank" else [], fields)
            if not issues:
                break
            summary = "／".join(dict.fromkeys(issues))
            last = round_no == FIX_ROUNDS
            log.append({"step": "例文の点検", "ok": False, "summary": summary + ("" if last else "（直しを依頼します）")})
            if last:
                raise Rejected("en_text", summary, log, partial)
            _set_label(ctx.llm, "直し")
            out = _chat(ctx, prompts.VOCAB_SYSTEM, prompts.fix_user(user, out, summary, list(schema["properties"])),
                        schema)
        log.append({"step": "例文の点検", "ok": True,
                    "summary": f"例文に「{word.word}」がちょうど 1 回、ほかの選択肢の語は入っていない、訳・解説は日本語"})
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
            explanation += f"\n\n答え: {word.word}（{word.meaning}）"
        elif template == "en-vocab-meaning":
            question = ("次の英文の [ ] で示した語の意味として最も適切なものを選べ。\n\n"
                        + _blank(sentence, word.word, f"[ {word.word} ]"))
            choices = [{"text": word.meaning, "correct": True, "mistake": None}]
            choices += [{"text": w.meaning, "correct": False, "mistake": "other_meaning"} for w in others]
            answer_display = word.meaning
            explanation += f"\n\n答え: {word.word} ＝「{word.meaning}」"
        else:
            question = (f"日本語の意味に合う英単語を書け（{len(word.word)} 文字。最初の文字は示してある）。\n\n"
                        f"意味: {word.meaning}（{POS[word.pos]}）\n\n"
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
    def _vocab_issues(word: Word, others: list[Word], fields: dict[str, str]) -> list[str]:
        sentence = fields["sentence"]
        out = check.english_issues(sentence, "例文")
        if "\n" in sentence.strip():
            out.append("例文が 1 文になっていない（改行がある）")
        n_words = check.word_count(sentence)
        if not 6 <= n_words <= 30:
            out.append(f"例文の長さが {n_words} 語（8〜20 語にする）")
        times = check.count_word(sentence, word.word)
        if times != 1:
            out.append(f"例文に「{word.word}」がそのままの形で {times} 回（ちょうど 1 回、語尾を変えずに使う）")
        used = [w.word for w in others if check.count_word(sentence, w.word)]
        if used:
            out.append("例文に入らない語（" + ", ".join(used) + "）が使われている")
        if re.search(rf"(?<![A-Za-z])an?\s+{re.escape(word.word)}(?![A-Za-z])", sentence, re.I):
            out.append(f"「{word.word}」の直前に a / an がある（答えの手がかりになる）")
        out += check.japanese_issues(fields["translation"], "和訳")
        out += check.japanese_issues(fields["hint"], "ヒント")
        out += check.japanese_issues(fields["explanation"], "解説", min_len=20)
        if check.count_word(fields["hint"], word.word):
            out.append("ヒントに答えの単語が書かれている")
        return out

    # --- 検証 ---------------------------------------------------------------

    def verify(self, llm: LLM, sandbox: Sandbox, model: str, problem: Any) -> VerifyResult:
        from ...llm import LLMError

        passage = problem.passage.body if problem.passage is not None else ""
        choices = list(problem.choices)
        random.shuffle(choices)  # 毎回並べ替える（記号の位置で当たらないように）
        user = prompts.solve_user(problem.question, [c["text"] for c in choices], passage)
        out: dict[str, Any] = {}
        for attempt in range(2):  # 出力が読めなかったときは 1 回だけ解かせ直す
            try:
                out = llm.chat_json(model, prompts.SOLVE_SYSTEM, user, prompts.SOLVE_SCHEMA, temperature=0.6)
                break
            except LLMError as exc:
                if attempt == 1:
                    return VerifyResult(False, f"{model}: 出力を読めません（{exc}）", "", "llm_output",
                                        raw=getattr(exc, "raw", ""))
        raw = json.dumps(out, ensure_ascii=False)
        work = str(out.get("work") or "").strip()
        given = str(out.get("answer") or "").strip()
        if choices:
            got = read_letter(given, work, len(choices))
            if got is None:  # 記号でなく選択肢の文をそのまま書いたとき
                got = next((i for i, c in enumerate(choices) if check.flat(c["text"]) == check.flat(given)
                            and check.flat(given)), None)
            if got is None:
                return VerifyResult(False, f"{model}: 答え「{given[:60] or '（空）'}」の記号を読み取れません", work,
                                    "solver_unreadable", raw=raw)
            picked = choices[got]
            if picked.get("correct"):
                return VerifyResult(True, f"{model}: 「{picked['text'][:80]}」（一致）", work, raw=raw)
            right = next((c["text"] for c in choices if c.get("correct")), "")
            return VerifyResult(False, f"{model}: 「{picked['text'][:80]}」（正解「{right[:80]}」と不一致）", work,
                                "solver_mismatch", raw=raw)
        word = normalize_word(given.splitlines()[0] if given else "")
        if not re.fullmatch(r"[a-z][a-z'-]*", word):
            return VerifyResult(False, f"{model}: 答え「{given[:60] or '（空）'}」を英単語として読み取れません", work,
                                "solver_unreadable", raw=raw)
        expected = problem.answer.get("word", "")
        if word == expected.lower():
            return VerifyResult(True, f"{model}: {word}（一致）", work, raw=raw)
        return VerifyResult(False, f"{model}: {word}（正解 {expected} と不一致）", work, "solver_mismatch", raw=raw)

    def review(self, llm: LLM, model: str, problem: Any) -> VerifyResult | None:
        """解説の見直し：正解が 1 つに決まるか・英文の誤り・解説が本文と合っているか。"""
        from ...llm import LLMError

        passage = problem.passage.body if problem.passage is not None else ""
        user = prompts.review_user(problem.question, [(c["text"], bool(c.get("correct"))) for c in problem.choices],
                                   problem.answer_display, problem.explanation, problem.hint, passage)
        out: dict[str, Any] = {}
        for _ in range(2):  # 出力が読めなかったときは 1 回だけ呼び直す
            try:
                out = llm.chat_json(model, prompts.REVIEW_SYSTEM, user, prompts.REVIEW_SCHEMA, temperature=0.2)
                break
            except LLMError as exc:
                out = {"_error": str(exc), "_raw": getattr(exc, "raw", "")}
        verdict = next((ln.strip(" 　。.*「」") for ln in str(out.get("verdict") or "").splitlines() if ln.strip()), "")
        issues = str(out.get("issues") or "").strip()
        raw = out.get("_raw") or f"[[verdict]]\n{verdict}\n[[issues]]\n{issues}"
        if verdict.startswith("問題あり"):
            return VerifyResult(False, f"{model}: 誤りがあると指摘（{issues[:300] or '理由なし'}）", issues,
                                "en_review", raw=raw)
        if verdict.startswith("問題なし"):
            return VerifyResult(True, f"{model}: 誤りは見つからなかった", issues, raw=raw)
        return VerifyResult(True, f"{model}: 見直しの結果を読めなかった（{out.get('_error') or verdict[:60]}）",
                            "", raw=raw)

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
