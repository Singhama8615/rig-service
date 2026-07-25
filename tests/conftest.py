"""pytest 共通フィクスチャ。

APIテストは実際の bpy を回すと1ケース数秒かかるため、既定ではリグエンジンを
スタブに差し替えてジョブのライフサイクルだけを検証する
(image-3d が mock ジェネレータでAPIを検証しているのと同じ考え方)。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# server.config はインポート時に環境変数を読むため、import より前に差し替える。
_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("RIGSVC_DATA_DIR", _TMP.name)


@pytest.fixture(scope="session", autouse=True)
def _cleanup_tmp():
    yield
    _TMP.cleanup()


@pytest.fixture
def data_dir() -> Path:
    return Path(_TMP.name)
