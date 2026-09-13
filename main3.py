#!/usr/bin/env python3
"""入口。文件名**必须**是 `main3.py`。

平台按官方 demo 的约定拉起入口，而 demo 根目录里只有 `main3.py` 这一个入口文件。
名字改错的症状极具迷惑性：进程从未启动 → 没有日志、没有报错、判题器也不报错，
只是"所有单位一动不动"，而本地怎么跑都是对的（排查方向会被完全带偏）。

这里只做三件事：读端口 → 把 `src/` 塞进 `sys.path` → 起服务。**不放任何策略代码。**
"""

import logging
import os
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python main3.py <port>")
    port = int(sys.argv[1])

    root = Path(__file__).resolve().parent
    os.chdir(root)  # 同 demo：判题器的启动目录未知，相对路径一律以仓库根为基准
    sys.path.insert(0, str(root / "src"))

    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s | %(message)s",
    )

    from coregeek.app import run

    run(port)


if __name__ == "__main__":
    main()
