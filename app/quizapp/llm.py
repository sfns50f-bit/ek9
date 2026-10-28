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

from .runtime import API_PROVIDERS, provider_of  # noqa: F401（モデル名の「提供元/」の読み方は runtime にある）


class LLMError(RuntimeError):
    """モデルの出力が使えない（JSON でない、項目が足りないなど）。raw はモデルの生の出力。"""

    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


class LLMUnavailable(RuntimeError):
    """Ollama に接続できない、またはモデルが見つからない。"""


class LLMModelMissing(RuntimeError):
    """選んだモデルが見つからない（Ollama に入っていない、API の提供元が設定されていない）。

    待っても直らないので、その依頼は失敗で止める（LLMUnavailable のように再試行しない）。
    """


class LLMBudgetExceeded(RuntimeError):
    """API の利用額が上限に達した。待っても直らないので、その依頼は失敗で止める（LLMError ではない）。"""


class LLMCancelled(RuntimeError):
    """呼び出し中に取り消された（should_abort が真になった）。"""


CONNECT_TIMEOUT = 10
ABORT_CHECK_SECONDS = 0.5


def _hang_up(sock: socket.socket) -> None:
    """読んでいる途中の接続を切る。shutdown しないと、別スレッドの recv が起きずに接続が残る。"""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


# JSON として正しくないバックスラッシュ（\sqrt の \s、\cdot の \c など）。直前のバックスラッシュの数が
# 偶数（= エスケープされていない）ときだけ二重にする
_BAD_ESCAPE_RE = re.compile(r'(?<!\\)((?:\\\\)*)\\(?![\\"/bfnrtu])')
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        # LaTeX の \sqrt などが 1 本のバックスラッシュのまま入っていると JSON として読めないので直して読む
        return json.loads(_BAD_ESCAPE_RE.sub(lambda m: m.group(1) + "\\\\", text))


def extract_json(text: str) -> dict[str, Any]:
    """出力から JSON オブジェクトを取り出す。```json で囲まれていても、思考（<think>）が混ざっていても読む。"""
    raw = text or ""
    text = _THINK_RE.sub("", raw).strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        data = _loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("モデルの出力に JSON が見つかりません", raw=raw) from None
        try:
            data = _loads(text[start:end + 1])
        except ValueError as exc:
            raise LLMError("モデルの出力の JSON が壊れています", raw=raw) from exc
    if not isinstance(data, dict):
        raise LLMError("モデルの出力が JSON オブジェクトではありません", raw=raw)
    return _fix_controls(data)


# JSON では "\frac" が改ページ文字 + "rac"、"\times" がタブ + "imes" として正しく読めてしまう。
# 後ろに英字が続く制御文字は LaTeX のバックスラッシュだったとみなして戻す（改行 \n は本当の改行が多いので除く）
_CTRL_LETTER_RE = re.compile(r"[\x08\x0c\t\r](?=[A-Za-z])")
_CTRL_NAMES = {"\x08": "b", "\x0c": "f", "\t": "t", "\r": "r"}


def _fix_controls(value: Any) -> Any:
    if isinstance(value, str):
        return _CTRL_LETTER_RE.sub(lambda m: "\\" + _CTRL_NAMES[m.group()], value)
    if isinstance(value, dict):
        return {k: _fix_controls(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_fix_controls(v) for v in value]
    return value


# --- 出力の形 -----------------------------------------------------------------
# Ollama の format（JSON スキーマ）を使うと、文字列の中のバックスラッシュの後ろに JSON で許される文字
# （" \\ / b f n r t u）しか出せなくなる。モデルが \\circ や \\angle を 1 本のバックスラッシュで書こうとすると
# \\top・\\times・\\rho・ngle・\\u00b0 のように別の文字に押し曲げられて、LaTeX が壊れていた。
# そこで JSON を使わず、[[question]] のような見出しで区切った平文で書かせる（バックスラッシュはそのまま届く）。
# 見出しの揺れも読む：**[[question]]**、## [[question]]、[[ Question ]]:
_TAG_RE = re.compile(r"^[ \t#>*_]*\[\[\s*([A-Za-z][A-Za-z0-9_]*)\s*\]\][*_ \t:：]*(.*)$", re.M)
_FENCE_RE = re.compile(r"^\s*```[A-Za-z]*\s*$", re.M)


def tagged_instructions(schema: dict[str, Any]) -> str:
    """見出しつきの平文で出力させるための指示（システムプロンプトの最後に付ける）。"""
    lines = ["", "出力の形（JSON は使わない。数式のバックスラッシュは 1 本のまま書く）:"]
    for key, prop in schema.get("properties", {}).items():
        lines += [f"[[{key}]]", f"（{prop.get('description', key)}をここに書く）"]
    lines.append("見出し [[…]] は上のとおり 1 行に書き、この形以外のもの（前置き・まとめ・``` など）は書かない。")
    return "\n".join(lines)


def parse_tagged(text: str, schema: dict[str, Any]) -> dict[str, Any]:
    """[[key]] の見出しで区切った出力を dict にする。見出しがなければ JSON として読んでみる。"""
    raw = text or ""
    body = _THINK_RE.sub("", raw)
    if "</think>" in body:  # 開きタグがないまま思考が出てきたとき（チャットのテンプレートが <think> を付けた）
        body = body.rsplit("</think>", 1)[1]
    body = body.split("<think>", 1)[0]  # 閉じていない思考の続きは捨てる
    keys = list(schema.get("properties", {}))
    found = [m for m in _TAG_RE.finditer(body) if m.group(1).lower() in keys]
    if not found:
        try:
            data = extract_json(body)
        except LLMError:
            required = schema.get("required", [])
            main = keys[0] if len(keys) == 1 else (required[0] if len(required) == 1 else "")
            if main and body.strip():
                # 答えの項目が 1 つだけのとき（本文なしの確認の記号、正解を抜いた判定の番号、スペルの語の一覧など）は、
                # 見出しを書かずに答えだけ返すモデルが多い。そのときは出力全体をその項目とみなす
                # （第 5 回のレビュー A-1、第 7 回のレビュー A：「4」だけの返事を読めず、要確認の大半になっていた）
                data = {main: _FENCE_RE.sub("", body).strip()}
            else:
                raise LLMError("出力に見出し（[[" + "]] [[".join(keys) + "]]）が見つかりません", raw=raw) from None
    else:
        data = {}
        for i, m in enumerate(found):
            end = found[i + 1].start() if i + 1 < len(found) else len(body)
            value = (m.group(2) + "\n" + body[m.end():end]).strip()
            # 同じ見出しが何度も出たら後のものを使う（前回の出力を書き写してから直したものを書く、など）
            data[m.group(1).lower()] = _FENCE_RE.sub("", value).strip()
    missing = [k for k in schema.get("required", []) if not str(data.get(k, "")).strip()]
    if len(missing) == 1 and missing[0] == "answer" and found:
        # 答えの見出しの代わりに答えそのものを見出しの形で書いた返事（「[[1]]」「[[なし]]」の次に [[reason]]）は、
        # 最初の見出しより前に書いた短い答えをその項目とみなす（第 10 回のレビュー 4-5。思考なしの e4b の #136）
        head = _FENCE_RE.sub("", body[:found[0].start()]).strip()
        inner = re.fullmatch(r"\[\[\s*([^\[\]\n]{1,20}?)\s*\]\]|([0-9０-９]{1,2}|なし|両方)", head)
        if inner:
            data[missing[0]] = (inner.group(1) or inner.group(2)).strip()
            missing = []
    if missing:
        raise LLMError("出力に " + "・".join(f"[[{k}]]" for k in missing) + " がありません", raw=raw)
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
        # モデル名 → 思考モード（True / False / None=送らない）。worker が /status の設定から決める
        self.think_for: Callable[[str], bool | None] | None = None

    def chat_json(self, model: str, system: str, user: str, schema: dict[str, Any], *,
                  temperature: float = 0.7) -> dict[str, Any]:
        """1 回問い合わせて、schema の項目を持つ dict を返す。

        Ollama の format（JSON スキーマ）は使わず、[[項目名]] の見出しで区切った平文で書かせて読む
        （tagged_instructions / parse_tagged。理由は上の「出力の形」）。
        応答はストリーミングで受け取り、別のスレッドで読む。呼び出し中も約 1 秒ごとに
        should_abort() を確かめ、真なら接続を切って LLMCancelled を投げる（接続が切れると
        Ollama は生成をやめるので、デスクトップの GPU も止まる）。
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system + "\n" + tagged_instructions(schema)},
                {"role": "user", "content": user},
            ],
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": temperature,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            },
        }
        # 思考モードはモデルごとに決める（runtime.resolve_think。「あり」「なし」を明示して送る）
        think = self.think_for(model) if self.think_for is not None else self.think
        if think is not None:
            body["think"] = think
        if not self.base_url:
            raise LLMUnavailable("OLLAMA_BASE_URL が設定されていません（.env を確認してください）")
        started = time.monotonic()
        state = self._stream("/api/chat", body)
        status = state.get("status")
        if status == 400 and "think" in body and "thinking" in str(state.get("text", "")).lower():
            # 思考に対応していないモデル（「… does not support thinking」）。思考の指定を外して送り直す（思考なしで動く）
            body.pop("think")
            state = self._stream("/api/chat", body)
            status = state.get("status")
        if status == 404:
            raise LLMModelMissing(f"モデル {model} が Ollama に見つかりません。`ollama pull {model}` を実行するか、"
                                  "別のモデルを選んでください")
        if status is not None and status >= 500:
            # GPU のメモリ不足やモデルの読み込み失敗など。作り直しの回数を使わずに、後で再試行する
            raise LLMUnavailable(f"Ollama がエラーを返しました（{status}）: {state.get('text', '')[:300]}")
        if status is not None and status >= 400:
            raise LLMError(f"Ollama がエラーを返しました（{status}）: {state.get('text', '')[:300]}")
        if state.get("error"):
            raise LLMError(f"Ollama がエラーを返しました: {state['error'][:300]}")
        final = state.get("final") or {}
        self._record(model, time.monotonic() - started, final)
        raw = "".join(state.get("content", []))
        thinking = "".join(state.get("thinking", []))
        # 思考の部分（Ollama の message.thinking）も、出力が読めなかったときの記録に残す（第 4 回のレビュー E-4）
        shown = raw + (f"\n\n[思考の部分（message.thinking）の最後]\n{thinking[-2000:]}" if thinking.strip() else "")
        if final.get("done_reason") == "length":
            raise LLMError(f"出力の上限（num_predict={self.num_predict}）で途中で切れました", raw=shown)
        try:
            return parse_tagged(raw, schema)
        except LLMError as exc:
            if thinking.strip() and not raw.strip():
                # 答えまで思考の部分に書いてしまったとき（本文が空）は、思考の部分から読んでみる
                try:
                    return parse_tagged(thinking, schema)
                except LLMError:
                    pass
            raise LLMError(str(exc), raw=shown) from None

    def _stream(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST して、改行区切りの JSON の応答を読み切る。取り消し・時間切れのときは接続を切る。"""
        url = urlsplit(self.base_url)
        conn_cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(url.hostname or "", url.port, timeout=CONNECT_TIMEOUT)
        try:
            conn.connect()
        except OSError as exc:
            raise LLMUnavailable(f"Ollama に接続できません（{self.base_url}）: {exc}") from exc
        # 自分で持っておく（応答が Connection: close のとき、http.client は conn.sock を None にして
        # ソケットを応答側に渡してしまい、conn.sock からは切れなくなるため）
        sock = conn.sock
        assert sock is not None
        sock.settimeout(self.timeout)  # 応答が途切れてもこの秒数までは待つ
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
                    message = chunk.get("message") or {}
                    state["content"].append(message.get("content", ""))
                    if message.get("thinking"):
                        state.setdefault("thinking", []).append(message["thinking"])
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
                _hang_up(sock)
                thread.join(2)
            conn.close()
            sock.close()
        exc = state.get("exc")
        if exc is not None:
            if isinstance(exc, (OSError, http.client.HTTPException)):
                raise LLMUnavailable(f"Ollama との通信が切れました（{self.base_url}）: {exc}") from exc
            raise LLMError(f"Ollama の応答を読めません: {exc}") from exc
        return state

    def _record(self, model: str, seconds: float, data: dict[str, Any]) -> None:
        """1 回の呼び出しの時間の内訳を残す（/status の「最近の LLM 呼び出し」と依頼ごとの集計で使う）。

        Ollama の応答には、モデルの読み込みにかかった時間（load_duration）、出力したトークン数
        （eval_count、考えている分も含む）とその時間（eval_duration）が入っている（単位はナノ秒）。
        """
        ns = 1e9
        eval_s = (data.get("eval_duration") or 0) / ns
        tokens = int(data.get("eval_count") or 0)
        self.calls.append({
            "at": time.time(), "model": model, "label": self.label, "seconds": round(seconds, 1),
            "load_s": round((data.get("load_duration") or 0) / ns, 2),
            "prompt_tokens": int(data.get("prompt_eval_count") or 0), "tokens": tokens, "eval_s": round(eval_s, 2),
            "tok_per_s": round(tokens / eval_s, 1) if eval_s > 0 else 0.0,
            "cut_off": data.get("done_reason") == "length",
        })

    def pop_calls(self) -> list[dict[str, Any]]:
        calls, self.calls = self.calls, []
        return calls

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


# ---------------------------------------------------------------------------
# 提供元ごとの振り分け（将来 API で作成するときの入口）
# ---------------------------------------------------------------------------

# モデル名の頭に「提供元/」を付けると、その提供元の API に振り分ける（例: anthropic/claude-…）。
# 付けなければ Ollama。Ollama のモデル名は「gemma4:e4b」のように / を含まないので区別できる。
# 提供元を足すときは、OllamaClient と同じ形（chat_json・list_models・pop_calls と、属性 label・
# should_abort・think_for）のクラスを作り、LLMRouter の providers に登録する（CLAUDE.md の手順を参照）。
class LLMRouter:
    """モデル名で提供元のクライアントに振り分ける。worker からは 1 つのクライアントに見える。"""

    def __init__(self, ollama: Any, providers: dict[str, Any] | None = None) -> None:
        self.providers: dict[str, Any] = {"ollama": ollama, **(providers or {})}
        self._label = ""
        self._should_abort: Callable[[], bool] | None = None
        self._think_for: Callable[[str], bool | None] | None = None
        self._budget_check: Callable[[], None] | None = None

    # worker が設定する属性（呼び出しの種類・取り消しの確認・思考モード）は、すべての提供元に配る
    def _broadcast(self, name: str, value: Any) -> None:
        for client in self.providers.values():
            setattr(client, name, value)

    @property
    def label(self) -> str:
        return self._label

    @label.setter
    def label(self, value: str) -> None:
        self._label = value
        self._broadcast("label", value)

    @property
    def should_abort(self) -> Callable[[], bool] | None:
        return self._should_abort

    @should_abort.setter
    def should_abort(self, value: Callable[[], bool] | None) -> None:
        self._should_abort = value
        self._broadcast("should_abort", value)

    @property
    def budget_check(self) -> Callable[[], None] | None:
        return self._budget_check

    @budget_check.setter
    def budget_check(self, value: Callable[[], None] | None) -> None:
        self._budget_check = value
        self._broadcast("budget_check", value)

    @property
    def think_for(self) -> Callable[[str], bool | None] | None:
        return self._think_for

    @think_for.setter
    def think_for(self, value: Callable[[str], bool | None] | None) -> None:
        self._think_for = value
        self._broadcast("think_for", value)

    def chat_json(self, model: str, system: str, user: str, schema: dict[str, Any], *,
                  temperature: float = 0.7) -> dict[str, Any]:
        provider, name = provider_of(model)
        client = self.providers.get(provider)
        if client is None:
            raise LLMModelMissing(f"API の提供元「{provider}」はまだ使えるように設定されていません（モデル {model}）")
        return client.chat_json(name, system, user, schema, temperature=temperature)

    def list_models(self) -> list[dict[str, Any]]:
        """Ollama のモデル（つながらなければ LLMUnavailable）。API のモデルは api_models() で別に出す。"""
        return self.providers["ollama"].list_models()

    def pending_cost(self) -> float:
        """API の提供元で、まだ集計していない呼び出しの料金（上限の確認に足す）。"""
        return sum(c.pending_cost() for c in self.providers.values() if hasattr(c, "pending_cost"))

    def api_models(self) -> list[str]:
        """設定済みの API の提供元のモデル（「提供元/モデル名」）。"""
        names = []
        for provider, client in self.providers.items():
            if provider != "ollama":
                names += [f"{provider}/{m['name']}" for m in client.list_models()]
        return names

    def pop_calls(self) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        for client in self.providers.values():
            calls += client.pop_calls() if hasattr(client, "pop_calls") else []
        return calls
