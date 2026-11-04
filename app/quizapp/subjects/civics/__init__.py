"""公共の部品（フェーズ3）。資料（e-Gov の法令・Wikipedia）を取ってきて本文にし、その本文を読んで答える 4 択を作る。

作成：資料の一覧（catalog.SOURCES）から、まだ使っていない資料を取ってくる（sources.py。手を加えずに抜き出す）
      → 作成役が設問を書く → 根拠の文が資料にそのまま入っているかなどを Python で点検（落ちたら 1 回だけ直させる）。
      前に取った資料で設問が足りないものには設問を足す（英語の本文の使い回しと同じ。出典が更新された資料は使わない）
検証：英語の読解と同じく、選択肢を見せて 1 つずつ「裏付けあり／資料と食い違う／…」を引用つきで判定させる
      （正解は見せない。順番は毎回入れ替える）。公共だけの確認として、誤答が「資料に書かれていないが事実として
      正しい」ことがないかを、資料を見せずに知識で判定させる（正しいとされたら要確認）
見直し：解説・ヒントが資料と合っているか、意見が分かれるテーマで立場を決めつけていないか
採点：選択式（選んだ選択肢）。LLM も sandbox も使わない
鮮度の調査：worker が定期的に check_source を呼び、資料が今も同じかを確かめる（docs/civics.md）
公共だけの決めごとの詳細は docs/civics.md。
"""

from __future__ import annotations

import functools
import hashlib
import json
import random
from typing import Any

from .. import choicejudge
from ..base import LLM, Draft, GenContext, Rejected, Sandbox, Subject, Subunit, Unit, VerifyResult
from . import check, prompts
from .catalog import MISTAKES, SOURCES, SPECS, UNITS, Source, questions_for

FIX_ROUNDS = 1  # 点検で落ちたとき、直しを頼む回数
LETTERS = "ABCD"
MAX_FETCH_TRIES = 4  # 1 回の作成で資料の取得を試す数（記事・条が見つからなければ次の資料へ）
_EXPLANATION_ONLY = {"解説が空", "解説が短すぎる"}
ANSWER_CHECK = "正解の裏付け"
SOFT_CHECKS = ("事実として正しい誤答", "正解が事実と食い違う疑い")


def _params_key(template: str, params: dict[str, Any]) -> str:
    raw = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    return template + ":" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def _key(template: str, topic: str, i: int) -> str:
    return _params_key(template, {"topic": topic, "q": i})


@functools.cache
def _units() -> list[Unit]:
    return [Unit(uid, label, tuple(Subunit(s.id, s.label, ("choice",), s.level) for s in specs))
            for uid, label, specs in UNITS]


def _unit_of(template: str) -> str:
    return next(uid for uid, _l, specs in UNITS if any(s.id == template for s in specs))


def _passage_title(problem: Any) -> str:
    p = getattr(problem, "passage", None)
    return getattr(p, "title", "") if p is not None else ""


def _passage_body(problem: Any) -> str:
    p = getattr(problem, "passage", None)
    return getattr(p, "body", "") if p is not None else ""


def _tamper(choices: list[dict[str, Any]], rng: random.Random) -> None:
    """検証のテスト用に、正解の印を誤答の 1 つに移す（検証役が本当の正解を選べば弾かれる）。"""
    right = next(i for i, c in enumerate(choices) if c["correct"])
    wrong = rng.choice([i for i in range(len(choices)) if i != right])
    choices[right] = {**choices[right], "correct": False, "mistake": "contradicts"}
    choices[wrong] = {**choices[wrong], "correct": True, "mistake": None}


class CivicsSubject(Subject):
    id = "civics"
    name = "公共"
    formats = ("choice",)
    mistakes = MISTAKES
    math_text = False  # 「$」を数式として表示しない（資料に金額などが出る）
    reject_codes = {
        "cv_source": "資料を取れなかった（記事・条文が見つからない、短すぎる）",
        "cv_text": "設問の点検で落ちた（根拠の文が資料にない・選択肢の重複・意見を問う設問など）",
        "cv_review": "見直しで誤りを指摘された（解説・ヒントが資料と食い違う、立場の決めつけなど）",
        "cv_review_unread": "見直しの結果を読めなかった",
        "cv_verify_multiple": "検証で、正解のほかにも資料で裏付けられる選択肢があった（正解が 1 つに決まらない）",
    }
    test_format = "choice"  # 検証のテスト：正解の印を誤答に移した問題を流す
    verify_split_check = True  # 2 回の判定が割れたら要確認（英語と同じ）
    verify_options = {
        "fact_check": {"label": "誤答が事実として正しくないかの確認（資料を見せずに知識で判定）", "default": "on",
                       "choices": {"on": "する（正しいとされたら要確認）", "off": "しない"}},
        "reuse_passages": {"label": "資料の使い回し（作成）", "default": "on",
                           "choices": {"on": "前に取った資料に設問を足す", "off": "使わない（新しい資料だけ）"}},
    }
    # 既定のモデルは英語と同じ形から始める（見本集を作ってから見直す。docs/civics.md）
    default_models = {"gen": "gemma4:26b-a4b-it-qat", "verify": "gemma4:e4b-it-qat", "review": "gemma4:12b-it-qat"}
    default_think = {"gemma4:e4b-it-qat": "on", "gemma4:e4b": "on"}

    def __init__(self) -> None:
        self._fetcher: Any = None  # sources.Fetcher（テストでは差し替える）

    @property
    def fetcher(self) -> Any:
        if self._fetcher is None:
            from .sources import Fetcher

            self._fetcher = Fetcher()
        return self._fetcher

    @fetcher.setter
    def fetcher(self, value: Any) -> None:
        self._fetcher = value

    def units(self) -> list[Unit]:
        return _units()

    # --- 作成 ---------------------------------------------------------------

    def remaining(self, template: str, fmt: str, existing: set[str], used_topics: set[str]) -> int | None:
        """まだ使っていない資料 × 2 問（短い条文の設問の数。長い資料は 3 問になる）。"""
        left = [s for s in SOURCES.get(template, []) if s.id not in used_topics
                and _key(template, s.id, 1) not in existing]
        return len(left) * 2

    def generate(self, ctx: GenContext, template: str, fmt: str) -> list[Draft]:
        spec = SPECS[template]
        existing = ctx.existing_keys(template)
        stored = ctx.passages(template)
        reuse = ctx.options.get("reuse_passages", "on") != "off"
        reusable = [p for p in stored if p.get("status") == "ok" and p.get("topic")
                    and len(p["questions"]) < questions_for(p["body"])
                    and (p.get("source_info") or {}).get("check") not in ("changed", "missing")
                    and p.get("rejected_by", {}).get(ctx.gen_model, 0) < 2 * questions_for(p["body"])]
        log: list[dict[str, Any]] = []
        if reusable and reuse:
            src = reusable[0]
            topic, title, body, have = src["topic"], src["title"], src["body"], list(src["questions"])
            passage: dict[str, Any] = {"id": src["id"]}
            source_title = src.get("source_title", "")
            log.append({"step": "資料", "ok": True,
                        "summary": f"前に取った資料 #{src['id']}「{title}」に設問を足す（今 {len(have)} 問）"})
            start = len(have) + src.get("rejected", 0) + 1
        else:
            fetched, src_spec, log = self._fetch_new(ctx, template, stored, existing)
            topic, title, body, have = src_spec.id, fetched.title, fetched.body, []
            source_title = fetched.source_title
            passage = {"title": title, "body": body, "source_title": fetched.source_title,
                       "source_url": fetched.source_url, "template": template, "topic": topic, "status": "ok",
                       "gen_model": "", "source_info": fetched.info, "log": list(log)}
            start = 1
        n = questions_for(body) - len(have)
        params = {"topic": topic, "seed": ctx.rng.randrange(1 << 30), "test": ctx.is_test}
        partial: dict[str, Any] = {"unit": _unit_of(template), "template": template, "format": "choice",
                                   "params": params}
        qs, q_issues = self._write_questions(ctx, spec, title, body, have, n, log, partial)
        drafts = []
        for i, (q, problems) in enumerate(zip(qs, q_issues), start):
            qlog = list(log)
            choices = []
            if 0 <= q["answer"] < len(q["options"]):
                choices = [{"text": t, "correct": j == q["answer"],
                            "mistake": None if j == q["answer"] else q["wrong"].get(LETTERS[j])}
                           for j, t in enumerate(q["options"])]
            if problems:
                summary = "設問の点検で落ちた: " + "／".join(dict.fromkeys(problems))
                drafts.append(self._rejected_draft(template, "cv_text", summary, qlog,
                                                   {**partial, "params": {**params, "q": i}, "question": q["question"],
                                                    "explanation": q["explanation"], "hint": q["hint"],
                                                    "choices": choices}, passage))
                continue
            qlog.append({"step": "設問の点検", "ok": True,
                         "summary": f"根拠の文が資料にある・選択肢 4 つが重複しない（資料 {len(body)} 字）"})
            ctx.rng.shuffle(choices)
            if ctx.is_test:
                _tamper(choices, ctx.rng)
            correct = next(c for c in choices if c["correct"])
            explanation = q["explanation"].strip() + f"\n\n資料の根拠:「{q['evidence']}」"
            drafts.append(Draft(
                unit=_unit_of(template), template=template, format="choice", question=q["question"],
                answer={"kind": "choice"}, answer_display=correct["text"], explanation=explanation, hint=q["hint"],
                choices=choices, source=f"資料 {source_title}・設問 {ctx.gen_model}。正解の根拠の文は資料と照合済み",
                params={**params, "q": i, "evidence": q["evidence"]}, params_key=_key(template, topic, i), log=qlog,
                passage=passage,
            ))
        return drafts

    def _fetch_new(self, ctx: GenContext, template: str, stored: list[dict[str, Any]],
                   existing: set[str]) -> tuple[Any, Source, list[dict[str, Any]]]:
        """まだ使っていない資料を取ってくる。見つからない資料は飛ばして次へ（MAX_FETCH_TRIES まで）。"""
        from ...llm import LLMUnavailable
        from .sources import SourceMissing, SourceUnavailable, fetch

        used = {p.get("topic") for p in stored if p.get("status") != "rejected"}
        left = [s for s in SOURCES.get(template, []) if s.id not in used and _key(template, s.id, 1) not in existing]
        if not left:
            raise Rejected("exhausted", "この小単元の資料をすべて使いました（catalog.SOURCES に資料を足すと作れます）")
        ctx.rng.shuffle(left)
        log: list[dict[str, Any]] = []
        for src in left[:MAX_FETCH_TRIES]:
            try:
                fetched = fetch(self.fetcher, src)
            except SourceUnavailable as exc:  # つながらない・混んでいる：依頼を待たせてやり直す（回数を使わない）
                raise LLMUnavailable(f"資料を取れません：{exc}") from exc
            except SourceMissing as exc:
                log.append({"step": "資料の取得", "ok": False, "summary": f"{src.name}：{exc}（次の資料へ）"})
                continue
            log.append({"step": "資料の取得", "ok": True,
                        "summary": f"{fetched.source_title}（{len(fetched.body)} 字"
                                   + ("、長いので段落で切った" if fetched.info.get("clipped") else "") + "）"
                                   + ("。検証のテストのため、正解の印を誤答に移します" if ctx.is_test else ""),
                        "detail": fetched.source_url})
            return fetched, src, log
        raise Rejected("cv_source", "資料を取れなかった：" + "／".join(e["summary"] for e in log), log,
                       {"unit": _unit_of(template), "template": template, "format": "choice",
                        "question": "（資料を取れなかった）", "explanation": ""})

    def _write_questions(self, ctx: GenContext, spec: Any, title: str, body: str, have: list[str], n: int,
                         log: list[dict[str, Any]], partial: dict[str, Any]) -> tuple[list[dict[str, Any]], list[list[str]]]:
        schema = prompts.question_schema(n)
        user = prompts.question_user(title, body, n, spec.focus, have)
        log.append({"step": "設問の作成", "ok": True, "model": ctx.gen_model,
                    "summary": f"作成役 {ctx.gen_model} が設問を {n} 問書く",
                    "detail": "作成役に送った文章:\n" + user + "\n\n（システムの指示）\n" + prompts.QUESTION_SYSTEM})
        choicejudge.set_label(ctx.llm, "作成")
        out = choicejudge.chat_retry(ctx.llm, ctx.gen_model, prompts.QUESTION_SYSTEM, user, schema)
        qs: list[dict[str, Any]] = []
        q_issues: list[list[str]] = []
        for round_no in range(FIX_ROUNDS + 1):
            qs = [choicejudge.parse_question(check.normalize(str(out.get(f"q{i}") or ""))) for i in range(1, n + 1)]
            q_issues = [check.question_issues(q, body, MISTAKES) for q in qs]
            partial.update(question=qs[0]["question"] if qs else "", explanation=body)
            all_issues = [f"設問 {i}: {x}" for i, xs in enumerate(q_issues, 1) for x in xs]
            if not all_issues:
                break
            last = round_no == FIX_ROUNDS
            summary = "／".join(dict.fromkeys(all_issues))
            log.append({"step": "設問の点検", "ok": False, "summary": summary + ("" if last else "（直しを依頼します）")})
            if last:
                break  # 落ちた設問だけを不合格にし、ほかの設問は先へ進める
            choicejudge.set_label(ctx.llm, "直し")
            out = choicejudge.chat_retry(ctx.llm, ctx.gen_model, prompts.QUESTION_SYSTEM,
                                         prompts.fix_user(user, out, summary, list(schema["properties"])), schema)
        # 解説が空・短すぎるだけなら、設問ごと捨てずに解説だけを書き直させる（英語の第 11 回のレビュー 2-2 と同じ）
        for i, (q, issues) in enumerate(zip(qs, q_issues)):
            if issues and all(x in _EXPLANATION_ONLY for x in issues):
                q_issues[i] = self._rewrite_explanation(ctx, body, q, i + 1, log)
        return qs, q_issues

    def _rewrite_explanation(self, ctx: GenContext, body: str, q: dict[str, Any], no: int,
                             log: list[dict[str, Any]]) -> list[str]:
        from ...llm import LLMError

        choicejudge.set_label(ctx.llm, "直し")
        try:
            out = choicejudge.chat_retry(ctx.llm, ctx.gen_model, prompts.QUESTION_SYSTEM,
                                         prompts.explanation_user(body, q), prompts.EXPLANATION_SCHEMA)
        except LLMError as exc:
            log.append({"step": "解説の書き直し", "ok": False, "summary": f"設問 {no}: 出力を読めなかった（{exc}）"})
            return check.question_issues(q, body, MISTAKES)
        q["explanation"] = check.normalize(str(out.get("explanation") or ""))
        issues = check.question_issues(q, body, MISTAKES)
        log.append({"step": "解説の書き直し", "ok": not issues,
                    "summary": f"設問 {no}: 解説だけを書き直させた" + (f"（まだ: {'／'.join(issues)}）" if issues else "")})
        return issues

    @staticmethod
    def _rejected_draft(template: str, code: str, summary: str, log: list[dict[str, Any]],
                        partial: dict[str, Any], passage: dict[str, Any] | None) -> Draft:
        """同じ資料のほかの設問は検証に進めるので、この設問だけを不合格として返す（worker が資料ごと残す）。"""
        log = log + [{"step": "設問", "ok": False, "summary": summary}]
        return Draft(unit=_unit_of(template), template=template, format="choice",
                     question=partial.get("question", ""), answer={}, answer_display="",
                     explanation=partial.get("explanation", ""), hint=partial.get("hint", ""), log=log,
                     passage=passage, rejected=Rejected(code, summary, log, partial))

    # --- 検証 ---------------------------------------------------------------

    def verify(self, llm: LLM, sandbox: Sandbox, model: str, problem: Any,
               options: dict[str, str] | None = None) -> VerifyResult:
        """選択肢ごとの判定（資料の引用つき。引用は資料と機械的に照らす）と、事実としての判定。"""
        opts = {k: spec["default"] for k, spec in self.verify_options.items()} | (options or {})
        checks: list[tuple[str, bool | None, str]] = []
        raws: list[str] = []
        choices = list(problem.choices)
        random.shuffle(choices)  # 毎回並べ替える（記号の位置で決まらないように）
        self._judge_choices(llm, model, problem, choices, checks, raws)
        if opts.get("fact_check") == "on":
            choicejudge.set_label(llm, "検証（事実）")
            self._judge_facts(llm, model, problem, choices, checks, raws)
        return self._verdict_of(model, checks, raws)

    @staticmethod
    def _verdict_of(model: str, checks: list[tuple[str, bool | None, str]], raws: list[str]) -> VerifyResult:
        detail, raw = choicejudge.check_lines(checks), "\n\n".join(raws)[:4000]
        failed = [(name, text) for name, ok, text in checks if ok is False]
        if failed:
            code = ("solver_mismatch" if any(name == ANSWER_CHECK for name, _t in failed)
                    else "needs_check" if all(name in SOFT_CHECKS for name, _t in failed) else "cv_verify_multiple")
            summary = "／".join(f"{name}（{text[:150]}）" for name, text in failed)
            return VerifyResult(False, f"{model}: {summary[:400]}", detail, code, raw=raw)
        unread = [f"{name}：{text[:120]}" for name, ok, text in checks if ok is None]
        if unread:
            return VerifyResult(False, f"{model}: 判定を読めなかった（{'／'.join(unread)}）", detail,
                                "solver_unreadable", raw=raw)
        return VerifyResult(True, f"{model}: 正解は 1 つに決まる（{len(checks)} つの確認）", detail, raw=raw)

    def _judge_choices(self, llm: LLM, model: str, problem: Any, choices: list[dict[str, Any]],
                       checks: list[tuple[str, bool | None, str]], raws: list[str]) -> None:
        passage = _passage_body(problem)
        choicejudge.set_label(llm, "検証")
        user = prompts.choice_judge_user(_passage_title(problem), passage, problem.question,
                                         [c["text"] for c in choices])
        got: dict[int, tuple[str, str]] = {}
        bad: list[str] = []
        note = ""
        for _ in range(3):  # 読めない・引用が資料にないときはやり直す
            out, raw = choicejudge.ask(llm, model, prompts.CHOICE_VERIFY_SYSTEM, user + note, prompts.JUDGMENTS_SCHEMA)
            raws.append("【選択肢の判定】\n" + raw)
            new = choicejudge.parse_judgments(str(out.get("judgments") or ""), prompts.CHOICE_LABELS, len(choices))
            got = {**got, **new} if note.startswith(choicejudge.MISSING_HEAD) else new
            # 引用を照らすのは「裏付けあり」「一部だけ合う」だけ（正解が 2 つあるのを見つけるのに要る判定）
            bad = [q for lb, reason in got.values() for q in [choicejudge.quote_of(reason, 8)]
                   if lb in ("裏付けあり", "一部だけ合う") and (q and not check.near_quote(passage, q)
                                                            or not q and lb == "裏付けあり")]
            if len(got) >= len(choices) and not bad:
                break
            if bad:
                note = (f"\n\n前の判定の引用「{'」「'.join(q[:60] for q in bad[:2])}」は資料にありません"
                        "（または引用がありません）。資料からそのまま引用してください。")
            elif got:
                note = choicejudge.missing_note(got, len(choices))
            else:
                note = ""
        if len(got) < len(choices):
            checks.append(("選択肢の判定", None, "判定を読めなかった"))
            return
        if bad:
            checks.append(("選択肢の判定", None, f"資料にない文を引用した・引用のない判定が残った（{bad[0][:60] or '引用なし'}）"))
            return
        right = next(i for i, c in enumerate(choices) if c.get("correct"))
        label, reason = got[right]
        checks.append((ANSWER_CHECK, label == "裏付けあり",
                       f"正解「{choices[right]['text'][:60]}」は{label}" + (f"（{reason[:100]}）" if reason else "")))
        also = [i for i, (lb, _r) in got.items() if i != right and lb == "裏付けあり"]
        checks.append(("正解が 1 つか", not also,
                       "ほかに裏付けのある選択肢はない" if not also
                       else "「" + "」「".join(choices[i]["text"][:60] for i in also) + "」も資料で裏付けられる（正解が複数）"))

    def _judge_facts(self, llm: LLM, model: str, problem: Any, choices: list[dict[str, Any]],
                     checks: list[tuple[str, bool | None, str]], raws: list[str]) -> None:
        """資料を見せずに、選択肢が事実として正しいかを判定させる。誤答が「正しい」なら正解が 2 つの疑い（要確認）。
        読めなければ記録に残すだけ（この確認では不合格にしない）。"""
        out, raw = choicejudge.ask(llm, model, prompts.FACT_SYSTEM, prompts.fact_user(problem.question,
                                                                                      [c["text"] for c in choices]),
                                   prompts.JUDGMENTS_SCHEMA)
        raws.append("【事実としての判定】\n" + raw)
        got = choicejudge.parse_judgments(str(out.get("judgments") or ""), prompts.FACT_LABELS, len(choices))
        if len(got) < len(choices):
            checks.append(("事実としての判定", True, "判定を読めなかった（この確認では止めない）"))
            return
        true_wrong = [i for i, (lb, _r) in got.items() if not choices[i].get("correct") and lb == "正しい"]
        right = next(i for i, c in enumerate(choices) if c.get("correct"))
        if true_wrong:
            checks.append(("事実として正しい誤答", False, "「" + "」「".join(
                f"{choices[i]['text'][:50]}」（{got[i][1][:60]}）" for i in true_wrong).rstrip("」") + "」"
                + "は事実として正しい（資料にないだけで、正解が 2 つの疑い）"))
        if got[right][0] == "誤り":
            checks.append(("正解が事実と食い違う疑い", False,
                           f"正解「{choices[right]['text'][:50]}」を知識では誤りとした（{got[right][1][:80]}。資料が古い・読み違いの疑い）"))
        if not true_wrong and got[right][0] != "誤り":
            checks.append(("事実としての判定", True, "誤答に事実として正しいものはない"))

    # --- 見直し -------------------------------------------------------------

    def review(self, llm: LLM, model: str, problem: Any) -> VerifyResult | None:
        """解説・ヒントが資料と合っているか、立場を決めつけていないか。読めなければ 1 回聞き直し、それでも読めなければ不合格。"""
        user = prompts.review_user(_passage_title(problem), _passage_body(problem), problem.question,
                                   [(c["text"], bool(c.get("correct"))) for c in problem.choices],
                                   problem.explanation, problem.hint)
        choicejudge.set_label(llm, "見直し")
        raws = []
        verdict, issues = "", ""
        for attempt in range(2):
            out, raw = choicejudge.ask(llm, model, prompts.REVIEW_SYSTEM, user + (
                "\n\n前の出力は形が違って読めませんでした。[[verdict]] の 1 行目に「問題なし」か「問題あり」だけを書いてください。"
                if attempt else ""), prompts.REVIEW_SCHEMA)
            raws.append(raw)
            verdict, issues = choicejudge.verdict_line(out)
            if verdict.startswith(("問題なし", "問題あり")):
                break
        raw = "\n\n".join(raws)[:4000]
        if verdict.startswith("問題あり"):
            return VerifyResult(False, f"{model}: 誤りを指摘：{issues[:300] or '理由なし'}", issues, "cv_review", raw=raw)
        if verdict.startswith("問題なし"):
            return VerifyResult(True, f"{model}: 誤りは見つからなかった", issues, raw=raw)
        return VerifyResult(False, f"{model}: 見直しの結果を読めなかった", "", "cv_review_unread", raw=raw)

    def rereview(self, llm: LLM, model: str, problem: Any,
                 options: dict[str, str] | None = None) -> VerifyResult | None:
        """見直しのやり直し：今の検証の判定（1 回）と解説の見直しの両方（英語と同じ）。"""
        parts = [("検証の判定", self.verify(llm, None, model, problem, options=options)),
                 ("解説の見直し", self.review(llm, model, problem))]
        bad = [(name, r) for name, r in parts if r is not None and not r.ok]
        detail = "\n".join(f"【{name}】\n{r.detail}" for name, r in parts if r is not None and r.detail)
        raw = "\n\n".join(r.raw for _n, r in parts if r is not None and r.raw)[:4000]
        if bad:
            return VerifyResult(False, "／".join(r.summary for _n, r in bad)[:500], detail, bad[0][1].code, raw=raw)
        return VerifyResult(True, "／".join(r.summary for _n, r in parts if r is not None)[:500], detail, raw=raw)

    # --- 鮮度の調査（worker が定期的に呼ぶ） --------------------------------------

    def check_source(self, info: dict[str, Any], body: str) -> dict[str, Any] | None:
        """資料が今も同じか（sources.check）。つながらなければ SourceUnavailable を ConnectionError として投げる。"""
        from .sources import SourceUnavailable
        from .sources import check as source_check

        if info.get("kind") not in ("law", "wiki"):
            return None
        try:
            return source_check(self.fetcher, info, body)
        except SourceUnavailable as exc:
            raise ConnectionError(str(exc)) from exc

    # --- 問題検証モード（web。LLM は使わない） -----------------------------

    def text_issues(self, problem: Any) -> list[str]:
        choices = list(problem.choices or [])
        if problem.format != "choice" or len(choices) != 4:
            return []
        right = next((i for i, c in enumerate(choices) if c.get("correct")), -1)
        q = {"question": problem.question, "options": [c.get("text", "") for c in choices], "answer": right,
             "wrong": {LETTERS[i]: c.get("mistake") for i, c in enumerate(choices) if not c.get("correct")},
             "evidence": str((problem.params or {}).get("evidence", "")),
             "hint": problem.hint, "explanation": problem.explanation}
        return check.question_issues(q, _passage_body(problem), MISTAKES)

    def normalize_text(self, text: str) -> str:
        return check.normalize(text)
