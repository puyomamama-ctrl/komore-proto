#!/bin/bash
# Render用の起動スクリプト。Renderが渡す PORT で待受（0.0.0.0）。鍵とパスワードはRenderの環境変数から。
set -e
cd "$(dirname "$0")"
mkdir -p data
export SK_HOST=0.0.0.0
export SK_PORT="${PORT:-10000}"
# 写真取り込み用の画像ライブラリ（Pillow）。ビルドコマンドを変えられないため、無ければ起動時に入れる（失敗しても起動は続ける）
python3 -c "import PIL" 2>/dev/null || python3 -m pip install --quiet --disable-pip-version-check -r requirements.txt || echo "警告: Pillowを入れられませんでした（写真取り込みは使えません）"
python3 render_seed.py
exec python3 render_server.py
