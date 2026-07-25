"""リグエンジンのプラガブルな解決 (計画書 §4)。"""
from __future__ import annotations

import logging

from .. import config
from .base import RigEngine, RigEngineError, RigParams, RigResult
from .blender_cli import BlenderCliEngine
from .bpy_engine import BpyEngine

logger = logging.getLogger(__name__)

__all__ = [
    "RigEngine",
    "RigEngineError",
    "RigParams",
    "RigResult",
    "BpyEngine",
    "BlenderCliEngine",
    "build_engine",
]

ENGINES = {"bpy": BpyEngine, "blender_cli": BlenderCliEngine}


def build_engine(name: str | None = None) -> RigEngine:
    """設定からエンジンを組み立てる。

    "auto" は bpy モジュールが使えれば bpy、ダメなら Blender CLI に解決する
    (計画書 §8「bpy pipパッケージがPython 3.11固定」のリスク対策)。
    """
    name = name or config.ENGINE
    if name == "auto":
        bpy_engine = BpyEngine()
        ok, detail = bpy_engine.available()
        if ok:
            logger.info("RIGSVC_ENGINE=auto -> bpy (%s)", detail)
            return bpy_engine
        logger.warning(
            "RIGSVC_ENGINE=auto: bpy モジュールを使えないため Blender CLI に"
            "フォールバックします (%s)",
            detail,
        )
        return BlenderCliEngine()
    if name not in ENGINES:
        raise ValueError(f"Unknown engine: {name} (choices: {sorted(ENGINES)} or 'auto')")
    return ENGINES[name]()
