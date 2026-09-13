"""HTTP 接入层。**不认识任何游戏概念**——只做「收字节 → 交给 App → 回字节」。

设计见 docs/design/code-design.md §3.1 / §5。

对齐官方 demo 的形态（`ThreadingHTTPServer` + `0.0.0.0` + 不覆写 HTTP 版本），
因为那条路径已被官方验证可用，而判题器是我们没法调试的黑盒。

本层唯一的职责是**保证一定回得出包**：
  - 请求体读不出来 → 回空指令；
  - `App.handle` 抛异常 → 回空指令；
  - 任何情况下都回 200 + 合法 JSON。

HTTP 层不设超时：真正的 5 秒闸门在 `app.py` 的决策预算里，
在这里再砍一刀只会把"我们算不完"变成"判题器读不到响应"，那才是第 3 类异常。
"""

from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..infra.logstore import console

__all__ = ["serve", "build_server"]

#: 兜底响应：空指令集是**合法**的，且不计异常。
FALLBACK_BODY = b'{"roleCommandMap":{},"prompt":"","executeCmd":""}'

_MAX_BODY = 8 << 20


class _Handler(BaseHTTPRequestHandler):
    #: 由 build_server 注入
    app: Any = None
    server_version = "CoreGeek/1.0"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定命名
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length < 0 or length > _MAX_BODY:
            console(f"transport: rejected Content-Length={length}")
            self._reply(FALLBACK_BODY)
            return

        try:
            raw = self.rfile.read(length) if length else b""
        except Exception as exc:  # noqa: BLE001 - 连接层的任何异常都不能冒泡
            console(f"transport: read failed ({type(exc).__name__}: {exc})")
            self._reply(FALLBACK_BODY)
            return

        try:
            body = self.app.handle(raw)
        except Exception as exc:  # noqa: BLE001 - 最后一道兜底
            console(f"transport: handle raised ({type(exc).__name__}: {exc})")
            body = FALLBACK_BODY

        self._reply(body)

    def do_GET(self) -> None:  # noqa: N802
        """健康检查。判题器不用，但本地起服务时能一眼确认端口通了。"""
        self._reply(b'{"status":"ok"}')

    def _reply(self, body: bytes) -> None:
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:  # noqa: BLE001 - 对端断开是常态，不值得记异常
            console(f"transport: reply failed ({type(exc).__name__}: {exc})")

    def log_message(self, fmt: str, *args: Any) -> None:
        """默认实现会把每个请求写进 stderr。自己接管，避免污染判题日志。"""
        return

    def handle_one_request(self) -> None:
        """读请求头时的连接重置也要吞掉——判题器随时可能掐连接。"""
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True


def build_server(app: Any, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    handler = type("_BoundHandler", (_Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    # 判题器会并发 POST；ThreadingHTTPServer 默认 daemon_threads=True，
    # 进程退出时不会被残留的执行线程拖住。
    return server


def serve(app: Any, port: int, host: str = "0.0.0.0") -> None:
    server = build_server(app, port, host)
    console(f"listening on {host}:{port}")
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        app.shutdown()
        console("server stopped")
        sys.stdout.flush()
