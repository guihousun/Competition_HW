#!/usr/bin/env python3
"""本地解密 CLI：把加密日志还原成可读 JSONL。

    py -3.13 tools/decrypt_log.py logs/match-20260913-101500.cgl
    py -3.13 tools/decrypt_log.py logs/x.cgl --round 85
    py -3.13 tools/decrypt_log.py logs/x.cgl --grep attack --out d.jsonl
    py -3.13 tools/decrypt_log.py logs/x.cgl --pretty --fields roundNo,gold
    py -3.13 tools/decrypt_log.py logs/x.cgl --passphrase 'xxx'

复用 src/coregeek/infra/crypto.py 的 Cipher 与内置密钥，不依赖任何进程状态。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from coregeek.infra import config  # noqa: E402
from coregeek.infra.crypto import MAGIC, Cipher, CryptoError  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def _looks_like_header(line: bytes) -> bool:
    return line.lstrip()[:1] == b"{" and MAGIC.encode("ascii") in line


def _project(rec: dict, fields: list[str]) -> dict:
    return {k: rec.get(k) for k in fields}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="解密 coregeek 加密日志")
    ap.add_argument("path", help="日志文件路径")
    ap.add_argument("--round", type=int, default=None, help="只看某一回合")
    ap.add_argument("--grep", default=None, help="只输出包含该子串的记录")
    ap.add_argument("--out", default=None, help="输出到文件（默认 stdout）")
    ap.add_argument("--pretty", action="store_true", help="缩进格式化输出")
    ap.add_argument("--fields", default=None, help="逗号分隔，只保留这些字段")
    ap.add_argument(
        "--passphrase", default=config.LOG_PASSPHRASE, help="覆盖内置口令（默认用代码内置值）"
    )
    args = ap.parse_args(argv)

    path = Path(args.path)
    if not path.is_file():
        print(f"error: not a file: {path}", file=sys.stderr)
        return 2

    fields = [f.strip() for f in args.fields.split(",")] if args.fields else None

    raw = path.read_bytes()
    # 头部行是明文；如果没有头部说明是明文模式（LOG_ENCRYPT=False）的日志
    first, sep, rest = raw.partition(b"\n")
    if _looks_like_header(first):
        try:
            cipher = Cipher.from_header(args.passphrase, first)
        except CryptoError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3
        body = rest.split(b"\n")
    else:
        cipher = None
        body = raw.split(b"\n")

    out_lines: list[str] = []
    ok = bad = skipped = 0

    for lineno, line in enumerate(body, start=1):
        line = line.strip()
        if not line:
            continue
        if cipher is not None:
            try:
                plain = cipher.unseal(line)
            except CryptoError as exc:
                bad += 1
                out_lines.append(
                    json.dumps(
                        {"_decrypt_error": str(exc), "_line": lineno},
                        ensure_ascii=False,
                    )
                )
                continue
        else:
            plain = line

        ok += 1
        text = plain.decode("utf-8", "replace")
        rec: object
        try:
            rec = json.loads(text)
        except json.JSONDecodeError:
            rec = {"_raw": text}

        if args.round is not None:
            if not isinstance(rec, dict) or rec.get("roundNo") != args.round:
                skipped += 1
                continue
        if args.grep and args.grep not in text:
            skipped += 1
            continue
        if fields and isinstance(rec, dict):
            rec = _project(rec, fields)

        if args.pretty:
            out_lines.append(json.dumps(rec, ensure_ascii=False, indent=2))
        else:
            out_lines.append(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))

    payload = "\n".join(out_lines)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
        print(f"wrote {len(out_lines)} records -> {args.out}", file=sys.stderr)
    else:
        print(payload)

    if bad:
        print(f"warning: {bad} record(s) failed MAC verification", file=sys.stderr)
    print(
        f"# {ok} ok, {bad} bad, {skipped} filtered out",
        file=sys.stderr,
    )
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
