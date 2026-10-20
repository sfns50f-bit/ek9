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
        self.bodies = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                outer.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
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
                pieces = {"ok": ['{"answer": ', '"42"}'], "cut": ['{"answer": "4'],
                          "tagged": ["[[answer]]\n$120^", "\\circ$ と \\angle A\n"]}.get(outer.mode, [" "] * 300)
                try:
                    for piece in pieces:
                        chunk = {"message": {"content": piece}, "done": False}
                        self.wfile.write((json.dumps(chunk) + "\n").encode())
                        self.wfile.flush()
                        if outer.mode == "slow":
                            time.sleep(0.1)
                    final = {"message": {"content": ""}, "done": True, "load_duration": 2_000_000_000,
                             "done_reason": "length" if outer.mode == "cut" else "stop",
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
    from quizapp.llm import LLMModelMissing

    server = fake("missing")
    with pytest.raises(LLMModelMissing, match="見つかりません"):
        OllamaClient(server.url).chat_json("m", "sys", "user", SCHEMA)
    with pytest.raises(LLMUnavailable, match="接続できません"):
        OllamaClient("http://127.0.0.1:9").chat_json("m", "sys", "user", SCHEMA)


def test_output_cut_off_by_num_predict_is_reported(fake):
    from quizapp.llm import LLMError

    server = fake("cut")
    with pytest.raises(LLMError, match="出力の上限") as exc:
        OllamaClient(server.url).chat_json("m", "sys", "user", SCHEMA)
    assert exc.value.raw == '{"answer": "4'


def test_latex_backslashes_and_thinking_in_json_are_read():
    from quizapp.llm import extract_json

    raw = '{"answer": "2\\sqrt{21}", "work": "\\frac{1}{2} \\cdot x"}'.replace("\\\\", "\\")
    assert extract_json(raw) == {"answer": "2\\sqrt{21}", "work": "\\frac{1}{2} \\cdot x"}
    assert extract_json('<think>{maybe}</think>{"answer": "3"}') == {"answer": "3"}
    assert extract_json('```json\n{"answer": "3"}\n```') == {"answer": "3"}


def test_router_sends_api_models_to_their_provider(fake):
    """モデル名の頭に「提供元/」があれば API の提供元へ。設定されていなければモデルが見つからない扱い。"""
    from quizapp.llm import LLMModelMissing, LLMRouter, provider_of

    assert provider_of("gemma4:e4b") == ("ollama", "gemma4:e4b")
    assert provider_of("anthropic/claude-x") == ("anthropic", "claude-x")
    server = fake("ok")

    class FakeApi:
        label, should_abort, think_for = "", None, None

        def __init__(self):
            self.seen = []

        def chat_json(self, model, system, user, schema, temperature=0.7):
            self.seen.append((model, self.label))
            return {"answer": "api"}

        def list_models(self):
            return [{"name": "claude-x"}]

        def pop_calls(self):
            return []

    api = FakeApi()
    router = LLMRouter(OllamaClient(server.url), {"anthropic": api})
    router.label = "検証"
    assert router.chat_json("anthropic/claude-x", "s", "u", SCHEMA) == {"answer": "api"}
    assert api.seen == [("claude-x", "検証")]
    assert router.chat_json("gemma4:e4b", "s", "u", SCHEMA) == {"answer": "42"}
    with pytest.raises(LLMModelMissing, match="openai"):
        LLMRouter(OllamaClient(server.url)).chat_json("openai/gpt-x", "s", "u", SCHEMA)


def test_output_is_tagged_text_not_json_schema(fake):
    """JSON スキーマ（format）は送らない。[[項目]] の見出しの平文で受け取り、バックスラッシュはそのまま届く。"""
    server = fake("tagged")
    out = OllamaClient(server.url).chat_json("m", "sys", "user", SCHEMA)
    assert out == {"answer": "$120^\\circ$ と \\angle A"}
    body = server.bodies[0]
    assert "format" not in body and "[[answer]]" in body["messages"][0]["content"]


def test_tagged_output_parsing():
    from quizapp.llm import LLMError, parse_tagged

    schema = {"properties": {"question": {}, "hint": {}, "explanation": {}},
              "required": ["question", "hint", "explanation"]}
    text = "<think>考え中</think>\n[[question]]\n問題 $x^2$\n[[hint]] 公式\n[[explanation]]\n```\n解説\n```\n"
    assert parse_tagged(text, schema) == {"question": "問題 $x^2$", "hint": "公式", "explanation": "解説"}
    with pytest.raises(LLMError, match="hint"):
        parse_tagged("[[question]]\nq\n[[explanation]]\ne", schema)


def test_tagged_output_variants():
    from quizapp.llm import parse_tagged

    schema = {"properties": {"question": {}, "hint": {}}, "required": ["question", "hint"]}
    assert parse_tagged("**[[question]]**\nQ\n## [[ Hint ]]:\nH", schema) == {"question": "Q", "hint": "H"}
    # 同じ見出しが 2 回出たら後のもの（前回の出力を書き写してから直したものを書いた）
    assert parse_tagged("[[question]]\nold\n[[hint]]\nold\n[[question]]\nnew\n[[hint]]\nnew", schema) == \
        {"question": "new", "hint": "new"}
    # 開きタグのない思考、閉じていない思考
    assert parse_tagged("下書き\n</think>[[question]]\nQ\n[[hint]]\nH", schema) == {"question": "Q", "hint": "H"}
    assert parse_tagged("[[question]]\nQ\n[[hint]]\nH\n<think>まだ考え中", schema) == {"question": "Q", "hint": "H"}
