"""英語（読解・語彙・スペル）の作成・検証・採点・出題のテスト。"""

import random
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from quizapp.db import Job, Problem
from quizapp.subjects import get_subject
from quizapp.subjects.english import (EnglishSubject, check, parse_question, pick_distractors, read_letter,
                                      spelling_pattern)
from quizapp.subjects.english.catalog import MISTAKES, READING_MISTAKES, READINGS, TOPICS
from quizapp.subjects.english.words import POS, SPELLING_EXCLUDE, WORDS
from quizapp.web import create_app
from quizapp.worker import Worker

from .test_worker import add_job, job, make_worker, problems

SENTENCES = [
    "Tom visited the old library in Greenfield on Monday morning with his younger sister.",
    "The library opened a new reading room for students who want to study quietly after school.",
    "Many people came to the opening event because the library offered free coffee and cookies.",
    "Tom borrowed three books about birds and returned them two weeks later without any trouble.",
    "His sister joined a short drawing class and painted a picture of the river near the library.",
    "After the visit, Tom decided to go to the library every Saturday to read and study.",
]


class EnglishOracle:
    """テスト用の LLM。

    本文作成役：SENTENCES を本文にする（題名 A Visit to the Library）
    本文の見直し：passage_verdict を返す
    作成役（設問）：設問 i（すでにある設問の続きの番号）の正解は「Right answer i」、根拠の文は本文の i 番目の文
    作成役（語彙）：指定された単語を 1 回だけ使った例文を書く
    本文なしの確認：X（blind_correct に入れた設問番号のときだけ正解の記号を答える）
    検証役：データベースの問題から本当の正解（ずらす前のもの）を選ぶ
    """

    def __init__(self, sessions, *, bad_evidence_once=False, bad_passage_once=False, blind_correct=(),
                 review_verdict="問題なし", passage_verdict="問題なし", bad_questions=(), judge="right",
                 multiple_calls=None, absent="museum", awkward=False, coherence="自然"):
        self.sessions = sessions
        self.bad_evidence_once = bad_evidence_once
        self.bad_passage_once = bad_passage_once
        self.blind_correct = set(blind_correct)
        self.bad_questions = set(bad_questions)  # この番号の設問は根拠の文をいつも本文にないものにする
        self.review_verdict = review_verdict
        self.passage_verdict = passage_verdict
        # 見直しの判定："right"（正しく判定）、"multiple"（誤答も裏付けあり・意味が通る）、"relabel"（誤答の種類を変える）、
        # "garbled"（読めない判定）
        self.judge = judge
        self.multiple_calls = multiple_calls  # judge="multiple" を最初の何回の判定だけにするか（None はいつも）
        self.n_judgments = 0
        self.absent = absent  # 読解の誤答の「違う点」に書く内容（D。空なら書かない）
        self.awkward = awkward  # 語彙の誤答を「文法的には入るが不自然」と判定する（C）
        self.coherence = coherence  # 例文の筋の確認の答え
        self.n_sentences = 0
        self.calls = []
        self.label = ""

    def chat_json(self, model, system, user, schema, *, temperature=0.7):
        props = schema["properties"]
        if "judgments" in props:
            self.calls.append(("judge", model))
            self.n_judgments += 1
            return {"judgments": self._judgments(system, user)}
        if "verdict" in props:
            if "筋が通っているかだけ" in system:  # 例文の筋（第 8 回のレビュー 3-1）
                self.calls.append(("coherence", model))
                return {"verdict": self.coherence, "reason": "理由と結果がつながらない" if self.coherence == "不自然" else ""}
            if "本文を点検" in system:
                self.calls.append(("passage_review", model))
                # passage_verdict はリストにすると、呼ぶたびに前から使う（最後のものは使い続ける）
                v = self.passage_verdict
                verdict = (v.pop(0) if len(v) > 1 else v[0]) if isinstance(v, list) else v
                return {"verdict": verdict, "issues": "時系列が食い違う" if "あり" in verdict else "なし"}
            self.calls.append(("review", model))
            return {"verdict": self.review_verdict, "issues": "なし"}
        if "passage" in props:
            self.calls.append(("passage", model))
            return self._passage(user)
        if "q1" in props:
            self.calls.append(("gen", model))
            return self._questions(user, props)
        if "sentence" in props:
            self.calls.append(("gen", model))
            word = re.search(r"使う単語: (\S+)（", user).group(1)
            self.n_sentences += 1  # 文ごとに番号を変える（見直しで、正解を入れた文を見分けるため）
            cands = re.search(r"誤答の候補（ここから 3 つ選ぶ）: (.*)$", user, re.M)
            meanings = re.search(r"意味: (.*)）$", user, re.M).group(1).split("／")
            return {"meaning": meanings[-1], "others": ", ".join(cands.group(1).split(", ")[:3]) if cands else "",
                    "sentence": f"Student number {self.n_sentences} wants to {word} with friends during the long summer.",
                    "translation": "学校の生徒はみな、長い夏の間に友だちと〜したいと思っている。",
                    "hint": "文の後半の with friends に注目しよう。",
                    "explanation": "夏の間に友だちと一緒にすることなので、この語の意味が文に合う。"}
        if "words" in props:  # 検証（スペル）：条件に合う語をすべて
            self.calls.append(("solve", model))
            with self.sessions() as s:
                p = next(p for p in s.execute(select(Problem).where(Problem.status == "draft")).scalars()
                         if p.question in user)
            return {"work": "意味から考えた", "words": p.answer["word"]}
        if "reason" in props and "answer" in props:  # 検証（語彙）：正解を抜いた選択肢と「どれも合わない」
            self.calls.append(("solve", model))
            items = re.findall(r"^(\d+): (.*)$", user, re.M)
            multiple = self._multiple()
            pick = items[0][0] if multiple else next(n for n, t in items if t == "どれも合わない")
            return {"answer": pick, "reason": "文に合う" if multiple else "どれも合わない"}
        if set(props) == {"answer"}:  # 本文なしの確認
            self.calls.append(("blind", model))
            m = re.search(r"\(part (\d+)\)", user)
            if m and int(m.group(1)) in self.blind_correct:
                letters = re.findall(r"^([A-D]): (.*)$", user, re.M)
                return {"answer": next(c for c, t in letters if t.startswith("Right"))}
            return {"answer": "X"}
        self.calls.append(("solve", model))
        return self._solve(user)

    def _multiple(self):
        return self.judge == "multiple" and (self.multiple_calls is None or self.n_judgments <= self.multiple_calls)

    def _judgments(self, system, user):
        text = self._judgments2(system, user)
        if "自然に入る" in system:  # 3 段階の判定（第 7 回のレビュー C）
            text = text.replace("通らない", "入らない").replace("通る", "自然に入る")
            if self.awkward:  # 正解以外の「入らない」を「文法的には入るが不自然」にする
                text = text.replace("入らない", "文法的には入るが不自然")
        return text

    def _judgments2(self, system, user):
        if self.judge == "garbled":
            return "よく分かりません"
        items = re.findall(r"^(\d+): (.*)$", user, re.M)
        lines = []
        if "本文と照らして" in system:  # 読解の選択肢（検証は本文の文を引用する）
            for n, text in items:
                label = ("裏付けあり" if text.startswith("Right") else
                         "本文に書かれていない" if "museum" in text else
                         "本文と食い違う" if "park" in text else "細部の取り違え")
                if self._multiple() and "museum" in text:
                    label = "裏付けあり"
                if self.judge == "relabel" and "park" in text:
                    label = "本文に書かれていない"
                part = int(re.search(r"answer (\d+)", text).group(1))
                quote = f' — 引用: "{SENTENCES[(part - 1) % len(SENTENCES)]}"'
                if "違う点" in system and label not in ("裏付けあり", "本文と食い違う"):  # 作り直した D（第 8 回 2-2）
                    quote = f" — 違う点: {self.absent}" if self.absent else ""
                elif label == "本文に書かれていない":
                    quote = ""
                if self.judge == "bad_quote" and label == "裏付けあり":
                    quote = ' — 引用: "This sentence is not in the passage."'
                lines.append(f"{n}: {label}{quote} — 本文の第1段落")
            return "\n".join(lines)
        with self.sessions() as s:  # 語彙：データベースの下書きから正解を探す
            drafts = list(s.execute(select(Problem).where(Problem.status == "draft")).scalars())
        if "[ ] で示した語の意味" in system:
            word = re.search(r"\[ (\w+) \]", user).group(1)
            right = {m for w in WORDS if w.word == word for m in w.meanings}
            wrong_one = next((n for n, text in items if text not in right), None)
            for n, text in items:
                fit = text in right or (self._multiple() and n == wrong_one)
                lines.append(f"{n}: {'通る' if fit else '通らない'}")
            return "\n".join(lines)
        right = {n for n, text in items if any(p.template == "en-vocab-blank" and text == p.question.split(
            "\n\n")[-1].replace("(　　　　)", p.params["word"]) for p in drafts)}
        wrong_one = next((n for n, _t in items if n not in right), None)
        for n, text in items:
            ok = n in right
            fit = ok or (self._multiple() and n == wrong_one)
            lines.append(f"{n}: {'通る' if fit else '通らない'} — 理由")
        return "\n".join(lines)

    def list_models(self):
        return [{"name": "gemma4:12b-it-qat", "size_gb": 8.1, "parameter_size": "12B", "quantization": "Q4"},
                {"name": "gemma4:e4b", "size_gb": 3.1, "parameter_size": "8B", "quantization": "Q4"}]

    def roles(self):
        return [r for r, _ in self.calls]

    def _passage(self, user):
        lo = int(re.search(r"本文の長さ: (\d+)", user).group(1))
        sentences = list(SENTENCES)
        while check.word_count(" ".join(sentences)) < lo:
            sentences += SENTENCES
        passage = " ".join(sentences[:3]) + "\n\n" + " ".join(sentences[3:])
        if self.bad_passage_once and "直してほしい点" not in user:
            passage += "\n\n図書館は楽しい。"
        return {"title": "A Visit to the Library", "passage": passage}

    def _questions(self, user, props):
        have = len(re.findall(r"^- ", user.split("すでにある設問", 1)[1], re.M)) if "すでにある設問" in user else 0
        fixing = "直してほしい点" in user
        out = {}
        for i in range(1, len(props) + 1):
            part = have + i
            evidence = SENTENCES[part - 1]
            if (self.bad_evidence_once and not fixing and part == 1) or part in self.bad_questions:
                evidence = "This sentence is not in the passage at all."
            out[f"q{i}"] = "\n".join([
                f"Question {i}: What is true according to the passage (part {part})?",
                f"A: Right answer {part} about the library",
                f"B: Wrong answer {part} about a museum",
                f"C: Wrong answer {part} about a park",
                f"D: Wrong answer {part} about a station",
                "Answer: A",
                "Wrong: B=not_stated, C=contradicts, D=detail",
                f"Evidence: {evidence}",
                "Hint: 本文の第1段落を読もう。",
                "Explanation: 本文に図書館のことが書かれているので、図書館についての選択肢が正しい。"
                "博物館・公園・駅のことは本文に書かれていない。",
            ])
        return out

    def _solve(self, user):
        question = user.split("問題:\n", 1)[1].split("\n\n", 1)[0] if "問題:\n" in user else ""
        letters = re.findall(r"^([A-D]): (.*)$", user, re.M)
        with self.sessions() as s:
            cands = list(s.execute(select(Problem).where(Problem.status == "draft")).scalars())
        problem = next(p for p in cands if p.question.startswith(question.split("\n")[0]) and
                       (not letters or {t for _c, t in letters} == {c["text"] for c in p.choices}))
        word = problem.params.get("word")
        if not letters:
            return {"work": "意味から考えた", "answer": word.upper()}
        if problem.template == "en-vocab-blank":
            right = word
        elif problem.template == "en-vocab-meaning":
            right = next(w.meaning for w in WORDS if w.word == word and w.pos == problem.params["pos"])
        else:
            right = next(t for _c, t in letters if t.startswith("Right"))
        return {"work": "本文の該当箇所を読んだ", "answer": next(c for c, t in letters if t == right)}


def run_job(settings, sessions, sandbox, llm, **kw):
    jid = add_job(sessions, subject="english", **kw)
    make_worker(settings, sessions, sandbox, llm).run_once()
    return jid


# --- 部品 -------------------------------------------------------------------------

def test_english_is_registered_with_units_and_levels():
    sub = get_subject("english")
    assert isinstance(sub, EnglishSubject) and sub.math_text is False
    assert sub.templates_for("unit:en-reading", "choice") == ["en-notice", "en-expository", "en-story"]
    assert sub.templates_for("", "input") == ["en-spelling"]
    assert set(sub.templates_for("", levels="basic")) == {"en-notice", "en-vocab-meaning", "en-spelling"}
    assert set(TOPICS) == set(READINGS) and all(len(set(t)) == len(t) >= 30 for t in TOPICS.values())
    assert set(READING_MISTAKES) <= set(MISTAKES)
    # 不合格の理由の名前が数学と重ならない（/status では全科目の名前をまとめて表示する）
    assert not set(sub.reject_codes) & set(get_subject("math").reject_codes)


def test_word_list_is_well_formed():
    keys = [(w.word, w.pos) for w in WORDS]
    assert len(keys) == len(set(keys)) and len(WORDS) >= 1000
    for w in WORDS:
        assert re.fullmatch(r"[a-z]+", w.word), w
        assert w.pos in POS and w.level in ("basic", "common")
        assert re.search(r"[぀-ヿ㐀-鿿]", w.meaning), w
    assert SPELLING_EXCLUDE <= {w.word for w in WORDS}


def test_distractors_share_pos_level_and_not_meaning():
    rng = random.Random(1)
    for w in rng.sample(WORDS, 200):
        others = pick_distractors(w, rng)
        assert len(others) == 3
        assert all(o.pos == w.pos and o.level == w.level and o.word != w.word for o in others)
        assert len({o.meaning for o in others} | {w.meaning}) == 4
    suggest = next(w for w in WORDS if w.word == "suggest")
    for seed in range(50):  # 「提案する」の語（propose）は「suggest」の誤答にしない
        assert "propose" not in [o.word for o in pick_distractors(suggest, random.Random(seed))]


def test_parse_question_and_read_letter():
    q = parse_question("**Question:** Why?\nA) one\n(B): two\nC. three\nD: four\nAnswer: (C)\n"
                       "Wrong: A=detail, b=partial, D=not_stated\nEvidence: “It was late.”\n"
                       "Hint: 第2段落\nExplanation: 一行目\nA: これも解説の続き")
    assert q["question"] == "Why?" and q["options"] == ["one", "two", "three", "four"] and q["answer"] == 2
    assert q["wrong"] == {"A": "detail", "B": "partial", "D": "not_stated"} and q["evidence"] == "It was late."
    assert q["explanation"] == "一行目\nA: これも解説の続き"
    assert read_letter("B", "", 4) == 1 and read_letter("(c)", "", 4) == 2 and read_letter("答えは D です", "", 4) == 3
    assert read_letter("Because a cat", "", 4) is None  # 冠詞の a を記号と取り違えない
    assert read_letter("", "よって答え: B", 4) == 1


def test_text_checks():
    passage = "Tom went to the park. It’s a “nice” place!"
    assert check.contains(passage, "it's a \"nice\" place") and not check.contains(passage, "Tom went home")
    assert check.letter_reference("選択肢Bは本文と食い違う") and check.letter_reference("（C）は誤り")
    assert not check.letter_reference("本文の第2段落に書かれている")
    assert check.choice_issues(["a", "b", "b", "c"], 0) == ["選択肢に同じものがある"]
    assert any("長い" in x for x in check.choice_issues(
        ["It was because the weather was very bad and the trains were all stopped", "Rain", "Snow", "Wind"], 0))
    assert check.english_issues("これは日本語", "本文") == ["本文に日本語・全角・韓国語の文字が入っている"]
    assert check.japanese_issues("This is English.", "解説") == ["解説が日本語で書かれていない"]
    assert spelling_pattern("decide") == "d _ _ _ _ _"


# --- 作成と検証（worker） --------------------------------------------------------------

def test_reading_makes_one_passage_with_shared_questions(settings, sessions, sandbox):
    llm = EnglishOracle(sessions)
    jid = run_job(settings, sessions, sandbox, llm, scope="tmpl:en-expository", count=3)
    assert job(sessions, jid).status == "done"
    ready = problems(sessions, status="ready")
    assert len(ready) == 3 and len({p.passage_id for p in ready}) == 1 and ready[0].passage_id is not None
    # 本文作成 → 本文の見直し → 設問の作成 → 本文なしの確認 3 問分 → 検証 2 回 × 3 問 → 見直し 3 問
    # （見直しは 1 問につき、選択肢の判定と解説の点検の 2 回）
    assert llm.roles() == (["passage", "passage_review", "gen"] + ["blind"] * 3 + ["judge"] * 6
                           + ["review"] * 3)  # 検証は選択肢ごとの判定（2 回）、見直しは解説の点検だけ
    assert {m for r, m in llm.calls if r == "blind"} == {"gemma4:12b-it-qat"}  # 作成役のモデル（入れ替えなし）
    with sessions() as s:
        p = s.get(Problem, ready[0].id)
        assert p.passage.title == "A Visit to the Library" and "Greenfield" in p.passage.body
        assert "が書いた英文" in p.passage.source_title
    for p in ready:
        assert p.subject == "english" and p.unit == "en-reading" and p.level == "common"
        assert len(p.choices) == 4 and sum(c["correct"] for c in p.choices) == 1
        assert all(c["mistake"] in READING_MISTAKES for c in p.choices if not c["correct"])
        assert p.answer_display.startswith("Right") and "本文の根拠" in p.explanation
    assert len({p.params_key for p in ready}) == 3  # 同じ本文の設問を「重複」にしない
    # 選択肢は並べ替える（LLM はいつも A を正解にしている）
    assert any(not p.choices[0]["correct"] for p in ready)


def test_reading_topics_are_not_reused(settings, sessions, sandbox):
    llm = EnglishOracle(sessions)
    run_job(settings, sessions, sandbox, llm, scope="tmpl:en-notice", count=4)
    ready = problems(sessions, status="ready")
    assert len(ready) == 4 and len({p.passage_id for p in ready}) == 2
    assert len({p.params["topic"] for p in ready}) == 2


def test_evidence_not_in_passage_is_fixed(settings, sessions, sandbox):
    llm = EnglishOracle(sessions, bad_evidence_once=True)
    run_job(settings, sessions, sandbox, llm, scope="tmpl:en-notice", count=2)
    ready = problems(sessions, status="ready")
    assert len(ready) == 2
    assert any("本文にそのまま入っていない" in e["summary"] and "直しを依頼します" in e["summary"]
               for e in ready[0].verification)


def test_question_answerable_without_passage_is_dropped(settings, sessions, sandbox):
    """本文なしで正解できた設問だけを不合格にし、同じ本文のほかの設問は検証に進める。"""
    llm = EnglishOracle(sessions, blind_correct={2})
    run_job(settings, sessions, sandbox, llm, scope="tmpl:en-story", count=3)
    rejected = problems(sessions, status="rejected")
    assert len(rejected) == 1 and rejected[0].reject_code == "en_no_passage"
    assert "(part 2)" in rejected[0].question and len(rejected[0].choices) == 4
    # 設問 2 を外すと 3 問に足りないので、同じ本文に設問を 1 問足す（本文を使い回す）
    ready = problems(sessions, status="ready")
    assert len(ready) == 3 and all("(part 2)" not in p.question for p in ready)
    assert len({p.passage_id for p in ready}) == 1 and rejected[0].passage_id == ready[0].passage_id
    assert llm.roles().count("passage") == 1
    assert len({p.params_key for p in ready}) == 3  # 足した設問の番号が、すでにある設問と重ならない
    assert any("に設問を足す" in e["summary"] for e in ready[-1].verification)


def test_passage_review_rejects_the_whole_passage(settings, sessions, sandbox):
    """本文の見直しで「問題あり」なら本文ごと不合格。設問は作らない。"""
    llm = EnglishOracle(sessions, passage_verdict="問題あり")
    jid = run_job(settings, sessions, sandbox, llm, scope="tmpl:en-story", count=1)
    assert job(sessions, jid).status in ("partial", "failed")
    rejected = problems(sessions, status="rejected")
    assert rejected and {p.reject_code for p in rejected} == {"en_passage_review"}
    assert "gen" not in llm.roles() and problems(sessions, status="ready") == []
    with sessions() as s:
        from quizapp.db import Passage

        # 落ちた本文は状態 rejected で残し（書き出しで見られる）、使い回さない。題材はまた使える
        rows = list(s.execute(select(Passage)).scalars())
        assert rows and {r.status for r in rows} == {"rejected"}
        assert all(p.passage_id for p in rejected) and "本文の段階で不合格" in rejected[0].question


def test_passage_is_fixed_and_review_can_be_off(settings, sessions, sandbox):
    llm = EnglishOracle(sessions, bad_passage_once=True)
    run_job(settings, sessions, sandbox, llm, scope="tmpl:en-notice", count=2, explanation_review=False,
            review_model="anthropic/claude-haiku-5-5", gen_model="gemma4:12b-it-qat", verify_model="gemma4:e4b",
            verify_count=2)
    assert llm.roles()[:2] == ["passage", "passage"]  # 本文の点検で落ちたので本文作成役に直させた
    assert "passage_review" not in llm.roles() and "review" not in llm.roles()  # 見直しを切った依頼
    ready = problems(sessions, status="ready")
    assert len(ready) == 2
    assert any(e["step"] == "本文の点検" and "日本語" in e["summary"] for e in ready[0].verification)


def test_passage_with_rejected_questions_is_kept_and_reused(settings, sessions, sandbox):
    """設問がすべて不合格でも本文は残し、次から設問を足す。不合格が設問の数の 2 倍になった本文は使わない。"""
    from quizapp.db import Passage

    llm = EnglishOracle(sessions, bad_questions={1, 2})  # お知らせの設問（2 問）はいつも根拠が本文にない
    run_job(settings, sessions, sandbox, llm, scope="tmpl:en-notice", count=1)
    rejected = problems(sessions, status="rejected")
    assert rejected and all(p.reject_code == "en_text" for p in rejected)
    with sessions() as s:
        first = s.execute(select(Passage).order_by(Passage.id)).scalars().first()
        on_first = [p for p in rejected if p.passage_id == first.id]
    assert first.status == "ok" and first.template == "en-notice" and first.topic
    assert len(on_first) == 4  # 1 回目 2 問 ＋ 使い回して 2 問。そこで使うのをやめる
    assert llm.roles().count("passage") >= 2  # 新しい本文に移った


def test_review_rejects(settings, sessions, sandbox):
    llm = EnglishOracle(sessions, review_verdict="問題あり")
    run_job(settings, sessions, sandbox, llm, scope="tmpl:en-vocab-blank", count=1)
    rejected = problems(sessions, status="rejected")
    assert rejected and all(p.reject_code == "en_review" for p in rejected)


@pytest.mark.parametrize("template,fmt", [("en-vocab-blank", "choice"), ("en-vocab-meaning", "choice"),
                                          ("en-spelling", "input")])
def test_vocab_problems(settings, sessions, sandbox, template, fmt):
    llm = EnglishOracle(sessions)
    jid = run_job(settings, sessions, sandbox, llm, scope=f"tmpl:{template}", format=fmt, count=3)
    assert job(sessions, jid).status == "done"
    ready = problems(sessions, status="ready")
    assert len(ready) == 3 and len({p.params["word"] for p in ready}) == 3
    for p in ready:
        word = p.params["word"]
        assert p.unit == "en-vocab" and p.passage_id is None
        assert not re.search(rf"(?<![A-Za-z]){word}(?![A-Za-z])", p.question.split("\n\n")[-1]) or \
            template == "en-vocab-meaning"
        if fmt == "choice":
            assert len(p.choices) == 4 and sum(c["correct"] for c in p.choices) == 1
            assert {c["mistake"] for c in p.choices if not c["correct"]} == {
                "other_word" if template == "en-vocab-blank" else "other_meaning"}
        else:
            assert p.answer == {"kind": "word", "word": word} and spelling_pattern(word) in p.question
            assert word not in p.input_hint and word not in p.hint
        assert p.explanation.startswith("訳: ") and word in p.explanation


def test_tampered_english_problem_is_rejected_by_verification(settings, sessions, sandbox):
    llm = EnglishOracle(sessions)
    jid = run_job(settings, sessions, sandbox, llm, scope="tmpl:en-notice", count=1, is_test=True)
    j = job(sessions, jid)
    assert "期待どおり" in j.error, j.error
    assert problems(sessions, status="ready") == []
    assert ("blind", "gemma4:12b-it-qat") not in llm.calls  # テストのときは本文なしの確認をしない
    assert any(p.reject_code == "solver_mismatch" for p in problems(sessions, status="rejected"))


@pytest.mark.parametrize("template", ["en-vocab-blank", "en-vocab-meaning", "en-expository", "en-story"])
def test_tampered_answer_for_each_choice_subunit_is_rejected(settings, sessions, sandbox, template):
    llm = EnglishOracle(sessions)
    jid = run_job(settings, sessions, sandbox, llm, scope=f"tmpl:{template}", count=1, is_test=True)
    assert "期待どおり" in job(sessions, jid).error


# --- 採点・出題・画面（web） -------------------------------------------------------------

def test_spelling_grade():
    sub = EnglishSubject()

    class P:
        format = "input"
        answer = {"kind": "word", "word": "decide"}

    assert sub.grade(None, P(), " Decide ").correct and sub.grade(None, P(), "ｄｅｃｉｄｅ.").correct
    assert not sub.grade(None, P(), "decides").correct
    assert sub.grade(None, P(), "").retry_message and sub.grade(None, P(), "決める").retry_message
    assert sub.grade(None, P(), "make up").retry_message


@pytest.fixture
def stocked(settings, sessions, sandbox):
    llm = EnglishOracle(sessions)
    w = Worker(settings, sessions, llm, sandbox, rng=random.Random(4))
    w.last_backup_check = 1e18
    with sessions.begin() as s:
        s.add(Job(subject="english", scope="tmpl:en-expository", format="choice", count=6))
        s.add(Job(subject="english", scope="tmpl:en-vocab-blank", format="choice", count=4))
    w.run_once()
    w.run_once()
    return problems(sessions, status="ready")


def test_session_keeps_passage_questions_together(settings, sessions, stocked):
    from quizapp import stock

    assert len(stocked) == 10
    passage_of = {p.id: p.passage_id for p in stocked}
    sub = get_subject("english")
    for seed in range(20):
        with sessions() as s:
            ids = stock.build_session(s, 1, sub, "", "phone", 10, 30, rng=random.Random(seed))
        assert sorted(ids) == sorted(passage_of)
        for pid in {v for v in passage_of.values() if v}:
            pos = [i for i, x in enumerate(ids) if passage_of[x] == pid]
            assert pos == list(range(pos[0], pos[0] + 3))  # 続けて出す
            assert [ids[i] for i in pos] == sorted(ids[i] for i in pos)  # 設問の順
        with sessions() as s:  # 4 問のセット：本文の塊を途中で切るときは 1 問目から使い、続けて出す
            ids = stock.build_session(s, 1, sub, "unit:en-reading", "phone", 4, 30, rng=random.Random(seed))
        assert len(ids) == 4
        for pid in {passage_of[x] for x in ids}:
            pos = [i for i, x in enumerate(ids) if passage_of[x] == pid]
            assert pos == list(range(pos[0], pos[0] + len(pos)))
            all_q = sorted(k for k, v in passage_of.items() if v == pid)
            assert [ids[i] for i in pos] == all_q[:len(pos)]


def test_play_shows_passage_tabs_without_math(settings, sessions, sandbox, stocked):
    client = TestClient(create_app(settings, sessions, sandbox))
    r = client.post("/start", data={"subject": "english", "scope": "unit:en-reading", "device": "phone",
                                    "count": "3"}, follow_redirects=True)
    assert r.status_code == 200
    assert 'class="reading nomath"' in r.text and "設問へ" in r.text and "本文を見る" in r.text
    assert "Greenfield" in r.text and 'id="preview"' not in r.text
    sid = int(re.search(r"/play/(\d+)/answer", r.text).group(1))
    pid = int(re.search(r'name="problem_id" value="(\d+)"', r.text).group(1))
    r = client.post(f"/play/{sid}/answer", data={"problem_id": pid, "response": "0", "shown_at": 0},
                    follow_redirects=True)
    assert r.status_code == 200 and "本文の根拠" in r.text and "passage-review" in r.text
    assert "nomath" in r.text
    r = client.get(f"/problems/{pid}")
    assert r.status_code == 200 and "Greenfield" in r.text


def test_spelling_play_and_grade(settings, sessions, sandbox):
    llm = EnglishOracle(sessions)
    run_job(settings, sessions, sandbox, llm, scope="tmpl:en-spelling", format="input", count=1)
    p = problems(sessions, status="ready")[0]
    client = TestClient(create_app(settings, sessions, sandbox))
    r = client.post("/start", data={"subject": "english", "device": "keyboard", "count": "1"},
                    follow_redirects=True)
    assert 'autocapitalize="off"' in r.text and 'spellcheck="false"' in r.text and 'id="preview"' not in r.text
    sid = int(re.search(r"/play/(\d+)/answer", r.text).group(1))
    r = client.post(f"/play/{sid}/answer", data={"problem_id": p.id, "response": p.params["word"].upper()},
                    follow_redirects=True)
    assert "正解" in r.text


def test_review_mode_uses_english_checks(settings, sessions, sandbox, stocked):
    from quizapp import review

    with sessions() as s:
        p = s.get(Problem, stocked[0].id)
        _ = p.passage
        assert review.text_issues(p) == []
        p.explanation = "This is English only."
        assert "解説が日本語で書かれていない" in review.text_issues(p)
    client = TestClient(create_app(settings, sessions, sandbox))
    r = client.get("/review?subject=english")
    assert r.status_code == 200 and "nomath" in r.text and "Greenfield" in r.text
    md = client.get("/review/export.md?subject=english").text
    assert "### 本文" in md and "（誤答：" in md


def test_generate_error_explains_format_and_level(settings, sessions, sandbox):
    """スペルを選択式やレベル違いで頼んだときは、使える形式・レベルをエラーに出す。"""
    client = TestClient(create_app(settings, sessions, sandbox))
    r = client.post("/generate", data={"subject": "english", "scope": "tmpl:en-spelling", "format": "choice",
                                       "count": "3"})
    assert r.status_code == 400
    assert "語彙／スペル：形式はキーボード入力だけ（選んだのは選択式）" in r.text
    r = client.post("/generate", data={"subject": "english", "scope": "tmpl:en-spelling", "format": "input",
                                       "levels": ["common"], "count": "3"})
    assert "レベルは高校基礎（選んだのは共通テスト）" in r.text


# --- 第 4 回のレビューの A（機械的な点検）・B（プロンプト） ---------------------------------

def test_review04_letter_internal_words_and_domains():
    # A-1：「A 選択肢」「C の」も記号で呼んでいるとみなす（英語の Team A などは見ない）
    for text in ("A 選択肢は本文と食い違う", "C の内容は書かれていない", "「D」は誤り", "選択肢Ｂは"):
        assert check.letter_reference(text), text
    assert not check.letter_reference("Team A won the game. 本文の第2段落を読もう。")
    # A-2：アプリの内部の言葉
    assert check.internal_words("入らない語の music は合わない") == ["入らない語"]
    assert check.japanese_issues("与えられた単語を使うと意味が通る。", "解説")
    assert not check.internal_words("生徒に与えられた課題をやり終えた。")
    # A-3：例示用のドメインだけ
    assert check.bad_hosts("Visit www.example.org/events or mail info@example.org or shop.example.net.") == []
    assert check.bad_hosts("See greenleafcafe-example.org and www.greenleaf.com") == [
        "greenleafcafe-example.org", "greenleaf.com"]
    assert any("example.org など" in x for x in check.english_issues("Write to cafe@greenleaf.com.", "本文"))
    # A-6：根拠の文が内側の引用符で終わるとき、閉じの引用符を消さない
    assert check.strip_quotes('"She said, “Have a cup of tea.”"') == "She said, “Have a cup of tea.”"
    assert check.strip_quotes("“I was furious.”") == "I was furious."
    assert parse_question('Evidence: "He said, “Thank you.”"')["evidence"] == "He said, “Thank you.”"


def test_review04_question_checks():
    from quizapp.subjects.english import _question_issues

    body = "A Day Out\n\nTom went to the park. He met Ken there.\n\nThey ate lunch together. It was sunny."
    q = {"question": "What did Tom do?", "options": ["Ate lunch with Ken", "Swam", "Slept", "Read"], "answer": 0,
         "wrong": {"B": "not_stated", "C": "not_stated", "D": "not_stated"}, "evidence": "They ate lunch together.",
         "hint": "第2段落を読もう。", "explanation": "本文に昼食を一緒に食べたとある。ほかの選択肢は書かれていない内容である。"}
    assert _question_issues(q, body) == []
    # A-5：ヒントの段落番号が根拠の文のある段落と違う（お知らせは照らさない）
    wrong_para = {**q, "hint": "第1段落を読もう。"}
    assert any("第2段落" in x for x in _question_issues(wrong_para, body))
    assert _question_issues(wrong_para, body, check_paragraph=False) == []
    # A-4：ヒントが解説の 1 文目とほぼ同じ／ヒントに根拠の文
    same = {**q, "hint": "本文に昼食を一緒に食べたとある。"}
    assert any("1 文目" in x for x in _question_issues(same, body))
    quoted = {**q, "hint": "第2段落の They ate lunch together. を読もう。"}
    assert any("根拠の文がそのまま" in x for x in _question_issues(quoted, body))
    # A-1：解説が選択肢を記号で呼ぶ
    letter = {**q, "explanation": q["explanation"] + "A 選択肢が正しい。"}
    assert any("記号" in x for x in _question_issues(letter, body))


def test_review04_vocab_checks():
    sub = EnglishSubject()
    word = next(w for w in WORDS if w.word == "decide" and w.pos == "v")
    ok = {"sentence": "We need to decide where to go before the summer vacation starts next week.",
          "translation": "来週夏休みが始まる前に、どこへ行くか決める必要がある。",
          "hint": "where to go と一緒に使われている動詞に注目しよう。",
          "explanation": "どこへ行くかを「決める」と考えると意味が通る。"}
    assert sub._vocab_issues(word, [], ok, "en-vocab-meaning") == []
    leak = {**ok, "hint": "「決める」という意味の語です。"}
    assert any("意味「決める」" in x for x in sub._vocab_issues(word, [], leak, "en-vocab-meaning"))
    assert not any("意味「決める」" in x for x in sub._vocab_issues(word, [], leak, "en-spelling"))  # スペルは問題文に意味がある
    blank = {**ok, "explanation": "空所に入る語は decide で、「決める」という意味である。"}
    assert any("空所" in x for x in sub._vocab_issues(word, [], blank, "en-vocab-meaning"))
    inside = {**ok, "explanation": "入らない語の buy は「買う」なので合わない。decide が正しい。"}
    assert any("内部の言葉" in x for x in sub._vocab_issues(word, [], inside, "en-vocab-blank"))


def test_review04_prompts():
    from quizapp.subjects.english import prompts

    every = [prompts.vocab_system(t) for t in ("en-vocab-blank", "en-vocab-meaning", "en-spelling")]
    every += [prompts.QUESTION_SYSTEM, prompts.PASSAGE_SYSTEM, prompts.vocab_user("a", "動詞", ["x"], ["b"])]
    assert not any("入らない語" in p for p in every)  # 作成役が書き写していた言葉をプロンプトから無くした
    # スペル・意味の問題には、ほかの語の話を書かない（A-2）
    assert "誤答の候補" in prompts.vocab_system("en-vocab-blank")
    assert "誤答の候補" not in prompts.vocab_system("en-spelling")
    assert "空所」「入る」「入れる」という言葉を使わない" in prompts.vocab_system("en-vocab-meaning")
    # B-1〜B-5
    q = prompts.QUESTION_SYSTEM
    assert "まず正解がなぜ正しいか" in q and "誤答の説明から始めない" in q
    assert "because・so・more・less・only・all" in q and "理由・目的を問う設問" in q
    assert "理由と結果・前後の内容が常識的につながる" in prompts.vocab_system("en-vocab-blank")
    assert "指定された品詞のとおりに説明する" in prompts.vocab_system("en-vocab-meaning")


def test_blind_check_can_use_another_model(settings, sessions, sandbox):
    """本文なしの確認は、英語の「本文なしの確認」役で検証役などのモデルに変えられる（A-8）。"""
    from quizapp import runtime

    with sessions.begin() as s:
        runtime.save_subject_settings(s, "english", {"blind": "gemma4:e4b"})
    llm = EnglishOracle(sessions)
    run_job(settings, sessions, sandbox, llm, scope="tmpl:en-notice", count=2)
    assert {m for r, m in llm.calls if r == "blind"} == {"gemma4:e4b"}
    assert {m for r, m in llm.calls if r == "gen"} == {"gemma4:12b-it-qat"}


# --- 第 4 回のレビューの C（問いを絞った見直し） ----------------------------------------

@pytest.mark.parametrize("template", ["en-expository", "en-vocab-blank", "en-vocab-meaning"])
def test_verify_rejects_a_second_correct_answer(settings, sessions, sandbox, template):
    """検証で、誤答も本文で裏付けられる・空所に入る・意味が合う（正解が複数）と 2 回とも判定されたら不合格
    （第 6 回のレビュー 2-1・2-2）。"""
    llm = EnglishOracle(sessions, judge="multiple")
    run_job(settings, sessions, sandbox, llm, scope=f"tmpl:{template}", count=1)
    rejected = [p for p in problems(sessions, status="rejected") if p.reject_code == "en_verify_multiple"]
    assert rejected and all("正解が複数" in p.reject_reason or "正解が 2 つ" in p.reject_reason for p in rejected)
    entries = [e for e in rejected[0].verification if e["step"] == "検証"]
    assert len(entries) == 2 and "× 正解が 1 つか" in entries[0]["detail"] and "【" in entries[0]["raw"]
    if template != "en-expository":
        assert "× 正解を抜いた判定" in entries[0]["detail"]
    assert not any(e["step"] == "解説の見直し" for e in rejected[0].verification)


def test_verify_split_goes_to_needs_check(settings, sessions, sandbox):
    """2 回の判定が割れたら、すぐ捨てずに要確認にする。問題検証モードで確かめてストックに入れられる（2-4）。"""
    llm = EnglishOracle(sessions, judge="multiple", multiple_calls=1)  # 1 回目の判定だけ正解が複数
    run_job(settings, sessions, sandbox, llm, scope="tmpl:en-vocab-blank", count=1)
    p = next(p for p in problems(sessions, status="rejected"))
    assert p.reject_code == "needs_check" and "1/2 回合格" in p.reject_reason
    assert [e["ok"] for e in p.verification if e["step"] == "検証"] == [False, True]
    md = TestClient(create_app(settings, sessions, sandbox)).get(
        "/review/export.md?subject=english&status=needs_check").text
    assert "× 正解が 1 つか" in md and "○ 正解を抜いた判定" in md  # 判定の結果を書き出す（2-1）
    client = TestClient(create_app(settings, sessions, sandbox))
    page = client.get("/review?status=needs_check").text
    assert f"#{p.id}" in page and "確かめたのでストックに入れる" in page
    client.post(f"/review/{p.id}/restore", data={"back": "/review"})
    with sessions() as s:
        row = s.get(Problem, p.id)
        assert row.status == "ready" and row.reject_code == "" and row.verification[-1]["step"] == "要確認からストックへ"


def test_verify_passes_and_records_each_check(settings, sessions, sandbox):
    llm = EnglishOracle(sessions)
    run_job(settings, sessions, sandbox, llm, scope="tmpl:en-vocab-blank", count=1)
    p = problems(sessions, status="ready")[0]
    verify = next(e for e in p.verification if e["step"] == "検証")
    assert verify["ok"] and "○ 正解が合うか" in verify["detail"] and "○ 正解を抜いた判定" in verify["detail"]
    review = next(e for e in p.verification if e["step"] == "解説の見直し")
    assert review["ok"] and "○ 解説・例文" in review["detail"] and "正解が合うか" not in review["detail"]


def test_verify_rejects_a_quote_not_in_the_passage(settings, sessions, sandbox):
    """読解の判定の引用は本文と照らし、本文にない引用の判定はやり直させる。直らなければ合格にしない（2-1）。"""
    llm = EnglishOracle(sessions, judge="bad_quote")
    run_job(settings, sessions, sandbox, llm, scope="tmpl:en-notice", count=1)
    assert problems(sessions, status="ready") == []
    p = problems(sessions, status="rejected")[0]
    assert p.reject_code == "solver_unreadable" and "本文にない文を引用" in p.reject_reason
    entry = next(e for e in p.verification if e["step"] == "検証")
    assert entry["raw"].count("【選択肢の判定】") == 3  # 2 回やり直した


def test_review_keeps_label_when_judgments_disagree(settings, sessions, sandbox):
    """見直し役の判定が作成役の誤答の種類と違うだけでは付け直さず、食い違いを記録して書き出しに出す（第 5 回のレビュー C）。"""
    llm = EnglishOracle(sessions, judge="relabel")
    run_job(settings, sessions, sandbox, llm, scope="tmpl:en-notice", count=2)
    ready = problems(sessions, status="ready")
    assert len(ready) == 2
    for p in ready:
        park = next(c for c in p.choices if "park" in c["text"])
        assert park["mistake"] == "contradicts" and park["label_doubt"] == "not_stated"  # 作成役の申告のまま
        assert any("判定が分かれた" in (e.get("detail") or "") for e in p.verification)
    md = TestClient(create_app(settings, sessions, sandbox)).get("/review/export.md?subject=english").text
    assert "（誤答：本文と食い違う／見直しでは「本文に書かれていない」）" in md


def test_not_stated_label_is_checked_mechanically():
    """「本文に書かれていない」の誤答の中心の語が本文にあれば、作成のときに直させ、見直しでは付け直す（C-3）。"""
    from quizapp.subjects.english import _question_issues

    body = "Ocean Life\n\nThe aquarium has many fish and turtles.\n\nVisitors can see a coral reef where fish live and hide."
    q = {"question": "What can visitors see?", "options": ["A coral reef", "A home for fish in the coral reef",
                                                            "Tickets for twenty dollars", "A large dinosaur"],
         "answer": 0, "wrong": {"B": "not_stated", "C": "not_stated", "D": "not_stated"},
         "evidence": "Visitors can see a coral reef where fish live and hide.", "hint": "第2段落を読もう。",
         "explanation": "本文にサンゴ礁が見られるとある。ほかの選択肢は本文と合わない内容である。"}
    issues = _question_issues(q, body)
    assert len(issues) == 1 and "A home for fish" in issues[0] and "not_stated" in issues[0]


def test_unreadable_judgments_are_retried_then_rejected(settings, sessions, sandbox):
    """読めなかった判定は 1 回やり直し、それでも読めなければ合格にしない（第 5 回のレビュー A-2）。
    検証の判定は「答えが読み取れなかった」、見直しは「見直しの結果を読めなかった」。"""
    llm = EnglishOracle(sessions, judge="garbled")
    run_job(settings, sessions, sandbox, llm, scope="tmpl:en-notice", count=1)
    assert problems(sessions, status="ready") == []
    rejected = [p for p in problems(sessions, status="rejected") if p.reject_code == "solver_unreadable"]
    assert rejected and "判定を読めなかった" in rejected[0].reject_reason
    entry = next(e for e in rejected[0].verification if e["step"] == "検証")
    assert entry["raw"].count("【選択肢の判定】") == 3
    llm = EnglishOracle(sessions, review_verdict="よく分からない")
    run_job(settings, sessions, sandbox, llm, scope="tmpl:en-vocab-blank", count=1)
    rejected = [p for p in problems(sessions, status="rejected") if p.reject_code == "en_review_unread"]
    assert rejected and "見直しの結果を読めなかった" in rejected[0].reject_reason
    entry = next(e for e in rejected[0].verification if e["step"] == "解説の見直し")
    assert entry["raw"].count("【解説の点検】") == 2  # 1 回やり直した


# --- 第 4 回のレビューの D（語彙・スペル） ----------------------------------------------

@pytest.mark.parametrize("template,fmt", [("en-vocab-blank", "choice"), ("en-vocab-meaning", "choice"),
                                          ("en-spelling", "input")])
def test_review04_vocab_generation(settings, sessions, sandbox, template, fmt):
    from quizapp.subjects.english import spelling_hint
    from quizapp.subjects.english.jhs_words import JHS_WORDS

    llm = EnglishOracle(sessions)
    run_job(settings, sessions, sandbox, llm, scope=f"tmpl:{template}", format=fmt, count=4)
    ready = problems(sessions, status="ready")
    assert len(ready) == 4
    for p in ready:
        assert p.params["word"] not in JHS_WORDS  # D-1：中学の語は使わない
        assert not set(p.params["others"]) & JHS_WORDS
        if template == "en-spelling":
            assert p.hint == spelling_hint(p.params["word"]) and p.params["others"] == []  # D-4
        else:
            # D-5：作成役が候補から選んだ誤答の語（テスト用の LLM は候補の最初の 3 つを選ぶ）
            step = next(e for e in p.verification if e["step"] == "誤答")
            assert step["summary"].startswith("作成役が候補から選んだ誤答の語")
            cands = re.search(r"誤答の候補（ここから 3 つ選ぶ）: (.*)$", p.verification[0]["detail"], re.M).group(1)
            assert p.params["others"] == cands.split(", ")[:3]
        if template == "en-vocab-meaning":
            assert all(len(c["text"].replace("〜", "")) >= 2 for c in p.choices if not c["correct"])  # D-2
        word = next(w for w in WORDS if w.word == p.params["word"] and w.pos == p.params["pos"])
        assert p.params["meaning"] in word.meanings  # D-3：選んだ意味


def test_review04_word_helpers():
    from quizapp.subjects.english import CANDIDATES, chosen_meaning, chosen_others, spelling_hint
    from quizapp.subjects.english.jhs_words import JHS_WORDS

    guest = next(w for w in WORDS if w.word == "guest")
    assert guest.meanings == ("客", "招待客") and guest.label == "招待客"
    assert chosen_meaning(guest, "招待客") == "招待客" and chosen_meaning(guest, "「客」") == "客"
    assert chosen_meaning(guest, "よく分からない") == "客"  # 読めなければ中心の意味
    chief = next(w for w in WORDS if w.word == "chief")
    assert chief.label == "組織の長"  # D-2：1 文字の意味だけの語はない（中学の語を除く）
    assert not [w for w in WORDS if w.word not in JHS_WORDS and not w.label]
    rng = random.Random(3)
    word = next(w for w in WORDS if w.word == "decide")
    cands = pick_distractors(word, rng, CANDIDATES)
    assert len(cands) == CANDIDATES and not {c.word for c in cands} & JHS_WORDS
    picked, ok = chosen_others(f"{cands[4].word}, {cands[7].word}, banana, {cands[4].word}", cands)
    assert not ok and picked[:2] == [cands[4], cands[7]] and len(picked) == 3  # 足りない分は候補から
    picked, ok = chosen_others(", ".join(c.word for c in cands[2:5]), cands)
    assert ok and picked == cands[2:5]
    assert spelling_hint("arrive") == "最後の文字は「e」です。"
    assert spelling_hint("necessary") == "最後の文字は「y」です。2 文字目は「e」です。"
    # 中学の語を外しても、高校基礎の語が足りている
    from collections import Counter
    usable = Counter((w.level, w.pos) for w in WORDS if w.word not in JHS_WORDS)
    assert all(usable[(lv, pos)] >= 30 for lv in ("basic", "common") for pos in ("n", "v", "adj", "adv"))


# --- 第 4 回のレビューの E（記録） ------------------------------------------------------

def test_passage_rejected_by_one_generator_is_reused_by_another(settings, sessions, sandbox):
    """弱い作成役で設問が落ち続けた本文にも、別の作成役なら設問を足す（E-5）。"""
    from quizapp.db import Passage

    run_job(settings, sessions, sandbox, EnglishOracle(sessions, bad_questions={1, 2}), scope="tmpl:en-notice",
            count=1, gen_model="weak-model", verify_model="gemma4:e4b", verify_count=2)
    with sessions() as s:
        first = s.execute(select(Passage).order_by(Passage.id)).scalars().first()
    assert len([p for p in problems(sessions, status="rejected") if p.passage_id == first.id]) == 4
    llm = EnglishOracle(sessions)
    run_job(settings, sessions, sandbox, llm, scope="tmpl:en-notice", count=2,
            gen_model="gemma4:12b-it-qat", verify_model="gemma4:e4b", verify_count=2)
    ready = problems(sessions, status="ready")
    assert ready and all(p.passage_id == first.id for p in ready) and "passage" not in llm.roles()


def test_export_shows_unreadable_output_and_passage_think(settings, sessions, sandbox):
    """読めなかった出力を書き出しに出す（E-4）。本文の思考モードは、本文を作ったときの記録だけ（E-2）。"""
    from quizapp import review
    from quizapp.db import Passage, Problem

    with sessions.begin() as s:
        s.add(Problem(subject="english", unit="en-vocab", template="en-vocab-blank", format="choice",
                      status="rejected", question="Q", reject_code="llm_output", reject_reason="読めない",
                      verification_json='[{"step": "文章", "ok": false, "summary": "作成役の出力を読めません", '
                                        '"raw": "ゴミの出力\\n[思考の部分（message.thinking）の最後]\\n考え中"}]'))
    with sessions() as s:
        p = s.scalars(select(Problem)).first()
        lines = "\n".join(review.verification_lines(p))
    assert "× 文章：作成役の出力を読めません" in lines and "ゴミの出力" in lines and "```" in lines
    run_job(settings, sessions, sandbox, EnglishOracle(sessions), scope="tmpl:en-notice", count=2)
    with sessions.begin() as s:
        for row in s.scalars(select(Passage)):
            row.think = ""  # 思考モードを記録する前に作った本文
    with sessions() as s:
        p = s.scalars(select(Problem).where(Problem.status == "ready")).first()
        _ = p.passage, p.job
        assert review.think_text(p).startswith("本文作成 記録なし／作成 なし")


# --- 第 5 回のレビューの A ------------------------------------------------------------

def test_review05_brackets_hangul_and_spelling_verifier():
    from quizapp.subjects.english import EnglishSubject as E

    # A-3：二重の括弧を直す（既存の問題は「表記をまとめて直す」で）。例文に括弧が残っていたら弾く
    assert check.normalize("次の英文の [[ leak ]] で示した語") == "次の英文の [ leak ] で示した語"
    word = next(w for w in WORDS if w.word == "decide")
    fields = {"sentence": "We must [decide] before the long summer vacation starts next week.",
              "translation": "来週夏休みが始まる前に決めなければならない。", "hint": "後半に注目しよう。",
              "explanation": "夏休み前に「決める」と考えると意味が通る。"}
    assert any("括弧" in x for x in E._vocab_issues(word, [], fields, "en-vocab-meaning"))
    # A-4：韓国語の文字
    assert check.japanese_issues("到着하자 すぐに出発した。", "解説") == ["解説に韓国語の文字（하자）"]


class _Spell:
    template = "en-spelling"
    format = "input"
    question = "日本語の意味に合う英単語を書け。\n\n意味: 形\n\nWhat ( s _ _ _ _ ) is it?"
    answer = {"kind": "word", "word": "shape"}
    choices = []
    passage = None


def test_spelling_verifier_lists_every_word_that_fits():
    """スペルの検証：条件に合う語をすべて挙げさせ、正解のほかに例文に入る語があれば不合格（第 6 回のレビュー 2-3）。
    最初の文字・文字数に合わない語や語形違いだけのときは、問題の不合格にせず聞き直す（05 A-5）。"""
    class Lister:
        def __init__(self, words, fits=""):
            self.words, self.fits, self.users = list(words), fits, []

        def chat_json(self, model, system, user, schema, **kw):
            self.users.append(user)
            if "judgments" in schema["properties"]:
                return {"judgments": self.fits}
            return {"work": "", "words": self.words.pop(0)}

    sub = EnglishSubject()
    llm = Lister(["form", "shape"])
    res = sub.verify(llm, None, "m", _Spell())
    assert res.ok and "聞き直した" in res.summary and "shape だけ" in res.summary
    assert "「s」で始まる 5 文字" in llm.users[0] and "前に挙げた「form」" in llm.users[1]
    res = sub.verify(Lister(["shave"]), None, "m", _Spell())
    assert not res.ok and res.code == "solver_mismatch"  # 指定に合う別の語だけで、正解が挙がらない
    res = sub.verify(Lister(["form", "figure", "outline"]), None, "m", _Spell())
    assert not res.ok and res.code == "solver_unreadable" and "合わない答えが続いた" in res.summary
    res = sub.verify(Lister(["shapes", "shape"]), None, "m", _Spell())  # 語形違いは聞き直す
    assert res.ok
    # 正解のほかに条件に合う語が挙がった：例文に入るかを判定させる（既定は 2 段階。第 8 回のレビュー 1 章）
    llm = Lister(["shape, sharp"], fits="1: 通らない — 意味が合わない")
    res = sub.verify(llm, None, "m", _Spell())
    assert res.ok and "sharp は例文に入らない" in res.summary and "What sharp is it?" in llm.users[-1]
    res = sub.verify(Lister(["shape, style"], fits="1: 通る — 自然"), None, "m", _Spell())
    assert not res.ok and res.code == "en_verify_multiple" and "style" in res.summary
    # 3 段階のとき、スペルは不自然でも入れば答えが 2 つになる（#1006 の method と manner。第 7 回のレビュー H）
    three = {"fit3": "on"}
    res = sub.verify(Lister(["shape, style"], fits="1: 文法的には入るが不自然 — ふつう言わない"), None, "m", _Spell(),
                     options=three)
    assert not res.ok and res.code == "en_verify_multiple"
    res = sub.verify(Lister(["shape, style"], fits="1: 入らない — 合わない"), None, "m", _Spell(), options=three)
    assert res.ok


# --- 第 5 回のレビューの B（本文の在庫） ------------------------------------------------

def _old_passage(settings, sessions, sandbox):
    """ほかのモデル（old-model）が書き、設問がすべて落ちた本文を 1 つ作る。"""
    from quizapp.db import Passage

    run_job(settings, sessions, sandbox, EnglishOracle(sessions, bad_questions={1, 2}), scope="tmpl:en-notice",
            count=1, gen_model="old-model", verify_model="gemma4:e4b", verify_count=2,
            extra_models_json='{"passage": "old-model"}')
    with sessions() as s:
        return s.execute(select(Passage).order_by(Passage.id)).scalars().first().id


@pytest.mark.parametrize("verdict,reused", [("問題なし", True), ("問題あり", False)])
def test_other_models_passage_is_reviewed_before_reuse(settings, sessions, sandbox, verdict, reused):
    """今の本文作成役以外が書いた本文は、使い回す前に今の見直し役で見直す。誤りがあれば使えない印を付ける（B-1）。"""
    from quizapp.db import Passage

    old = _old_passage(settings, sessions, sandbox)
    llm = EnglishOracle(sessions, passage_verdict=[verdict, "問題なし"])
    # 古い本文は作ったときに e4b の見直しを通っている。今の見直し役（Haiku）ではまだ見ていない
    run_job(settings, sessions, sandbox, llm, scope="tmpl:en-notice", count=2, review_model="anthropic/claude-haiku-5-5",
            gen_model="gemma4:12b-it-qat", verify_model="gemma4:e4b", verify_count=2)
    with sessions() as s:
        row = s.get(Passage, old)
        steps = [e["step"] for e in row.log]
        assert steps[-1] == "使い回す前の本文の見直し" and "設問の点検" not in steps  # 本文の記録だけ
        assert row.status == ("ok" if reused else "rejected")
    ready = problems(sessions, status="ready")
    assert ready and all((p.passage_id == old) == reused for p in ready)
    if reused:
        assert "passage" not in llm.roles()  # 新しい本文は作らない
    # 誤りがあった本文は使わず、ほかの本文（見直しを通ったもの）か新しい本文に設問を付ける
    if reused:
        client = TestClient(create_app(settings, sessions, sandbox))
        md = client.get("/review/export.md?subject=english").text
        assert "使い回す前の本文の見直し" in md


def test_other_models_passage_is_not_reused_without_review(settings, sessions, sandbox):
    old = _old_passage(settings, sessions, sandbox)
    llm = EnglishOracle(sessions)
    run_job(settings, sessions, sandbox, llm, scope="tmpl:en-notice", count=2, explanation_review=False,
            review_model="anthropic/claude-haiku-5-5", gen_model="gemma4:12b-it-qat", verify_model="gemma4:e4b",
            verify_count=2)
    assert all(p.passage_id != old for p in problems(sessions, status="ready")) and "passage" in llm.roles()


def test_rereview_can_review_passages_once(settings, sessions, sandbox):
    """見直しだけをやり直す依頼で、本文の見直しもできる（同じ本文は 1 回だけ。B-2）。書き出しに本文の記録も出る（B-3）。"""
    from quizapp.db import Job, Passage

    run_job(settings, sessions, sandbox, EnglishOracle(sessions), scope="tmpl:en-expository", count=3)
    client = TestClient(create_app(settings, sessions, sandbox))
    client.post("/review/rereview", data={"subject": "english", "status": "ready", "model": "",
                                          "model_text": "anthropic/claude-haiku-5-5", "limit": "3",
                                          "with_passage": "on"})
    with sessions() as s:
        jobrow = s.execute(select(Job).order_by(Job.id.desc())).scalars().first()
        assert jobrow.kind == "review" and jobrow.review_passage
    llm = EnglishOracle(sessions, passage_verdict="問題あり")
    make_worker(settings, sessions, sandbox, llm).run_once()
    assert llm.roles().count("passage_review") == 1  # 3 問とも同じ本文
    ready = problems(sessions, status="ready")
    assert len(ready) == 3 and all(any(e["step"] == "本文の見直しのやり直し" and not e["ok"] for e in p.verification)
                                   for p in ready)
    with sessions() as s:
        assert s.get(Passage, ready[0].passage_id).log[-1]["step"] == "本文の見直しのやり直し"
    assert "本文の見直しのやり直し（anthropic/claude-haiku-5-5）: 誤りあり" in client.get(
        "/review?subject=english&status=reflagged").text
    assert "本文の見直しのやり直し" in client.get("/generate").text or "（本文も）" in client.get("/generate").text


def test_rereview_reapplies_mechanical_checks(settings, sessions, sandbox):
    """見直しのやり直しのときに、今の機械的な点検もかけ直す（第 5 回のレビュー D-2）。"""
    from quizapp.db import Problem

    run_job(settings, sessions, sandbox, EnglishOracle(sessions), scope="tmpl:en-vocab-blank", count=1)
    p = problems(sessions, status="ready")[0]
    with sessions.begin() as s:
        s.get(Problem, p.id).explanation += "\n入らない語の banana は合わない。"
    client = TestClient(create_app(settings, sessions, sandbox))
    client.post("/review/rereview", data={"subject": "english", "status": "ready", "model": "x", "limit": "1"})
    make_worker(settings, sessions, sandbox, EnglishOracle(sessions)).run_once()
    with sessions() as s:
        e = [e for e in s.get(Problem, p.id).verification if e["step"] == "見直しのやり直し"][-1]
    assert not e["ok"] and "機械的な点検" in e["summary"] and "内部の言葉" in e["summary"]


# --- 第 5 回のレビューの D（機械的な点検の追加） -------------------------------------------

def test_review05_more_checks():
    from quizapp.subjects.english import _question_issues

    # D-1：語彙の解説に、例文にも選択肢にもない英単語
    assert check.stray_english("decide は「決める」。decided の形もある。recognize とは違う。",
                               ["We must decide now.", "decide", "borrow"]) == ["recognize"]
    sub = EnglishSubject()
    word = next(w for w in WORDS if w.word == "decide")
    fields = {"sentence": "We need to decide where to go before the summer vacation starts next week.",
              "translation": "来週夏休みが始まる前に、どこへ行くか決める必要がある。",
              "hint": "where to go と一緒に使われている動詞に注目しよう。",
              "explanation": "どこへ行くかを決めるという意味なので decide が入る。choose でも近い意味になる。"}
    assert any("choose" in x for x in sub._vocab_issues(word, [], fields, "en-vocab-blank"))
    # D-3・D-4
    body = "Why Bees Matter\n\nOne reason bees are important is that they help plants grow.\n\nThey also make honey."
    q = {"question": "What is the main reason bees are important?", "options": ["They help plants grow", "They sing",
                                                                                 "They sleep", "They swim"],
         "answer": 0, "wrong": {"B": "contradicts", "C": "contradicts", "D": "contradicts"},
         "evidence": "One reason bees are important is that they help plants grow.",
         "hint": "第1条を読もう。", "explanation": "本文に植物が育つのを助けるとある。ほかの選択肢は本文と合わない内容である。"}
    issues = _question_issues(q, body)
    assert any("第N条" in x for x in issues) and any("One reason" in x for x in issues)


# --- 第 7 回のレビュー（見本集の結果を受けた検証の改善） ------------------------------------

class _Replies:
    """決まった平文を順に返す LLM（見出しの読み取りは本物の parse_tagged を使う）。"""

    def __init__(self, *texts):
        self.texts, self.users, self.think = list(texts), [], []
        self.think_for = None

    def chat_json(self, model, system, user, schema, **kw):
        from quizapp.llm import parse_tagged

        self.users.append(user)
        self.think.append(self.think_for(model) if self.think_for else None)
        return parse_tagged(self.texts.pop(0) if len(self.texts) > 1 else self.texts[0], schema)


_CHOICES = [{"text": "集まる", "correct": False}, {"text": "探す", "correct": False},
            {"text": "見て回る", "correct": True}, {"text": "分け合う", "correct": False}]


@pytest.mark.parametrize("reply,ok", [
    ("4", True), ("4: どれも合わない", True), ("**4**", True), ("なし", True), ("なし\n探すも分け合うも合わない", True),
    ("[[answer]]\n4\n[[reason]]\nどれも合わない", True), ("[[answer]]\nなし\n[[reason]]\n合う語はない", True),
    ("2", False), ("答え: 2\n理由: 探すも文に合う", False), ("2. 探す", False), ("[[answer]]\n(2)\n[[reason]]\n合う", False),
])
def test_none_fit_reads_replies_without_headings(reply, ok):
    """正解を抜いた判定（第 7 回のレビュー A）：見出しなしの「4」・「なし」・番号と選択肢の言葉も読む。
    見本集の測定で、どのモデルでも「判定を読めなかった」になっていた（要確認の大半）。"""
    from quizapp.subjects.english import prompts

    checks, raws = [], []
    EnglishSubject()._judge_without_answer(_Replies(reply), "m", prompts.NONE_FIT_MEANING_SYSTEM,
                                           "I decided to [ browse ] the shelves.", _CHOICES, checks, raws)
    assert checks[0][0] == "正解を抜いた判定" and checks[0][1] is ok, checks
    if not ok:
        assert "探す" in checks[0][2]


def test_none_fit_retries_once_then_needs_a_person():
    """選択肢ごとに説明を書いた長い返事は、最初の数字を拾って取り違えないよう読まずに 1 回だけやり直す。"""
    from quizapp.subjects.english import prompts

    rambling = "1: 集まる → 合わない\n2: 探す → 合わない\n3: 分け合う → 合わない\nよって 4"
    llm = _Replies(rambling, "4")
    checks, raws = [], []
    EnglishSubject()._judge_without_answer(llm, "m", prompts.NONE_FIT_MEANING_SYSTEM, "x", _CHOICES, checks, raws)
    assert checks == [("正解を抜いた判定", True, "ほかの選択肢はどれも合わない")]
    assert "前の出力は形が違って読めませんでした" in llm.users[1] and "[[answer]] の下に番号を 1 つだけ" in llm.users[0]
    checks = []
    EnglishSubject()._judge_without_answer(_Replies(rambling), "m", prompts.NONE_FIT_MEANING_SYSTEM, "x", _CHOICES,
                                           checks, [])
    assert checks[0][1] is None  # 読めなければ「読めなかった」（要確認）


def test_spelling_list_without_headings_is_read():
    """スペルの「条件に合う語」も、見出しなしで語だけ返す返事を読む（#642 の「出力を読めません」）。"""
    res = EnglishSubject().verify(_Replies("shape"), None, "m", _Spell())
    assert res.ok


def test_vocab_three_levels_do_not_reject_awkward_words(settings, sessions, sandbox):
    """語彙の判定を 3 段階に（第 7 回のレビュー C）：「文法的には入るが不自然」な誤答では弾かず、記録に残す（#987）。"""
    add_job(sessions, subject="english", scope="tmpl:en-vocab-blank", count=1,
            options_json='{"fit3": "on", "reading_strict": "off", "reading_think": "model"}')
    make_worker(settings, sessions, sandbox, EnglishOracle(sessions, awkward=True)).run_once()
    p = problems(sessions, status="ready")[0]
    verify = next(e for e in p.verification if e["step"] == "検証")
    assert "文法的には入るが不自然" in verify["detail"]


def test_vocab_two_levels_can_be_chosen(settings, sessions, sandbox):
    """今の方式（2 段階）にも切り替えられる。依頼に記録した検証のやり方で最後まで検証する。"""
    jid = add_job(sessions, subject="english", scope="tmpl:en-vocab-blank", count=1,
                  options_json='{"fit3": "off", "reading_strict": "on", "reading_think": "model"}')
    llm = EnglishOracle(sessions)
    make_worker(settings, sessions, sandbox, llm).run_once()
    assert job(sessions, jid).options["fit3"] == "off"
    p = problems(sessions, status="ready")[0]
    assert "○ 正解が合うか：正解「" in next(e for e in p.verification if e["step"] == "検証")["detail"]


@pytest.mark.parametrize("differs", ["the time", ""])
def test_reading_wrong_options_need_a_reason(settings, sessions, sandbox, differs):
    """作り直した D（第 8 回のレビュー 2-2）：引用を求めるのは「本文と食い違う」だけ、ほかの誤答は「違う点」を 1 行。
    書き方が足りなければ形を示して 1 回だけ聞き直し、それでも足りなくても不合格・要確認にしない（記録に残す）。
    本文にない内容の機械的な照合はしない（#588・#589・#605 を止めすぎた）。"""
    add_job(sessions, subject="english", scope="tmpl:en-notice", count=1,
            options_json='{"fit3": "off", "reading_strict": "all", "reading_think": "model"}')
    llm = EnglishOracle(sessions, absent=differs)
    make_worker(settings, sessions, sandbox, llm).run_once()
    ready = problems(sessions, status="ready")
    assert ready and not [p for p in problems(sessions, status="rejected") if p.reject_code == "needs_check"]
    verify = next(e for e in ready[0].verification if e["step"] == "検証")
    if differs:
        assert "○ 誤答の根拠：どの誤答にも根拠がある" in verify["detail"]
        assert "前の判定は形が足りませんでした" not in verify["raw"]
    else:
        assert "違う点が書かれていない" in verify["detail"]


def test_reason_questions_only_get_the_reason_check():
    """「理由・目的を問う設問だけ」（reason）では、Why / reason などの設問にだけ D をかける。"""
    from quizapp.subjects.english import REASON_RE

    assert REASON_RE.search("Why did the writer go to the park?")
    assert REASON_RE.search("What was the main reason for the change?")
    assert not REASON_RE.search("What is true according to the passage?")

    class P:
        template = "en-notice"
        format = "choice"
        passage = type("Pa", (), {"body": "The library opens at nine. It closes at five."})()
        choices = [{"text": "It opens at nine.", "correct": True, "mistake": None},
                   {"text": "It opens at ten.", "correct": False, "mistake": "detail"}]

    systems = []

    class Spy(_Replies):
        def chat_json(self, model, system, user, schema, **kw):
            systems.append(system)
            return super().chat_json(model, system, user, schema, **kw)

    judged = ('[[judgments]]\n1: 裏付けあり — 引用: "The library opens at nine." — 理由\n'
              '2: 細部の取り違え — 違う点: 時刻 — 理由')
    for question, strict in (("Why does the library open early?", True), ("What is true?", False)):
        systems.clear()
        p = P()
        p.question = question
        EnglishSubject().verify(Spy(judged), None, "m", p, options={"reading_strict": "reason"})
        assert ("違う点" in systems[0]) is strict


def test_reading_reason_check_can_be_turned_off(settings, sessions, sandbox):
    add_job(sessions, subject="english", scope="tmpl:en-notice", count=1,
            options_json='{"fit3": "on", "reading_strict": "off", "reading_think": "model"}')
    llm = EnglishOracle(sessions, absent="library")
    make_worker(settings, sessions, sandbox, llm).run_once()
    assert problems(sessions, status="ready")


def test_reading_think_applies_only_to_reading_judgments():
    """読解の選択肢の判定だけ思考モードを決められる（第 7 回のレビュー E）。終わったら元に戻す。"""
    class P:
        template = "en-notice"
        format = "choice"
        question = "What is true?"
        passage = type("Pa", (), {"body": "The library opens at nine. It closes at five."})()
        choices = [{"text": "It opens at nine.", "correct": True, "mistake": None},
                   {"text": "It opens at ten.", "correct": False, "mistake": "detail"}]

    judged = ('1: 裏付けあり — 引用: "The library opens at nine." — 理由\n'
              '2: 細部の取り違え — 引用: "The library opens at nine." — 理由')
    llm = _Replies(f"[[judgments]]\n{judged}")
    llm.think_for = original = (lambda m: False)
    shuffled = random.getstate()
    random.seed(1)
    res = EnglishSubject().verify(llm, None, "m", P(), options={"reading_think": "on", "reading_strict": "off"})
    random.setstate(shuffled)
    assert llm.think == [True] and llm.think_for is original
    assert res.ok or res.code != "solver_unreadable"


def test_spelling_rivals_are_not_asked():
    """スペル：同じ最初の文字・文字数・品詞で意味が重なる語がある語は出さない（第 7 回のレビュー H）。"""
    from quizapp.subjects.english import spelling_rivals

    severe = next(w for w in WORDS if w.word == "severe")
    assert spelling_rivals(severe) == ["strict"]
    assert spelling_rivals(next(w for w in WORDS if w.word == "decide")) == []


def test_review_checks_that_example_sentences_make_sense(settings, sessions, sandbox):
    """見直しに例文の筋だけの短い確認（第 8 回のレビュー 3-1。#775「予報が正確でなかったので家にいた」）。
    答えを入れた例文を「自然／不自然」で聞き、不自然なら見直しの不合格。"""
    from quizapp.subjects.english import example_sentence

    llm = EnglishOracle(sessions, coherence="不自然")
    run_job(settings, sessions, sandbox, llm, scope="tmpl:en-vocab-blank", count=1)
    p = next(p for p in problems(sessions, status="rejected") if p.reject_code == "en_review")
    review = next(e for e in p.verification if e["step"] == "解説の見直し")
    assert "× 例文の筋：理由と結果がつながらない" in review["detail"]
    assert "(　　　　)" not in example_sentence(p) and p.params["word"] in example_sentence(p)
    assert example_sentence(_Spell()) == "What shape is it?"
    reading = EnglishOracle(sessions)
    run_job(settings, sessions, sandbox, reading, scope="tmpl:en-notice", count=1)
    assert "coherence" not in reading.roles()  # 読解には聞かない


# --- 第 9 回のレビュー（1 対 1 の判定・本文の使い回しの切り替え） --------------------------------

class _Music:
    """#860 の形：誤答「ドーパミンを出す」が、正解「報酬系につながる」と同じ段落の内容を使っている。"""
    template = "en-expository"
    format = "choice"
    question = "Why does listening to music make people feel good?"
    params = {}
    passage = type("Pa", (), {"body": (
        "Music and the Brain\n\nMany people listen to music every day. Listening to music releases dopamine, "
        "which connects to the reward system of the brain. That is why music can make people feel good.\n\n"
        "Some songs also have a tempo that is close to walking speed.")})()
    choices = [{"text": "Because it connects to the reward system of the brain.", "correct": True, "mistake": None},
               {"text": "Because listening releases dopamine in the brain.", "correct": False, "mistake": "partial"},
               {"text": "Because every song has the speed of a heartbeat.", "correct": False, "mistake": "not_stated"},
               {"text": "Because doctors tell people to sing loudly.", "correct": False, "mistake": "not_stated"}]


class _PairLLM:
    """選択肢の判定は正しく（正解だけ裏付けあり）、1 対 1 の判定は pair の答えを返す。"""

    def __init__(self, pair):
        self.pair, self.pair_calls = pair, []

    def chat_json(self, model, system, user, schema, **kw):
        if "成り立つのはどちらか" in system:
            self.pair_calls.append(user)
            items = dict(re.findall(r"^([12]): (.*)$", user, re.M))
            right = next(n for n, t in items.items() if "reward" in t)
            answer = {"both": "両方", "right": right, "wrong": "2" if right == "1" else "1"}.get(self.pair, self.pair)
            return {"answer": answer, "reason": "理由"}
        lines = []
        for n, text in re.findall(r"^(\d+): (.*)$", user, re.M):
            if "reward" in text:
                lines.append(f'{n}: 裏付けあり — 引用: "Listening to music releases dopamine, which connects to the '
                             f'reward system of the brain." — 理由')
            else:
                lines.append(f"{n}: 本文に書かれていない — 理由")
        return {"judgments": "\n".join(lines)}


@pytest.mark.parametrize("pair,code", [("both", "needs_check"), ("wrong", "needs_check"), ("right", ""),
                                       ("よく分からない", "needs_check")])
def test_pair_check_holds_answers_that_share_the_evidence(pair, code):
    """怪しい誤答（根拠の文と前後 1 文の言葉を多く使う誤答）だけを正解と 1 対 1 で比べ、「両方」か誤答を選んだら
    要確認（第 9 回のレビュー 1 章。#860・#796）。正解を選べば通す（#605 のような通すべき問題を止めない）。
    読めなければ 1 回聞き直して要確認。重なりの少ない誤答は比べない。"""
    llm = _PairLLM(pair)
    res = EnglishSubject().verify(llm, None, "m", _Music(), options={"pair_check": "on"})
    assert len(llm.pair_calls) == (2 if pair == "よく分からない" else 1)  # dopamine の誤答だけ
    assert "dopamine" in llm.pair_calls[0] and "heartbeat" not in llm.pair_calls[0]
    assert res.ok is (code == "") and (res.code == code or res.ok)
    assert "1 対 1 の判定" in res.detail


def test_pair_check_only_on_the_first_run_and_can_be_off():
    llm = _PairLLM("both")
    assert EnglishSubject().verify(llm, None, "m", _Music(), options={"pair_check": "on", "_run": "2"}).ok
    assert EnglishSubject().verify(llm, None, "m", _Music(), options={"pair_check": "off"}).ok
    assert llm.pair_calls == []
    # 怪しい誤答の選び方：根拠の文（分からなければ正解といちばん重なる文）と前後 1 文の内容語の割合
    window = check.evidence_window(_Music.passage.body, "", _Music.choices[0]["text"])
    assert "dopamine" in window and "walking speed" not in window
    assert check.overlap_share(_Music.choices[1]["text"], window) == 1.0
    assert check.overlap_share(_Music.choices[2]["text"], window) == 0.0


def test_pair_check_split_goes_to_needs_check_in_a_job(settings, sessions, sandbox, monkeypatch):
    """依頼の中では、1 回目の検証の 1 対 1 で疑いが出れば、2 回目が通っても要確認になる。"""
    from quizapp.subjects.english import check as check_mod

    monkeypatch.setattr(check_mod, "overlap_share", lambda option, window: 1.0 if "museum" in option else 0.0)

    class Oracle(EnglishOracle):
        def chat_json(self, model, system, user, schema, *, temperature=0.7):
            if "成り立つのはどちらか" in system:
                self.calls.append(("pair", model))
                return {"answer": "両方", "reason": "どちらも成り立つ"}
            return super().chat_json(model, system, user, schema, temperature=temperature)

    llm = Oracle(sessions)
    run_job(settings, sessions, sandbox, llm, scope="tmpl:en-notice", count=1)
    held = [p for p in problems(sessions, status="rejected") if p.reject_code == "needs_check"]
    assert held and "pair" in llm.roles()
    entries = [e for e in held[0].verification if e["step"] == "検証"]
    assert "× 1 対 1 の判定" in entries[0]["detail"] and entries[1]["ok"]


def test_passages_can_be_made_new_only(settings, sessions, sandbox, monkeypatch):
    """最終確認用：本文の在庫を使わず、新しく作った本文だけで作る（第 9 回のレビュー 3-2）。"""
    run_job(settings, sessions, sandbox, EnglishOracle(sessions, bad_questions={2, 3}), scope="tmpl:en-notice", count=1)
    from quizapp.db import Passage

    with sessions() as s:
        before = {p.id for p in s.execute(select(Passage)).scalars()}
    used = []
    real = EnglishSubject._reusable_passage
    monkeypatch.setattr(EnglishSubject, "_reusable_passage",
                        lambda self, ctx, c: used.append(c) or real(self, ctx, c))
    # 使い回せる本文が在庫にあっても、「使わない」なら新しい本文で作る（在庫があるときに止まっていた。#145）
    jid = add_job(sessions, subject="english", scope="tmpl:en-notice", count=1,
                  options_json='{"reuse_passages": "off"}')
    make_worker(settings, sessions, sandbox, EnglishOracle(sessions)).run_once()
    assert used == [] and job(sessions, jid).status == "done", job(sessions, jid).error
    with sessions() as s:
        new = [p for p in problems(sessions, status="ready") if p.job_id == jid]
        assert new and all(p.passage_id not in before for p in new)
    jid = add_job(sessions, subject="english", scope="tmpl:en-notice", count=1, options_json='{"reuse_passages": "on"}')
    make_worker(settings, sessions, sandbox, EnglishOracle(sessions)).run_once()
    assert used and job(sessions, jid).status == "done"  # 既定（使い回す）なら在庫を見る


def test_reading_works_when_no_stored_passage_can_be_reused(settings, sessions, sandbox, monkeypatch):
    """在庫の本文がどれも使い回せない（見直しを通らない）ときも、新しい本文で作る（同じ所で止まっていた）。"""
    run_job(settings, sessions, sandbox, EnglishOracle(sessions, bad_questions={2, 3}), scope="tmpl:en-notice", count=1)
    monkeypatch.setattr(EnglishSubject, "_reusable_passage", lambda self, ctx, c: None)
    jid = run_job(settings, sessions, sandbox, EnglishOracle(sessions), scope="tmpl:en-notice", count=1)
    assert job(sessions, jid).status == "done", job(sessions, jid).error


def test_bracketed_judgment_lines_are_read():
    """判定の行を「[[1: 通る — …]]」や「[[」「]]」で囲んだ返事も読む（第 10 回のレビュー 4-5。思考なしの e4b）。"""
    from quizapp.subjects.english import prompts

    got = EnglishSubject._judgments("[[1: 通らない — 理由]]\n[[2: 通る — x]]", prompts.FIT_LABELS, 2)
    assert got == {0: ("通らない", "理由"), 1: ("通る", "x")}
    got = EnglishSubject._judgments('[[\n1: 裏付けあり — 引用: "a" — r\n2: 本文に書かれていない — x\n]]',
                                    prompts.CHOICE_LABELS, 2)
    assert got[0][0] == "裏付けあり" and got[1][0] == "本文に書かれていない"


def test_markdown_marks_are_removed_and_narrator_pronouns_are_checked(settings, sessions, sandbox):
    """本文の Markdown の記号（**・行頭の *）は表記の直しで外す（#1114・#1130）。性別の分からない語り手を
    he・she で呼ぶ設問は点検で引っかける（#1100。第 10 回のレビュー 4-5、第 6 回の 3-4）。"""
    from quizapp import review as review_mod
    from quizapp.db import Passage

    assert check.normalize("**Details:**\n* **Date:** Saturday\nIt is *very* fun. 2 * 3 = 6") == \
        "Details:\n- Date: Saturday\nIt is very fun. 2 * 3 = 6"
    story = "I met my neighbor yesterday. I helped him with his garden, and I felt happy."
    assert check.narrator_pronoun("How did the writer feel when he helped?", ["a", "b", "c", "d"], story)
    assert check.narrator_pronoun("How did the writer feel?", ["He felt sad.", "He felt happy.", "c", "d"], story)
    assert not check.narrator_pronoun("How did the writer feel?", ["The writer felt sad.", "b", "c", "d"], story)
    # 語り手の語のあとにほかの人物を挟んだ his・he は、その人物を指す（フェーズの終わりのレビュー 5）
    assert not check.narrator_pronoun("What did the writer learn from Mr. Tanaka about his past?", ["a", "b", "c", "d"],
                                      story)
    assert not check.narrator_pronoun("How did the writer feel when he helped?", ["a", "b", "c", "d"],
                                      "I am a boy. I met my neighbor and I helped him.")
    run_job(settings, sessions, sandbox, EnglishOracle(sessions), scope="tmpl:en-notice", count=1)
    with sessions.begin() as s:
        p = s.execute(select(Problem).where(Problem.status == "ready")).scalars().first()
        p.passage.body = p.passage.body + "\n\n**Note:** bring a pen."
        pid = p.passage.id
    with sessions.begin() as s:
        assert review_mod.normalize_all(s) >= 1
    with sessions() as s:
        row = s.get(Passage, pid)
        assert "**" not in row.body and "Note: bring a pen." in row.body and row.log[-1]["step"] == "表記の直し"
