#!/usr/bin/env python3
"""入口。文件名必须是 `main3.py`：平台按官方 demo 的约定拉起它，不许重命名。

改错名字的症状极具迷惑性：进程从未启动，没有日志、没有报错、判题器也不报错，
只是"所有单位一动不动"，而本地怎么跑都是对的。职责：读端口 → chdir →
把 `src/` 塞进 `sys.path` → 起服务，不放任何策略代码。
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
