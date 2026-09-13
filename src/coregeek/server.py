"""HTTP 接入层：收字节 → `app.handle` → 回字节。

本层**不认识任何游戏概念**。形态照抄官方 demo（`ThreadingHTTPServer` + `0.0.0.0`）——
那条路径已被官方验证可用，而判题器是我们没法调试的黑盒。
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import app


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = app.handle(self.rfile.read(length))
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """默认实现往 stderr 打一行；我们的日志统一走 logging。"""
        return


def serve(port: int) -> None:
    ThreadingHTTPServer(("0.0.0.0", port), _Handler).serve_forever()
