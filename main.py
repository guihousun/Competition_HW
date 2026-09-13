#!/usr/bin/env python3
"""入口。形态对齐官方 demo 的 `main3.py`（已被判题器验证可用）。

    python main.py <port>          # 判题器这样起
    bash run.sh <port>             # 接口文档给的写法，内部转调上面这条

只做四件事：读端口 → 切工作目录 → 把 `src/` 塞进 `sys.path` → 起服务。
**不放任何策略代码**：策略全在 `src/coregeek/`，这里只是那 20 行胶水。

`os.chdir(root)` 是必须的（demo 也这么做）：判题器的启动目录未知，
而 `config.LOG_DIR = "logs"` 是相对路径——不切目录的话日志会落到随机位置。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python main.py <port>")

    try:
        port = int(sys.argv[1])
    except ValueError:
        raise SystemExit(f"端口必须是整数，收到 {sys.argv[1]!r}") from None
    if not (0 < port < 65536):
        raise SystemExit(f"端口越界：{port}")

    root = Path(__file__).resolve().parent
    os.chdir(root)
    sys.path.insert(0, str(root / "src"))

    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s | %(message)s",
    )

    from coregeek.app import run

    try:
        run(port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
