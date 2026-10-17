"""数式サンドボックス（別コンテナ）の HTTP クライアント。"""

from __future__ import annotations

from typing import Any

import httpx


class SandboxUnavailable(RuntimeError):
    pass


class SandboxClient:
    def __init__(self, base_url: str, timeout: float = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = httpx.post(f"{self.base_url}/{path}", json=payload, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SandboxUnavailable(f"サンドボックスに接続できません: {exc}") from exc
        if not isinstance(data, dict):
            raise SandboxUnavailable("サンドボックスの応答が不正です")
        return data

    def evaluate(self, code: str, kind: str) -> dict[str, Any]:
        """検算コードを実行し、変数 answer の値を返す。"""
        return self._post("evaluate", {"code": code, "kind": kind})

    def compare(self, kind: str, expected: dict[str, str], given: dict[str, str], *,
                var: str = "x", allow_float: bool = False) -> dict[str, Any]:
        """expected / given は {"srepr": ...}（保存済みの答え）か {"text": ...}（入力文字列）。"""
        return self._post("compare", {
            "kind": kind, "expected": expected, "given": given, "var": var, "allow_float": allow_float,
        })

    def preview(self, text: str, kind: str, var: str = "x") -> dict[str, Any]:
        return self._post("preview", {"text": text, "kind": kind, "var": var})

    def healthy(self) -> bool:
        try:
            return httpx.get(f"{self.base_url}/healthz", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False
