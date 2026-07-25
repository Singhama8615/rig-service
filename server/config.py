"""rig-service の設定。

すべて環境変数 `RIGSVC_*` で上書き可能。コードにハードコードしない
(image-3d の DEVELOPMENT_POLICY.md §4 と同じ方針)。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# --- パス ---------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("RIGSVC_DATA_DIR", BASE_DIR / "data"))
JOBS_DIR = DATA_DIR / "jobs"
WEB_DIR = Path(os.environ.get("RIGSVC_WEB_DIR", BASE_DIR / "web"))
BPY_SCRIPTS_DIR = BASE_DIR / "server" / "bpy_scripts"
AUTORIG_SCRIPT = BPY_SCRIPTS_DIR / "autorig.py"

# --- サーバ ---------------------------------------------------------------
HOST = os.environ.get("RIGSVC_HOST", "127.0.0.1")
PORT = int(os.environ.get("RIGSVC_PORT", "8100"))

# --- リグエンジン選択 (計画書 §4) ------------------------------------------
# "auto": bpy モジュールが import できれば bpy、なければ Blender CLI に解決する。
# 明示指定は "bpy" | "blender_cli"。
ENGINE = os.environ.get("RIGSVC_ENGINE", "auto")

# bpy エンジンが autorig.py を実行するのに使う Python。
# 既定はサーバ自身の Python(= bpy を含む .venv-rig)。
BPY_PYTHON = os.environ.get("RIGSVC_BPY_PYTHON", sys.executable)

# Blender CLI フォールバックで使う blender 実行ファイル。
BLENDER_BIN = os.environ.get("RIGSVC_BLENDER_BIN", "blender")

# 1ジョブあたりのリグ処理タイムアウト(秒)。実測は 200k面で約8秒。
RIG_TIMEOUT_SEC = int(os.environ.get("RIGSVC_RIG_TIMEOUT_SEC", "900"))

# --- アップロード制限 -------------------------------------------------------
MAX_UPLOAD_BYTES = int(os.environ.get("RIGSVC_MAX_UPLOAD_BYTES", str(200 * 1024 * 1024)))

# --- リグパラメータの既定値 (計画書 §6) --------------------------------------
# 出力の全高(メートル)。入力は造形用の高さ100単位なので、VRM/Godot の
# メートル系に合わせて変換する(計画書 §10 の発見1)。
DEFAULT_HEIGHT_M = float(os.environ.get("RIGSVC_DEFAULT_HEIGHT_M", "1.6"))
ALLOWED_UP_AXIS = {"auto", "y", "z"}
ALLOWED_FACING = {"auto", "+y", "-y"}
ALLOWED_BONE_SETS = {"standard"}
# 正面プレビューPNG(Cyclesのため数秒かかる)を既定で描くか
DEFAULT_PREVIEW = os.environ.get("RIGSVC_DEFAULT_PREVIEW", "true").lower() in (
    "1",
    "true",
    "yes",
)
# VRM 1.0 も既定で書き出すか。純Python変換で1秒以下なので既定で有効にする。
DEFAULT_VRM = os.environ.get("RIGSVC_DEFAULT_VRM", "true").lower() in ("1", "true", "yes")


def ensure_dirs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
