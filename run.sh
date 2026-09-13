#!/usr/bin/env bash
# 接口文档规定的启动方式：bash run.sh <port>
#
# 判题环境是 Linux（`python3`），本地是 Windows（`py -3.13`，而 `python` 是 3.7）。
# 所以必须逐个试并**验版本** —— 挑中 3.7 会因为类型语法当场崩掉。
set -euo pipefail

cd "$(dirname "$0")"

if [ "$#" -ne 1 ]; then
  echo "Usage: bash run.sh <port>" >&2
  exit 2
fi

PY=""
for candidate in python3 python py; do
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "run.sh: 找不到 Python >= 3.11（试过 python3 / python / py）" >&2
  exit 1
fi

export PYTHONUNBUFFERED=1  # 判题器掐进程时不能丢掉最后几行日志
export PYTHONUTF8=1        # Windows 控制台默认 GBK，不强制 UTF-8 会把日志转成乱码

exec "$PY" main3.py "$1"
