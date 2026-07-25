"""pip の `bpy` モジュールで autorig.py を実行するエンジン(第一実装)。

計画書 §4 方式B。専用 Python 3.11 venv (`.venv-rig`) に `bpy==4.5.*` を入れて
おり、外部の Blender バイナリを必要としない(R1-1 スパイクで確認済み)。
"""
from __future__ import annotations

import subprocess

from .. import config
from .base import RigEngine


class BpyEngine(RigEngine):
    name = "bpy"

    def command(self, args: list[str]) -> list[str]:
        return [config.BPY_PYTHON, str(config.AUTORIG_SCRIPT), *args]

    def available(self) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                [config.BPY_PYTHON, "-c", "import bpy; print(bpy.app.version_string)"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"{config.BPY_PYTHON}: {exc}"
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            return False, detail[-1] if detail else "import bpy に失敗しました"
        return True, f"bpy {proc.stdout.strip()}"
