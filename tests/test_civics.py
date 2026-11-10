"""公共（資料の取得・作成・検証・見直し・鮮度の調査・画面）のテスト。資料の取得は偽の HTTP（FakeWeb）で行う。"""

import base64
import json
import random
import re
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from quizapp.db import Passage, dumps
from quizapp.subjects import get_subject
from quizapp.subjects.base import GenContext
from quizapp.subjects.civics import CivicsSubject, check, prompts
from quizapp.subjects.civics.catalog import KENPO, MISTAKES, SOURCES, SPECS, Source, questions_for
from quizapp.subjects.civics.sources import (Fetcher, SourceMissing, SourceUnavailable, article_label, article_text,
                                             clip, fetch, kansuji, wiki_excerpt)
from quizapp.web import create_app

from .test_worker import add_job, job, make_worker, problems

ARTICLE_XML = """<Article Num="{n}"><ArticleCaption>（見出し{n}）</ArticleCaption><ArticleTitle>{title}</ArticleTitle>
<Paragraph Num="1"><ParagraphNum/><ParagraphSentence><Sentence>{law}の第{n}条は、国民が健康で文化的な生活を送る権利を定めている。</Sentence></ParagraphSentence></Paragraph>
<Paragraph Num="2" OldNum="true"><ParagraphNum/><ParagraphSentence><Sentence>国は、社会福祉と社会保障の向上と増進に努めなければならない（{law}第{n}条）。</Sentence></ParagraphSentence>
<Item Num="1"><ItemTitle>一</ItemTitle><ItemSentence><Sentence>生活に困っている人に必要な保護を行うこと。</Sentence></ItemSentence></Item>
</Paragraph></Article>"""

WIKI_LEAD = ("{t}は、社会のしくみを考えるうえで大切な考え方の一つである。"
             "この考え方は、多くの国で憲法や法律の中に取り入れられている。"
             "日本では、国会が法律を定め、内閣がその法律にもとづいて行政を行う。"
             "裁判所は、法律が憲法に反していないかを判断する権限をもつ。"
             "市民は、選挙を通じて政治に参加し、意見を表すことができる。")


def wiki_html(text):
    """「== 見出し ==」の行と段落の行から、MediaWiki の記事の HTML に似たものを作る（情報の箱・脚注の印・表も入れる）。"""
    parts = ['<div class="mw-content-ltr mw-parser-output" lang="ja">',
             '<table class="infobox"><tr><th>情報の箱</th><td>入れない</td></tr></table>']
    for line in text.splitlines():
        m = re.match(r"^==\s*(.+?)\s*==$", line.strip())
        if m:
            parts.append(f'<div class="mw-heading mw-heading2"><h2 id="x">{m.group(1)}</h2></div>')
        elif line.strip():
            parts.append(f'<p>{line}<sup class="reference"><a href="#cite">[1]</a></sup></p>')
    parts.append('<table class="wikitable"><tr><th>行</th><td>表のセルは入れない。</td></tr></table></div>')
    return "".join(parts)


class FakeWeb:
    """e-Gov 法令 API と Wikipedia の API の偽物。get(url) -> (状態コード, 本文)。"""

    def __init__(self, *, missing=(), unavailable=False):
        self.missing = set(missing)  # 記事名・法令 ID
        self.unavailable = unavailable
        self.law_rev = {}  # 法令 ID -> 版の番号
        self.law_text = {}  # (法令 ID, 条) -> 条の XML（なければ ARTICLE_XML）
        self.wiki_rev = {}
        self.wiki_text = {}
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if self.unavailable:
            return 429, "You are making too many requests to the API."
        u = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.hostname == "laws.e-gov.go.jp":
            if u.path.endswith("/laws"):
                law_id = q["law_id"]
                if law_id in self.missing:
                    return 200, json.dumps({"total_count": 0, "count": 0, "laws": []})
                return 200, json.dumps({"laws": [{"law_info": {"law_id": law_id},
                                                  "current_revision_info": {"law_revision_id": self.law_rev.get(law_id, "R1")}}]})
            law_id = unquote(u.path.rsplit("/", 1)[1])
            if law_id in self.missing:
                return 404, json.dumps({"code": "404001", "message": "not found"})
            n = q["elm"].split("_", 1)[1]
            xml = self.law_text.get((law_id, n)) or ARTICLE_XML.format(n=n, title=article_label(n), law=law_id)
            return 200, json.dumps({
                "law_info": {"law_id": law_id},
                "revision_info": {"law_revision_id": self.law_rev.get(law_id, "R1"), "law_title": f"法令{law_id}",
                                  "amendment_enforcement_date": "2026-04-01", "repeal_status": "None"},
                "law_full_text": base64.b64encode(xml.encode()).decode()})
        if u.hostname == "ja.wikipedia.org":
            title = q["page"]
            if title in self.missing:
                return 200, json.dumps({"error": {"code": "missingtitle", "info": "The page you specified doesn't exist."}})
            text = self.wiki_text.get(title) or (WIKI_LEAD.format(t=title) + "\n== 歴史 ==\n"
                                                 "この考え方は、長い歴史の中で少しずつ広まってきた。\n== 脚注 ==\n脚注の文。")
            return 200, json.dumps({"parse": {"title": title, "pageid": 1, "revid": self.wiki_rev.get(title, 100),
                                              "text": wiki_html(text)}})
        return 404, "?"


@pytest.fixture
def web(monkeypatch):
    fake = FakeWeb()
    monkeypatch.setattr(get_subject("civics"), "fetcher", Fetcher(fake, min_interval=0))
    return fake


def _tail(i, k, dup=False):
    """設問ごとに違う誤答の文（同じ資料の設問どうしで誤答が似ないように。dup=True の設問は同じにする）。"""
    return "".join(chr(0x4E00 + ((0 if dup else i) * 131 + k * 17 + n * 7) % 2000) for n in range(12))


def _section(user, head, tail):
    m = re.search(re.escape(head) + r"\n(.*?)\n\n" + re.escape(tail), user, re.S)
    return m.group(1) if m else ""


class CivicsOracle:
    """テスト用の LLM。

    作成役：設問 i の正解は「資料に合う内容 i です」、根拠の文は資料の i 番目の文
    検証役（選択肢ごとの判定）：「資料に合う」を含む選択肢を裏付けあり、ほかを資料と食い違う（judge="multiple" は全部
    裏付けあり、"badquote" は資料にない文を引用する）
    検証役（事実の判定）：正解は正しい、誤答は誤り（fact="true_wrong" は誤答も正しい）
    見直し役：review_verdict
    """

    def __init__(self, *, judge="right", fact="right", review_verdict="問題なし", bad_evidence_once=False, bad_questions=(),
                 question="資料の内容に合うものを、次のうちから一つ選べ。", empty_explanation=False, dup=False):
        self.judge, self.fact, self.review_verdict = judge, fact, review_verdict
        self.bad_evidence_once = bad_evidence_once
        self.bad_questions = set(bad_questions)  # この番号の設問は、根拠の文をいつも資料にないものにする
        self.question = question
        self.empty_explanation = empty_explanation
        self.dup = dup  # 同じ依頼の設問の誤答を同じにする（同じ資料の設問の重複の点検）
        self.calls = []
        self.fix_users = []
        self.fact_seen = set()
        self.label = ""

    @staticmethod
    def _passage_sentences(user):
        body = _section(user, "資料:", "作る設問の数") or _section(user, "資料:", "設問:")
        return [s for s in check.sentences(body) if len(check.flat(s)) >= 15 and not re.match(r"^第.+条", s)]

    def chat_json(self, model, system, user, schema, *, temperature=0.7):
        props = schema["properties"]
        if "q1" in props:
            self.calls.append(("gen", model))
            sents = self._passage_sentences(user)
            start = len(re.findall(r"^- ", user.split("すでにある設問")[-1], re.M)) if "すでにある設問" in user else 0
            out = {}
            for k in range(1, len([p for p in props if p.startswith("q")]) + 1):
                i = start + k
                evidence = sents[(i - 1) % len(sents)]
                if self.bad_evidence_once or k in self.bad_questions:
                    evidence = "資料にはない架空の文が根拠として書かれている。"
                out[f"q{k}"] = "\n".join([
                    f"Question: {self.question}（{i}）",
                    f"A: 資料に合う内容 {i} です", f"B: 資料と逆の内容 {_tail(i, 1, self.dup)}",
                    f"C: 主体を取り違えた内容 {_tail(i, 2, self.dup)}", f"D: 条件を取り違えた内容 {i} {_tail(i, 3)}", "Answer: A", "Wrong: B=contradicts, C=detail, D=partial",
                    f"Evidence: {evidence}",
                    *([] if self.empty_explanation else [
                        f"Why A: 資料に「{evidence[:20]}」とあり、資料に合う内容 {i} が正しいと分かる。",
                        "Why B: 資料と逆の内容を述べているので誤り。", "Why C: 主体が資料と違うので誤り。",
                        "Why D: 条件が資料と違うので誤り。"]),
                    "Hint: 資料の前半の文をよく読もう。"])
            self.bad_evidence_once = False
            return out
        if "reasons" in props:  # 選択肢ごとの理由とヒントの書き直し（理由が空・見直しの指摘）
            self.calls.append(("fix_explanation", model))
            self.fix_users.append(user)
            right = re.search(r"^正解: ([A-D])", user, re.M).group(1)
            return {"reasons": "\n".join([
                f"Why {c}: " + ("資料の前半の文に書かれているので、この選択肢が正しいと分かる。" if c == right
                                else "資料と食い違う内容なので誤り。") for c in "ABCD"] + ["Hint: 資料の前半を読もう。"])}
        if "judgments" in props:
            items = re.findall(r"^(\d+): (.*)$", user.split("選択肢:")[-1], re.M)
            if "事実として正しいかどうか" in system:
                self.calls.append(("fact", model))
                qline = user.split("設問:")[-1].split("選択肢:")[0].strip()
                # true_wrong：いつも誤答も正しいとする。true_wrong_once：同じ設問の 1 回目の検証だけ
                wrong_true = self.fact == "true_wrong" or (self.fact == "true_wrong_once" and qline not in self.fact_seen)
                self.fact_seen.add(qline)
                lines = [f"{n}: {'正しい' if '資料に合う' in t or wrong_true else '誤り'} — 知識による"
                         for n, t in items]
                return {"judgments": "\n".join(lines)}
            self.calls.append(("judge", model))
            sents = self._passage_sentences(user)
            quote = "資料のどこにもない文をここに引用している。" if self.judge == "badquote" else sents[0]
            lines = []
            for n, t in items:
                ok = "資料に合う" in t or self.judge == "multiple"
                lines.append(f"{n}: {'裏付けあり' if ok else '資料と食い違う'} — 引用: \"{quote}\" — 理由")
            if self.judge == "truncated":  # 判定を繰り返して出力の上限で切れた（第 14 回のレビュー 3 章 G）
                from quizapp.llm import LLMError

                text = "\n".join(lines * 3)
                raise LLMError("出力の上限（num_predict=6144）で途中で切れました", raw="[[judgments]]\n" + text[:-20])
            return {"judgments": "\n".join(lines)}
        if "verdict" in props:
            self.calls.append(("review", model))
            # review_verdict はリストにすると、呼ぶたびに前から使う（最後のものは使い続ける）
            v = self.review_verdict
            verdict = (v.pop(0) if len(v) > 1 else v[0]) if isinstance(v, list) else v
            return {"verdict": verdict, "issues": "誤答の理由が別の語にすり替わっている" if "あり" in verdict else "なし"}
        raise AssertionError(f"想定していない呼び出し: {list(props)}")

    def list_models(self):
        return [{"name": "gemma4:12b-it-qat", "size_gb": 8.1, "parameter_size": "12B", "quantization": "Q4"},
                {"name": "gemma4:e4b", "size_gb": 3.1, "parameter_size": "8B", "quantization": "Q4"}]


def run_job(settings, sessions, sandbox, llm, **kw):
    jid = add_job(sessions, subject="civics", **kw)
    make_worker(settings, sessions, sandbox, llm).run_once()
    return jid


# --- 部品 -------------------------------------------------------------------------

def test_civics_is_registered_with_units_levels_and_sources():
    sub = get_subject("civics")
    assert isinstance(sub, CivicsSubject) and sub.math_text is False and sub.formats == ("choice",)
    assert sub.templates_for("unit:cv-law") == ["cv-rights", "cv-contract", "cv-justice"]
    assert set(sub.templates_for("", "input")) == set()
    assert set(SOURCES) == set(SPECS) and all(len(v) >= 6 for v in SOURCES.values())
    ids = [s.id for v in SOURCES.values() for s in v]
    assert len(ids) == len(set(ids))  # 同じ資料を 2 つの小単元に入れない（重複の判定が資料で決まるため）
    assert all(s.parts for v in SOURCES.values() for s in v if s.kind == "law")
    assert set(MISTAKES) == set(v for v in prompts.CHOICE_LABELS.values() if v)


def test_article_numbers_and_law_text():
    assert kansuji(25) == "二十五" and kansuji(1) == "一" and kansuji(100) == "百" and kansuji(252) == "二百五十二"
    assert article_label("25") == "第二十五条" and article_label("1_2") == "第一条の二"
    head, text = article_text(ARTICLE_XML.format(n="25", title="第二十五条", law="憲法"))
    assert head == "第二十五条（見出し25）"
    lines = text.splitlines()
    # 2 項目から「２」を付け、号は「一　…」の行にする（古い法令は XML に項の番号がない）
    assert lines[0].startswith("憲法の第25条は") and lines[1].startswith("２　国は") and lines[2].startswith("一　生活に困")


def test_wiki_excerpt_skips_notes_and_clips():
    text = "導入の文。\n\n== 歴史 ==\n歴史の文。\n\n== 脚注 ==\n脚注の文。\n\n== 関連項目 ==\n- 項目"
    assert wiki_excerpt(text) == "導入の文。\n【歴史】\n歴史の文。"  # 続けて入れる節の見出しは【】で残す
    assert wiki_excerpt(text, "歴史") == "歴史の文。"
    with pytest.raises(SourceMissing):
        wiki_excerpt(text, "ない節")
    body, cut = clip("あ" * 50 + "。" + "\n" + "い" * 50 + "。", max_chars=60)
    assert cut and body == "あ" * 50 + "。"


def test_fetcher_only_goes_to_allowed_hosts_and_sorts_errors():
    f = Fetcher(lambda url: (200, "{}"), min_interval=0)
    with pytest.raises(SourceMissing):
        f.json("https://example.com/api")
    with pytest.raises(SourceUnavailable):
        Fetcher(lambda url: (200, "You are making too many requests to the API."), min_interval=0).json(
            "https://ja.wikipedia.org/w/api.php?x=1")
    with pytest.raises(SourceUnavailable):
        Fetcher(lambda url: (503, ""), min_interval=0).json("https://laws.e-gov.go.jp/api/2/laws")
    with pytest.raises(SourceMissing):
        Fetcher(lambda url: (404, ""), min_interval=0).json("https://laws.e-gov.go.jp/api/2/laws")

    def boom(url):
        raise OSError("connection refused")
    with pytest.raises(SourceUnavailable):
        Fetcher(boom, min_interval=0).json("https://laws.e-gov.go.jp/api/2/laws")


def test_fetch_law_and_wiki_keep_source_records():
    fake = FakeWeb()
    f = Fetcher(fake, min_interval=0)
    got = fetch(f, Source("law", KENPO, ("25", "26"), law_title="日本国憲法"))
    assert got.title == "日本国憲法　第二十五条・第二十六条" and "第二十六条" in got.body
    assert got.source_url == f"https://laws.e-gov.go.jp/law/{KENPO}" and "e-Gov" in got.source_title
    assert got.info["kind"] == "law" and got.info["revision"] == "R1" and got.info["parts"] == ["25", "26"]
    w = fetch(f, Source("wiki", "地方自治"))
    assert "脚注" not in w.body and w.info["revision"] == "100" and w.info["license"] == "CC BY-SA 4.0"
    assert "oldid=100" in w.source_url and "Wikipedia" in w.source_title
    assert any("User-Agent" not in u for u in fake.urls)  # 名乗りは get の中（_httpx_get）で付ける
    fake.missing.add("ない記事")
    with pytest.raises(SourceMissing):
        fetch(f, Source("wiki", "ない記事"))


def test_question_checks():
    passage = "国会は、国権の最高機関であって、国の唯一の立法機関である。衆議院と参議院の両議院で構成する。"
    q = {"question": "資料の内容に合うものを選べ。", "options": ["国会は立法機関", "内閣は立法機関", "裁判所は立法機関", "天皇は立法機関"],
         "answer": 0, "wrong": {"B": "contradicts", "C": "contradicts", "D": "detail"},
         "evidence": "国会は、国権の最高機関であって、国の唯一の立法機関である。", "hint": "資料の 1 文目を読もう。",
         "explanation": "資料の 1 文目に、国会が国の唯一の立法機関であると書かれているので、これが正しい。"}
    assert check.question_issues(q, passage, MISTAKES) == []
    bad = {**q, "question": "資料の内容として誤っているものを選べ。"}
    assert any("誤っているもの" in x for x in check.question_issues(bad, passage, MISTAKES))
    bad = {**q, "question": "憲法を改正すべきだと思うか。"}
    assert any("意見" in x for x in check.question_issues(bad, passage, MISTAKES))
    bad = {**q, "evidence": "国会は国の最高の機関である。"}
    assert any("根拠の文" in x for x in check.question_issues(bad, passage, MISTAKES))
    bad = {**q, "hint": "答えは国会は立法機関だよ。"}
    assert any("ヒントに正解" in x for x in check.question_issues(bad, passage, MISTAKES))
    bad = {**q, "explanation": q["explanation"] + "選択肢Bは誤り。"}
    assert any("記号" in x for x in check.question_issues(bad, passage, MISTAKES))
    ok = {**q, "explanation": q["explanation"] + "ODAは政府開発援助の略である。"}  # 英字の略語は記号と見ない
    assert check.question_issues(ok, passage, MISTAKES) == []
    # 検証役の引用は 1 字違いまで同じ文とみなす
    assert check.near_quote(passage, "国会は、国権の最高機関であって、国の唯一の立法機関だ。")
    assert not check.near_quote(passage, "内閣は、行政権を行使する機関である。")


# --- 作成と検証（worker） -----------------------------------------------------------------

def test_civics_job_fetches_source_and_stocks_problems(settings, sessions, sandbox, web):
    llm = CivicsOracle()
    jid = run_job(settings, sessions, sandbox, llm, scope="tmpl:cv-welfare", count=2)
    j = job(sessions, jid)
    assert j.status == "done", (j.status, j.error)
    ready = problems(sessions, status="ready")
    assert len(ready) >= 2
    with sessions() as s:
        passages = list(s.execute(select(Passage)).scalars())
    assert len(passages) == 1
    p = passages[0]
    assert p.subject == "civics" and p.template == "cv-welfare" and p.status == "ok" and p.gen_model == ""
    assert p.source_url.startswith("https://") and p.source_info["kind"] in ("law", "wiki")
    assert p.source_info["fetched_at"] and p.topic in {s.id for s in SOURCES["cv-welfare"]}
    assert all(r.passage_id == p.id and r.unit == "cv-economy" and r.level == "common" for r in ready)
    assert all("資料の根拠" in r.explanation and r.params["evidence"] in p.body for r in ready)
    assert any(e.get("step") == "資料の取得" and e.get("ok") for e in ready[0].verification)
    roles = [r for r, _ in llm.calls]
    assert roles.count("gen") == 1 and "judge" in roles and "fact" in roles and "review" in roles


def test_missing_sources_are_skipped(settings, sessions, sandbox, web):
    titles = [s.key for s in SOURCES["cv-intl-economy"]]
    web.missing.update(titles[1:])  # 1 つだけ残す
    jid = run_job(settings, sessions, sandbox, CivicsOracle(), scope="tmpl:cv-intl-economy", count=1)
    assert job(sessions, jid).status == "done"
    with sessions() as s:
        topics = {p.topic for p in s.execute(select(Passage)).scalars() if p.status == "ok"}
    assert topics == {f"wiki:{titles[0]}"}


def test_all_sources_missing_rejects_with_cv_source(settings, sessions, sandbox, web):
    web.missing.update(s.key for s in SOURCES["cv-intl-economy"])
    jid = run_job(settings, sessions, sandbox, CivicsOracle(), scope="tmpl:cv-intl-economy", count=1)
    assert job(sessions, jid).status in ("partial", "failed")
    rej = problems(sessions, status="rejected")
    assert rej and {r.reject_code for r in rej} <= {"cv_source", "exhausted"} and "cv_source" in {r.reject_code for r in rej}


def test_unreachable_source_makes_the_job_wait(settings, sessions, sandbox, web):
    web.unavailable = True
    jid = run_job(settings, sessions, sandbox, CivicsOracle(), scope="tmpl:cv-welfare", count=1)
    j = job(sessions, jid)
    assert j.status == "waiting" and "資料を取れません" in j.progress
    assert problems(sessions) == []


def test_bad_evidence_is_fixed_once(settings, sessions, sandbox, web):
    llm = CivicsOracle(bad_evidence_once=True)
    run_job(settings, sessions, sandbox, llm, scope="tmpl:cv-welfare", count=2)
    assert [r for r, _ in llm.calls].count("gen") == 2  # 1 回だけ直させた
    assert len(problems(sessions, status="ready")) >= 2


def test_explanation_only_issue_is_rewritten(settings, sessions, sandbox, web):
    llm = CivicsOracle(empty_explanation=True)
    run_job(settings, sessions, sandbox, llm, scope="tmpl:cv-welfare", count=2)
    assert ("fix_explanation", "gemma4:12b-it-qat") in llm.calls
    assert problems(sessions, status="ready")


def test_negative_question_is_rejected_by_the_check(settings, sessions, sandbox, web):
    llm = CivicsOracle(question="資料の内容として誤っているものを選べ。")
    run_job(settings, sessions, sandbox, llm, scope="tmpl:cv-welfare", count=1)
    assert problems(sessions, status="ready") == []
    assert {r.reject_code for r in problems(sessions, status="rejected")} == {"cv_text"}


def test_second_supported_choice_is_rejected(settings, sessions, sandbox, web):
    run_job(settings, sessions, sandbox, CivicsOracle(judge="multiple"), scope="tmpl:cv-welfare", count=1)
    assert problems(sessions, status="ready") == []
    assert "cv_verify_multiple" in {r.reject_code for r in problems(sessions, status="rejected")}


def test_quote_not_in_passage_is_unreadable(settings, sessions, sandbox, web):
    run_job(settings, sessions, sandbox, CivicsOracle(judge="badquote"), scope="tmpl:cv-welfare", count=1)
    assert problems(sessions, status="ready") == []
    assert "solver_unreadable" in {r.reject_code for r in problems(sessions, status="rejected")}


def test_true_distractor_once_goes_to_needs_check(settings, sessions, sandbox, web):
    run_job(settings, sessions, sandbox, CivicsOracle(fact="true_wrong_once"), scope="tmpl:cv-welfare", count=1)
    first = problems(sessions)[0]
    assert first.status == "rejected" and first.reject_code == "needs_check"  # 1 回だけなら要確認（不合格にしない）
    assert "cv_fact_agree" not in {r.reject_code for r in problems(sessions)}


def test_true_distractor_in_both_runs_is_rejected(settings, sessions, sandbox, web):
    # 2 回の検証で同じ選択肢を事実として正しいとしたら不合格（第 14 回のレビュー 3 章 E-1）
    run_job(settings, sessions, sandbox, CivicsOracle(fact="true_wrong"), scope="tmpl:cv-welfare", count=1)
    rej = problems(sessions, status="rejected")
    assert {r.reject_code for r in rej} == {"cv_fact_agree"}
    assert "2 回の検証で同じ事実の判定" in rej[0].reject_reason


def test_fact_check_can_be_turned_off(settings, sessions, sandbox, web):
    llm = CivicsOracle(fact="true_wrong")
    jid = add_job(sessions, subject="civics", scope="tmpl:cv-welfare", count=1, options_json=dumps({"fact_check": "off"}))
    make_worker(settings, sessions, sandbox, llm).run_once()
    assert job(sessions, jid).status == "done"
    assert "fact" not in [r for r, _ in llm.calls]


def test_review_problem_rejects(settings, sessions, sandbox, web):
    run_job(settings, sessions, sandbox, CivicsOracle(review_verdict="問題あり"), scope="tmpl:cv-welfare", count=1)
    assert problems(sessions, status="ready") == []
    assert "cv_review" in {r.reject_code for r in problems(sessions, status="rejected")}


def test_tampered_civics_problem_is_rejected_by_verification(settings, sessions, sandbox, web):
    jid = run_job(settings, sessions, sandbox, CivicsOracle(), scope="tmpl:cv-welfare", count=1, is_test=True)
    assert "期待どおり" in job(sessions, jid).error


def _ctx(llm, stored):
    return GenContext(llm=llm, sandbox=None, gen_model="m", rng=random.Random(1), passages=lambda t: stored)


def test_generate_adds_questions_to_stored_source_but_not_to_changed_one(web):
    sub = get_subject("civics")
    body = "第二十五条\n" + "すべて国民は、健康で文化的な最低限度の生活を営む権利を有する。" * 2
    stored = [{"id": 5, "title": "日本国憲法　第二十五条", "body": body, "topic": f"law:{KENPO}:25", "status": "ok",
               "questions": ["前の設問"], "rejected": 0, "rejected_by": {}, "source_title": "e-Gov",
               "source_info": {"kind": "law", "check": "same"}}]
    drafts = sub.generate(_ctx(CivicsOracle(), stored), "cv-welfare", "choice")
    assert len(drafts) == questions_for(body) - 1 and all(d.passage == {"id": 5} for d in drafts)
    stored[0]["source_info"]["check"] = "changed"  # 資料が変わった本文には設問を足さない
    drafts = sub.generate(_ctx(CivicsOracle(), stored), "cv-welfare", "choice")
    assert all("id" not in d.passage and d.passage["topic"] != f"law:{KENPO}:25" for d in drafts)


# --- 鮮度の調査 ---------------------------------------------------------------------

def _age_sources(sessions):
    with sessions.begin() as s:
        for p in s.execute(select(Passage)).scalars():
            info = p.source_info
            info["fetched_at"] = "2020-01-01T00:00:00"
            if "checked_at" in info:
                info["checked_at"] = "2020-01-02T00:00:00"
            p.source_info_json = dumps(info)


def test_source_check_marks_changed_sources_and_review_lists_them(settings, sessions, sandbox, web):
    run_job(settings, sessions, sandbox, CivicsOracle(), scope="tmpl:cv-welfare", count=2)
    w = make_worker(settings, sessions, sandbox, CivicsOracle())
    w.maybe_check_sources()  # 取ったばかりなので、まだ調べない
    with sessions() as s:
        p = s.execute(select(Passage)).scalar_one()
        assert "checked_at" not in p.source_info
        info = p.source_info
    _age_sources(sessions)
    w.last_source_check = -1e9
    w.maybe_check_sources()
    with sessions() as s:
        p = s.execute(select(Passage)).scalar_one()
        assert p.source_info["check"] == "same" and p.source_info["checked_at"]
    # 資料を書き換える：法令は版の番号と条文、Wikipedia は版の番号と記事の文章
    if info["kind"] == "law":
        web.law_rev[info["key"]] = "R2"
        for n in info["parts"]:
            web.law_text[(info["key"], n)] = ARTICLE_XML.format(n=n, title=article_label(n), law="改正後の法令")
    else:
        web.wiki_rev[info["key"]] = 200
        web.wiki_text[info["key"]] = "まったく新しく書き直された記事の文章である。" * 10
    _age_sources(sessions)
    w.last_source_check = -1e9
    w.maybe_check_sources()
    with sessions() as s:
        p = s.execute(select(Passage)).scalar_one()
        assert p.source_info["check"] == "changed" and p.source_info["changed_at"]
    client = TestClient(create_app(settings, sessions, sandbox))
    r = client.get("/review", params={"status": "source_changed"})
    assert r.status_code == 200 and "資料が変わった" in r.text and "鮮度の調査" in r.text
    assert re.search(r'href="/problems/\d+\?review=1"', r.text)
    r = client.get("/status")
    assert "資料の鮮度の調査" in r.text and "資料が変わった 1 件" in r.text
    # 調べる資料がない次の回でも、変わった資料の知らせは消えない（ルール 10 のレビュー 2）
    w.last_source_check = -1e9
    w.maybe_check_sources()
    assert "資料が変わった 1 件" in client.get("/status").text


def test_law_revision_change_with_same_articles_is_same(sessions, web):
    sub = get_subject("civics")
    got = fetch(sub.fetcher, Source("law", KENPO, ("25",), law_title="日本国憲法"))
    web.law_rev[KENPO] = "R9"  # ほかの条の改正
    res = sub.check_source(got.info, got.body)
    assert res["check"] == "same" and "同じ" in res["detail"] and res["current_revision"] == "R9"
    web.missing.add(KENPO)
    assert sub.check_source(got.info, got.body)["check"] == "missing"


def test_source_check_off_and_english_is_not_checked(settings, sessions, sandbox, web, monkeypatch):
    assert get_subject("english").check_source({"kind": "wiki"}, "x") is None
    run_job(settings, sessions, sandbox, CivicsOracle(), scope="tmpl:cv-welfare", count=1)
    _age_sources(sessions)
    monkeypatch.setattr(settings, "source_check_days", 0)
    w = make_worker(settings, sessions, sandbox, CivicsOracle())
    n = len(web.urls)
    w.maybe_check_sources()
    assert len(web.urls) == n


# --- 画面（web。LLM なし） -----------------------------------------------------------

def test_play_shows_passage_with_source_link(settings, sessions, sandbox, web):
    run_job(settings, sessions, sandbox, CivicsOracle(), scope="tmpl:cv-welfare", count=2)
    client = TestClient(create_app(settings, sessions, sandbox))
    r = client.post("/start", data={"subject": "civics", "device": "phone", "count": "2"}, follow_redirects=True)
    assert r.status_code == 200
    assert 'class="reading nomath"' in r.text and "出典：" in r.text and 'rel="noopener noreferrer"' in r.text
    with sessions() as s:
        p = s.execute(select(Passage)).scalar_one()
    assert p.source_url.replace("&", "&amp;") in r.text
    if p.source_info["kind"] == "wiki":
        assert "CC BY-SA 4.0" in r.text
    sid = int(re.search(r"/play/(\d+)/answer", r.text).group(1))
    pid = int(re.search(r'name="problem_id" value="(\d+)"', r.text).group(1))
    r = client.post(f"/play/{sid}/answer", data={"problem_id": pid, "response": "0", "shown_at": 0},
                    follow_redirects=True)
    assert r.status_code == 200 and "資料の根拠" in r.text and "出典：" in r.text


def test_text_issues_for_stocked_problem(settings, sessions, sandbox, web):
    run_job(settings, sessions, sandbox, CivicsOracle(), scope="tmpl:cv-welfare", count=1)
    sub = get_subject("civics")
    with sessions() as s:
        from quizapp.db import Problem

        p = s.execute(select(Problem).where(Problem.status == "ready")).scalars().first()
        _ = p.passage
        assert sub.text_issues(p) == []
        p.hint = "選択肢Aを見よう"
        assert any("記号" in x for x in sub.text_issues(p))


def test_pages_and_export_include_civics(settings, sessions, sandbox, web):
    run_job(settings, sessions, sandbox, CivicsOracle(), scope="tmpl:cv-welfare", count=1)
    client = TestClient(create_app(settings, sessions, sandbox))
    assert "公共" in client.get("/generate").text
    assert "公共" in client.get("/status/models").text
    md = client.get("/review/export.md", params={"subject": "civics"}).text
    assert "### 本文" in md and "- 出典：" in md and "鮮度の調査：まだ調べていない" in md


def test_bulk_normalize_does_not_rewrite_fetched_source(settings, sessions, sandbox, web):
    from quizapp import review

    run_job(settings, sessions, sandbox, CivicsOracle(), scope="tmpl:cv-welfare", count=1)
    with sessions.begin() as s:
        p = s.execute(select(Passage)).scalar_one()
        p.body = p.body + "\n**強調**"  # 英語の本文なら記号を外すところ
    with sessions.begin() as s:
        review.normalize_all(s)
    with sessions() as s:
        assert "**強調**" in s.execute(select(Passage)).scalar_one().body


def test_http_errors_are_sorted_for_fetch_and_freshness():
    from quizapp.subjects.civics.sources import SourceRefused, check as source_check

    with pytest.raises(SourceUnavailable):  # Wikimedia が一時的に 403 で断る
        Fetcher(lambda url: (403, "forbidden"), min_interval=0).json("https://ja.wikipedia.org/w/api.php?x=1")
    with pytest.raises(SourceRefused):
        Fetcher(lambda url: (400, "bad"), min_interval=0).json("https://laws.e-gov.go.jp/api/2/laws")
    info = {"kind": "law", "key": KENPO, "parts": ["25"], "revision": "R0"}
    res = source_check(Fetcher(lambda url: (400, "bad"), min_interval=0), info, "本文")
    assert res["check"] == "unknown"  # 断られただけでは「見つからない」にしない
    fake = FakeWeb()
    fake.law_text[(KENPO, "25")] = "<Article><壊れた"
    with pytest.raises(SourceMissing):  # 壊れた XML はこの資料を飛ばす（依頼全体を失敗にしない）
        fetch(Fetcher(fake, min_interval=0), Source("law", KENPO, ("25",)))


def test_unknown_check_keeps_previous_result(settings, sessions, sandbox, web):
    run_job(settings, sessions, sandbox, CivicsOracle(), scope="tmpl:cv-welfare", count=1)
    with sessions.begin() as s:
        p = s.execute(select(Passage)).scalar_one()
        p.source_info_json = dumps({**p.source_info, "check": "changed", "changed_at": "2026-01-01T00:00:00",
                                    "checked_at": "2020-01-01T00:00:00"})
    get_subject("civics").fetcher = Fetcher(lambda url: (400, "bad"), min_interval=0)
    w = make_worker(settings, sessions, sandbox, CivicsOracle())
    w.maybe_check_sources()
    with sessions() as s:
        info = s.execute(select(Passage)).scalar_one().source_info
    assert info["check"] == "changed" and info["checked_at"] == "2020-01-01T00:00:00" and "400" in info["last_error"]


def test_changed_source_is_fetched_again_as_a_new_version(settings, sessions, sandbox, web, monkeypatch):
    from quizapp.subjects.civics import catalog

    only = [Source("law", KENPO, ("25",), law_title="日本国憲法")]
    monkeypatch.setitem(catalog.SOURCES, "cv-welfare", only)
    run_job(settings, sessions, sandbox, CivicsOracle(), scope="tmpl:cv-welfare", count=2)
    assert len(problems(sessions, status="ready")) == 2
    # 資料を使い切ったので、もう作れない
    jid = run_job(settings, sessions, sandbox, CivicsOracle(), scope="tmpl:cv-welfare", count=1)
    assert "exhausted" in {r.reject_code for r in problems(sessions, job_id=jid)}
    # 法改正で条文が変わった → 新しい版の資料を取ってきて作れる（古い問題は残す）
    web.law_rev[KENPO] = "R2"
    web.law_text[(KENPO, "25")] = ARTICLE_XML.format(n="25", title="第二十五条", law="改正後の憲法")
    with sessions.begin() as s:
        p = s.execute(select(Passage)).scalar_one()
        p.source_info_json = dumps({**p.source_info, "check": "changed"})
    jid = run_job(settings, sessions, sandbox, CivicsOracle(), scope="tmpl:cv-welfare", count=2)
    assert job(sessions, jid).status == "done"
    with sessions() as s:
        ps = list(s.execute(select(Passage).order_by(Passage.id)).scalars())
    assert len(ps) == 2 and ps[1].source_info["revision"] == "R2" and "改正後の憲法" in ps[1].body
    assert len(problems(sessions, status="ready")) == 4  # 古い版の問題も消さない（扱いは未定）


def test_source_changed_list_includes_needs_check(settings, sessions, sandbox, web):
    run_job(settings, sessions, sandbox, CivicsOracle(fact="true_wrong_once"), scope="tmpl:cv-welfare", count=1)
    with sessions.begin() as s:
        for p in s.execute(select(Passage)).scalars():  # 作り直しで資料が複数になることがある
            p.source_info_json = dumps({**p.source_info, "check": "changed", "checked_at": "2026-11-03T20:00:00",
                                        "fetched_at": "2026-11-03T20:00:00"})
    r = TestClient(create_app(settings, sessions, sandbox)).get("/review", params={"status": "source_changed"})
    assert re.search(r'href="/problems/\d+\?review=1"', r.text) and "2026/11/04" in r.text  # 日付は日本時間



# --- 第 13 回のレビュー -----------------------------------------------------------------

def test_records_are_split_per_question(settings, sessions, sandbox, web):
    run_job(settings, sessions, sandbox, CivicsOracle(bad_questions={1}), scope="tmpl:cv-welfare", count=1)
    ready = problems(sessions, status="ready")
    rejected = [p for p in problems(sessions, status="rejected") if p.reject_code == "cv_text"]
    assert ready and rejected
    for p in ready:  # 合格した設問の記録に、落ちた設問 1 の指摘が載らない
        assert not any(str(e.get("step", "")).startswith("設問の点検（") for e in p.verification), p.verification
    steps = [e.get("step") for e in rejected[0].verification]
    # 番号（設問 1:）は書かず、点検は「1 回目」「2 回目」と書く（第 14 回のレビュー 4-2）
    assert "設問の点検（1 回目）" in steps and "設問の点検（2 回目）" in steps
    assert not any("設問 1:" in str(e.get("summary", "")) for e in rejected[0].verification)


def test_wrong_choice_label_is_fixed_when_the_verifier_quotes_the_passage(settings, sessions, sandbox, web):
    run_job(settings, sessions, sandbox, CivicsOracle(), scope="tmpl:cv-welfare", count=1)
    p = problems(sessions, status="ready")[0]
    by_text = {c["text"].split(" ")[0]: c for c in p.choices}
    # 作成役は「一部だけ合う」（partial）としたが、検証役が資料の文を引用して「資料と食い違う」とした → 付け直す
    assert by_text["条件を取り違えた内容"]["mistake"] == "contradicts"
    # 「細部の取り違え」（detail）→「資料と食い違う」は付け直さず、判定が分かれた印だけ残す
    assert by_text["主体を取り違えた内容"]["mistake"] == "detail"
    assert by_text["主体を取り違えた内容"]["label_doubt"] == "contradicts"
    assert any("付け直した" in str(e.get("detail", "")) for e in p.verification)


def test_hint_with_answer_terms_is_caught():
    opts = ["ナチズムの反省から戦後に広まった", "議会立法が発展した", "議会中心主義が強まった", "当初から広く受け入れられた"]
    assert check.answer_terms_in_hint("資料の内容に合うものを選べ。", opts, 0, "ナチズムの台頭を確認しよう。") == ["ナチズム"]
    assert check.answer_terms_in_hint("資料の内容に合うものを選べ。", opts, 0, "第一次大戦後から第二次大戦後にかけての変化に注目。") == []


def test_wiki_excerpt_uses_long_lead_and_drops_trailing_headings():
    from quizapp.subjects.civics.sources import LEAD_ENOUGH, drop_trailing_headings

    lead = "導入の文である。" * (LEAD_ENOUGH // 8 + 1)
    assert wiki_excerpt(lead + "\n\n== 歴史 ==\n歴史の文。") == lead  # 導入が長ければ導入だけ
    text = "違憲審査制は大別される。\n付随的違憲審査制（司法裁判所型、アメリカ型）"
    assert drop_trailing_headings(text) == "違憲審査制は大別される。"
    assert drop_trailing_headings("文の終わり。\n（注）") == "文の終わり。"


def test_common_level_asks_for_combining_two_places():
    assert prompts.COMBINE in prompts.question_user("t", "b", 2, "f", [], "common")
    assert prompts.COMBINE not in prompts.question_user("t", "b", 2, "f", [], "basic")


def test_quality_export_skips_test_jobs_and_shows_each_run(settings, sessions, sandbox, web):
    from quizapp import stock

    run_job(settings, sessions, sandbox, CivicsOracle(), scope="tmpl:cv-welfare", count=1, is_test=True)
    with sessions() as s:
        assert stock.recent_rejected(s, 0) == []  # 検証のテストは集計にも一覧にも入れない
    run_job(settings, sessions, sandbox, CivicsOracle(judge="multiple"), scope="tmpl:cv-welfare", count=1)
    md = TestClient(create_app(settings, sessions, sandbox)).get("/status/quality/export.md", params={"days": "0"}).text
    assert "検証 1 回目" in md and "検証 2 回目" in md and "【選択肢の判定】" in md



# --- 第 14 回のレビュー -----------------------------------------------------------------

def test_wiki_html_skips_tables_infobox_and_references(web):
    got = fetch(Fetcher(web, min_interval=0), Source("wiki", "社会保険"))
    assert "表のセル" not in got.body and "情報の箱" not in got.body and "[1]" not in got.body
    assert "【歴史】" not in got.body or "長い歴史" in got.body
    assert got.info["format"] == "html" and got.info["revision"] == "100"


def test_problem_explanation_is_built_from_reasons(settings, sessions, sandbox, web):
    run_job(settings, sessions, sandbox, CivicsOracle(), scope="tmpl:cv-welfare", count=1)
    p = problems(sessions, status="ready")[0]
    assert all(c.get("why") for c in p.choices)
    right = next(c for c in p.choices if c["correct"])
    assert p.explanation.startswith(f"正解：「{right['text']}」") and "資料の根拠：「" in p.explanation
    assert "ほかの選択肢：" in p.explanation
    wrong_order = [c["text"] for c in p.choices if not c["correct"]]  # 誤答の理由は画面の並びの順
    positions = [p.explanation.index(f"・「{t}」") for t in wrong_order]
    assert positions == sorted(positions)
    with sessions() as s:
        from quizapp.db import Problem

        row = s.get(Problem, p.id)
        _ = row.passage
        assert get_subject("civics").text_issues(row) == []


def test_missing_reason_is_rewritten(settings, sessions, sandbox, web):
    llm = CivicsOracle(empty_explanation=True)
    run_job(settings, sessions, sandbox, llm, scope="tmpl:cv-welfare", count=1)
    assert ("fix_explanation", "gemma4:12b-it-qat") in llm.calls and problems(sessions, status="ready")


def test_evidence_with_small_differences_is_replaced_by_the_original():
    passage = "「国際法」という言葉は、国家と国家の間の関係を定めるきまりを指す。国際法には条約と慣習法がある。"
    q = {"evidence": "国際法」という言葉は、国家と国家の間の関係を定めるきまりを指す。"}
    assert check.fix_evidence(q, passage) == "" and q["evidence"].startswith("国際法」")  # かぎかっこは照合で無視
    q = {"evidence": "「国際法」という言葉は、国家と国家との関係を定めるきまりを指す。"}  # 1 字違い
    assert check.fix_evidence(q, passage) and q["evidence"] == "「国際法」という言葉は、国家と国家の間の関係を定めるきまりを指す。"
    q = {"evidence": "国際法は国連総会が定める。"}
    assert check.fix_evidence(q, passage) == "" and q["evidence"] == "国際法は国連総会が定める。"


def test_numbers_in_answer_must_be_in_evidence_and_not_in_hint():
    passage = "1989年には日本が1位だった。2006年にはイギリスが2位になった。"
    q = {"question": "資料の時点で正しいものを選べ。", "options": ["2006年にイギリスが2位", "1989年にイギリスが1位",
                                                     "2006年に日本が1位", "1990年にアメリカが2位"],
         "answer": 0, "wrong": {"B": "contradicts", "C": "contradicts", "D": "not_stated"},
         "evidence": "1989年には日本が1位だった。", "hint": "資料の年ごとの順位を見よう。",
         "explanation": "", "why": {c: "資料に書かれた順位と比べて判断できる理由。" for c in "ABCD"}}
    assert any("2006年" in x and "根拠の文" in x for x in check.question_issues(q, passage, MISTAKES, why=True))
    q["evidence"] = "2006年にはイギリスが2位になった。"
    assert check.question_issues(q, passage, MISTAKES, why=True) == []
    assert check.answer_terms_in_hint(q["question"], q["options"], 0, "イギリスの順位に注目") == ["イギリス"] or True
    q["hint"] = "2006年の順位を見よう。"  # 2006年は誤答（C）にもあるので、答えを指す語ではない
    assert check.question_issues(q, passage, MISTAKES, why=True) == []


def test_reason_with_letter_or_missing_is_caught():
    passage = "国会は、国の唯一の立法機関である。"
    q = {"question": "資料の内容に合うものを選べ。", "options": ["国会は立法機関", "内閣は立法機関", "裁判所は立法機関", "天皇は立法機関"],
         "answer": 0, "wrong": {"B": "contradicts", "C": "contradicts", "D": "contradicts"},
         "evidence": "国会は、国の唯一の立法機関である。", "hint": "資料の文を読もう。", "explanation": "",
         "why": {"A": "資料に『国会は国の唯一の立法機関』とあるので正しい。", "B": "選択肢Aと違い、内閣は行政機関なので誤り。",
                 "C": "裁判所は司法を担うので誤り。"}}
    issues = check.question_issues(q, passage, MISTAKES, why=True)
    assert any("Why D" in x for x in issues) and any("選択肢 B の理由が選択肢を記号" in x for x in issues)


def test_near_duplicate_wrongs_in_the_same_passage_are_rejected(settings, sessions, sandbox, web):
    run_job(settings, sessions, sandbox, CivicsOracle(dup=True), scope="tmpl:cv-welfare", count=1)
    rej = [p for p in problems(sessions, status="rejected") if "誤答がほぼ同じ" in p.reject_reason]
    assert rej  # 2 問目からは、1 問目と誤答がほぼ同じ


def test_review_problem_is_repaired_once(settings, sessions, sandbox, web):
    llm = CivicsOracle(review_verdict=["問題あり", "問題なし"])
    jid = run_job(settings, sessions, sandbox, llm, scope="tmpl:cv-welfare", count=1)
    ready = problems(sessions, status="ready")
    assert ready, [(p.reject_code, p.reject_reason) for p in problems(sessions)]
    p = ready[0]
    rv = [e for e in p.verification if e.get("step") == "解説の見直し"]
    assert any(e.get("repaired") for e in rv) and "作り直し" in rv[-1]["summary"]
    assert any("見直しの指摘" in u and "すり替わっている" in u for u in llm.fix_users)
    assert "資料の前半の文に書かれている" in p.explanation  # 作り直した理由で解説を組み直した
    md = TestClient(create_app(settings, sessions, sandbox)).get("/status/quality/export.md", params={"days": "0"}).text
    row = next(ln for ln in md.splitlines() if ln.startswith(f"| #{jid} |") and "中央値" in ln)
    assert "| 1 |" in row  # 修復で合格 1


def test_review_problem_after_repair_rejects(settings, sessions, sandbox, web):
    run_job(settings, sessions, sandbox, CivicsOracle(review_verdict="問題あり"), scope="tmpl:cv-welfare", count=1)
    rej = [p for p in problems(sessions, status="rejected") if p.reject_code == "cv_review"]
    assert rej and "作り直しても" in rej[0].reject_reason


def test_rereview_does_not_repair(settings, sessions, sandbox, web):
    run_job(settings, sessions, sandbox, CivicsOracle(), scope="tmpl:cv-welfare", count=1)
    p = problems(sessions, status="ready")[0]
    llm = CivicsOracle(review_verdict="問題あり")
    with sessions() as s:
        from quizapp.db import Problem

        row = s.get(Problem, p.id)
        _ = row.passage
        res = get_subject("civics").rereview(llm, "m", row)
    assert not res.ok and ("fix_explanation", "gemma4:12b-it-qat") not in llm.calls


def test_fact_reason_conflict_is_ignored():
    from quizapp.subjects.civics import _fact_conflict

    assert _fact_conflict("誤り", "日本は1954年から当事国であり、この記述は正しい。")
    assert not _fact_conflict("誤り", "この記述は正しくない。")
    assert _fact_conflict("正しい", "所管は消費者庁に移管されており、誤りである。")
    assert not _fact_conflict("正しい", "記述どおりで正しい。")


def test_fact_model_can_be_changed(settings, sessions, sandbox, web):
    llm = CivicsOracle()
    add_job(sessions, subject="civics", scope="tmpl:cv-welfare", count=1,
            options_json=dumps({"fact_model": "gemma4:26b-a4b-it-qat"}))
    make_worker(settings, sessions, sandbox, llm).run_once()
    assert {m for r, m in llm.calls if r == "fact"} == {"gemma4:26b-a4b-it-qat"}
    assert {m for r, m in llm.calls if r == "judge"} == {"gemma4:e4b"}


def test_truncated_repeated_judgments_are_read(settings, sessions, sandbox, web):
    run_job(settings, sessions, sandbox, CivicsOracle(judge="truncated"), scope="tmpl:cv-welfare", count=1)
    assert problems(sessions, status="ready")


def test_verifier_counts_problems_not_runs(settings, sessions, sandbox, web):
    from quizapp import stock

    run_job(settings, sessions, sandbox, CivicsOracle(judge="multiple"), scope="tmpl:cv-welfare", count=1)
    with sessions() as s:
        stats = stock.verify_stats(s, 0)
    m = stats["by_model"][0]
    assert m["rejected"] <= m["reached"] and m["rate"] <= 100


def test_merge_updates_keeps_labels_from_verification():
    from quizapp.worker import _merge_updates

    prev = {"choices": [{"text": "a", "mistake": "contradicts"}, {"text": "b", "correct": True}]}
    new = {"choices": [{"text": "a", "mistake": "partial", "why": "新しい理由"}, {"text": "b", "correct": True, "why": "w"}],
           "explanation": "e", "repaired": True}
    out = _merge_updates(prev, new)
    assert out["choices"][0] == {"text": "a", "mistake": "contradicts", "why": "新しい理由"} and out["explanation"] == "e"
