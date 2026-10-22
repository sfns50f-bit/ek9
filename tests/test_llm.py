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


class FakeAnthropic:
    """Anthropic の Messages API（ストリーミング）のふりをする小さな HTTP サーバー。"""

    def __init__(self, text, *, status=200, stop_reason="end_turn", model="claude-haiku-5-5"):
        self.bodies, self.headers = [], []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                outer.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                outer.headers.append(dict(self.headers))
                if status != 200:
                    body = json.dumps({"type": "error", "error": {"type": "not_found_error", "message": "model"}})
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body.encode())
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                usage = {"input_tokens": 100, "output_tokens": 0, "cache_creation_input_tokens": 1000,
                         "cache_read_input_tokens": 0}
                events = [
                    ("message_start", {"type": "message_start", "message": {
                        "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
                        "stop_reason": None, "stop_sequence": None, "usage": usage}}),
                    ("content_block_start", {"type": "content_block_start", "index": 0,
                                             "content_block": {"type": "text", "text": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                             "delta": {"type": "text_delta", "text": text}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason,
                                                                          "stop_sequence": None},
                                       "usage": {"output_tokens": 500}}),
                    ("message_stop", {"type": "message_stop"}),
                ]
                for name, data in events:
                    self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
                    self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def _anthropic_client(server, **kw):
    import anthropic
    from quizapp.llm_anthropic import AnthropicClient, parse_prices

    sdk = anthropic.Anthropic(api_key="test-key", base_url=server.url, max_retries=0)
    prices = parse_prices("claude-haiku-5-5=0.10/0.50,claude-sonnet-5-5=2/10")
    return AnthropicClient("test-key", models=["claude-haiku-5-5"], prices=prices, client=sdk, **kw)


def test_anthropic_client_reads_tagged_text_records_cost_and_caches_system():
    server = FakeAnthropic("[[answer]]\n$120^\\circ$ と \\angle A")
    client = _anthropic_client(server)
    client.label = "検証"
    assert client.chat_json("claude-haiku-5-5", "sys", "user", SCHEMA) == {"answer": "$120^\\circ$ と \\angle A"}
    body = server.bodies[0]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"} and "[[answer]]" in body["system"][0]["text"]
    assert body["output_config"] == {"effort": "low"} and "temperature" not in body and "fallbacks" not in body
    (call,) = client.pop_calls()
    # (100 × 0.10 + 1000 × 0.10 × 1.25 + 500 × 0.50) / 100 万 ドル
    assert call["cost_usd"] == round((100 * 0.10 + 1000 * 0.10 * 1.25 + 500 * 0.50) / 1e6, 6)
    assert call["model"] == "anthropic/claude-haiku-5-5" and call["label"] == "検証" and call["tokens"] == 500
    server.server.shutdown()


def test_anthropic_client_errors_fallbacks_and_budget():
    from quizapp.llm import LLMError, LLMModelMissing
    from quizapp.llm_anthropic import LLMBudgetExceeded

    server = FakeAnthropic("[[answer]]\n1", model="claude-sonnet-5-5")
    client = _anthropic_client(server)
    client.chat_json("claude-sonnet-5-5", "sys", "user", SCHEMA)
    # 断られたときにサーバー側で別のモデルが続ける（fallbacks）。対応するモデルだけ
    assert server.bodies[0]["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in server.headers[0].get("anthropic-beta", "")
    with pytest.raises(LLMError, match="出力の上限"):
        _anthropic_client(FakeAnthropic("[[answer]]\n1", stop_reason="max_tokens")).chat_json(
            "claude-haiku-5-5", "s", "u", SCHEMA)
    with pytest.raises(LLMModelMissing, match="見つかりません"):
        _anthropic_client(FakeAnthropic("", status=404)).chat_json("claude-nope", "s", "u", SCHEMA)
    client = _anthropic_client(server)

    def over():
        raise LLMBudgetExceeded("上限")

    client.budget_check = over
    n = len(server.bodies)
    with pytest.raises(LLMBudgetExceeded):
        client.chat_json("claude-haiku-5-5", "s", "u", SCHEMA)
    assert len(server.bodies) == n  # 上限なら呼ばない


def test_anthropic_cancel_stops_streaming():
    server = FakeAnthropic("[[answer]]\n1")
    client = _anthropic_client(server)
    client.should_abort = lambda: True
    with pytest.raises(LLMCancelled):
        client.chat_json("claude-haiku-5-5", "s", "u", SCHEMA)


def test_anthropic_bad_request_and_billing_fail_fast():
    """400（このモデルで使えない指定など）・402（支払い）は、問題ごとに作り直しても直らないので依頼を止める。"""
    from quizapp.llm import LLMModelMissing

    for status in (400, 402):
        with pytest.raises(LLMModelMissing):
            _anthropic_client(FakeAnthropic("", status=status)).chat_json("claude-haiku-5-5", "s", "u", SCHEMA)
