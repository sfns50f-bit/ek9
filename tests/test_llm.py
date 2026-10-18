"""Ollama のクライアント：ストリーミングでの受け取り、取り消しで数秒以内に接続を切ること。"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from quizapp.llm import LLMCancelled, LLMUnavailable, OllamaClient

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}


class FakeOllama:
    """モードに応じて応答する小さな HTTP サーバー。"""

    def __init__(self, mode: str):
        self.mode = mode
        self.disconnected = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers["Content-Length"]))
                if outer.mode == "missing":
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'{"error":"model not found"}')
                    return
                if outer.mode == "loading":
                    time.sleep(30)  # モデルの読み込み中（ヘッダーもまだ返さない）
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                pieces = ['{"answer": ', '"42"}'] if outer.mode == "ok" else [" "] * 300
                try:
                    for piece in pieces:
                        chunk = {"message": {"content": piece}, "done": False}
                        self.wfile.write((json.dumps(chunk) + "\n").encode())
                        self.wfile.flush()
                        if outer.mode == "slow":
                            time.sleep(0.1)
                    final = {"message": {"content": ""}, "done": True, "load_duration": 2_000_000_000,
                             "eval_count": 10, "eval_duration": 500_000_000, "prompt_eval_count": 5}
                    self.wfile.write((json.dumps(final) + "\n").encode())
                except (BrokenPipeError, ConnectionResetError):
                    outer.disconnected.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake():
    servers = []

    def make(mode):
        servers.append(FakeOllama(mode))
        return servers[-1]

    yield make
    for s in servers:
        s.close()


def test_streamed_reply_is_joined_and_timed(fake):
    server = fake("ok")
    client = OllamaClient(server.url)
    client.label = "作成"
    assert client.chat_json("m", "sys", "user", SCHEMA) == {"answer": "42"}
    (call,) = client.pop_calls()
    assert call["load_s"] == 2.0 and call["eval_s"] == 0.5 and call["tokens"] == 10 and call["label"] == "作成"


@pytest.mark.parametrize("mode", ["loading", "slow"])
def test_cancel_stops_within_seconds(fake, mode):
    """生成の途中（モデルの読み込み中も含む）で取り消すと、数秒以内に止まる。"""
    server = fake(mode)
    client = OllamaClient(server.url)
    started = time.monotonic()
    client.should_abort = lambda: time.monotonic() - started > 0.5
    with pytest.raises(LLMCancelled):
        client.chat_json("m", "sys", "user", SCHEMA)
    assert time.monotonic() - started < 3
    if mode == "slow":
        # 接続が切れたことがサーバー側にも伝わる（Ollama はこれで生成をやめる）
        assert server.disconnected.wait(5)


def test_missing_model_and_unreachable(fake):
    server = fake("missing")
    with pytest.raises(LLMUnavailable, match="見つかりません"):
        OllamaClient(server.url).chat_json("m", "sys", "user", SCHEMA)
    with pytest.raises(LLMUnavailable, match="接続できません"):
        OllamaClient("http://127.0.0.1:9").chat_json("m", "sys", "user", SCHEMA)
