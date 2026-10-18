"""Ollama の /api/chat を JSON スキーマ付きで呼ぶクライアント。"""

from __future__ import annotations

import http.client
import json
import re
import socket
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx


class LLMError(RuntimeError):
    """モデルの出力が使えない（JSON でない、項目が足りないなど）。"""


class LLMUnavailable(RuntimeError):
    """Ollama に接続できない、またはモデルが見つからない。"""


class LLMCancelled(RuntimeError):
    """呼び出し中に取り消された（should_abort が真になった）。"""


CONNECT_TIMEOUT = 10
ABORT_CHECK_SECONDS = 0.5


def _hang_up(conn: http.client.HTTPConnection) -> None:
    """読んでいる途中の接続を切る。shutdown しないと、別スレッドの recv が起きずに接続が残る。"""
    sock = conn.sock
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


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
        self.label = ""  # 呼び出しの種類（作成・直し・検証）。worker が呼ぶ前に設定する
        self.calls: list[dict[str, Any]] = []
        # 真を返したら、呼び出し中でも接続を切って LLMCancelled を投げる（worker が依頼の取り消しを確かめる）
        self.should_abort: Callable[[], bool] | None = None

    def chat_json(self, model: str, system: str, user: str, schema: dict[str, Any], *,
                  temperature: float = 0.7) -> dict[str, Any]:
        """JSON スキーマ付きで 1 回問い合わせる。

        応答はストリーミングで受け取り、別のスレッドで読む。呼び出し中も約 1 秒ごとに
        should_abort() を確かめ、真なら接続を切って LLMCancelled を投げる（接続が切れると
        Ollama は生成をやめるので、デスクトップの GPU も止まる）。
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": True,
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
        if not self.base_url:
            raise LLMUnavailable("OLLAMA_BASE_URL が設定されていません（.env を確認してください）")
        started = time.monotonic()
        state = self._stream("/api/chat", body)
        status = state.get("status")
        if status == 404:
            raise LLMUnavailable(f"モデル {model} が見つかりません。`ollama pull {model}` を実行してください")
        if status is not None and status >= 500:
            # GPU のメモリ不足やモデルの読み込み失敗など。作り直しの回数を使わずに、後で再試行する
            raise LLMUnavailable(f"Ollama がエラーを返しました（{status}）: {state.get('text', '')[:300]}")
        if status is not None and status >= 400:
            raise LLMError(f"Ollama がエラーを返しました（{status}）: {state.get('text', '')[:300]}")
        if state.get("error"):
            raise LLMError(f"Ollama がエラーを返しました: {state['error'][:300]}")
        self._record(model, time.monotonic() - started, state.get("final") or {})
        return extract_json("".join(state.get("content", [])))

    def _stream(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST して、改行区切りの JSON の応答を読み切る。取り消し・時間切れのときは接続を切る。"""
        url = urlsplit(self.base_url)
        conn_cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(url.hostname or "", url.port, timeout=CONNECT_TIMEOUT)
        try:
            conn.connect()
        except OSError as exc:
            raise LLMUnavailable(f"Ollama に接続できません（{self.base_url}）: {exc}") from exc
        assert conn.sock is not None
        conn.sock.settimeout(self.timeout)  # 応答が途切れてもこの秒数までは待つ
        state: dict[str, Any] = {"content": []}

        def read() -> None:
            try:
                conn.request("POST", url.path.rstrip("/") + path, body=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                state["status"] = resp.status
                if resp.status >= 400:
                    state["text"] = resp.read().decode("utf-8", "replace")
                    return
                for raw in resp:
                    line = raw.strip()
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        state["error"] = str(chunk["error"])
                        return
                    state["content"].append((chunk.get("message") or {}).get("content", ""))
                    if chunk.get("done"):
                        state["final"] = chunk
                        return
            except Exception as exc:  # 取り消しで接続を切ったときもここに来る
                state["exc"] = exc

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        deadline = time.monotonic() + self.timeout
        try:
            while thread.is_alive():
                thread.join(ABORT_CHECK_SECONDS)
                if not thread.is_alive():
                    break
                if self.should_abort is not None and self.should_abort():
                    raise LLMCancelled("取り消されました")
                if time.monotonic() > deadline:
                    raise LLMUnavailable(f"Ollama が {self.timeout:.0f} 秒以内に応答を終えませんでした")
        finally:
            if thread.is_alive():
                _hang_up(conn)
                thread.join(2)
            conn.close()
        exc = state.get("exc")
        if exc is not None:
            if isinstance(exc, (OSError, http.client.HTTPException)):
                raise LLMUnavailable(f"Ollama との通信が切れました（{self.base_url}）: {exc}") from exc
            raise LLMError(f"Ollama の応答を読めません: {exc}") from exc
        return state

    def list_models(self) -> list[dict[str, Any]]:
        """ダウンロード済みのモデル（埋め込み専用モデルは除く）。"""
        if not self.base_url:
            raise LLMUnavailable("OLLAMA_BASE_URL が設定されていません（.env を確認してください）")
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=3)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"Ollama に接続できません（{self.base_url}）: {exc}") from exc
        models = []
        for m in resp.json().get("models", []):
            name = m.get("name", "")
            if not name or "embed" in name:
                continue
            details = m.get("details") or {}
            models.append({
                "name": name,
                "size_gb": round((m.get("size") or 0) / 1e9, 1),
                "parameter_size": details.get("parameter_size", ""),
                "quantization": details.get("quantization_level", ""),
            })
        return sorted(models, key=lambda m: m["name"])
