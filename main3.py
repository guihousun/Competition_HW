#!/usr/bin/env python3
"""入口。文件名必须是 `main3.py`：平台按官方 demo 的约定拉起它，不许重命名。

改错名字的症状：进程从未启动、没有日志、判题器也不报错，只是"所有单位一动不动"。
职责：读端口 → chdir → `src/` 进 `sys.path` → 起服务，不放任何策略代码。
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

    from coregeek import logfile

    handlers = [logging.StreamHandler(sys.stdout)]
    sink = logfile.encrypted_handler()  # stdout 那份照旧，加密文件是新增的第二份
    if sink is None:
        # 建不出来也要起服务：进程没启动的症状是"所有单位一动不动"，比没有日志严重得多。
        print("加密日志建不起来，只往 stdout 打", file=sys.stderr)
    else:
        handlers.append(sink)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(message)s",
        handlers=handlers,
    )
    if sink is not None:
        logging.info("加密日志：%s", sink.baseFilename)  # 赛后要捞的就是这个文件

    from coregeek.app import run

    run(port)


if __name__ == "__main__":
    main()
