#!/usr/bin/env bash
# rig-service 起動スクリプト
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="${VENV_DIR:-.venv-rig}"

if [ ! -d "$VENV_DIR" ]; then
  echo "venvが見つかりません。先に以下を実行してください:"
  echo "  uv venv --python 3.11 $VENV_DIR"
  echo "  uv pip install --python $VENV_DIR/bin/python -r requirements.txt"
  exit 1
fi

# auto: bpy モジュールが使えれば bpy、なければ Blender CLI にフォールバック
export RIGSVC_ENGINE="${RIGSVC_ENGINE:-auto}"
export RIGSVC_HOST="${RIGSVC_HOST:-127.0.0.1}"
export RIGSVC_PORT="${RIGSVC_PORT:-8100}"

# `bin/uvicorn` ではなく `python -m` で起動する。venv のコンソールスクリプトは
# shebang に絶対パスを埋め込むため、ディレクトリを移動すると壊れる
# (`python -m` なら shebang を経由しないので影響を受けない)。
exec "$VENV_DIR/bin/python" -m uvicorn server.main:app \
  --host "$RIGSVC_HOST" \
  --port "$RIGSVC_PORT" \
  "$@"
