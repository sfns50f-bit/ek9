"""/status 画面から変えられる設定（作成・検証のモデル、検証の回数）。

画面で選んだ値は kv 表に入る。空のときは .env（config.Settings）の値を使う。
worker は依頼を処理するたびに読み直すので、次の依頼から反映される。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from .config import Settings
from .db import dumps, kv_get, kv_json, kv_set

# API の提供元。モデル名の頭に「提供元/」を付けると（例 anthropic/claude-…）その API へ送る（llm.LLMRouter）。
# 頭に付けなければ Ollama のモデル。web からも使うので、LLM のクライアントを読み込まないここに置く
API_PROVIDERS = ("anthropic", "openai", "google")


def provider_of(model: str) -> tuple[str, str]:
    """(提供元, 提供元の中でのモデル名)。"""
    head, sep, rest = model.partition("/")
    if sep and head in API_PROVIDERS:
        return head, rest
    return "ollama", model


KEYS = ("gen_model", "verify_model", "verify_count", "verify_model_2", "review")
MAX_VERIFY_COUNT = 5


@dataclass(frozen=True)
class ModelPlan:
    gen_model: str
    verify_model: str
    verify_count: int
    verify_model_2: str  # 空なら使わない。使うときは 1 回だけ解かせる
    # 解説の見直し：検証役が全回一致したあと、検証役のモデルに解説を読ませて誤りを探させる（1 回だけ）
    review: bool = True

    @property
    def verify_runs(self) -> list[str]:
        """検証役の呼び出し順。モデルの入れ替えが少なくなるよう、同じモデルを続ける。"""
        runs = [self.verify_model] * self.verify_count
        if self.verify_model_2:
            runs.append(self.verify_model_2)
        return runs

    @property
    def needs_ollama(self) -> bool:
        return any(provider_of(m)[0] == "ollama" for m in [self.gen_model, *self.verify_runs])

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
        review=o["review"] != "off",
    )


def plan_for_job(settings: Settings, session: Session, job: object) -> ModelPlan:
    """依頼で選んだモデル。依頼に記録がなければ（前の版で作った依頼）今の設定。"""
    if not getattr(job, "gen_model", ""):
        return model_plan(settings, session)
    return ModelPlan(
        gen_model=job.gen_model,  # type: ignore[attr-defined]
        verify_model=job.verify_model or settings.verify_model,  # type: ignore[attr-defined]
        verify_count=_count(str(job.verify_count), settings.verify_count),  # type: ignore[attr-defined]
        verify_model_2=job.verify_model_2,  # type: ignore[attr-defined]
        review=bool(getattr(job, "explanation_review", True)),
    )


def apply_plan(job: object, plan: ModelPlan) -> None:
    """依頼にモデルを記録する（後で /status の設定を変えても、この依頼はこのモデルで最後まで処理する）。"""
    job.gen_model = plan.gen_model  # type: ignore[attr-defined]
    job.verify_model = plan.verify_model  # type: ignore[attr-defined]
    job.verify_count = plan.verify_count  # type: ignore[attr-defined]
    job.verify_model_2 = plan.verify_model_2  # type: ignore[attr-defined]
    job.explanation_review = plan.review  # type: ignore[attr-defined]


# --- 思考モード（モデルごと） ----------------------------------------------------
# 画面で一度選ぶとモデルごとに保存され、そのモデルを使うたびに同じ設定になる。
# 選んでいないモデルは「自動」：qwen3 系（qwen3.5 も含む）は切る。gemma4 などはモデルの既定のまま
# （JSON スキーマで出力させていたころ、gemma4 は think=false で JSON の指定が効かなくなる不具合があった）。
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
