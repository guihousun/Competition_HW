"""HTTP 接入层：收字节 → `handler` → 回字节。

不认识任何游戏概念、不认识 `app`：处理函数由调用方注入，依赖方向只有一个
（`app` → `web`），无循环 import。形态照抄官方 demo（`ThreadingHTTPServer` + `0.0.0.0`）。
`do_POST` 不接异常：handler 抛出去连接就断，判题器那边是"响应超时"（红线第一条），
所以 handler 必须自己兜住一切。
"""

from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: 一次请求的处理函数。约定：永不抛异常，返回的字节一定是合法响应。
Handler = Callable[[bytes], bytes]


def serve(port: int, handler: Handler) -> None:
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = handler(self.rfile.read(length))
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            """默认实现往 stderr 打一行；我们的日志统一走 logging。"""
            return

    ThreadingHTTPServer(("0.0.0.0", port), _Handler).serve_forever()
