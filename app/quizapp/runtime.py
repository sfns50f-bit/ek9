"""/status 画面から変えられる設定（作成・検証のモデル、検証の回数）。

画面で選んだ値は kv 表に入る。空のときは .env（config.Settings）の値を使う。
worker は依頼を処理するたびに読み直すので、次の依頼から反映される。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

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


KEYS = ("gen_model", "verify_model", "verify_count", "verify_model_2", "review", "review_model")
MAX_VERIFY_COUNT = 5
# どの科目にもある役割（科目だけの役割は Subject.model_roles。英語の「本文作成」など）
ROLES = {"gen": "作成", "verify": "検証", "review": "見直し"}
# 科目ごとの設定（kv の subject_models）で、モデルのほかに持つもの：API の 1 日の上限（金額・回数。0 は上限なし）
DAILY_KEYS = {"api_daily_usd": "API の 1 日の上限（ドル）", "api_daily_calls": "API の 1 日の上限（呼び出しの回数）"}


@dataclass(frozen=True)
class ModelPlan:
    gen_model: str
    verify_model: str
    verify_count: int
    verify_model_2: str  # 空なら使わない。使うときは 1 回だけ解かせる
    # 解説の見直し：検証役が全回一致したあと、見直し役のモデルに解説を読ませて誤りを探させる（1 回だけ）
    review: bool = True
    review_model: str = ""  # 空なら検証役のモデル
    # 科目だけの役割のモデル（(役割, モデル) の並び。英語の本文作成など）。設定がなければ作成役のモデル
    extra: tuple[tuple[str, str], ...] = ()

    @property
    def reviewer(self) -> str:
        return self.review_model or self.verify_model

    def model_for(self, role: str) -> str:
        """役割のモデル。gen / verify / review と、科目だけの役割（なければ作成役のモデル）。"""
        if role == "verify":
            return self.verify_model
        if role == "review":
            return self.reviewer
        return dict(self.extra).get(role) or self.gen_model

    @property
    def models(self) -> dict[str, str]:
        return {"gen": self.gen_model, "verify": self.verify_model, "review": self.reviewer, **dict(self.extra)}

    @property
    def verify_runs(self) -> list[str]:
        """検証役の呼び出し順。モデルの入れ替えが少なくなるよう、同じモデルを続ける。"""
        runs = [self.verify_model] * self.verify_count
        if self.verify_model_2:
            runs.append(self.verify_model_2)
        return runs

    @property
    def needs_ollama(self) -> bool:
        used = [self.gen_model, *self.verify_runs, *(m for _r, m in self.extra)]
        if self.review:
            used.append(self.reviewer)
        return any(provider_of(m)[0] == "ollama" for m in used if m)

    @property
    def verify_label(self) -> str:
        label = f"{self.verify_model} ×{self.verify_count}"
        return label + (f" ＋ {self.verify_model_2} ×1" if self.verify_model_2 else "")

    @property
    def same_gen_verify(self) -> bool:
        """作成役と検証役が同じモデル（見落としが重なりやすいので画面で注意を出す）。"""
        return self.gen_model == self.verify_model


def overrides(session: Session) -> dict[str, str]:
    """全体の設定（既定値）。前の版の設定（全体で 1 つ）がそのまま既定値になる。"""
    return {key: kv_get(session, f"setting_{key}") for key in KEYS}


def save_overrides(session: Session, values: dict[str, str]) -> None:
    for key in KEYS:
        if key in values:
            kv_set(session, f"setting_{key}", values[key].strip())


def subject_settings(session: Session) -> dict[str, dict[str, str]]:
    """科目ごとの上書き {科目ID: {役割: モデル, "api_daily_usd": …}}。空の値は「全体の設定のまま」。"""
    data = kv_json(session, "subject_models", {}) or {}
    return {str(k): {str(r): str(m) for r, m in v.items() if str(m).strip()} for k, v in data.items()
            if isinstance(v, dict)}


def save_subject_settings(session: Session, subject_id: str, values: dict[str, str]) -> None:
    """科目の上書きを保存する。values に入れなかった役割は消す（＝全体の設定に戻す）。"""
    data = subject_settings(session)
    data[subject_id] = {k: v.strip()[:128] for k, v in values.items() if v and v.strip()}
    if not data[subject_id]:
        data.pop(subject_id)
    kv_set(session, "subject_models", dumps(data))


def daily_limits(session: Session, subject_id: str) -> tuple[float, int]:
    """科目の API の 1 日の上限（ドル、回数）。0 は上限なし。"""
    o = subject_settings(session).get(subject_id, {})
    try:
        usd = max(0.0, float(o.get("api_daily_usd") or 0))
    except ValueError:
        usd = 0.0
    try:
        calls = max(0, int(o.get("api_daily_calls") or 0))
    except ValueError:
        calls = 0
    return (usd if math.isfinite(usd) else 0.0), calls


def _count(value: str, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(1, min(MAX_VERIFY_COUNT, n))


def model_plan(settings: Settings, session: Session, subject: object | None = None) -> ModelPlan:
    """全体の設定（空の欄は .env）に、科目の上書きを重ねたもの。subject は Subject（なければ全体の設定）。"""
    o = overrides(session)
    v2 = o["verify_model_2"]
    gen = o["gen_model"] or settings.gen_model
    verify = o["verify_model"] or settings.verify_model
    review_model = o["review_model"]
    extra: list[tuple[str, str]] = []
    if subject is not None:
        mine = subject_settings(session).get(getattr(subject, "id", ""), {})
        gen = mine.get("gen") or gen
        verify = mine.get("verify") or verify
        review_model = mine.get("review") or review_model
        extra = [(role, mine[role]) for role in getattr(subject, "model_roles", {}) if mine.get(role)]
    return ModelPlan(
        gen_model=gen,
        verify_model=verify,
        verify_count=_count(o["verify_count"], settings.verify_count),
        # 画面で「使わない」を選んだときは "-" を保存する
        verify_model_2="" if v2 == "-" else (v2 or settings.verify_model_2),
        review=o["review"] != "off",
        review_model="" if review_model == verify else review_model,
        extra=tuple(extra),
    )


def plan_for_job(settings: Settings, session: Session, job: object) -> ModelPlan:
    """依頼で選んだモデル。依頼に記録がなければ（前の版で作った依頼）今の設定（その科目の設定）。"""
    if not getattr(job, "gen_model", ""):
        from .subjects import get_subject

        return model_plan(settings, session, get_subject(getattr(job, "subject", "")))
    extra = getattr(job, "extra_models", {}) or {}
    return ModelPlan(
        gen_model=job.gen_model,  # type: ignore[attr-defined]
        verify_model=job.verify_model or settings.verify_model,  # type: ignore[attr-defined]
        verify_count=_count(str(job.verify_count), settings.verify_count),  # type: ignore[attr-defined]
        verify_model_2=job.verify_model_2,  # type: ignore[attr-defined]
        review=bool(getattr(job, "explanation_review", True)),
        review_model=getattr(job, "review_model", "") or "",
        extra=tuple(sorted(extra.items())),
    )


def apply_plan(job: object, plan: ModelPlan) -> None:
    """依頼にモデルを記録する（後で /status の設定を変えても、この依頼はこのモデルで最後まで処理する）。"""
    job.gen_model = plan.gen_model  # type: ignore[attr-defined]
    job.verify_model = plan.verify_model  # type: ignore[attr-defined]
    job.verify_count = plan.verify_count  # type: ignore[attr-defined]
    job.verify_model_2 = plan.verify_model_2  # type: ignore[attr-defined]
    job.explanation_review = plan.review  # type: ignore[attr-defined]
    job.review_model = plan.review_model  # type: ignore[attr-defined]
    job.extra_models_json = dumps(dict(plan.extra))  # type: ignore[attr-defined]


def record_think(settings: Settings, session: Session, job: object, plan: ModelPlan) -> None:
    """依頼したときの思考モードを依頼に記録する（後で設定を変えても、この依頼はこの値で最後まで処理する）。"""
    job.think_json = dumps(think_map(settings, session, plan))  # type: ignore[attr-defined]


# --- API の利用額（ドル） ----------------------------------------------------------
# 月ごとの合計を kv の api_spend（{"2026-10": 1.23}）に、上限を setting_api_monthly_limit / setting_api_job_limit に持つ。
# 月の区切りは日本時間（DISPLAY_TZ）。


def month_key(settings: Settings, now: datetime | None = None) -> str:
    from zoneinfo import ZoneInfo

    now = now or datetime.now(timezone.utc)
    return now.astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m")


def api_spend(session: Session) -> dict[str, float]:
    data = kv_json(session, "api_spend", {}) or {}
    return {str(k): float(v) for k, v in data.items()}


def add_api_spend(settings: Settings, session: Session, usd: float) -> None:
    data = api_spend(session)
    key = month_key(settings)
    data[key] = round(data.get(key, 0.0) + usd, 6)
    kv_set(session, "api_spend", dumps(data))


def day_key(settings: Settings, now: datetime | None = None) -> str:
    from zoneinfo import ZoneInfo

    now = now or datetime.now(timezone.utc)
    return now.astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")


API_DAILY_KEEP = 31  # 科目ごとの 1 日の記録を残す日数


def api_daily(session: Session) -> dict[str, dict[str, dict[str, float]]]:
    """科目ごと・日ごとの API の利用 {"2026-10-24": {"english": {"usd": 0.12, "calls": 30}}}（日本時間の日）。"""
    data = kv_json(session, "api_daily", {}) or {}
    return {str(d): {str(sub): {"usd": float(v.get("usd", 0)), "calls": float(v.get("calls", 0))}
                     for sub, v in subs.items()} for d, subs in data.items() if isinstance(subs, dict)}


def add_api_daily(settings: Settings, session: Session, subject_id: str, usd: float, calls: int) -> None:
    data = api_daily(session)
    today = data.setdefault(day_key(settings), {}).setdefault(subject_id, {"usd": 0.0, "calls": 0.0})
    today["usd"] = round(today["usd"] + usd, 6)
    today["calls"] += calls
    keep = sorted(data)[-API_DAILY_KEEP:]
    kv_set(session, "api_daily", dumps({d: data[d] for d in keep}))


def api_limits(settings: Settings, session: Session) -> tuple[float, float]:
    """(月の上限, 1 回の依頼の上限)。画面で変えた値があればそれ、なければ .env。依頼の上限 0 は上限なし。"""
    def value(key: str, default: float) -> float:
        try:
            return max(0.0, float(kv_get(session, f"setting_{key}")))
        except ValueError:
            return default

    return value("api_monthly_limit", settings.api_monthly_limit), value("api_job_limit", settings.api_job_limit)


def save_api_limits(session: Session, monthly: str, per_job: str) -> None:
    for key, text in (("api_monthly_limit", monthly), ("api_job_limit", per_job)):
        try:
            value = float(text)
        except ValueError:
            continue
        if math.isfinite(value):  # inf・nan は受け付けない
            kv_set(session, f"setting_{key}", str(max(0.0, value)))


# --- 思考モード（モデルごと） ----------------------------------------------------
# 「あり」「なし」の 2 つだけ（第 4 回のレビュー：「モデルの既定」だと実際にどちらで動いたかが分からない）。
# 画面で一度選ぶとモデルごとに保存され、そのモデルを使うたびに同じ設定になる。選んでいないモデルは「なし」
# （.env の OLLAMA_THINK を書けば、選んでいないモデルはその値）。Ollama にはいつも think を明示して送る。
THINK_CHOICES = {"off": "なし", "on": "あり"}


def think_settings(session: Session) -> dict[str, str]:
    data = kv_json(session, "think_settings", {}) or {}
    return {str(k): str(v) for k, v in data.items() if v in THINK_CHOICES}


def save_think_setting(session: Session, model: str, choice: str) -> None:
    data = think_settings(session)
    if choice in THINK_CHOICES:
        data[model] = choice
    kv_set(session, "think_settings", dumps(data))


def resolve_think(model: str, choices: dict[str, str], env_think: bool | None) -> bool:
    """そのモデルに送る think の値（いつも True か False）。"""
    choice = choices.get(model)
    if choice in THINK_CHOICES:
        return choice == "on"
    return bool(env_think)


# 問題に残す思考モードの記録（依頼したときの設定）。API のモデルには思考モードの設定がない。
# "default" はこの形にする前の版の記録（モデルの既定のまま送らなかった）
THINK_RECORD = {"on": "あり", "off": "なし", "api": "API", "default": "モデルの既定（前の版）"}


def think_record(model: str, choices: dict[str, str], env_think: bool | None) -> str:
    """そのモデルを呼ぶときの思考モード（THINK_RECORD のキー）。"""
    if provider_of(model)[0] != "ollama":
        return "api"
    return "on" if resolve_think(model, choices, env_think) else "off"


def think_map(settings: Settings, session: Session, plan: ModelPlan) -> dict[str, str]:
    """依頼で使うモデルごとの思考モード（依頼したときの設定。依頼に記録し、worker は最後までこの値で呼ぶ）。"""
    choices = think_settings(session)
    used = [*plan.models.values(), *plan.verify_runs]
    return {m: think_record(m, choices, settings.think) for m in dict.fromkeys(used) if m}


def think_from_record(state: str) -> bool | None:
    """記録した思考モードを、Ollama に送る think の値に（"default" と "api" は送らない）。"""
    return {"on": True, "off": False}.get(state)
