"""システムの Blender をサブプロセス実行するエンジン(フォールバック)。

計画書 §4 方式A。`bpy` の pip パッケージが入らない環境(Python 3.11 が用意
できない等)向け。`autorig.py` はスタンドアロンスクリプトなので **bpy エンジンと
まったく同じスクリプトを共有**する。`--` 以降がスクリプト自身の引数になる。
"""
from __future__ import annotations

import re
import subprocess

from .. import config
from .base import RigEngine

_VERSION_RE = re.compile(r"Blender\s+([\d.]+)")


class BlenderCliEngine(RigEngine):
    name = "blender_cli"

    def command(self, args: list[str]) -> list[str]:
        return [
            config.BLENDER_BIN,
            "--background",
            "--factory-startup",
            "--python",
            str(config.AUTORIG_SCRIPT),
            "--",
            *args,
        ]

    def available(self) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                [config.BLENDER_BIN, "--version"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"{config.BLENDER_BIN}: {exc}"
        if proc.returncode != 0:
            return False, f"{config.BLENDER_BIN} --version が失敗しました"
        match = _VERSION_RE.search(proc.stdout)
        return True, f"Blender {match.group(1)}" if match else proc.stdout.strip()
