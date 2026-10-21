"""Anthropic（Claude）の API のクライアント。worker だけが使う（web では読み込まない）。

llm.LLMRouter に「anthropic」として登録し、モデル名「anthropic/claude-haiku-5-5」のように選ぶ。
OllamaClient と同じ形（chat_json・list_models・pop_calls と label / should_abort / think_for）で使える。

トークンを安く使う工夫:
  - プロンプトキャッシュ：毎回同じシステムの指示（作成・検証・見直しの指示と出力の形）にキャッシュの印を付ける。
    2 回目以降の読み込みは約 1 割の料金（最近のモデルは 512 トークン以上の前置きならキャッシュされる）
  - 考える量（effort）は低め（既定 low。考えた分も出力の料金になる）
  - 出力は Ollama と同じ [[項目]] の見出しの平文（llm.parse_tagged）
費用：応答の usage（入力・キャッシュの書き込み・読み込み・出力のトークン数）に料金表を掛けて、呼び出しごとに
cost_usd として記録する。呼び出しの前に budget_check() を呼び、上限に達していれば LLMBudgetExceeded を投げる。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .llm import (  # noqa: F401（LLMBudgetExceeded はここからも import できるように）
    LLMBudgetExceeded,
    LLMCancelled,
    LLMError,
    LLMModelMissing,
    LLMUnavailable,
    parse_tagged,
    tagged_instructions,
)

# 断られた（refusal）ときに、サーバー側で別のモデルが続きを書く（server-side fallback）。対応するモデルだけ
FALLBACK_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# 料金（ドル／100 万トークン）の倍率：キャッシュの書き込みは入力の 1.25 倍、読み込みは 0.1 倍
CACHE_WRITE = 1.25
CACHE_READ = 0.1


def parse_prices(text: str) -> dict[str, tuple[float, float]]:
    """「claude-haiku-5-5=0.10/0.50,claude-sonnet-5-5=2/10」→ {モデル: (入力, 出力)}（ドル／100 万トークン）。"""
    out: dict[str, tuple[float, float]] = {}
    for item in (text or "").split(","):
        name, _, price = item.strip().partition("=")
        inp, _, outp = price.partition("/")
        try:
            out[name.strip()] = (float(inp), float(outp))
        except ValueError:
            continue
    return out


def cost_usd(prices: dict[str, tuple[float, float]], model: str, usage: Any) -> float:
    """1 回の呼び出しの料金。料金表にないモデルは、表のうち一番高い料金で見積もる（少なく見積もらないように）。"""
    if model in prices:
        inp, outp = prices[model]
    elif prices:
        inp, outp = max(prices.values(), key=lambda p: p[1])
    else:
        inp, outp = 0.0, 0.0
    tokens_in = getattr(usage, "input_tokens", 0) or 0
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    tokens_out = getattr(usage, "output_tokens", 0) or 0
    return (tokens_in * inp + written * inp * CACHE_WRITE + read * inp * CACHE_READ + tokens_out * outp) / 1e6


class AnthropicClient:
    def __init__(self, api_key: str, *, models: list[str], prices: dict[str, tuple[float, float]],
                 effort: str = "low", max_tokens: int = 16000, timeout: float = 600, client: Any = None) -> None:
        self.models = models
        self.prices = prices
        self.effort = effort
        self.max_tokens = max_tokens
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)
        self.client = client
        self.label = ""
        self.calls: list[dict[str, Any]] = []
        self.should_abort: Callable[[], bool] | None = None
        self.think_for: Callable[[str], bool | None] | None = None  # Ollama 用。API では effort で決める
        # 呼び出しの前に呼ぶ。上限に達していれば LLMBudgetExceeded を投げる（worker が設定する）
        self.budget_check: Callable[[], None] | None = None

    def chat_json(self, model: str, system: str, user: str, schema: dict[str, Any], *,
                  temperature: float = 0.7) -> dict[str, Any]:
        """1 回問い合わせて、schema の項目を持つ dict を返す（temperature は今のモデルでは送れないので使わない）。"""
        import anthropic

        if self.budget_check is not None:
            self.budget_check()
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_tokens,
            # 毎回同じ指示を前に置き、キャッシュの印を付ける（問題ごとに変わる内容は messages の方）
            "system": [{"type": "text", "text": system + "\n" + tagged_instructions(schema),
                        "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": {"effort": self.effort},
        }
        if model in FALLBACK_MODELS:
            params.update(betas=[FALLBACK_BETA], fallbacks="default")
        started = time.monotonic()
        try:
            with self.client.beta.messages.stream(**params) as stream:
                for _event in stream:
                    if self.should_abort is not None and self.should_abort():
                        raise LLMCancelled("取り消されました")  # with を抜けると接続が閉じ、生成も止まる
                message = stream.get_final_message()
        except LLMCancelled:
            raise
        except anthropic.NotFoundError as exc:
            raise LLMModelMissing(f"モデル {model} が Anthropic の API に見つかりません（{exc.message}）") from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMModelMissing(f"Anthropic の API キーが使えません（.env の ANTHROPIC_API_KEY を確かめてください）: "
                                  f"{exc.message}") from exc
        except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError) as exc:
            raise LLMUnavailable(f"Anthropic の API に今つながりません: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LLMUnavailable(f"Anthropic の API がエラーを返しました（{exc.status_code}）") from exc
            raise LLMError(f"Anthropic の API がエラーを返しました（{exc.status_code}）: {exc.message}") from exc
        text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
        self._record(model, getattr(message, "model", model) or model, time.monotonic() - started, message.usage,
                     message.stop_reason)
        if message.stop_reason == "max_tokens":
            raise LLMError(f"出力の上限（max_tokens={self.max_tokens}）で途中で切れました", raw=text)
        if message.stop_reason == "refusal":
            raise LLMError("モデルが答えるのを断りました（refusal）", raw=text)
        return parse_tagged(text, schema)

    def _record(self, model: str, served: str, seconds: float, usage: Any, stop_reason: str | None) -> None:
        tokens = int(getattr(usage, "output_tokens", 0) or 0)
        self.calls.append({
            "at": time.time(), "model": f"anthropic/{model}", "label": self.label, "seconds": round(seconds, 1),
            "load_s": 0.0, "prompt_tokens": int(getattr(usage, "input_tokens", 0) or 0), "tokens": tokens,
            # API では出力の時間だけを分けられないので、全体の時間を出力の時間とみなす（依頼ごとの比較用）
            "eval_s": round(seconds, 2), "tok_per_s": round(tokens / seconds, 1) if seconds > 0 else 0.0,
            "cut_off": stop_reason == "max_tokens",
            "cache_read": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            "cache_write": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            "cost_usd": round(cost_usd(self.prices, served, usage), 6),
        })

    def pop_calls(self) -> list[dict[str, Any]]:
        calls, self.calls = self.calls, []
        return calls

    def list_models(self) -> list[dict[str, Any]]:
        return [{"name": m} for m in self.models]
