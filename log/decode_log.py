#!/usr/bin/env python3
"""解密加密日志。

用法：`py log/decode_log.py <encode_file>`
读 `encode_file`（`log/` 下那份 `.enc`），写出 `decode_log.log`（当前目录，明文）。

解不开的行**不中止**：进程被杀在半条记录上时尾巴是残的，前面的内容照旧要拿到。
格式与密钥都在 `coregeek.logfile` 里 —— 那边改了口令，这里的旧文件也就解不开了。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coregeek.logfile import unseal  # noqa: E402

#: 解密结果的落点（当前目录）。
OUT = "decode_log.log"


def main(argv) -> int:
    if len(argv) != 2:
        print(f"用法：{Path(argv[0]).name} <encode_file>", file=sys.stderr)
        return 2
    src = Path(argv[1])
    if not src.is_file():
        print(f"读不到：{src}", file=sys.stderr)
        return 1

    ok = bad = 0
    with src.open("r", encoding="utf-8") as fin, open(OUT, "w", encoding="utf-8") as fout:
        for lineno, line in enumerate(fin, 1):
            if not line.strip():
                continue
            try:
                fout.write(unseal(line) + "\n")
                ok += 1
            except ValueError as exc:  # 认不出 base64 / 短了 / 校验不过，都从这里出来
                bad += 1
                fout.write(f"# 第 {lineno} 行解密失败：{exc}\n")
    print(f"{src} → {OUT}：{ok} 行成功，{bad} 行失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
