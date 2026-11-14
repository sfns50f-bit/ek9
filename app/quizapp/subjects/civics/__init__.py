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
import re
import threading
from types import SimpleNamespace
from typing import Any

from .. import choicejudge
from ..base import LLM, Draft, GenContext, Rejected, Sandbox, Subject, Subunit, Unit, VerifyResult
from . import check, prompts
from .catalog import MISTAKES, SOURCE_IDS, SOURCES, SPECS, UNITS, Source, questions_for

FIX_ROUNDS = 1  # 点検で落ちたとき、直しを頼む回数
LETTERS = "ABCD"
MAX_FETCH_TRIES = 4  # 1 回の作成で資料の取得を試す数（記事・条が見つからなければ次の資料へ）
def _why_only(issue: str) -> bool:
    """選択肢ごとの理由とヒントを書き直させれば直る指摘（設問ごと捨てない。第 14 回のレビュー 3 章 B・F）。"""
    return (issue.startswith("選択肢 ") and "の理由" in issue) or issue.startswith("ヒント")
ANSWER_CHECK = "正解の裏付け"
SOFT_CHECKS = ("事実として正しい誤答", "正解が事実と食い違う疑い")
FACT_AGREE = "2 回の検証で同じ事実の判定"
SWAP_CHECKS = ("正解が 1 つか", "事実として正しい誤答")  # この確認だけで止まったら、止められた誤答を差し替えて確かめ直す
REPAIR_ROUNDS = 1  # 見直しで落ちたとき、理由・ヒントを作り直させる回数（第 14 回のレビュー 3 章 F-1）
# 事実の判定の理由と結論の食い違い（第 14 回のレビュー 3 章 E-3。理由は正しさを述べているのに結論が「誤り」など）
_FACT_POS = re.compile(r"正しい|事実である|事実です|合っている|そのとおり|その通り|正確で")
_FACT_NEG = re.compile(r"正しくない|誤り|誤っ|事実ではな|ではない|ではありません|違う|違い|異なる|不正確|反する|ない。?$")


def _fact_conflict(label: str, reason: str) -> bool:
    pos, neg = bool(_FACT_POS.search(reason or "")), bool(_FACT_NEG.search(reason or ""))
    return (label == "誤り" and pos and not neg) or (label == "正しい" and neg and not pos)


def _cut(text: str, n: int) -> str:
    """文の切れ目（。・／）で n 字以内に切る。"""
    if len(text) <= n:
        return text
    head = text[:n]
    cut = max(head.rfind("。"), head.rfind("／"))
    return (head[:cut + 1] if cut > n // 3 else head) + "…"


def _params_key(template: str, params: dict[str, Any]) -> str:
    raw = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    return template + ":" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def _key(template: str, topic: str, i: int, revision: str = "") -> str:
    """同じ問題の印（params_key）。資料の版を含める（改正・書き換えのあとの新しい版では、同じ資料からまた作れる）。"""
    return _params_key(template, {"topic": topic, "q": i, "rev": revision})


def _old_extract(p: dict[str, Any], template: str) -> bool:
    """設問を足さない本文（第 16 回のレビュー A-2・A-3）：資料の一覧から外した資料（エリクソンの経歴など）と、
    前の版の取り込み（本文だけの API。見出しが落ちている）で取った Wikipedia の本文。"""
    info = p.get("source_info") or {}
    return p.get("topic") not in SOURCE_IDS.get(template, set()) or (
        info.get("kind") == "wiki" and info.get("format") != "html")


def _view(problem: Any, choices: list[dict[str, Any]], explanation: str) -> Any:
    """選択肢を差し替えた問題（検証・見直しにかける形。データベースの行は変えない）。"""
    return SimpleNamespace(id=getattr(problem, "id", None), question=problem.question, choices=choices,
                           explanation=explanation, hint=problem.hint, passage=getattr(problem, "passage", None),
                           params=getattr(problem, "params", None), gen_model=getattr(problem, "gen_model", ""),
                           status=getattr(problem, "status", ""), format=getattr(problem, "format", "choice"))


def _stale(p: dict[str, Any]) -> bool:
    """鮮度の調査で資料が変わったと分かった本文（設問を足さない。同じ資料の新しい版を取ってきてよい）。"""
    return (p.get("source_info") or {}).get("check") == "changed"


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


_NO_RE = re.compile(r"設問 \d+: ")
_ASKED = "（直しを依頼します）"


def _own_log(log: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    """同じ資料の設問をまとめて点検した記録（「設問 1: …／設問 2: …」）から、k 番目の設問の分だけを残す
    （第 13 回のレビュー：合格した問題に、落ちたほかの設問の指摘が載って見えた）。番号は消し、設問の点検は
    「設問の点検（1 回目）」「（2 回目）」と書く（第 14 回のレビュー 4-2：どの問題の記録か判別できなかった）。"""
    out = []
    rounds = 0
    for e in log:
        text = str(e.get("summary", ""))
        if not _NO_RE.search(text):
            if e.get("step") == "設問の点検" and not e.get("ok"):  # 全部の設問が通った回（この問題は指摘なし）
                continue
            out.append(e)
            continue
        tail = _ASKED if text.endswith(_ASKED) else ""
        parts = [p.removeprefix(f"設問 {k}: ") for p in text.removesuffix(tail).split("／") if p.startswith(f"設問 {k}: ")]
        if not parts:
            continue
        entry = {**e, "summary": "／".join(parts) + tail}
        if e.get("step") == "設問の点検":
            rounds += 1
            entry["step"] = f"設問の点検（{rounds} 回目）"
        out.append(entry)
    return out


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
        "cv_fact_agree": "2 回の検証で、同じ選択肢を同じように事実の判定で止めた（誤答が事実として正しい・正解が事実と違う）",
    }
    verify_overrides = ("cv_fact_agree",)  # 1 回目は要確認で返し、2 回目で同じ判定なら不合格（第 14 回のレビュー 3 章 E-1）
    test_format = "choice"  # 検証のテスト：正解の印を誤答に移した問題を流す
    verify_split_check = True  # 2 回の判定が割れたら要確認（英語と同じ）
    verify_options = {
        "fact_check": {"label": "誤答が事実として正しくないかの確認（知識で判定。資料は参考に見せる）", "default": "on",
                       "choices": {"on": "する（1 回だけなら要確認、2 回とも同じなら不合格）", "off": "しない"}},
        # 事実の判定だけ別のモデルにする（第 14 回のレビュー 3 章 E-4。e4b は知識の古さ・限定語の見落としで誤検出した）
        "fact_model": {"label": "事実の判定のモデル", "default": "verify",
                       "choices": {"verify": "検証役と同じ", "gemma4:12b-it-qat": "gemma4:12b-it-qat",
                                   "gemma4:26b-a4b-it-qat": "gemma4:26b-a4b-it-qat",
                                   "anthropic/claude-haiku-5-5": "anthropic/claude-haiku-5-5（API）"}},
        # 検証の 1 回目で「資料で裏付けられる・事実として正しい」とされた誤答だけを作成役に作り直させ、確かめ直す
        # （第 16 回のレビュー 3 章 B。作成の途中の問題だけ。見本集の測定・見直しのやり直しではしない）
        "swap_wrong": {"label": "誤りになっていない誤答の差し替え（検証の 1 回目）", "default": "on",
                       "choices": {"on": "する（その誤答だけ作り直して確かめ直す）", "off": "しない（そのまま判定する）"}},
        "reuse_passages": {"label": "資料の使い回し（作成）", "default": "on",
                           "choices": {"on": "前に取った資料に設問を足す", "off": "使わない（新しい資料だけ）"}},
    }
    # 既定のモデルは英語と同じ形から始める（見本集を作ってから見直す。docs/civics.md）
    default_models = {"gen": "gemma4:26b-a4b-it-qat", "verify": "gemma4:e4b-it-qat", "review": "gemma4:12b-it-qat"}
    default_think = {"gemma4:e4b-it-qat": "on", "gemma4:e4b": "on"}

    def __init__(self) -> None:
        self._fetcher: Any = None  # sources.Fetcher（テストでは差し替える）
        # 1 回目の検証の事実の判定で止めた選択肢（(問題, モデル) → {(種類, 選択肢の文)}）。2 回目と照らす（E-1）
        self._fact_memo: dict[tuple[Any, str], set[tuple[str, str]]] = {}
        # 検証の 1 回目で誤答を差し替えた問題（問題 → (選択肢, 解説)）。2 回目の検証と見直しは差し替えた後の形で行う
        self._swap_memo: dict[Any, tuple[list[dict[str, Any]], str]] = {}
        self._memo_lock = threading.Lock()

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
        """まだ使っていない資料 × 2 問（短い条文の設問の数。長い資料は 3 問になる）。資料が変わって取り直せるものは数えない。"""
        left = [s for s in SOURCES.get(template, []) if s.id not in used_topics]
        return len(left) * 2

    def generate(self, ctx: GenContext, template: str, fmt: str) -> list[Draft]:
        spec = SPECS[template]
        existing = ctx.existing_keys(template)
        stored = ctx.passages(template)
        reuse = ctx.options.get("reuse_passages", "on") != "off"
        reusable = [p for p in stored if p.get("status") == "ok" and p.get("topic")
                    and len(p["questions"]) < questions_for(p["body"])
                    and (p.get("source_info") or {}).get("check") not in ("changed", "missing")
                    and not _old_extract(p, template)
                    and p.get("rejected_by", {}).get(ctx.gen_model, 0) < 2 * questions_for(p["body"])]
        log: list[dict[str, Any]] = []
        if reusable and reuse:
            src = reusable[0]
            topic, title, body = src["topic"], src["title"], src["body"]
            have = list(src.get("details") or src["questions"])  # 前の設問の選択肢も見せる（同じ誤答を作らない）
            passage: dict[str, Any] = {"id": src["id"]}
            source_title = src.get("source_title", "")
            revision = str((src.get("source_info") or {}).get("revision", ""))
            fetched_at = str((src.get("source_info") or {}).get("fetched_at", ""))[:10]
            log.append({"step": "資料", "ok": True,
                        "summary": f"前に取った資料 #{src['id']}「{title}」に設問を足す（今 {len(have)} 問）"})
            start = len(have) + src.get("rejected", 0) + 1
        else:
            fetched, src_spec, log = self._fetch_new(ctx, template, stored, existing)
            topic, title, body, have = src_spec.id, fetched.title, fetched.body, []
            source_title = fetched.source_title
            revision = str(fetched.info.get("revision", ""))
            fetched_at = str(fetched.info.get("fetched_at", ""))[:10]
            passage = {"title": title, "body": body, "source_title": fetched.source_title,
                       "source_url": fetched.source_url, "template": template, "topic": topic, "status": "ok",
                       "gen_model": "", "source_info": fetched.info, "log": list(log)}
            start = 1
        n = questions_for(body) - len(have)
        params = {"topic": topic, "seed": ctx.rng.randrange(1 << 30), "test": ctx.is_test}
        partial: dict[str, Any] = {"unit": _unit_of(template), "template": template, "format": "choice",
                                   "params": params}
        qs, q_issues = self._write_questions(ctx, spec, title, body, have, n, log, partial, fetched_at)
        drafts = []
        for i, (q, problems) in enumerate(zip(qs, q_issues), start):
            qlog = _own_log(log, i - start + 1)
            choices = []
            if 0 <= q["answer"] < len(q["options"]):
                choices = [{"text": t, "correct": j == q["answer"],
                            "mistake": None if j == q["answer"] else q["wrong"].get(LETTERS[j]),
                            "why": q["why"].get(LETTERS[j], "").strip()}
                           for j, t in enumerate(q["options"])]
            if problems:
                summary = "設問の点検で落ちた: " + "／".join(dict.fromkeys(problems))
                drafts.append(self._rejected_draft(template, "cv_text", summary, qlog,
                                                   {**partial, "params": {**params, "q": i}, "question": q["question"],
                                                    "explanation": check.build_explanation(choices, q["evidence"])
                                                    if choices else q["explanation"], "hint": q["hint"],
                                                    "choices": choices}, passage))
                continue
            qlog.append({"step": "設問の点検", "ok": True,
                         "summary": f"根拠の文が資料にある・選択肢 4 つが重複しない（資料 {len(body)} 字）"})
            ctx.rng.shuffle(choices)
            if ctx.is_test:
                _tamper(choices, ctx.rng)
            correct = next(c for c in choices if c["correct"])
            # 解説は選択肢ごとの理由から組み立てる（並べ替えた後の順。第 14 回のレビュー 3 章 B）
            explanation = check.build_explanation(choices, q["evidence"])
            drafts.append(Draft(
                unit=_unit_of(template), template=template, format="choice", question=q["question"],
                answer={"kind": "choice"}, answer_display=correct["text"], explanation=explanation, hint=q["hint"],
                choices=choices, source=f"資料 {source_title}・設問 {ctx.gen_model}。正解の根拠の文は資料と照合済み",
                params={**params, "q": i, "evidence": q["evidence"], "revision": revision},
                params_key=_key(template, topic, i, revision), log=qlog,
                passage=passage,
            ))
        return drafts

    def _fetch_new(self, ctx: GenContext, template: str, stored: list[dict[str, Any]],
                   existing: set[str]) -> tuple[Any, Source, list[dict[str, Any]]]:
        """まだ使っていない資料を取ってくる。見つからない資料は飛ばして次へ（MAX_FETCH_TRIES まで）。"""
        from ..base import WaitForSource
        from .sources import SourceMissing, SourceUnavailable, fetch

        # 使った資料（落ちた本文と、鮮度の調査で変わったと分かった本文の資料は、また取ってきてよい。新しい版で作る）
        used = {p.get("topic") for p in stored if p.get("status") != "rejected" and not _stale(p)}
        left = [s for s in SOURCES.get(template, []) if s.id not in used]
        if not left:
            raise Rejected("exhausted", "この小単元の資料をすべて使いました（catalog.SOURCES に資料を足すと作れます）")
        ctx.rng.shuffle(left)
        log: list[dict[str, Any]] = []
        for src in left[:MAX_FETCH_TRIES]:
            try:
                fetched = fetch(self.fetcher, src)
            except SourceUnavailable as exc:  # つながらない・混んでいる：依頼を待たせてやり直す（回数を使わない）
                raise WaitForSource(f"資料を取れません：{exc}") from exc
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

    def _write_questions(self, ctx: GenContext, spec: Any, title: str, body: str, have: list[Any], n: int,
                         log: list[dict[str, Any]], partial: dict[str, Any],
                         fetched_at: str = "") -> tuple[list[dict[str, Any]], list[list[str]]]:
        schema = prompts.question_schema(n)
        user = prompts.question_user(title, body, n, spec.focus, have, spec.level, fetched_at)
        log.append({"step": "設問の作成", "ok": True, "model": ctx.gen_model,
                    "summary": f"作成役 {ctx.gen_model} が設問を {n} 問書く",
                    "detail": "作成役に送った文章:\n" + user + "\n\n（システムの指示）\n" + prompts.QUESTION_SYSTEM})
        choicejudge.set_label(ctx.llm, "作成")
        out = choicejudge.chat_retry(ctx.llm, ctx.gen_model, prompts.QUESTION_SYSTEM, user, schema)
        qs: list[dict[str, Any]] = []
        q_issues: list[list[str]] = []
        for round_no in range(FIX_ROUNDS + 1):
            qs = [choicejudge.parse_question(check.normalize(str(out.get(f"q{i}") or ""))) for i in range(1, n + 1)]
            fixed = [f"設問 {i}: {x}" for i, q in enumerate(qs, 1) for x in [check.fix_evidence(q, body)] if x]
            if fixed:
                log.append({"step": "設問の機械的な直し", "ok": True, "summary": "／".join(fixed)})
            q_issues = [check.question_issues(q, body, MISTAKES, why=True) + self._duplicates(qs, k, have)
                        for k, q in enumerate(qs)]
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
        # 理由・ヒントだけの指摘なら、設問ごと捨てずに理由とヒントだけを書き直させる（英語の第 11 回のレビュー 2-2 と同じ考え）
        for i, (q, issues) in enumerate(zip(qs, q_issues)):
            if issues and all(_why_only(x) for x in issues):
                q_issues[i] = self._rewrite_whys(ctx, body, q, i + 1, log, "／".join(issues))
        return qs, q_issues

    @staticmethod
    def _duplicates(qs: list[dict[str, Any]], k: int, have: list[Any]) -> list[str]:
        """同じ資料のほかの設問（この依頼で先に書いた設問・前からある設問）と、誤答が 2 つ以上ほぼ同じなら指摘する。"""
        q = qs[k]
        if not 0 <= q["answer"] < len(q["options"]):
            return []
        mine = [o for j, o in enumerate(q["options"]) if j != q["answer"]]
        others = [[o for j, o in enumerate(p["options"]) if j != p["answer"]] for p in qs[:k]]
        others += [list(h.get("choices") or []) for h in have if isinstance(h, dict)]
        for other in others:
            same = check.same_wrongs(mine, other)
            if len(same) >= 2:
                return [f"同じ資料のほかの設問と誤答がほぼ同じ（「{same[0][:30]}」など。別の誤り方の誤答にする）"]
        return []

    def _rewrite_whys(self, ctx: GenContext, body: str, q: dict[str, Any], no: int,
                      log: list[dict[str, Any]], note: str = "", model: str = "") -> list[str]:
        """選択肢ごとの理由とヒントだけを書き直させる（設問・選択肢・正解は変えない）。残った指摘を返す。"""
        from ...llm import LLMError

        choicejudge.set_label(ctx.llm, "直し")
        try:
            out = choicejudge.chat_retry(ctx.llm, model or ctx.gen_model, prompts.QUESTION_SYSTEM,
                                         prompts.why_user(body, q, note), prompts.WHY_SCHEMA)
        except LLMError as exc:
            log.append({"step": "理由の書き直し", "ok": False, "summary": f"設問 {no}: 出力を読めなかった（{exc}）"})
            return check.question_issues(q, body, MISTAKES, why=True)
        got = choicejudge.parse_question(check.normalize(str(out.get("reasons") or "")))
        q["why"] = {**q.get("why", {}), **got["why"]}
        if got["hint"]:
            q["hint"] = got["hint"]
        issues = check.question_issues(q, body, MISTAKES, why=True)
        log.append({"step": "理由の書き直し", "ok": not issues,
                    "summary": f"設問 {no}: 選択肢ごとの理由とヒントだけを書き直させた"
                               + (f"（まだ: {'／'.join(issues)}）" if issues else "")})
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
        """選択肢ごとの判定（資料の引用つき。引用は資料と機械的に照らす）と、事実としての判定。
        1 回目の検証で誤答だけが止められたら、その誤答を作成役に作り直させて確かめ直す（_swap_wrong）。"""
        opts = {k: spec["default"] for k, spec in self.verify_options.items()} | (options or {})
        pid = getattr(problem, "id", None) or id(problem)
        first = str(opts.get("_run", "1")) == "1"
        with self._memo_lock:
            if first:
                self._swap_memo.pop(pid, None)  # 待機・再起動のあとは 1 回目からやり直す
            swapped = self._swap_memo.get(pid)
        if swapped:
            problem = _view(problem, *swapped)
        res, flags = self._verify_once(llm, model, problem, opts)
        if not (first and flags and opts.get("swap_wrong") == "on" and getattr(problem, "status", "") == "draft"
                and getattr(problem, "gen_model", "") and not (getattr(problem, "params", None) or {}).get("test")):
            return res
        fixed = self._swap_wrong(llm, problem, flags)
        if isinstance(fixed, str):  # 作り直せなかった理由も記録に残す（第 17 回のレビュー：成績の書き出しから分からなかった）
            return VerifyResult(res.ok, _cut(res.summary, 600) + f"／誤答の差し替えはできなかった（{fixed}）",
                                res.detail + f"\n誤答の差し替え：作り直せなかった（{fixed}。そのまま判定）",
                                res.code, raw=res.raw, updates=res.updates)
        view, note, updates = fixed
        res2, _flags = self._verify_once(llm, model, view, opts)
        detail = f"{res.detail}\n誤答の差し替え：{note}\n【差し替えた後の判定】\n{res2.detail}"
        raw = (res.raw + "\n\n【誤答を差し替えた後】\n" + res2.raw)[:4000]
        if not res2.ok:  # 差し替えても止まった：差し替える前の形のまま、前の判定で返す
            if str(opts.get("_runs", "2")) != "1":
                with self._memo_lock:  # 2 回目と照らす事実の判定は、差し替える前の問題のもの
                    self._fact_memo[(pid, model)] = {(k, t) for k, t in self._first_hits}
            return VerifyResult(False, _cut(res.summary, 600) + "／誤答を差し替えても止まった（" + _cut(res2.summary, 200) + "）",
                                detail + "\n（差し替えた後も止まったので、差し替えない）", res.code, raw=raw)
        with self._memo_lock:
            if len(self._swap_memo) >= 500:
                self._swap_memo.pop(next(iter(self._swap_memo)))
            self._swap_memo[pid] = (view.choices, view.explanation)
        merged = {**updates, **res2.updates} if "choices" not in res2.updates else {**updates, **res2.updates,
                                                                                 "explanation": updates["explanation"]}
        return VerifyResult(True, f"{res2.summary}。{note}", detail, raw=raw, updates=merged)

    def _verify_once(self, llm: LLM, model: str, problem: Any,
                     opts: dict[str, str]) -> tuple[VerifyResult, dict[str, str]]:
        """検証 1 回分。(結果, 差し替えられる誤答 {選択肢の文: 検証役の指摘})。差し替えられるのは、正解は裏付けられていて、
        誤答が「資料で裏付けられる・事実として正しい」とされたことだけで止まったとき。"""
        checks: list[tuple[str, bool | None, str]] = []
        raws: list[str] = []
        updates: dict[str, Any] = {}
        flags: dict[str, str] = {}
        choices = list(problem.choices)
        random.shuffle(choices)  # 毎回並べ替える（記号の位置で決まらないように）
        self._judge_choices(llm, model, problem, choices, checks, raws, updates, flags)
        if "choices" in updates:  # 付け直した選択肢は、問題に保存した順に戻す（解説は保存した順で組み立ててある）
            by_text = {c["text"]: c for c in updates["choices"]}
            updates["choices"] = [by_text.get(c["text"], c) for c in problem.choices]
        self._first_hits: set[tuple[str, str]] = set()
        if opts.get("fact_check") == "on":
            choicejudge.set_label(llm, "検証（事実）")
            fact_model = model if opts.get("fact_model", "verify") in ("", "verify") else opts["fact_model"]
            hits = self._judge_facts(llm, fact_model, problem, choices, checks, raws, flags)
            key = (getattr(problem, "id", None) or id(problem), model)
            with self._memo_lock:
                if str(opts.get("_run", "1")) == "1":
                    self._first_hits = hits
                    if str(opts.get("_runs", "2")) != "1":  # 検証が 1 回だけなら 2 回目と照らさないので残さない
                        if len(self._fact_memo) >= 500:  # 取り消し・待機で 2 回目が来なかった分がたまらないように
                            self._fact_memo.pop(next(iter(self._fact_memo)))
                        self._fact_memo[key] = hits
                else:
                    same = hits & self._fact_memo.pop(key, set())
                    if same:
                        checks.append((FACT_AGREE, False, "／".join(f"「{t[:50]}」（{k}）" for k, t in sorted(same))))
        stopped = [name for name, ok, _t in checks if ok is not True]
        if not stopped or any(name not in SWAP_CHECKS for name in stopped):
            flags = {}
        return self._verdict_of(model, checks, raws, updates), flags

    def _swap_wrong(self, llm: LLM, problem: Any, flags: dict[str, str]) -> tuple[Any, str, dict[str, Any]] | str:
        """止められた誤答だけを作成役に作り直させる（設問・正解・ほかの選択肢は変えない）。
        (差し替えた問題（検証にかける形）, 説明, 問題に書き込む項目)。作り直せない・点検で落ちたら、その理由（文字列）。"""
        from ...llm import LLMError

        choices = [dict(c) for c in problem.choices]
        right = next((i for i, c in enumerate(choices) if c.get("correct")), -1)
        bad = {LETTERS[i]: flags[c["text"]] for i, c in enumerate(choices) if c["text"] in flags and i != right}
        if right < 0 or len(choices) != 4 or not bad:
            return "選択肢の形が違う"
        body = _passage_body(problem)
        evidence = str((getattr(problem, "params", None) or {}).get("evidence", ""))
        q = {"question": problem.question, "options": [c["text"] for c in choices], "answer": right,
             "wrong": {LETTERS[i]: c.get("mistake") for i, c in enumerate(choices) if not c.get("correct")},
             "evidence": evidence, "hint": problem.hint, "explanation": problem.explanation,
             "why": {LETTERS[i]: c.get("why", "") for i, c in enumerate(choices) if c.get("why")}}
        choicejudge.set_label(llm, "直し")
        try:
            out = choicejudge.chat_retry(llm, problem.gen_model, prompts.QUESTION_SYSTEM,
                                         prompts.swap_user(body, q, bad), prompts.SWAP_SCHEMA)
        except LLMError as exc:
            return f"作成役の出力を読めなかった：{exc}"[:120]
        finally:
            choicejudge.set_label(llm, "検証")
        got = choicejudge.parse_question(check.normalize(str(out.get("options") or "")))
        notes = []
        for c in bad:
            i = LETTERS.index(c)
            new, why = got["options"][i].strip(), got["why"].get(c, "").strip()
            old = choices[i]["text"]
            if not new or not why:
                return f"選択肢 {c} の新しい文・理由が出力にない"
            if check.similar(new, old, 0.9) or any(
                    check.similar(new, o["text"], 0.9) for j, o in enumerate(choices) if j != i):
                return f"新しい誤答が前の文・ほかの選択肢とほぼ同じ（「{new[:30]}」）"
            mistake = got["wrong"].get(c)
            choices[i] = {"text": new, "correct": False, "mistake": mistake if mistake in MISTAKES else "contradicts",
                          "why": why}
            q["options"][i], q["why"][c], q["wrong"][c] = new, why, choices[i]["mistake"]
            notes.append(f"「{old[:40]}」→「{new[:40]}」")
        issues = check.question_issues(q, body, MISTAKES, why=bool(q["why"]))
        if issues:
            return "設問の点検：" + _cut("／".join(issues), 150)
        explanation = check.build_explanation(choices, evidence) if q["why"] else problem.explanation
        note = f"誤答を差し替えた（{problem.gen_model}）：" + "、".join(notes)
        return _view(problem, choices, explanation), note, {"choices": choices, "explanation": explanation,
                                                           "swapped": True}

    @staticmethod
    def _verdict_of(model: str, checks: list[tuple[str, bool | None, str]], raws: list[str],
                    updates: dict[str, Any] | None = None) -> VerifyResult:
        detail, raw = choicejudge.check_lines(checks), "\n\n".join(raws)[:4000]
        failed = [(name, text) for name, ok, text in checks if ok is False]
        if failed:
            code = ("solver_mismatch" if any(name == ANSWER_CHECK for name, _t in failed)
                    else "cv_fact_agree" if any(name == FACT_AGREE for name, _t in failed)
                    else "needs_check" if all(name in SOFT_CHECKS for name, _t in failed) else "cv_verify_multiple")
            summary = "／".join(f"{name}（{_cut(text, 200)}）" for name, text in failed)
            return VerifyResult(False, f"{model}: {_cut(summary, 700)}", detail, code, raw=raw)
        unread = [f"{name}：{text[:120]}" for name, ok, text in checks if ok is None]
        if unread:
            return VerifyResult(False, f"{model}: 判定を読めなかった（{'／'.join(unread)}）", detail,
                                "solver_unreadable", raw=raw)
        relabel = "。誤答の種類を付け直した" if updates else ""
        return VerifyResult(True, f"{model}: 正解は 1 つに決まる（{len(checks)} つの確認）{relabel}", detail, raw=raw,
                            updates=updates or {})

    def _judge_choices(self, llm: LLM, model: str, problem: Any, choices: list[dict[str, Any]],
                       checks: list[tuple[str, bool | None, str]], raws: list[str],
                       updates: dict[str, Any] | None = None, flags: dict[str, str] | None = None) -> None:
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
            # 出力が繰り返して上限で途切れたとき（第 14 回のレビュー 3 章 G。#1515・#1484）も、届いた分から最初の
            # まとまりを読む（番号ごとに最初の判定を使う）
            text = str(out.get("judgments") or "") if out else raw
            new = choicejudge.parse_judgments(text, prompts.CHOICE_LABELS, len(choices))
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
        if flags is not None:
            flags.update({choices[i]["text"]: f"資料で裏付けられる（{got[i][1][:150]}）" for i in also})
        if updates is not None and not also and label == "裏付けあり":
            self._relabel(passage, choices, got, checks, updates)

    @staticmethod
    def _relabel(passage: str, choices: list[dict[str, Any]], got: dict[int, tuple[str, str]],
                 checks: list[tuple[str, bool | None, str]], updates: dict[str, Any]) -> None:
        """誤答の種類の付け直し（第 13 回のレビュー：作成役が「一部だけ合う」「資料に書かれていない」とした誤答が、
        実は資料と食い違っていた）。検証役が「資料と食い違う」とし、食い違う資料の文を引用していて、その引用が資料に
        あるときだけ付け直す（判定はモデルごとにばらつくので、引用で裏付けられるときだけ。英語の第 5 回のレビュー C-1 と
        同じ考え方）。それ以外の食い違いは記録に残すだけ（label_doubt。問題検証モードの書き出しに出る）。"""
        fixed, doubts, new = [], [], []
        for i, c in enumerate(choices):
            code = prompts.CHOICE_LABELS.get(got[i][0], "")
            mine = c.get("mistake")
            if c.get("correct") or not code or code == mine:
                new.append({k: v for k, v in c.items() if k != "label_doubt"})
                continue
            quote = choicejudge.quote_of(got[i][1], 8)
            if code == "contradicts" and mine in ("partial", "not_stated") and quote and check.near_quote(passage, quote):
                fixed.append(f"「{c['text'][:40]}」{MISTAKES.get(str(mine), mine)}→{MISTAKES[code]}（引用「{quote[:40]}」）")
                c = {k: v for k, v in c.items() if k != "label_doubt"} | {"mistake": code}
            else:
                doubts.append(f"「{c['text'][:40]}」作成役 {MISTAKES.get(str(mine), mine)}／検証役 {MISTAKES[code]}")
                c = {**c, "label_doubt": code}
            new.append(c)
        if fixed or doubts:
            updates["choices"] = new
        if fixed:  # 判定が分かれただけ（付け直さない）は記録に出さない（label_doubt に残る。第 14 回のレビュー 4-3）
            checks.append(("誤答の種類", True, "付け直した：" + "、".join(fixed)))

    def _judge_facts(self, llm: LLM, model: str, problem: Any, choices: list[dict[str, Any]],
                     checks: list[tuple[str, bool | None, str]], raws: list[str],
                     flags: dict[str, str] | None = None) -> set[tuple[str, str]]:
        """選択肢が事実として正しいかを判定させる（資料と取得した日は参考に見せる。資料に書かれた経緯は事実として扱う）。
        誤答が「正しい」・正解が「誤り」なら要確認。理由と結論が食い違う判定は使わない（E-3）。読めなければ記録に
        残すだけ。止めた選択肢を {(種類, 選択肢の文)} で返す（2 回目の検証と照らす）。"""
        passage = getattr(problem, "passage", None)
        info = getattr(passage, "source_info", None) or {}
        out, raw = choicejudge.ask(llm, model, prompts.FACT_SYSTEM, prompts.fact_user(
            problem.question, [c["text"] for c in choices], _passage_body(problem),
            str(info.get("fetched_at", ""))[:10]), prompts.JUDGMENTS_SCHEMA)
        raws.append(f"【事実としての判定】（{model}）\n" + raw)
        text = str(out.get("judgments") or "") if out else raw
        got = choicejudge.parse_judgments(text, prompts.FACT_LABELS, len(choices))
        if len(got) < len(choices):
            checks.append(("事実としての判定", True, "判定を読めなかった（この確認では止めない）"))
            return set()
        dropped = [i for i, (lb, reason) in got.items() if _fact_conflict(lb, reason)]
        for i in dropped:
            got[i] = ("分からない", got[i][1])
        right = next(i for i, c in enumerate(choices) if c.get("correct"))
        true_wrong = [i for i, (lb, _r) in got.items() if not choices[i].get("correct") and lb == "正しい"]
        hits: set[tuple[str, str]] = set()
        if true_wrong:
            hits |= {("誤答が事実として正しい", choices[i]["text"]) for i in true_wrong}
            if flags is not None:
                for i in true_wrong:
                    flags.setdefault(choices[i]["text"], f"事実として正しい（{got[i][1][:150]}）")
            checks.append(("事実として正しい誤答", False, "「" + "」「".join(
                f"{choices[i]['text'][:50]}」（{got[i][1][:60]}）" for i in true_wrong).rstrip("」") + "」"
                + "は事実として正しい（資料にないだけで、正解が 2 つの疑い）"))
        if got[right][0] == "誤り":
            hits.add(("正解が事実と違う", choices[right]["text"]))
            checks.append(("正解が事実と食い違う疑い", False,
                           f"正解「{choices[right]['text'][:50]}」を知識では誤りとした（{got[right][1][:80]}。資料が古い・読み違いの疑い）"))
        note = f"（理由と結論が食い違う判定 {len(dropped)} つは使わなかった）" if dropped else ""
        if not hits:
            checks.append(("事実としての判定", True, "誤答に事実として正しいものはない" + note))
        elif note:
            checks.append(("事実としての判定", True, note.strip("（）")))
        return hits

    # --- 見直し -------------------------------------------------------------

    def _review_once(self, llm: LLM, model: str, problem: Any) -> tuple[str, str, str]:
        """見直し役に 1 回聞く（読めなければ 1 回聞き直す）。(判定, 指摘, 生の出力)。"""
        user = prompts.review_user(_passage_title(problem), _passage_body(problem), problem.question,
                                   list(problem.choices), problem.explanation, problem.hint,
                                   str((getattr(problem, "params", None) or {}).get("evidence", "")))
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
        return verdict, issues, "\n\n".join(raws)

    def review(self, llm: LLM, model: str, problem: Any) -> VerifyResult | None:
        """理由・ヒントが資料と合っているか、立場を決めつけていないか。読めなければ 1 回聞き直し、それでも読めなければ不合格。
        「問題あり」なら、作成の途中の問題（下書き）は、指摘を作成役に渡して理由とヒントだけを作り直させ、もう一度見直す
        （REPAIR_ROUNDS 回まで。正解が 1 つに決まることは検証で確かめてあるので、問題ごと捨てない。第 14 回のレビュー 3 章 F-1）。"""
        with self._memo_lock:  # 検証で誤答を差し替えた問題は、差し替えた後の形を見直す
            swapped = self._swap_memo.pop(getattr(problem, "id", None) or id(problem), None)
        if swapped:
            problem = _view(problem, *swapped)
        return self._review(llm, model, problem, repair=getattr(problem, "status", "") == "draft")

    def _review(self, llm: LLM, model: str, problem: Any, repair: bool) -> VerifyResult:
        verdict, issues, raw = self._review_once(llm, model, problem)
        history: list[str] = []
        current, updates = problem, {}
        for _ in range(REPAIR_ROUNDS if repair and getattr(problem, "gen_model", "") else 0):
            if not verdict.startswith("問題あり"):
                break
            fixed = self._repair(llm, current, issues)
            history.append(f"指摘：{_cut(issues, 300) or '理由なし'}" + (f" → 作り直し：{fixed[1]}" if fixed else " → 作り直せなかった"))
            if fixed is None:
                break
            current, updates = fixed[0], fixed[2]
            verdict, more, raw2 = self._review_once(llm, model, current)
            raw += "\n\n【作り直した後の見直し】\n" + raw2
            issues = more
        raw = raw[:4000]
        detail = "\n".join(history + [f"最後の指摘：{issues}" if verdict.startswith("問題あり") else issues])
        if verdict.startswith("問題なし"):
            if updates:
                return VerifyResult(True, f"{model}: 指摘を受けて理由・ヒントを作り直し、誤りは見つからなかった", detail, raw=raw,
                                    updates={**updates, "repaired": True})
            return VerifyResult(True, f"{model}: 誤りは見つからなかった", issues, raw=raw)
        if verdict.startswith("問題あり"):
            if updates:
                # 作り直した後の 2 度目の指摘は、不合格にせず要確認にする（第 16 回のレビュー 3 章 D-1：1 度目の指摘は
                # 直っていて、2 度目は別の細かい指摘・見直し役の読み違いが多かった。問題検証モードで確かめて決める）
                return VerifyResult(False, f"{model}: 理由・ヒントを作り直した後も指摘（要確認）：{_cut(issues, 600) or '理由なし'}",
                                    detail, "needs_check", raw=raw)
            return VerifyResult(False, f"{model}: 誤りを指摘：{_cut(issues, 600) or '理由なし'}", detail, "cv_review",
                                raw=raw)
        return VerifyResult(False, f"{model}: 見直しの結果を読めなかった", detail, "cv_review_unread", raw=raw)

    def _repair(self, llm: LLM, problem: Any, issues: str) -> tuple[Any, str, dict[str, Any]] | None:
        """見直しの指摘を作成役に渡して、選択肢ごとの理由とヒントだけを作り直させる（設問・選択肢・正解は変えない）。
        (直した問題（見直しにかける形）, 説明, 問題に書き込む項目)。機械的な点検で落ちたら None。"""
        choices = [dict(c) for c in problem.choices]
        right = next((i for i, c in enumerate(choices) if c.get("correct")), -1)
        if right < 0 or len(choices) != 4:
            return None
        evidence = str((problem.params or {}).get("evidence", ""))
        q = {"question": problem.question, "options": [c["text"] for c in choices], "answer": right,
             "wrong": {LETTERS[i]: c.get("mistake") for i, c in enumerate(choices) if not c.get("correct")},
             "evidence": evidence, "hint": problem.hint, "explanation": problem.explanation,
             "why": {LETTERS[i]: c.get("why", "") for i, c in enumerate(choices) if c.get("why")}}
        log: list[dict[str, Any]] = []
        ctx = SimpleNamespace(llm=llm, gen_model=problem.gen_model)
        left = self._rewrite_whys(ctx, _passage_body(problem), q, 1, log, "見直しの指摘：" + issues)
        if left:
            return None
        for i, c in enumerate(choices):
            c["why"] = q["why"].get(LETTERS[i], "").strip()
        explanation = check.build_explanation(choices, evidence)
        fixed = SimpleNamespace(question=problem.question, choices=choices, explanation=explanation, hint=q["hint"],
                                passage=getattr(problem, "passage", None), params=problem.params,
                                gen_model=problem.gen_model, status="")
        choicejudge.set_label(llm, "見直し")
        return fixed, f"{problem.gen_model} が理由とヒントを書き直した", {"choices": choices, "explanation": explanation,
                                                                       "hint": q["hint"]}

    def rereview(self, llm: LLM, model: str, problem: Any,
                 options: dict[str, str] | None = None) -> VerifyResult | None:
        """見直しのやり直し：今の検証の判定（1 回）と解説の見直しの両方（英語と同じ）。"""
        parts = [("検証の判定", self.verify(llm, None, model, problem, options=options)),
                 ("解説の見直し", self._review(llm, model, problem, repair=False))]  # やり直しでは作り直さない
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
             "hint": problem.hint, "explanation": problem.explanation,
             "why": {LETTERS[i]: c["why"] for i, c in enumerate(choices) if c.get("why")}}
        return check.question_issues(q, _passage_body(problem), MISTAKES)

    def normalize_text(self, text: str) -> str:
        return check.normalize(text)
