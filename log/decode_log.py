#!/usr/bin/env python3
"""解密加密日志。

改下面那个 `raw_path`，然后直接跑：`py log/decode_log.py` ⇒ 明文写进当前目录的 `decode_log.log`。
带文件参数时用参数、不看 `raw_path`（用例走这一支）。

解不开的行**不中止**：进程被杀在半条记录上时尾巴是残的，前面的内容照旧要拿到。
格式与密钥都在 `coregeek.logfile` 里 —— 那边改了口令，这里的旧文件也就解不开了。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coregeek.logfile import unseal  # noqa: E402

#: 要解密的原始日志，相对仓库根 —— 在这行改路径即可，从哪个目录跑都认同一份。三种落点都是同一套
#: 密文：判题器把进程 stdout 落成的 `team.log`、本地重定向出来的 `run.enc`、文件 sink 的
#: `log/match-<年月日-时分秒>-<pid>.enc`。
raw_path = "team.log"

#: 解密结果的落点（当前目录）。
OUT = "decode_log.log"


def main(argv) -> int:
    if len(argv) > 2:
        print(f"用法：{Path(argv[0]).name} [encode_file]", file=sys.stderr)
        return 2
    src = Path(argv[1]) if len(argv) == 2 else ROOT / raw_path
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
