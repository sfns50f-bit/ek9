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
    """小単元。数学ではテンプレート 1 つに当たる。"""

    id: str
    label: str
    formats: tuple[str, ...]


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
    "solver_mismatch": "検証役の答えが正解と一致しない",
    "solver_unreadable": "検証役の答えを読み取れない",
    "duplicate": "既存の問題と同じ数値",
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


class Subject:
    id: str = ""
    name: str = ""
    formats: tuple[str, ...] = ("choice",)
    reject_codes: dict[str, str] = {}
    mistakes: dict[str, str] = {}  # 選択式の誤答につける「間違いの種類」

    def units(self) -> list[Unit]:
        raise NotImplementedError

    # --- 作成・検証（worker） ---------------------------------------------

    def generate(self, ctx: GenContext, template: str, fmt: str) -> Draft:
        """1 問の下書きを作る。使えなければ Rejected、Ollama につながらなければ LLMUnavailable。"""
        raise NotImplementedError

    def verify(self, llm: LLM, sandbox: Sandbox, model: str, problem: Any) -> VerifyResult:
        """検証役に模範解答を見せずに 1 回解かせ、正解と突き合わせる。"""
        raise NotImplementedError

    # --- 採点（web。LLM は使わない） ---------------------------------------

    def grade(self, sandbox: Sandbox | None, problem: Any, response: str) -> Grade:
        if problem.format == "choice":
            return grade_choice(problem, response)
        raise NotImplementedError

    # --- 共通の便利関数 ----------------------------------------------------

    def subunit_map(self) -> dict[str, tuple[Unit, Subunit]]:
        return {su.id: (u, su) for u in self.units() for su in u.subunits}

    def templates_for(self, scope: str, fmt: str | None = None) -> list[str]:
        """範囲（""・"unit:<ID>"・"tmpl:<ID>"）に入る小単元の ID。fmt を指定すればその形式に対応するものだけ。"""
        out = []
        for unit in self.units():
            for su in unit.subunits:
                if scope.startswith("unit:") and unit.id != scope[5:]:
                    continue
                if scope.startswith("tmpl:") and su.id != scope[5:]:
                    continue
                if fmt and fmt not in su.formats:
                    continue
                out.append(su.id)
        return out

    def scope_label(self, scope: str) -> str:
        if scope.startswith("unit:"):
            for unit in self.units():
                if unit.id == scope[5:]:
                    return unit.label
        if scope.startswith("tmpl:"):
            found = self.subunit_map().get(scope[5:])
            if found:
                return f"{found[0].label}／{found[1].label}"
        return f"{self.name}（全体）"

    def label_of(self, template: str) -> str:
        found = self.subunit_map().get(template)
        return f"{found[0].label}／{found[1].label}" if found else template

    def all_reject_codes(self) -> dict[str, str]:
        return {**REJECT_CODES, **self.reject_codes}


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
