"""数式サンドボックスの HTTP API。

POST /evaluate  {"code", "kind"}                        検算コードを実行
POST /compare   {"kind", "expected", "given", ...}      2 つの答えを比較
POST /preview   {"text", "kind"}                        入力のプレビュー
GET  /healthz

1 リクエストごとに forkserver から子プロセスを作り、時間・メモリを制限して実行する。
子プロセスからの結果は pickle ではなく JSON で受け取る。
"""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import tasks

log = logging.getLogger("sandbox")

TIMEOUTS = {
    "evaluate": float(os.environ.get("SANDBOX_EVAL_TIMEOUT", "20")),
    "compare": float(os.environ.get("SANDBOX_COMPARE_TIMEOUT", "10")),
    "preview": 5.0,
}
MAX_BODY = 200_000
MAX_RESULT = 2_000_000

_ctx = mp.get_context("forkserver")
_ctx.set_forkserver_preload(["sandbox_service.tasks"])
_slots = threading.BoundedSemaphore(int(os.environ.get("SANDBOX_CONCURRENCY", "4")))


def run_task(name: str, payload: dict) -> dict:
    timeout = TIMEOUTS[name]
    with _slots:
        recv_conn, send_conn = _ctx.Pipe(duplex=False)
        proc = _ctx.Process(target=tasks.child_main, args=(name, payload, send_conn, timeout), daemon=True)
        proc.start()
        send_conn.close()
        result = None
        try:
            if recv_conn.poll(timeout + 1):
                try:
                    result = json.loads(recv_conn.recv_bytes(MAX_RESULT))
                except (EOFError, OSError, ValueError):
                    result = None
        finally:
            if proc.is_alive():
                proc.kill()
            proc.join(2)
            recv_conn.close()
    if result is None:
        if proc.exitcode in (None, -signal.SIGKILL, -signal.SIGXCPU):
            return {"ok": False, "error": f"計算が時間内に終わりませんでした（{timeout:.0f}秒）"}
        return {"ok": False, "error": f"計算プロセスが異常終了しました（終了コード {proc.exitcode}）"}
    if not isinstance(result, dict):
        return {"ok": False, "error": "不正な結果が返されました"}
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "quiz-sandbox"

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        name = self.path.strip("/")
        if name not in tasks.TASKS:
            self._send(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._send(413, {"ok": False, "error": "リクエストが大きすぎます"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            self._send(400, {"ok": False, "error": "JSON が不正です"})
            return
        self._send(200, run_task(name, payload))

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = int(os.environ.get("SANDBOX_PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    log.info("sandbox listening on :%d", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
