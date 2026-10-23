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

    作成役（読解）：SENTENCES を本文にし、設問 i の正解は「Right answer i」、根拠の文は本文の i 番目の文
    作成役（語彙）：指定された単語を 1 回だけ使った例文を書く
    本文なしの確認：X（blind_correct に入れた設問番号のときだけ正解の記号を答える）
    検証役：データベースの問題から本当の正解（ずらす前のもの）を選ぶ
    """

    def __init__(self, sessions, *, bad_evidence_once=False, blind_correct=(), review_verdict="問題なし"):
        self.sessions = sessions
        self.bad_evidence_once = bad_evidence_once
        self.blind_correct = set(blind_correct)
        self.review_verdict = review_verdict
        self.calls = []
        self.label = ""

    def chat_json(self, model, system, user, schema, *, temperature=0.7):
        props = schema["properties"]
        if "verdict" in props:
            self.calls.append(("review", model))
            return {"verdict": self.review_verdict, "issues": "なし"}
        if "passage" in props:
            self.calls.append(("gen", model))
            return self._reading(user, props)
        if "sentence" in props:
            self.calls.append(("gen", model))
            word = re.search(r"使う単語: (\S+)（", user).group(1)
            return {"sentence": f"Every student at our school wants to {word} with friends during the long summer.",
                    "translation": "学校の生徒はみな、長い夏の間に友だちと〜したいと思っている。",
                    "hint": "文の後半の with friends に注目しよう。",
                    "explanation": "夏の間に友だちと一緒にすることなので、この語の意味が文に合う。"}
        if set(props) == {"answer"}:  # 本文なしの確認
            self.calls.append(("blind", model))
            m = re.search(r"\(part (\d+)\)", user)
            if m and int(m.group(1)) in self.blind_correct:
                letters = re.findall(r"^([A-D]): (.*)$", user, re.M)
                return {"answer": next(c for c, t in letters if t.startswith("Right"))}
            return {"answer": "X"}
        self.calls.append(("solve", model))
        return self._solve(user)

    def list_models(self):
        return [{"name": "gemma4:12b-it-qat", "size_gb": 8.1, "parameter_size": "12B", "quantization": "Q4"},
                {"name": "gemma4:e4b", "size_gb": 3.1, "parameter_size": "8B", "quantization": "Q4"}]

    def _reading(self, user, props):
        n = int(re.search(r"設問の数: (\d+)", user).group(1))
        lo = int(re.search(r"本文の長さ: (\d+)", user).group(1))
        sentences = list(SENTENCES)
        while check.word_count(" ".join(sentences)) < lo:
            sentences += SENTENCES
        passage = " ".join(sentences[:3]) + "\n\n" + " ".join(sentences[3:])
        out = {"title": "A Visit to the Library", "passage": passage}
        fixing = "直してほしい点" in user
        for i in range(1, n + 1):
            evidence = SENTENCES[i - 1]
            if self.bad_evidence_once and not fixing and i == 1:
                evidence = "This sentence is not in the passage at all."
            out[f"q{i}"] = "\n".join([
                f"Question {i}: What is true according to the passage (part {i})?",
                f"A: Right answer {i} about the library",
                f"B: Wrong answer {i} about a museum",
                f"C: Wrong answer {i} about a park",
                f"D: Wrong answer {i} about a station",
                "Answer: A",
                "Wrong: B=not_stated, C=contradicts, D=detail",
                f"Evidence: {evidence}",
                "Hint: 本文の第1段落を読もう。",
                "Explanation: 本文に図書館のことが書かれているので、図書館についての選択肢が正しい。"
                "博物館・公園・駅のことは本文に書かれていない。",
            ])
        return {k: out[k] for k in props}

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
    assert check.english_issues("これは日本語", "本文") == ["本文に日本語・全角の文字が入っている"]
    assert check.japanese_issues("This is English.", "解説") == ["解説が日本語で書かれていない"]
    assert spelling_pattern("decide") == "d _ _ _ _ _"


# --- 作成と検証（worker） --------------------------------------------------------------

def test_reading_makes_one_passage_with_shared_questions(settings, sessions, sandbox):
    llm = EnglishOracle(sessions)
    jid = run_job(settings, sessions, sandbox, llm, scope="tmpl:en-expository", count=3)
    assert job(sessions, jid).status == "done"
    ready = problems(sessions, status="ready")
    assert len(ready) == 3 and len({p.passage_id for p in ready}) == 1 and ready[0].passage_id is not None
    # 作成 1 回 → 本文なしの確認 3 問分 → 検証 2 回 × 3 問 → 見直し 3 問
    assert [r for r, _ in llm.calls] == ["gen"] + ["blind"] * 3 + ["solve"] * 6 + ["review"] * 3
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
    # 1 つ目の本文の設問 2 を外すと 3 問に足りないので、2 つ目の本文を作る（その設問 2 も外れる）
    assert len(rejected) == 2 and all(p.reject_code == "en_no_passage" for p in rejected)
    assert all("(part 2)" in p.question and len(p.choices) == 4 for p in rejected)
    ready = problems(sessions, status="ready")
    assert len(ready) == 4 and all("(part 2)" not in p.question for p in ready)
    assert len({p.passage_id for p in ready}) == 2


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
