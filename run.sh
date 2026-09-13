#!/usr/bin/env bash
# 接口文档规定的启动方式：bash run.sh port
#
# 判题环境是 Linux，本地是 Windows（用 `py -3.13`）。因此：
#   - 优先 python3（判题机），退回 python（本地 MSYS/Git-Bash）；
#   - PYTHONPATH 兜底：万一 main.py 里的 sys.path.insert 没生效（比如判题器
#     用了别的入口），仍能 import 到 coregeek；
#   - PYTHONUNBUFFERED：日志必须实时可见，否则判题器掐进程时会丢最后几行——
#     而最后几行往往正是崩溃原因。
set -euo pipefail

cd "$(dirname "$0")"

if [ "$#" -ne 1 ]; then
  echo "Usage: bash run.sh <port>" >&2
  exit 2
fi

PY=""
for candidate in python3 python py; do
  if command -v "$candidate" >/dev/null 2>&1; then
    # `py` 在 Windows 上可能只指向 3.7，必须验版本；其余同理
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      PY="$candidate"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "run.sh: 找不到 Python >= 3.11（试过 python3 / python / py）" >&2
  exit 1
fi

export PYTHONUNBUFFERED=1
export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUTF8=1

exec "$PY" main.py "$1"
