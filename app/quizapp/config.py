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


def _float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    try:
        return float(value) if value else default
    except ValueError:
        return default


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

    # Anthropic（Claude）の API。キーがなければ API は使えない（worker だけに渡す。docker-compose.yml）
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", "").strip())
    # 選べるモデル（「作る」画面の候補。名前の頭に anthropic/ が付く）
    anthropic_models: str = field(default_factory=lambda: _str(
        "ANTHROPIC_MODELS", "claude-haiku-5-5,claude-sonnet-5-5,claude-opus-5-5"))
    # 料金（ドル／100 万トークン、入力/出力）。値上げ・値下げがあったらここを直す
    anthropic_prices: str = field(default_factory=lambda: _str(
        "ANTHROPIC_PRICES", "claude-haiku-5-5=0.10/0.50,claude-sonnet-5-5=2/10,claude-opus-5-5=4/20"))
    # 考える量（low / medium / high）。考えた分も出力の料金になるので低め
    anthropic_effort: str = field(default_factory=lambda: _str("ANTHROPIC_EFFORT", "low"))
    anthropic_max_tokens: int = field(default_factory=lambda: _int("ANTHROPIC_MAX_TOKENS", 16000))
    # 利用額の上限（ドル）。「状態 → モデルと思考モード」で変えられる（画面の値が優先）。1 回の依頼の上限は 0 なら上限なし
    api_monthly_limit: float = field(default_factory=lambda: _float("API_MONTHLY_LIMIT_USD", 5.0))
    api_job_limit: float = field(default_factory=lambda: _float("API_JOB_LIMIT_USD", 0.0))

    # 問題検証モード（/review。正解つきの一覧・削除・書き出し）のパスワード。空ならパスワードなしで入れる
    review_password: str = field(default_factory=lambda: os.environ.get("REVIEW_PASSWORD", "").strip())

    timezone: str = field(default_factory=lambda: _str("DISPLAY_TZ", "Asia/Tokyo"))
    # 開発用：データの場所の目印ファイルを確認しない（テストでだけ使う）
    skip_data_check: bool = field(default_factory=lambda: _bool("SKIP_DATA_CHECK", False))

    @property
    def database_path(self) -> str:
        return os.path.join(self.data_dir, "quiz.db")

    @property
    def backup_dir(self) -> str:
        return os.path.join(self.data_dir, "backups")
