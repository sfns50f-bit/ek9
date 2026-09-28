"""Ollama の /api/chat を JSON スキーマ付きで呼ぶクライアント。"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx


class LLMError(RuntimeError):
    """モデルの出力が使えない（JSON でない、項目が足りないなど）。"""


class LLMUnavailable(RuntimeError):
    """Ollama に接続できない、またはモデルが見つからない。"""


def extract_json(text: str) -> dict[str, Any]:
    """出力から JSON オブジェクトを取り出す。```json で囲まれていても読む。"""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("モデルの出力に JSON が見つかりません") from None
        try:
            data = json.loads(text[start:end + 1])
        except ValueError as exc:
            raise LLMError("モデルの出力の JSON が壊れています") from exc
    if not isinstance(data, dict):
        raise LLMError("モデルの出力が JSON オブジェクトではありません")
    return data


class OllamaClient:
    def __init__(self, base_url: str, *, timeout: float = 900, num_ctx: int = 8192,
                 num_predict: int = 6144, keep_alive: str = "15m", think: bool | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.keep_alive = keep_alive
        self.think = think

    def chat_json(self, model: str, system: str, user: str, schema: dict[str, Any], *,
                  temperature: float = 0.7) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": schema,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": temperature,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            },
        }
        # gemma4 では think=false を送ると format（JSON スキーマ）が効かなくなる不具合があるため、
        # 既定では think を送らない。
        if self.think is not None:
            body["think"] = self.think
        try:
            resp = httpx.post(f"{self.base_url}/api/chat", json=body, timeout=self.timeout)
        except httpx.TransportError as exc:
            raise LLMUnavailable(f"Ollama に接続できません（{self.base_url}）: {exc}") from exc
        if resp.status_code == 404:
            raise LLMUnavailable(f"モデル {model} が見つかりません。`ollama pull {model}` を実行してください")
        if resp.status_code >= 400:
            raise LLMError(f"Ollama がエラーを返しました（{resp.status_code}）: {resp.text[:300]}")
        message = resp.json().get("message") or {}
        return extract_json(message.get("content", ""))

    def list_models(self) -> list[str]:
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"Ollama に接続できません（{self.base_url}）: {exc}") from exc
        return [m.get("name", "") for m in resp.json().get("models", [])]
