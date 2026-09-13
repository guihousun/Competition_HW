#!/usr/bin/env python3
"""入口。文件名**必须**是 `main3.py`，与官方 demo 完全一致。

    python main3.py <port>         # 判题器这样起
    bash run.sh <port>             # 接口文档给的写法，内部转调上面这条

⚠️ 这个文件名不是随便取的，是**平台约定的拉起入口**。官方 demo 的根目录里
只有 `main3.py` 一个入口文件。我们早期把它命名成 `main.py`（"照抄形态"时
自作主张改了名），结果是：平台照约定拉 `main3.py` 拉不到，我们的进程
**从未启动过** —— 对局里表现为"压根儿不动"，而所有策略代码其实都是好的，
本地 `run.sh` 也跑得通，排查方向被完全带偏。**不要重命名这个文件。**

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
        raise SystemExit("Usage: python main3.py <port>")

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
