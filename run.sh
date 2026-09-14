#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec python3 main.py "${1:?Usage: bash run.sh <port>}"
