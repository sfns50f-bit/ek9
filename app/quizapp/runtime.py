"""/status 画面から変えられる設定（作成・検証のモデル、検証の回数）。

画面で選んだ値は kv 表に入る。空のときは .env（config.Settings）の値を使う。
worker は依頼を処理するたびに読み直すので、次の依頼から反映される。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from .config import Settings
from .db import kv_get, kv_set

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
