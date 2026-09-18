"""加密的日志落点：一条记录一行、两个 sink（stdout 与 `log/` 下的文件）同一套密文。

格式：每行 = `base64(nonce(16) | tag(16) | 密文)`，密文 = 明文与
`HMAC-SHA256(密钥, nonce | 计数器)` 生成的密钥流异或。逐条换随机 nonce —— 日志行首那段
时间戳是可猜的明文，固定密钥流会把它漏出去。`tag` 认截断与改字节。

**进程的日志输出没有明文落点**（`src/` 里一处裸 `print` 都没有）：stdout 上也是这套 base64，
`log/decode_log.py log/match-*.enc` 与 `... > run.enc` 抓下来的 stdout 解出来是同一份正文。

密钥内置（判题机是黑盒，没有可配的地方；解密脚本直接 import 这里，格式只一份）。
只有标准库。**不放状态、不放格式化函数** —— 那两样分别归 `logging` 与 `utils.py`。
"""

import base64
import hashlib
import hmac
import logging
import os
from datetime import datetime
from pathlib import Path

#: 加密日志的目录，相对 cwd（`main3.py` 把 cwd 钉在仓库根）。
LOG_DIR = "log"

#: 共用口令。改它等于换密钥：**已经落盘的旧日志解不开了**。
_PASSPHRASE = b"coregeek-futurewar-2026"

_KEY = hashlib.sha256(_PASSPHRASE).digest()
_NONCE_LEN = 16
_TAG_LEN = 16


def _keystream(nonce: bytes, size: int) -> bytes:
    """`nonce` + 计数器 逐块做 HMAC，取前 `size` 字节。"""
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hmac.new(_KEY, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:size])


def _mix(data: bytes, box: bytes) -> bytes:
    """逐字节异或（加密与解密同一支）。走大整数是 C 层一次算完：顶格回合一条记录 300KB，
    逐字节的 Python 循环是 5 秒响应预算上不必要的开销。"""
    return (int.from_bytes(data, "big") ^ int.from_bytes(box, "big")).to_bytes(len(data), "big")


def _tag(nonce: bytes, body: bytes) -> bytes:
    return hmac.new(_KEY, nonce + body, hashlib.sha256).digest()[:_TAG_LEN]


def seal(text: str) -> str:
    """明文 → 一行 base64（不含换行，记录里的换行因此切不开这一行）。"""
    data = text.encode("utf-8")
    nonce = os.urandom(_NONCE_LEN)
    body = _mix(data, _keystream(nonce, len(data)))
    return base64.b64encode(nonce + _tag(nonce, body) + body).decode("ascii")


def unseal(line: str) -> str:
    """`seal` 的逆。**不是合法的一行就抛 `ValueError`**（认不出 base64 / 短了 / 校验不过）——
    是跳过还是中止由调用者定（截断的尾巴、混进来的明文行都走这里）。"""
    raw = base64.b64decode(line.strip(), validate=True)
    if len(raw) < _NONCE_LEN + _TAG_LEN:
        raise ValueError(f"只有 {len(raw)} 字节，不够一个 nonce+tag")
    nonce = raw[:_NONCE_LEN]
    tag = raw[_NONCE_LEN : _NONCE_LEN + _TAG_LEN]
    body = raw[_NONCE_LEN + _TAG_LEN :]
    if not hmac.compare_digest(tag, _tag(nonce, body)):
        raise ValueError("校验不过（截断或被改过）")
    return _mix(body, _keystream(nonce, len(body))).decode("utf-8")


def new_path(dir_=LOG_DIR) -> Path:
    """本次运行的日志路径：`<dir_>/match-<年月日-时分秒>-<pid>.enc`。
    带 pid ⇒ 同一台机器上先后两场（甚至同一秒起）不会挤进同一个文件。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(dir_) / f"match-{stamp}-{os.getpid()}.enc"


class EncryptedStreamHandler(logging.StreamHandler):
    """写出去之前加密的 handler：**只改 `format`** —— 它的返回值就是写出去的那一行。

    每条写完就 flush、写失败吞进 `handleError` 全部沿用父类：写日志绝不能反过来把请求打挂
    （父类那条 except 就是最后一道防线）。同步写不排队、不压缩、不 fsync —— 省掉这三样之后
    单条记录的加密在毫秒级，不值得为它引入一个写线程。
    """

    def format(self, record: logging.LogRecord) -> str:
        return seal(super().format(record))


class EncryptedFileHandler(EncryptedStreamHandler, logging.FileHandler):
    """落盘那一份，格式同上（`log/match-<年月日-时分秒>-<pid>.enc`）。

    类顺序有意：`__init__` 得从 `FileHandler` 取（它开文件、设 `self.stream`），
    `format` 从 `EncryptedStreamHandler` 取。
    """


def encrypted_handler(dir_=LOG_DIR):
    """建好本次运行的加密文件 sink。**建不出来（只读盘 / 没权限 / 名字被文件占着）返回 `None`**，
    绝不抛 —— 入口因为"写不了日志"起不来的症状是"所有单位一动不动"，与 `main3.py` 改名事故同形、
    极难排查。调用者拿到 `None` 时去 stderr 说一声（stdout 那个 sink 照旧可用）。"""
    try:
        Path(dir_).mkdir(parents=True, exist_ok=True)
        return EncryptedFileHandler(new_path(dir_), encoding="utf-8")
    except OSError:
        return None
