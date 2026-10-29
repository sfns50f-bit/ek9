"""数学の部品（テンプレート方式）。

作成: テンプレート（Python）が数値・正解・誤答を作る → 誤答が正解・互いと一致しないことを
      sandbox の sympy で確認 → LLM が問題文・解説・ヒントを書く → 問題文にテンプレートの
      式・値が、解説に正解がそのまま入っているかを機械的に確認
検証: 検証役に問題文だけを渡して解かせ（選択肢も正解も見せない）、答えを sandbox の sympy で
      正解と同値判定する
採点: 選択式は選んだ選択肢、キーボード入力は sandbox の sympy で同値判定（LLM は使わない）
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import random
import re
from typing import Any

from ... import latexfix, textcheck
from ..base import LLM, Draft, GenContext, Grade, Rejected, Sandbox, Subject, Subunit, Unit, VerifyResult
from . import prompts
from .catalog import MISTAKES, UNITS, template_info

logger = logging.getLogger(__name__)
N_DISTRACTORS = 3
MAX_ROLLS = 30
FIX_ROUNDS = 1  # 文章の確認で落ちたとき、直しを頼む回数


def _params_key(template: str, params: dict[str, Any]) -> str:
    raw = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    return template + ":" + hashlib.sha1(raw.encode()).hexdigest()[:16]


@functools.cache
def _units() -> list[Unit]:
    """catalog の単元に、templates.py の @template で登録した小単元（テンプレート）を並べる。"""
    info = template_info()
    return [Unit(uid, label, tuple(Subunit(i.id, i.label, i.formats, i.level, i.related)
                                   for i in info.values() if i.unit == uid))
            for uid, label in UNITS]


class MathSubject(Subject):
    id = "math"
    name = "数学"
    formats = ("choice", "input")
    mistakes = MISTAKES
    reject_codes = {"answer_leak": "問題文・ヒントに正解が書かれている",
                    "text_format": "表記の崩れ（KaTeX にない命令・$ の対応・数式の中の日本語・中国語の字など）",
                    "explanation_math": "解説の途中式の誤り（数どうしの = が等しくない）",
                    "explanation_review": "解説の見直しで誤りを指摘された（辺の対応・途中式・言葉の説明）"}
    test_format = "input"  # 検証のテスト（正解をずらした問題）に対応する

    def units(self) -> list[Unit]:
        return _units()

    # --- 作成 ---------------------------------------------------------------

    def _instance(self, ctx: GenContext, template: str, fmt: str) -> tuple[Any, list[dict[str, Any]], str]:
        """数値を選び、選択式なら誤答を 3 つ選ぶ。(Instance, 選択肢, params_key)。"""
        import sympy as sp

        from .templates import TEMPLATES

        build = TEMPLATES[template]
        existing = ctx.existing_keys(template)
        last_error = "同じ数値の問題がすでにあります"
        for _ in range(MAX_ROLLS):
            seed = ctx.rng.randrange(1 << 30)
            inst = build(random.Random(seed))
            key = _params_key(template, inst.params)
            if key in existing:
                continue
            if ctx.is_test:
                inst.answer = _tamper(inst.answer, inst.kind)
            inst.params = {**inst.params, "seed": seed, "test": ctx.is_test}
            if fmt != "choice":
                return inst, [], key
            values = [inst.answer] + [c.value for c in inst.candidates]
            res = ctx.sandbox.distinct(inst.kind, [sp.srepr(v) for v in values], inst.var)
            if not res.get("ok"):
                last_error = f"選択肢を確認できません: {res.get('error')}"
                continue
            equal = {tuple(sorted(p)) for p in res.get("equal_pairs", [])}
            chosen: list[int] = []
            texts = {textcheck.normalize(inst.fmt(inst.answer))}
            for i in range(1, len(values)):
                if (0, i) in equal or any((j, i) in equal for j in chosen):
                    continue
                if inst.valid is not None and not inst.valid(values[i]):
                    continue  # ありえない値（1 を超える確率、負の面積など）の誤答は出さない
                text = textcheck.normalize(inst.fmt(values[i]))
                if text in texts:  # 見た目が同じものも重複とみなす
                    continue
                texts.add(text)
                chosen.append(i)
                if len(chosen) == N_DISTRACTORS:
                    break
            if len(chosen) < N_DISTRACTORS:
                last_error = "正解・互いと一致しない誤答を 3 つ作れません"
                continue
            choices = [{"text": f"${inst.fmt(inst.answer)}$", "correct": True, "mistake": None}]
            choices += [{"text": f"${inst.fmt(values[i])}$", "correct": False,
                         "mistake": inst.candidates[i - 1].mistake} for i in chosen]
            ctx.rng.shuffle(choices)
            return inst, choices, key
        raise Rejected("choices" if "誤答" in last_error or "選択肢" in last_error else "exhausted", last_error)

    def generate(self, ctx: GenContext, template: str, fmt: str) -> Draft:
        import sympy as sp

        unit = self.subunit_map()[template][0].id
        inst, choices, key = self._instance(ctx, template, fmt)
        answer_latex = inst.fmt(inst.answer)
        log: list[dict[str, Any]] = [{
            "step": "テンプレート", "ok": True,
            "summary": f"Python で数値と正解を作成（正解 ${answer_latex}$）"
                       + ("。検証のテストのため正解をわざとずらしています" if ctx.is_test else ""),
        }]
        if choices:
            mistakes = "、".join(MISTAKES.get(c["mistake"], c["mistake"]) for c in choices if not c["correct"])
            log.append({"step": "選択肢", "ok": True,
                        "summary": f"誤答 3 つが正解とも互いとも一致しないことを sympy で確認（{mistakes}）"})
        partial = {"unit": unit, "template": template, "format": fmt, "params": inst.params, "params_key": key}

        # 検証のテストでは、解き方の要点（本当の正解が入っている）を渡さない。
        # 渡すと解説が本当の正解で書かれ、検証より前の「解説の確認」で落ちてしまい、テストにならない
        steps = [] if ctx.is_test else inst.steps
        user = prompts.gen_user(inst.task, [textcheck.first(g) for g in inst.givens], answer_latex, steps, fmt)
        # 作成役に送った文章をそのまま残す（表記が崩れたとき、送った側か受け取った側かを見分けるため）
        log[0]["detail"] = "作成役に送った文章:\n" + user[:4000] + "\n\n（システムの指示）\n" + prompts.GEN_SYSTEM
        out = self._chat(ctx, prompts.GEN_SYSTEM, user)
        # 文章の確認で落ちたら、同じ数値のまま、指摘した点だけを直させる（作り直しより速く、成功しやすい）
        for round_no in range(FIX_ROUNDS + 1):
            # 意味を変えずに直せる表記の崩れ（度の記号の揺れ、\\sin、文字どおりの \n など）は機械的に直す
            question, explanation, hint = (latexfix.normalize(textcheck.repair_latex(str(out.get(k) or "").strip()))
                                           for k in ("question", "explanation", "hint"))
            partial.update(question=question, explanation=explanation, hint=hint)
            problem = _check_text(inst, answer_latex, question, explanation, hint, ctx.sandbox,
                                  sp.srepr(inst.answer), choice=fmt == "choice", is_test=ctx.is_test)
            if problem is None:
                break
            code, step, summary = problem
            log.append({"step": step, "ok": False, "summary": summary + ("（直しを依頼します）" if round_no < FIX_ROUNDS else "")})
            if round_no == FIX_ROUNDS:
                raise Rejected(code, summary, log, partial)
            if hasattr(ctx.llm, "label"):
                ctx.llm.label = "直し"
            out = self._chat(ctx, prompts.GEN_SYSTEM, prompts.fix_user(user, out, summary))
        log.append({"step": "問題文の確認", "ok": True,
                    "summary": f"テンプレートの式・値 {len(inst.givens)} 個がそのまま入っている"})
        log.append({"step": "解説の確認", "ok": True, "summary": "解説に正解が入っている（問題文・ヒントには書かれていない）"})
        log.append({"step": "表記の確認", "ok": True,
                    "summary": "数式の表記（KaTeX の命令・$ の対応・数式の中の日本語・中国語の字）と解説の数の等式に問題なし"})

        return Draft(
            unit=unit, template=template, format=fmt, question=question,
            answer={"kind": inst.kind, "srepr": sp.srepr(inst.answer), "latex": answer_latex, "var": inst.var},
            answer_display=f"${answer_latex}$", explanation=explanation, hint=hint, choices=choices,
            input_hint=inst.input_hint if fmt == "input" else "",
            source=f"テンプレート {template}（数値と正解は Python で計算）",
            params=inst.params, params_key=key, log=log,
        )

    def _chat(self, ctx: GenContext, system: str, user: str) -> dict[str, Any]:
        """作成役を呼ぶ。出力の JSON が壊れていたときは、作り直しの回数を使わずに 1 回だけ呼び直す。"""
        from ...llm import LLMError

        try:
            return ctx.llm.chat_json(ctx.gen_model, system, user, prompts.GEN_SCHEMA, temperature=0.7)
        except LLMError:
            return ctx.llm.chat_json(ctx.gen_model, system, user, prompts.GEN_SCHEMA, temperature=0.7)

    # --- 検証 ---------------------------------------------------------------

    def verify(self, llm: LLM, sandbox: Sandbox, model: str, problem: Any,
               options: dict[str, str] | None = None) -> VerifyResult:
        from ...llm import LLMError

        ans = problem.answer
        input_hint = problem.input_hint or _default_hint(ans.get("kind", "value"))
        user = prompts.solve_user(problem.question, ans.get("kind", "value"), input_hint)
        try:
            out = llm.chat_json(model, prompts.SOLVE_SYSTEM, user, prompts.SOLVE_SCHEMA, temperature=0.6)
        except LLMError:  # 出力が読めなかったときは 1 回だけ解かせ直す
            try:
                out = llm.chat_json(model, prompts.SOLVE_SYSTEM, user, prompts.SOLVE_SCHEMA, temperature=0.6)
            except LLMError as exc:
                return VerifyResult(False, f"{model}: 出力を読めません（{exc}）", "", "llm_output",
                                    raw=getattr(exc, "raw", ""))
        raw = json.dumps(out, ensure_ascii=False)
        work = str(out.get("work") or "").strip()
        tried: list[str] = []
        res: dict[str, Any] = {}
        # answer 欄を読む。読めなければ、途中の考え方の最後にある答え（\boxed{…}、「答え: …」）も試す
        for given in _answer_candidates(str(out.get("answer") or ""), work):
            tried.append(given)
            res = sandbox.compare(ans["kind"], {"srepr": ans["srepr"]}, {"text": given}, var=ans.get("var", "x"))
            if res.get("ok"):
                break
        if not res.get("ok"):
            shown = "」「".join(tried) or "（空）"
            return VerifyResult(False, f"{model}: 答え「{shown}」を読み取れません（{res.get('error', '答えがありません')}）",
                                work, "solver_unreadable", raw=raw)
        shown = res.get("given", {}).get("latex", tried[-1])
        if res.get("equal"):
            return VerifyResult(True, f"{model}: ${shown}$（一致）", work, raw=raw)
        if _same_angle_in_radians(sandbox, ans, tried[-1]):
            return VerifyResult(True, f"{model}: ${shown}$（弧度法で答えたが、同じ角なので一致とみなす）", work, raw=raw)
        return VerifyResult(False, f"{model}: ${shown}$（正解 ${ans['latex']}$ と不一致）", work, "solver_mismatch",
                            raw=raw)

    def review(self, llm: LLM, model: str, problem: Any) -> VerifyResult | None:
        """解説の見直し。検証役のモデルに、辺の対応・途中式・言葉の説明の誤りを探させる。"""
        from ...llm import LLMError

        user = prompts.review_user(problem.question, problem.answer.get("latex", ""), problem.explanation)
        out: dict[str, Any] = {}
        for _ in range(2):  # 出力が読めなかったときは 1 回だけ呼び直す
            try:
                out = llm.chat_json(model, prompts.REVIEW_SYSTEM, user, prompts.REVIEW_SCHEMA, temperature=0.2)
                break
            except LLMError as exc:
                out = {"_error": str(exc), "_raw": getattr(exc, "raw", "")}
        # 1 行目だけを見る（指示の「「問題なし」または「問題あり」」を書き写したときに誤って弾かないよう）
        verdict = next((ln.strip(" 　。.*「」") for ln in str(out.get("verdict") or "").splitlines() if ln.strip()), "")
        issues = str(out.get("issues") or "").strip()
        raw = out.get("_raw") or f"[[verdict]]\n{verdict}\n[[issues]]\n{issues}"
        if verdict.startswith("問題あり"):
            return VerifyResult(False, f"{model}: 解説に誤りがあると指摘（{issues[:300] or '理由なし'}）", issues,
                                "explanation_review", raw=raw)
        if verdict.startswith("問題なし"):
            return VerifyResult(True, f"{model}: 解説に誤りは見つからなかった", issues, raw=raw)
        # 見直しの結果が読めないときは、それだけで不合格にはしない（検証役の答えは全回一致している）
        return VerifyResult(True, f"{model}: 見直しの結果を読めなかった（{out.get('_error') or verdict[:60]}）",
                            "", raw=raw)

    # --- 問題検証モード -----------------------------------------------------

    def text_issues(self, problem: Any) -> list[str]:
        return (latexfix.question_problems(problem.question, choice=problem.format == "choice")
                + latexfix.problems(problem.hint, field="ヒント") + latexfix.problems(problem.explanation, field="解説"))

    def normalize_text(self, text: str) -> str:
        return latexfix.normalize(text)

    # --- 採点 ---------------------------------------------------------------

    def grade(self, sandbox: Sandbox | None, problem: Any, response: str) -> Grade:
        if problem.format == "choice":
            return super().grade(sandbox, problem, response)
        text = (response or "").strip()
        if not text:
            return Grade(False, retry_message="答えを入力してください")
        if sandbox is None:
            return Grade(False, retry_message="採点の仕組み（sandbox）につながりません。少し待ってからもう一度送ってください")
        ans = problem.answer
        try:
            res = sandbox.compare(ans["kind"], {"srepr": ans["srepr"]}, {"text": text}, var=ans.get("var", "x"))
        except Exception:  # SandboxUnavailable など
            return Grade(False, retry_message="採点の仕組み（sandbox）につながりません。少し待ってからもう一度送ってください")
        if not res.get("ok"):
            if res.get("where") == "given":
                return Grade(False, retry_message=f"答えを読み取れません: {res.get('error')}")
            return Grade(False, retry_message=f"採点できませんでした: {res.get('error')}")
        if res.get("approx"):
            return Grade(False, retry_message="小数の近似値ではなく、分数や √ を使った正確な値で答えてください")
        shown = res.get("given", {}).get("latex", "")
        equal = bool(res.get("equal")) or _same_angle_in_radians(sandbox, ans, text)
        return Grade(equal, response_display=f"${shown}$" if shown else text)


_DEGREES_SREPR_RE = re.compile(r"^Integer\((-?\d+)\)$")


def _radian_srepr(ans: dict[str, Any]) -> str | None:
    """答えが度（120^\\circ など）の問題で、同じ角を弧度法で表した式（sympy の srepr）。違えば None。

    web からも使うので sympy を使わずに作る（120° → 2π/3 は Mul(Rational(2, 3), pi)）。
    """
    import math

    m = _DEGREES_SREPR_RE.match(str(ans.get("srepr", "")))
    if not m or not str(ans.get("latex", "")).rstrip().endswith("^\\circ"):
        return None
    deg = int(m.group(1))
    g = math.gcd(deg, 180)
    p, q = deg // g, 180 // g
    return f"Mul(Rational({p}, {q}), pi)" if q != 1 else f"Mul(Integer({p}), pi)"


def _same_angle_in_radians(sandbox: Any, ans: dict[str, Any], text: str) -> bool:
    """度で答える問題に、同じ角を弧度法（π/6 など）で答えたか。検証役は度で答えるよう頼んでも弧度法で答えることがあった。"""
    alt = _radian_srepr(ans)
    if alt is None or not text:
        return False
    try:
        res = sandbox.compare(ans["kind"], {"srepr": alt}, {"text": text}, var=ans.get("var", "x"))
    except Exception:
        return False
    return bool(res.get("ok") and res.get("equal"))


def _check_text(inst: Any, answer_latex: str, question: str, explanation: str, hint: str,
                sandbox: Any = None, answer_srepr: str = "", *, choice: bool = False,
                is_test: bool = False) -> tuple[str, str, str] | None:
    """LLM が書いた文章の確認。問題があれば (理由の種類, 段階, 説明)、なければ None。"""
    if not (question and explanation and hint):
        return "llm_output", "文章", "問題文・解説・ヒントのどれかが空です"
    lost = textcheck.missing(question, inst.givens)
    if lost:
        return ("text_givens", "問題文の確認",
                "問題文にテンプレートの式・値がそのまま入っていません: " + "、".join(f"${g}$" for g in lost))
    # 問題文やヒントに正解が書かれていたら、検証役にも子供にも答えが見えてしまう
    # （短い答え（"5" など）は問題文の数値と偶然重なるので、ある程度長いものだけ確かめる）
    leaks = [c for c in inst.answer_checks() if len(textcheck.normalize(c)) >= 4
             and not any(textcheck.contains(textcheck.first(g), c) for g in inst.givens)]
    for where, text in (("問題文", question), ("ヒント", hint)):
        if leaks and len(textcheck.missing(text, leaks)) < len(leaks):
            return "answer_leak", f"{where}の確認", f"{where}に正解 ${answer_latex}$ が書かれています"
    if textcheck.missing(explanation, inst.answer_checks()) and (sandbox is None or not _explanation_has_value(
            sandbox, inst, answer_srepr, explanation)):
        return "text_answer", "解説の確認", f"解説に正解 ${answer_latex}$ が入っていません"
    # 表記の崩れ（KaTeX にない命令、$ の対応、数式の中の日本語、中国語の字、選択式の「示せ」など）
    issues = (latexfix.question_problems(question, choice=choice) + latexfix.problems(hint, field="ヒント")
              + latexfix.problems(explanation, field="解説"))
    body = _ANSWER_LINE_RE.sub("", explanation)
    # 検証のテスト（正解をずらし、解き方の要点を渡さない）では、解説の長さと途中式は確かめない
    # （ずらした正解に合わせた解説は途中式が合わないので、検証より前に落ちてテストにならない）
    if not is_test and len(re.sub(r"\s", "", body)) < 40:
        issues.append("解説が短すぎる（解き方の要点の各段階を、途中式も省かずに書く）")
    if issues:
        return "text_format", "表記の確認", "表記の崩れ: " + "／".join(dict.fromkeys(issues))
    # 解説の数どうしの「=」が正しいか（58 - (-21) \cdot \frac{1}{2} = 79 のような途中式の誤り）
    if not is_test and sandbox is not None and hasattr(sandbox, "equalities"):
        segments = [(m.group(1) or m.group(2) or "") for m in _MATH_RE.finditer(explanation)]
        try:
            res = sandbox.equalities(segments)
        except Exception as exc:  # 確かめられないときは通す（検証役と問題検証モードで拾う）
            logger.warning("解説の途中式を確かめられませんでした: %s", exc)
            res = {}
        bad = res.get("mismatches") or []
        if bad:
            return ("explanation_math", "解説の確認",
                    "解説の途中式の左右が等しくありません: " + "、".join(f"${b}$" for b in bad[:3]))
    return None


_MATH_RE = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$", re.S)


def _explanation_has_value(sandbox: Any, inst: Any, answer_srepr: str, explanation: str) -> bool:
    """表記をそろえても見つからないとき、解説の数式を sympy で正解と比べる（2√21 と 2\\sqrt{21} など）。"""
    pieces: list[str] = []
    for m in _MATH_RE.finditer(explanation):
        math = (m.group(1) or m.group(2) or "").strip()
        pieces.append(math)
        if "=" in math:  # 「a = 2\sqrt{21}」の右辺
            pieces.append(math.rsplit("=", 1)[1].strip())
    pieces += [line.strip() for line in _ANSWER_LINE_RE.findall(explanation)]
    for piece in list(dict.fromkeys(p for p in pieces if p))[-12:]:
        try:
            res = sandbox.compare(inst.kind, {"srepr": answer_srepr}, {"text": piece}, var=inst.var)
        except Exception:
            return False
        if res.get("ok") and res.get("equal"):
            return True
    return False


_BOXED_RE = re.compile(r"\\boxed\{((?:[^{}]|\{[^{}]*\})*)\}")
_ANSWER_LINE_RE = re.compile(r"(?:答え|答|解答|Answer)\s*[:：は]\s*(.+)$", re.M)


def _answer_candidates(answer: str, work: str) -> list[str]:
    """検証役の答えとして試す文字列（読み取れた最初のものを使う）。

    answer 欄 → work の \\boxed{…} → work の「答え: …」の行 → work の最後の「=」の行（行全体、最後の「=」の右側）。
    answer 欄が「@35/4」のように崩れていても、work の最後が「S = 35/4」なら救える（実機の gemma4:e4b で多かった）。
    """
    out = [answer.strip()] if answer.strip() else []
    first_line = next((ln.strip() for ln in answer.splitlines() if ln.strip()), "")
    if first_line and first_line != answer.strip():  # 「3\n\n以上です」のような書き添え
        out.append(first_line)
    boxed = _BOXED_RE.findall(work)
    if boxed:
        out.append(boxed[-1].strip())
    lines = _ANSWER_LINE_RE.findall(work)
    if lines:
        out.append(lines[-1].strip())
    eq_lines = [ln.strip().strip("$").strip().rstrip("。.") for ln in work.splitlines() if "=" in ln]
    if eq_lines:
        out.append(eq_lines[-1])
        out.append(eq_lines[-1].rsplit("=", 1)[1].strip())
    return list(dict.fromkeys(c for c in out if c))


def _default_hint(kind: str) -> str:
    return prompts.ANSWER_FORMS.get(kind, "")


def _tamper(answer: Any, kind: str) -> Any:
    """検証のテスト用に、正解をわざとずらす（+1）。"""
    import sympy as sp

    if kind == "interval":
        return answer.xreplace({n: n + 1 for n in answer.atoms(sp.Number) if n.is_finite})
    if kind in ("tuple", "set"):
        items = list(answer.args)
        items[0] = items[0] + 1
        return sp.Tuple(*items) if kind == "tuple" else sp.FiniteSet(*items)
    return answer + 1
