"""環境変数から読む設定。docker-compose.yml / .env で上書きする。"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def _str(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _think() -> bool | None:
    """OLLAMA_THINK が空なら think パラメータを送らない（モデルの既定に任せる）。"""
    value = os.environ.get("OLLAMA_THINK", "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return None


@dataclass
class Settings:
    database_path: str = field(default_factory=lambda: _str("DATABASE_PATH", "/data/quiz.db"))
    sandbox_url: str = field(default_factory=lambda: _str("SANDBOX_URL", "http://sandbox:8080"))

    ollama_base_url: str = field(default_factory=lambda: _str("OLLAMA_BASE_URL", "http://host.docker.internal:11434"))
    ollama_model: str = field(default_factory=lambda: _str("OLLAMA_MODEL", "gemma4:e4b"))
    gen_model: str = field(default_factory=lambda: _str("GEN_MODEL", ""))
    solve_model: str = field(default_factory=lambda: _str("SOLVE_MODEL", ""))
    review_model: str = field(default_factory=lambda: _str("REVIEW_MODEL", ""))
    num_ctx: int = field(default_factory=lambda: _int("OLLAMA_NUM_CTX", 8192))
    num_predict: int = field(default_factory=lambda: _int("OLLAMA_NUM_PREDICT", 6144))
    keep_alive: str = field(default_factory=lambda: _str("OLLAMA_KEEP_ALIVE", "15m"))
    think: bool | None = field(default_factory=_think)
    llm_timeout: int = field(default_factory=lambda: _int("LLM_TIMEOUT", 900))

    max_attempts: int = field(default_factory=lambda: _int("MAX_ATTEMPTS", 4))
    review_enabled: bool = field(default_factory=lambda: _bool("REVIEW_ENABLED", True))
    pool_target: int = field(default_factory=lambda: _int("POOL_TARGET", 2))
    refill_enabled: bool = field(default_factory=lambda: _bool("REFILL_ENABLED", True))
    refill_days: int = field(default_factory=lambda: _int("REFILL_DAYS", 14))
    worker_idle_seconds: int = field(default_factory=lambda: _int("WORKER_IDLE_SECONDS", 10))

    timezone: str = field(default_factory=lambda: _str("DISPLAY_TZ", "Asia/Tokyo"))

    def __post_init__(self) -> None:
        self.gen_model = self.gen_model or self.ollama_model
        self.solve_model = self.solve_model or self.ollama_model
        self.review_model = self.review_model or self.ollama_model

    @property
    def models(self) -> list[str]:
        return list(dict.fromkeys([self.gen_model, self.solve_model, self.review_model]))


MODEL_ROLES = {"gen": "作問", "solve": "解答", "review": "審査"}


def with_model_overrides(settings: Settings, overrides: dict[str, str]) -> Settings:
    """画面で選んだモデル（空なら .env の設定のまま）を反映した設定を返す。"""
    changes = {f"{role}_model": name for role, name in overrides.items() if role in MODEL_ROLES and name}
    return dataclasses.replace(settings, **changes) if changes else settings
