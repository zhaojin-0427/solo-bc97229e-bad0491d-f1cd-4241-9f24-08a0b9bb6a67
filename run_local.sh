#!/usr/bin/env bash
# 离线/无 Flask 环境下的零依赖演示入口（仅用 Python 标准库）。
# 生产部署请使用 docker compose up（Flask + gunicorn）。
set -euo pipefail
cd "$(dirname "$0")"

export ELEV_DATA_DIR="${ELEV_DATA_DIR:-$(pwd)/data}"
export ELEV_HOST="${ELEV_HOST:-0.0.0.0}"
export ELEV_PORT="${ELEV_PORT:-8000}"

# -I 隔离模式不把 cwd 放进 sys.path，用 -c 显式注入项目根目录。
exec python3 -I -c "import sys; sys.path.insert(0, '.'); from elevation.wsgi_stdlib import main; main()"
