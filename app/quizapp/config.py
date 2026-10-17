"""環境変数から読む設定。docker-compose.yml / .env で上書きする。

モデルと検証回数は /status 画面からも変えられる（runtime.py。画面で選んでいなければここの値）。
"""

from __future__ import annotations

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
    data_dir: str = field(default_factory=lambda: _str("DATA_DIR", "/data"))
    sandbox_url: str = field(default_factory=lambda: _str("SANDBOX_URL", "http://sandbox:8080"))

    ollama_base_url: str = field(default_factory=lambda: _str("OLLAMA_BASE_URL", ""))
    gen_model: str = field(default_factory=lambda: _str("GEN_MODEL", "gemma4:12b-it-qat"))
    verify_model: str = field(default_factory=lambda: _str("VERIFY_MODEL", "gemma4:e4b"))
    verify_count: int = field(default_factory=lambda: _int("VERIFY_COUNT", 2))
    verify_model_2: str = field(default_factory=lambda: _str("VERIFY_MODEL_2", ""))
    num_ctx: int = field(default_factory=lambda: _int("OLLAMA_NUM_CTX", 8192))
    num_predict: int = field(default_factory=lambda: _int("OLLAMA_NUM_PREDICT", 6144))
    keep_alive: str = field(default_factory=lambda: _str("OLLAMA_KEEP_ALIVE", "10m"))
    think: bool | None = field(default_factory=_think)
    llm_timeout: int = field(default_factory=lambda: _int("LLM_TIMEOUT", 900))

    # 1 問あたり何回まで作り直すか（依頼 N 問なら最大 N × この回数だけ作る）
    max_attempts: int = field(default_factory=lambda: _int("MAX_ATTEMPTS", 3))
    # Ollama につながらないとき、次に試すまでの秒数
    retry_seconds: int = field(default_factory=lambda: _int("OLLAMA_RETRY_SECONDS", 60))

    backup_keep: int = field(default_factory=lambda: _int("BACKUP_KEEP", 14))
    backup_interval_hours: int = field(default_factory=lambda: _int("BACKUP_INTERVAL_HOURS", 24))

    # 科目ごと・端末ごとの未出題ストックがこれ未満なら「残りが少ない」と表示する
    low_stock: int = field(default_factory=lambda: _int("LOW_STOCK", 40))
    # 解き始めのセットに混ぜる「間違えた問題」の上限の割合（%）
    retry_percent: int = field(default_factory=lambda: _int("RETRY_PERCENT", 30))

    timezone: str = field(default_factory=lambda: _str("DISPLAY_TZ", "Asia/Tokyo"))
    # 開発用：データの場所の目印ファイルを確認しない（テストでだけ使う）
    skip_data_check: bool = field(default_factory=lambda: _bool("SKIP_DATA_CHECK", False))

    @property
    def database_path(self) -> str:
        return os.path.join(self.data_dir, "quiz.db")

    @property
    def backup_dir(self) -> str:
        return os.path.join(self.data_dir, "backups")
