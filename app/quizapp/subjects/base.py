"""科目の部品が満たす窓口。中心部分（順番待ち、保存、画面、/status）はここだけを使う。

科目を足すときは subjects/<科目>/ を作って Subject を実装し、subjects/__init__.py の
REGISTRY に 1 行足す。中心部分は触らない。

作成の流れ（worker が呼ぶ。1 回の依頼分をまとめて処理する）:
  1. 作成フェーズ：generate() を必要な数だけ呼ぶ（作成用モデルだけを使う）
  2. 検証フェーズ：verify() を「検証役のモデル × 回数 × 下書き」の分だけ呼ぶ
     （同じモデルを続けて呼ぶので、GPU でのモデルの入れ替えが少なくて済む）
  3. すべての検証に通った下書きだけが ready（ストック）になる
採点（web が呼ぶ。LLM は使わない）:
  grade()
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

FORMATS = {"choice": "選択式", "input": "キーボード入力"}
# 問題のレベル。中学は復習用（テンプレートは今後追加）
LEVELS = {"jhs": "中学", "basic": "高校基礎", "common": "共通テスト", "exam": "入試標準"}
# 端末ごとに出す形式。キーボードがあれば選択式も解ける
DEVICES = {
    "phone": ("スマホ", ("choice",)),
    "keyboard": ("キーボード", ("input", "choice")),
}


class LLM(Protocol):
    def chat_json(self, model: str, system: str, user: str, schema: dict[str, Any], *,
                  temperature: float = 0.7) -> dict[str, Any]: ...


class Sandbox(Protocol):
    def compare(self, kind: str, expected: dict[str, str], given: dict[str, str], *,
                var: str = "x", allow_float: bool = False) -> dict[str, Any]: ...

    def distinct(self, kind: str, items: list[str], var: str = "x") -> dict[str, Any]: ...


@dataclass(frozen=True)
class Subunit:
    """小単元。数学ではテンプレート 1 つに当たる。

    level：LEVELS のキー（中学・高校基礎・共通テスト・入試標準）
    related：主な単元とは別に関係する単元の ID（複数の単元を組み合わせた問題の印）
    """

    id: str
    label: str
    formats: tuple[str, ...]
    level: str = ""
    related: tuple[str, ...] = ()


@dataclass(frozen=True)
class Unit:
    id: str
    label: str
    subunits: tuple[Subunit, ...]


@dataclass
class Draft:
    """generate() が返す下書き。Problem の行になる。"""

    unit: str
    template: str
    format: str
    question: str
    answer: dict[str, Any]
    answer_display: str
    explanation: str
    hint: str
    choices: list[dict[str, Any]] = field(default_factory=list)
    input_hint: str = ""
    source: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    params_key: str = ""
    log: list[dict[str, Any]] = field(default_factory=list)
    # 本文（英語の長文・公共の資料）。{"title", "body", "source_title", "source_url"}。
    # 1 つの本文に複数の問題をつけるときは、generate() が同じ dict を持つ Draft のリストを返す。
    # 使い回すための記録 "template" "topic" "status" "gen_model" "log" も入れられる。
    # すでにある本文に設問を足すときは {"id": 本文の ID} を入れる（worker はその本文に結び付ける）
    passage: dict[str, Any] | None = None
    # 同じ本文の設問のうち、作成の段階で使えないと分かったもの（本文なしで解けるなど）。
    # worker は検証にかけずに不合格として残す（ほかの設問はそのまま検証する）
    rejected: Rejected | None = None


@dataclass
class Grade:
    correct: bool
    response_display: str = ""  # 当時の回答の表示（LaTeX を含んでよい）
    chosen_index: int | None = None
    chosen_mistake: str = ""
    retry_message: str = ""  # 入力を読み取れないなど、記録せずにもう一度入力させるときの理由


@dataclass
class VerifyResult:
    ok: bool
    summary: str
    detail: str = ""
    code: str = "solver_mismatch"  # 不合格の理由の種類（/status で集計する）
    raw: str = ""  # 検証役の生の出力（原因を調べるために、そのまま残す）
    # 見直しで直す項目（合格したときだけ worker が問題に書き込む）。今は {"choices": [...]}（誤答の種類の付け直し）だけ
    updates: dict[str, Any] = field(default_factory=dict)


class Rejected(Exception):
    """下書きを作れなかった・使えなかった。code は理由の種類（REJECT_CODES のキー）。"""

    def __init__(self, code: str, message: str, log: list[dict[str, Any]] | None = None,
                 partial: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.log = log or []
        self.partial = partial or {}


# 不合格の理由の種類（科目の部品は必要なら独自のものを reject_codes で足す）
REJECT_CODES = {
    "llm_output": "モデルの出力を読めない・項目が足りない",
    "text_givens": "問題文の数値・式がテンプレートと違う",
    "text_answer": "解説に正解が入っていない",
    "choices": "選択肢が重複・正解と一致する誤答がある",
    "solver_mismatch": "検証役の答えは読めたが、正解と違う",
    "solver_unreadable": "検証役の答えが読み取れなかった",
    "duplicate": "既存の問題と同じ（検証には合格したが、同じ数値・同じ語・同じ題材の問題がすでにストックにある）",
    # 作れる問題を使い切った（英語の題材・単語、数学の数値の組み合わせ）。作った問題ではないので合格率には数えない
    "exhausted": "題材・語・数値の組み合わせを使い切った（作れる問題が残っていない。合格率には数えない）",
    "needs_check": "検証の判定が割れた（要確認。問題検証モードで確かめてストックに入れるか決める）",
    "checked_rejected": "要確認を確かめて捨てた",
}


@dataclass
class GenContext:
    llm: LLM
    sandbox: Sandbox
    gen_model: str
    rng: random.Random
    is_test: bool = False
    # (小単元ID) -> すでにある問題の params_key の集合（同じ数値の問題を作らない）
    existing_keys: Callable[[str], set[str]] = lambda _template: set()
    # 役割ごとのモデル（gen / verify / review と、科目だけの役割。Subject.model_roles）
    models: dict[str, str] = field(default_factory=dict)
    review: bool = True  # 見直しをする依頼か（本文の見直しもこれに従う）
    # (小単元ID) -> その小単元の本文 [{"id", "title", "body", "topic", "status", "questions": [設問の文]}]
    # （設問を後から足すため。不合格になった設問は questions に入れない）
    passages: Callable[[str], list[dict[str, Any]]] = lambda _template: []
    # (本文の ID, {"status": …, "log_add": [記録]}) -> 本文を書き換える（使い回す前の見直しの結果を残す・使えない印を付ける）
    update_passage: Callable[[int, dict[str, Any]], None] = lambda _id, _changes: None
    # 依頼に記録した科目のやり方（Subject.verify_options の値。作成で使うもの：英語の本文の使い回しなど）
    options: dict[str, str] = field(default_factory=dict)

    def model(self, role: str) -> str:
        """役割のモデル。設定がなければ作成役のモデル。"""
        return self.models.get(role) or self.gen_model


class Subject:
    id: str = ""
    name: str = ""
    formats: tuple[str, ...] = ("choice",)
    reject_codes: dict[str, str] = {}
    mistakes: dict[str, str] = {}  # 選択式の誤答につける「間違いの種類」
    # 科目だけの役割（{役割ID: 画面の名前}。英語の「本文作成」など）。モデルは科目ごとに設定でき、
    # 設定がなければ作成役のモデルを使う。作成のときは ctx.model(役割ID) で読む
    model_roles: dict[str, str] = {}
    # 画面で $…$ を数式として表示するか。英語のように「$20」がそのまま出てくる科目は False にする
    math_text: bool = True

    def units(self) -> list[Unit]:
        raise NotImplementedError

    # --- 作成・検証（worker） ---------------------------------------------

    # 検証のテスト（正解をわざとずらした問題を流す）に使う形式。空なら、この科目はテストに対応しない
    # 検証の判定が割れたとき（設定の回数のうち一部だけ合格）、すぐ捨てずに「要確認」（needs_check）にするか。
    # True の科目は、検証の不合格が出ても検証の回数を最後までかける（英語：第 6 回のレビュー 2-4）
    verify_split_check: bool = False
    # 検証のやり方の切り替え（第 7 回のレビュー：見本集で今の方式と新しい方式を並べて測るため）。
    # {キー: {"label": 名前, "choices": {値: 説明}, "default": 既定の値}}。「状態 → モデルと思考モード」の科目ごとの
    # 設定で変えられ、依頼に記録して（jobs.options）verify(options=…) に渡す
    verify_options: dict[str, dict[str, Any]] = {}
    # 役割ごとの既定のモデル（全体・科目の設定がないときに使う。英語の見直し役＝gemma4:12b など）
    default_models: dict[str, str] = {}
    # モデルごとの思考モードの既定（"on" / "off"）。画面で設定していないモデルを、この科目の依頼ではこの値で呼ぶ
    default_think: dict[str, str] = {}

    test_format: str = ""

    def generate(self, ctx: GenContext, template: str, fmt: str) -> Draft | list[Draft]:
        """下書きを作る（1 問、または同じ本文の複数問）。

        使えなければ Rejected、Ollama につながらなければ LLMUnavailable。
        ctx.is_test のときは、正解をわざとずらした下書きを作る（test_format に対応する科目だけ）。
        """
        raise NotImplementedError

    def verify(self, llm: LLM, sandbox: Sandbox, model: str, problem: Any,
               options: dict[str, str] | None = None) -> VerifyResult:
        """検証役に模範解答を見せずに 1 回解かせ、正解と突き合わせる。"""
        raise NotImplementedError

    # --- 採点（web。LLM は使わない） ---------------------------------------

    def review(self, llm: LLM, model: str, problem: Any) -> VerifyResult | None:
        """解説の見直し（任意）。検証役が全回一致した問題の解説に、明らかな誤りがないかを LLM に確かめさせる。

        対応しない科目は None を返す（見直しをしない）。
        """
        return None

    def golden_params(self, template: str, question: str, explanation: str, choices: list[dict[str, Any]],
                      answer: str) -> dict[str, Any]:
        """見本集のファイルの問題（書き出しから復元したもの。params がない）の params を、問題文などから組み立てる。
        点検（text_issues）に要る値だけでよい。既定は空。"""
        return {}

    def rereview(self, llm: LLM, model: str, problem: Any,
                 options: dict[str, str] | None = None) -> VerifyResult | None:
        """見直しだけをやり直す依頼で、前に作った問題を点検し直す。ふつうは見直しと同じ。"""
        return self.review(llm, model, problem)

    def review_passage(self, llm: LLM, model: str, passage: Any) -> VerifyResult | None:
        """本文だけの見直し（任意）。見直しだけをやり直す依頼で「本文の見直しもする」を選んだときに呼ぶ。
        対応しない科目は None。"""
        return None

    def grade(self, sandbox: Sandbox | None, problem: Any, response: str) -> Grade:
        if problem.format == "choice":
            return grade_choice(problem, response)
        raise NotImplementedError

    # --- 問題検証モード（web。LLM は使わない） -----------------------------

    def text_issues(self, problem: Any) -> list[str]:
        """問題文・ヒント・解説の表記の崩れ（作成のときと同じ点検）。「表記の点検で引っかかる問題」に出す。"""
        return []

    def normalize_text(self, text: str) -> str:
        """意味を変えずに直せる表記の揺れを直す（問題検証モードの「表記をまとめて直す」）。"""
        return text

    # --- 共通の便利関数 ----------------------------------------------------

    def subunit_map(self) -> dict[str, tuple[Unit, Subunit]]:
        return {su.id: (u, su) for u in self.units() for su in u.subunits}

    def templates_for(self, scope: str, fmt: str | None = None, levels: str | list[str] = "") -> list[str]:
        """範囲に入る小単元の ID。

        scope：""（科目全体）、または "unit:<単元ID>" と "tmpl:<小単元ID>" をカンマでつないだもの（その和集合）
        fmt を指定すればその形式に対応するもの、levels（カンマ区切りかリスト）を指定すればそのレベルのものだけ
        """
        parts = split_scope(scope)
        units = {p[5:] for p in parts if p.startswith("unit:")}
        tmpls = {p[5:] for p in parts if p.startswith("tmpl:")}
        wanted_levels = set(levels.split(",") if isinstance(levels, str) else levels) - {""}
        out = []
        for unit in self.units():
            for su in unit.subunits:
                if parts and unit.id not in units and su.id not in tmpls:
                    continue
                if fmt and fmt not in su.formats:
                    continue
                if wanted_levels and su.level not in wanted_levels:
                    continue
                out.append(su.id)
        return out

    def scope_valid(self, scope: str) -> bool:
        """範囲の書き方が正しいか（知らない単元・小単元を含まないか）。"""
        units = {u.id for u in self.units()}
        tmpls = set(self.subunit_map())
        return all((p.startswith("unit:") and p[5:] in units) or (p.startswith("tmpl:") and p[5:] in tmpls)
                   for p in split_scope(scope))

    def scope_label(self, scope: str) -> str:
        parts = split_scope(scope)
        if not parts:
            return f"{self.name}（全体）"
        labels = []
        for p in parts:
            if p.startswith("unit:"):
                labels += [u.label for u in self.units() if u.id == p[5:]]
            elif p.startswith("tmpl:") and p[5:] in self.subunit_map():
                unit, su = self.subunit_map()[p[5:]]
                labels.append(f"{unit.label}／{su.label}")
        return "、".join(labels) or f"{self.name}（全体）"

    def label_of(self, template: str) -> str:
        found = self.subunit_map().get(template)
        return f"{found[0].label}／{found[1].label}" if found else template

    def remaining(self, template: str, fmt: str, existing: set[str], used_topics: set[str]) -> int | None:
        """まだ作れる問題の数の目安（題材・語を使い切る科目だけ。作る画面で「残りが少ない」と知らせる。
        第 11 回のレビュー 2-3）。existing：その小単元のストック・下書きの params_key、used_topics：本文の題材
        （落ちた本文を除く）。None は数えない（数学など、使い切ることがまずない科目）。web から呼ぶので LLM を読まない。"""
        return None

    def all_reject_codes(self) -> dict[str, str]:
        return {**REJECT_CODES, **self.reject_codes}


def split_scope(scope: str) -> list[str]:
    return [p.strip() for p in (scope or "").split(",") if p.strip()]


def join_scope(parts: list[str]) -> str:
    return ",".join(dict.fromkeys(p.strip() for p in parts if p and p.strip()))


def grade_choice(problem: Any, response: str) -> Grade:
    choices = problem.choices
    try:
        index = int(response)
    except (TypeError, ValueError):
        return Grade(False, retry_message="選択肢を選んでください")
    if not 0 <= index < len(choices):
        return Grade(False, retry_message="選択肢を選んでください")
    chosen = choices[index]
    return Grade(
        correct=bool(chosen.get("correct")),
        response_display=str(chosen.get("text", "")),
        chosen_index=index,
        chosen_mistake=str(chosen.get("mistake") or ""),
    )
