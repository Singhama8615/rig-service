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

exec "$VENV_DIR/bin/uvicorn" server.main:app \
  --host "$RIGSVC_HOST" \
  --port "$RIGSVC_PORT" \
  "$@"
