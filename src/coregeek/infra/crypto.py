"""加密原语：仅标准库，encrypt-then-MAC。

设计见 docs/design/code-design.md §10.1。

    master = PBKDF2-HMAC-SHA256(口令, salt, iters)     # salt 每文件随机，存在明文头部
    k_enc  = HMAC(master, b"enc")                      # 两个独立密钥，绝不复用
    k_mac  = HMAC(master, b"mac")
    每条记录：
      nonce     = secrets.token_bytes(16)              # 每条随机，不用全局计数器
      keystream = concat(HMAC(k_enc, nonce || i))[:len(pt)]
      ct        = pt XOR keystream
      tag       = HMAC(k_mac, header || nonce || ct)   # encrypt-then-MAC

⚠️ 密钥内置在代码中（需求阶段选定）。这**只防顺手翻看，不防拿到代码的人**——
这是日志混淆，不是安全边界。不要把它当作保密手段来用。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import struct
import zlib

MAGIC = "CGL1"
KDF_NAME = "pbkdf2-hmac-sha256"
KEY_LEN = 32
SALT_LEN = 16
NONCE_LEN = 16
TAG_LEN = 32
DEFAULT_ITERS = 20_000

__all__ = [
    "MAGIC",
    "KDF_NAME",
    "DEFAULT_ITERS",
    "CryptoError",
    "Cipher",
    "make_header",
    "parse_header",
]


class CryptoError(Exception):
    """解密或认证失败。调用方应当记录并继续，**不要让日志模块拖死对局**。"""


def _derive(passphrase: str, salt: bytes, iters: int) -> tuple[bytes, bytes]:
    """派生 (k_enc, k_mac)。两个密钥域分离，绝不复用同一把密钥做两件事。"""
    master = hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), salt, iters, dklen=KEY_LEN
    )
    k_enc = hmac.new(master, b"enc", hashlib.sha256).digest()
    k_mac = hmac.new(master, b"mac", hashlib.sha256).digest()
    return k_enc, k_mac


def _keystream(k_enc: bytes, nonce: bytes, n: int) -> bytes:
    """HMAC-SHA256 当 PRF 的计数器密钥流。绝不自己实现 AES。"""
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hmac.new(k_enc, nonce + struct.pack(">I", counter), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:n])


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def make_header(salt: bytes, iters: int) -> bytes:
    """明文头部（一行 JSON）。解密工具靠它独立工作，不依赖进程状态。"""
    return json.dumps(
        {
            "v": 1,
            "magic": MAGIC,
            "kdf": KDF_NAME,
            "iters": iters,
            "salt": base64.b64encode(salt).decode("ascii"),
        },
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def parse_header(line: bytes) -> tuple[bytes, int]:
    """解析明文头部，返回 (salt, iters)。任何异常都收敛为 CryptoError。"""
    try:
        obj = json.loads(line.decode("utf-8"))
        if obj.get("magic") != MAGIC:
            raise CryptoError(f"bad magic: {obj.get('magic')!r}")
        salt = base64.b64decode(obj["salt"], validate=True)
        iters = int(obj["iters"])
    except CryptoError:
        raise
    except Exception as exc:  # noqa: BLE001 - 头部是不可信输入
        raise CryptoError(f"malformed header: {exc}") from exc
    if len(salt) != SALT_LEN:
        raise CryptoError(f"bad salt length: {len(salt)}")
    if iters <= 0:
        raise CryptoError(f"bad iters: {iters}")
    return salt, iters


class Cipher:
    """一个日志文件对应一个 Cipher：salt 固定，nonce 每条随机。"""

    __slots__ = ("salt", "iters", "header", "_k_enc", "_k_mac", "_header_bytes")

    def __init__(self, passphrase: str, salt: bytes, iters: int) -> None:
        self.salt = salt
        self.iters = iters
        self.header = make_header(salt, iters)
        self._k_enc, self._k_mac = _derive(passphrase, salt, iters)
        self._header_bytes = self.header

    @classmethod
    def new(cls, passphrase: str, iters: int = DEFAULT_ITERS) -> "Cipher":
        return cls(passphrase, secrets.token_bytes(SALT_LEN), iters)

    @classmethod
    def from_header(cls, passphrase: str, header_line: bytes) -> "Cipher":
        salt, iters = parse_header(header_line)
        return cls(passphrase, salt, iters)

    def seal(self, plaintext: bytes) -> str:
        """压缩 → 加密 → 认证，返回单行 base64（不含换行）。"""
        comp = zlib.compress(plaintext, 6)
        nonce = secrets.token_bytes(NONCE_LEN)
        ct = _xor(comp, _keystream(self._k_enc, nonce, len(comp)))
        tag = hmac.new(self._k_mac, self._header_bytes + nonce + ct, hashlib.sha256).digest()
        return base64.b64encode(nonce + ct + tag).decode("ascii")

    def unseal(self, line: bytes) -> bytes:
        """认证 → 解密 → 解压。任何失败（含篡改）都抛 CryptoError。"""
        try:
            blob = base64.b64decode(line.strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CryptoError(f"bad base64: {exc}") from exc
        if len(blob) < NONCE_LEN + TAG_LEN:
            raise CryptoError(f"record too short: {len(blob)}")
        nonce = blob[:NONCE_LEN]
        tag = blob[-TAG_LEN:]
        ct = blob[NONCE_LEN:-TAG_LEN]
        expect = hmac.new(self._k_mac, self._header_bytes + nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expect):
            raise CryptoError("MAC verification failed (record tampered or wrong passphrase)")
        plain = _xor(ct, _keystream(self._k_enc, nonce, len(ct)))
        try:
            return zlib.decompress(plain)
        except zlib.error as exc:
            raise CryptoError(f"decompress failed: {exc}") from exc
