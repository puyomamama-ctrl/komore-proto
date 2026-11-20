#!/bin/bash
# Render用の起動スクリプト。Renderが渡す PORT で待受（0.0.0.0）。鍵とパスワードはRenderの環境変数から。
set -e
cd "$(dirname "$0")"
mkdir -p data
export SK_HOST=0.0.0.0
export SK_PORT="${PORT:-10000}"
python3 render_seed.py
exec python3 server.py
