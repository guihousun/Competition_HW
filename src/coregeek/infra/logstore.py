"""日志落盘：压缩 + 加密 + **后台线程**。

设计见 docs/design/code-design.md §10.2 / §11。

⚠️ 关键约束：压缩 + 加密 + fsync 是 5 秒响应闸门最大的现实威胁，因此
**请求线程只往有界队列投一条记录**，由单个 writer 线程消费落盘。
队列满则丢弃并计数——丢日志远好过丢比赛。
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config
from .crypto import Cipher, CryptoError

__all__ = ["LogStore", "safe_emit", "console"]

_DROP = object()


def safe_emit(text: str) -> None:
    """唯一允许出现在请求路径上的 stdout 出口。

    Windows 控制台默认编码会把中文 print 变成乱码，极端情况下抛
    UnicodeEncodeError 会污染回合执行路径，进而可能计入异常。这里把
    所有编码异常都吞掉——日志绝不能影响对局。
    """
    line = text if text.endswith("\n") else text + "\n"
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
        return
    except UnicodeEncodeError:
        pass
    except Exception:  # noqa: BLE001 - stdout 可能已关闭
        return
    try:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        sys.stdout.write(line.encode(enc, "replace").decode(enc, "replace"))
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


def console(text: str) -> None:
    """带时间戳的运维日志（明文，写 stdout）。"""
    safe_emit(f"{datetime.now().strftime('%H:%M:%S')} | {text}")


class LogStore:
    """有界队列 + 单 writer 线程的加密 JSONL 日志。

    记录本身是 dict；这里负责序列化、压缩、加密、轮转。
    解密工具 tools/decrypt_log.py 复用 Cipher，不依赖本模块的进程状态。
    """

    def __init__(
        self,
        log_dir: str | os.PathLike[str] = config.LOG_DIR,
        passphrase: str = config.LOG_PASSPHRASE,
        encrypt: bool = config.LOG_ENCRYPT,
        iters: int = config.LOG_ITERS,
        max_bytes: int = config.LOG_MAX_BYTES,
        queue_size: int = config.LOG_QUEUE_SIZE,
    ) -> None:
        self.dir = Path(log_dir)
        self.passphrase = passphrase
        self.encrypt = encrypt
        self.iters = iters
        self.max_bytes = max_bytes
        self._q: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._fh = None
        self._cipher: Cipher | None = None
        self._path: Path | None = None
        self._part = 0
        self._written = 0
        self.dropped = 0
        self.write_errors = 0
        self.accepted = 0
        self._consumed = 0
        self._flush_ok = threading.Event()

    # ── 生命周期 ────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="logstore", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 3.0) -> None:
        """判题器会杀进程，因此 main.py 必须注册 atexit/SIGTERM 调它。"""
        if self._thread is None:
            return
        self._stop.set()
        # 哨兵必须排在所有记录之后（队列 FIFO）。若队列满则阻塞等待，
        # 但加超时兜底，避免 writer 异常退出时把进程挂死。
        try:
            self._q.put(_DROP, timeout=timeout)
        except queue.Full:
            self.dropped += self._q.qsize()
        self._thread.join(timeout=timeout)
        self._thread = None

    def __enter__(self) -> "LogStore":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── 写入侧（请求线程） ──────────────────────────────────────────
    def submit(self, record: dict[str, Any]) -> None:
        """非阻塞投递。队列满则丢弃并计数，绝不阻塞请求线程。"""
        try:
            self._q.put_nowait(record)
            self.accepted += 1
        except queue.Full:
            self.dropped += 1

    @property
    def depth(self) -> int:
        return self._q.qsize()

    # ── 消费侧（writer 线程） ───────────────────────────────────────
    def _run(self) -> None:
        while True:
            item = self._q.get()
            # 只在收到哨兵时退出。**绝不能因为 _stop 已置位就丢弃手上的记录**——
            # close() 先 set(_stop) 再投哨兵，若这里按 _stop 提前 break，
            # 那条已经 get() 出来的真实记录会永久丢失（实测踩过）。
            if item is _DROP:
                self._drain()
                break
            self._write_one(item)
            if self._q.empty():
                self._flush()

    def _drain(self) -> None:
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is not _DROP:
                self._write_one(item)
        self._flush()
        self._close_file()

    def _write_one(self, record: dict[str, Any]) -> None:
        self._consumed += 1
        try:
            payload = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            # 必须先把文件（及其 cipher）建起来，才能编码——编码长度又决定要不要轮转，
            # 所以轮转后要用**新** cipher 重新编一次，否则那条记录会带着旧 salt 落进新文件。
            self._ensure_file()
            line = self._encode(payload)
            if self._written + len(line) + 1 > self.max_bytes:
                self._close_file()
                self._part += 1
                self._open_file()
                line = self._encode(payload)
            fh = self._ensure_file()
            fh.write(line + b"\n")
            self._written += len(line) + 1
        except Exception as exc:  # noqa: BLE001 - 日志绝不能拖死对局
            self.write_errors += 1
            if self.write_errors <= 3:
                console(f"logstore: write failed ({type(exc).__name__}: {exc})")

    def _encode(self, raw: bytes) -> bytes:
        if not self.encrypt:
            return raw
        if self._cipher is None:
            raise RuntimeError("logstore: cipher not initialised (call _open_file first)")
        return self._cipher.seal(raw).encode("ascii")

    # ── 文件与轮转 ──────────────────────────────────────────────────
    def _ensure_file(self):
        if self._fh is None or self._fh.closed:
            self._open_file()
        return self._fh

    def _open_file(self) -> None:
        # 文件名必须**独占创建**：秒级时间戳会撞车（同一秒内起两个实例、
        # 或进程快速重启都会覆盖已有日志）。加随机后缀 + "xb" 重试。
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        fh = None
        path = None
        for _ in range(64):
            candidate = self.dir / f"match-{stamp}-{secrets.token_hex(2)}.cgl"
            try:
                fh = open(candidate, "xb")
            except FileExistsError:
                continue
            path = candidate
            break
        if fh is None or path is None:
            raise RuntimeError(f"logstore: cannot create a unique log file under {self.dir}")

        self._fh = fh
        self._path = path
        self._written = 0
        if self.encrypt:
            self._cipher = Cipher.new(self.passphrase, self.iters)
            self._fh.write(self._cipher.header + b"\n")
            self._written += len(self._cipher.header) + 1
        else:
            self._cipher = None
        console(f"logstore: -> {path}")

    def _flush(self) -> None:
        if self._fh is None or self._fh.closed:
            return
        try:
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except OSError:
            pass

    def _close_file(self) -> None:
        if self._fh is None:
            return
        try:
            self._flush()
            self._fh.close()
        except OSError:
            pass
        finally:
            self._fh = None

    # ── 供 smoke/selfcheck 用的同步读取 ─────────────────────────────
    @property
    def path(self) -> Path | None:
        return self._path


def wait_flushed(store: LogStore, timeout: float = 3.0) -> bool:
    """测试用：等 writer 线程处理完已投递的记录。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if store._consumed >= store.accepted:  # noqa: SLF001 - 同模块内的测试辅助
            return True
        time.sleep(0.02)
    return store._consumed >= store.accepted  # noqa: SLF001
