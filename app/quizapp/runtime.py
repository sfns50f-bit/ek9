"""/status 画面から変えられる設定（作成・検証のモデル、検証の回数）。

画面で選んだ値は kv 表に入る。空のときは .env（config.Settings）の値を使う。
worker は依頼を処理するたびに読み直すので、次の依頼から反映される。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from .config import Settings
from .db import dumps, kv_get, kv_json, kv_set

KEYS = ("gen_model", "verify_model", "verify_count", "verify_model_2")
MAX_VERIFY_COUNT = 5


@dataclass(frozen=True)
class ModelPlan:
    gen_model: str
    verify_model: str
    verify_count: int
    verify_model_2: str  # 空なら使わない。使うときは 1 回だけ解かせる

    @property
    def verify_runs(self) -> list[str]:
        """検証役の呼び出し順。モデルの入れ替えが少なくなるよう、同じモデルを続ける。"""
        runs = [self.verify_model] * self.verify_count
        if self.verify_model_2:
            runs.append(self.verify_model_2)
        return runs

    @property
    def verify_label(self) -> str:
        label = f"{self.verify_model} ×{self.verify_count}"
        return label + (f" ＋ {self.verify_model_2} ×1" if self.verify_model_2 else "")


def overrides(session: Session) -> dict[str, str]:
    return {key: kv_get(session, f"setting_{key}") for key in KEYS}


def save_overrides(session: Session, values: dict[str, str]) -> None:
    for key in KEYS:
        if key in values:
            kv_set(session, f"setting_{key}", values[key].strip())


def _count(value: str, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(1, min(MAX_VERIFY_COUNT, n))


def model_plan(settings: Settings, session: Session) -> ModelPlan:
    o = overrides(session)
    v2 = o["verify_model_2"]
    return ModelPlan(
        gen_model=o["gen_model"] or settings.gen_model,
        verify_model=o["verify_model"] or settings.verify_model,
        verify_count=_count(o["verify_count"], settings.verify_count),
        # 画面で「使わない」を選んだときは "-" を保存する
        verify_model_2="" if v2 == "-" else (v2 or settings.verify_model_2),
    )


# --- 思考モード（モデルごと） ----------------------------------------------------
# 画面で一度選ぶとモデルごとに保存され、そのモデルを使うたびに同じ設定になる。
# 選んでいないモデルは「自動」：qwen3 系（qwen3.5 も含む）は切る。gemma4 などはモデルの既定のまま
# （gemma4 は think=false を送ると JSON スキーマの指定が効かなくなる不具合があったため）。
THINK_CHOICES = {"auto": "自動", "off": "切る", "on": "切らない（考えさせる）"}
THINK_OFF_PREFIXES = ("qwen3",)


def think_settings(session: Session) -> dict[str, str]:

    data = kv_json(session, "think_settings", {}) or {}
    return {str(k): str(v) for k, v in data.items() if v in THINK_CHOICES}


def save_think_setting(session: Session, model: str, choice: str) -> None:

    data = think_settings(session)
    if choice == "auto":
        data.pop(model, None)
    elif choice in THINK_CHOICES:
        data[model] = choice
    kv_set(session, "think_settings", dumps(data))


def auto_think(model: str) -> bool | None:
    return False if model.lower().startswith(THINK_OFF_PREFIXES) else None


def resolve_think(model: str, choices: dict[str, str], env_think: bool | None) -> bool | None:
    """True / False は think をその値で送る。None は送らない（モデルの既定）。"""
    choice = choices.get(model, "auto")
    if choice == "off":
        return False
    if choice == "on":
        return True
    return env_think if env_think is not None else auto_think(model)
