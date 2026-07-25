"""bpy を実際に回す E2E 検証(計画書 §7 R1-2 の検証基準)。

素体Tポーズ(人体標準比 / デフォルメ体型)に対して、

- ボーンヒート自動ウェイトでウェイト破綻が起きないこと
- 出力が **glTF 仕様準拠**(Y-up・原点=足元・メートル系)であること
- スキン(JOINTS_0 / WEIGHTS_0)が保持されていること

を確認する。1ケースあたり数秒かかる。
"""
from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from server import config
from server.engines.base import RigParams, RigResult
from server.engines.bpy_engine import BpyEngine

ROOT = Path(__file__).resolve().parent.parent
MAKE_GLB = ROOT / "tests" / "make_tpose_glb.py"

_available, _detail = BpyEngine().available()
pytestmark = pytest.mark.skipif(not _available, reason=f"bpy を使えません: {_detail}")


def _read_glb_json(path: Path) -> dict:
    """GLB の JSON チャンクを読む(座標系はここでしか確認できない)。"""
    data = path.read_bytes()
    assert data[:4] == b"glTF", "GLB ではありません"
    json_len = struct.unpack("<I", data[12:16])[0]
    return json.loads(data[20 : 20 + json_len])


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory) -> dict[str, Path]:
    """素体TポーズGLB(image-3d と同じ Z-up・高さ100単位)を bpy で作る。"""
    out_dir = tmp_path_factory.mktemp("fixtures")
    paths = {}
    for shape in ("humanoid", "chibi"):
        path = out_dir / f"{shape}.glb"
        subprocess.run(
            [sys.executable, str(MAKE_GLB), "--shape", shape, "--output", str(path)],
            check=True,
            capture_output=True,
            timeout=600,
        )
        paths[shape] = path
    return paths


@pytest.fixture(scope="module")
def rigged(fixtures, tmp_path_factory) -> dict[str, RigResult]:
    """両フィクスチャに実エンジンでリグをかける(モジュール内で使い回す)。"""
    engine = BpyEngine()
    work = tmp_path_factory.mktemp("rigged")
    results = {}
    for shape, src in fixtures.items():
        job_dir = work / shape
        job_dir.mkdir()
        # プレビュー(Cycles)は数秒かかるうえ検証対象ではないので切る
        results[shape] = engine.rig(
            src,
            work / f"{shape}_rigged.glb",
            RigParams(height_m=1.6, preview=False),
            job_dir,
            config.RIG_TIMEOUT_SEC,
        )
    return results


@pytest.mark.parametrize("shape", ["humanoid", "chibi"])
def test_all_vertices_are_weighted(rigged, shape):
    weights = rigged[shape].summary["weights"]
    assert weights["unweighted_vertices"] == 0
    assert weights["weighted_ratio"] == 1.0
    assert weights["bones_without_weight"] == []


@pytest.mark.parametrize("shape", ["humanoid", "chibi"])
def test_detected_as_tpose_without_fallbacks(rigged, shape):
    summary = rigged[shape].summary
    assert summary["measurements"]["t_pose"] is True
    assert rigged[shape].warnings == []
    # image-3d の Z-up 慣習を自動判定できていること(計画書 §10 の発見1)
    assert summary["normalize"]["up_axis"] == "z"
    assert summary["normalize"]["facing"] == "-y"


@pytest.mark.parametrize("shape", ["humanoid", "chibi"])
def test_output_is_spec_compliant_y_up_meters(rigged, shape):
    """出力は glTF 仕様準拠(Y-up)・原点=足元・メートル系であること。

    image-3d の Z-up 慣習を引き継ぐと Godot / VRM / three-vrm が正しく読めない
    (計画書 §10 の発見1)。
    """
    gltf = _read_glb_json(rigged[shape].output_path)
    accessor = gltf["accessors"][gltf["meshes"][0]["primitives"][0]["attributes"]["POSITION"]]
    lo, hi = accessor["min"], accessor["max"]

    assert hi[1] - lo[1] == pytest.approx(1.6, abs=0.02), "高さ(Y)が height_m でない"
    assert lo[1] == pytest.approx(0.0, abs=0.02), "原点が足元でない"
    # 上方向(Y)が前後方向(Z)より大きい = 横倒しになっていない
    assert hi[1] - lo[1] > hi[2] - lo[2]
    # 左右(X)は原点対称
    assert lo[0] == pytest.approx(-hi[0], abs=0.05)


@pytest.mark.parametrize("shape", ["humanoid", "chibi"])
def test_output_has_skin(rigged, shape):
    summary = rigged[shape].summary
    gltf = _read_glb_json(rigged[shape].output_path)
    assert len(gltf["skins"]) == 1
    assert len(gltf["skins"][0]["joints"]) == summary["bone_count"] == 21
    attributes = gltf["meshes"][0]["primitives"][0]["attributes"]
    assert "JOINTS_0" in attributes
    assert "WEIGHTS_0" in attributes
    # glTF の1セット(4影響)に収まっていること
    assert "JOINTS_1" not in attributes

    node_names = {n.get("name") for n in gltf["nodes"]}
    assert set(summary["bones"]) <= node_names


def test_engine_reports_failure_for_non_mesh_glb(tmp_path):
    """メッシュを含まない GLB は RigEngineError になり、ジョブを failed にできる。"""
    from server.engines.base import RigEngineError

    empty = tmp_path / "empty.glb"
    gltf = json.dumps({"asset": {"version": "2.0"}, "scenes": [{}], "scene": 0}).encode()
    gltf += b" " * (-len(gltf) % 4)
    chunk = struct.pack("<II", len(gltf), 0x4E4F534A) + gltf
    empty.write_bytes(b"glTF" + struct.pack("<II", 2, 12 + len(chunk)) + chunk)

    with pytest.raises(RigEngineError, match="メッシュ"):
        BpyEngine().rig(
            empty, tmp_path / "out.glb", RigParams(preview=False), tmp_path, 600
        )


def test_rigs_a_fragmented_mesh(tmp_path):
    """UVシームで頂点分割されたメッシュでもリグが付くこと(実機バグの回帰)。

    テクスチャ付きGLB(`texture_mode=paint` の出力)は、UVアトラス境界で頂点が
    複製され数千個の連結成分に分断されている。ボーンヒートは連結メッシュ上で
    熱方程式を解くため、溶接せずに投げると**ウェイトが1つも付かない**まま
    `skins` の無い不正なglTFが出来ていた(実測: 140087頂点/5900成分で付与率0%)。
    """
    fragmented = tmp_path / "fragmented.glb"
    subprocess.run(
        [
            sys.executable, str(MAKE_GLB), "--shape", "humanoid",
            "--fragmented", "--output", str(fragmented),
        ],
        check=True, capture_output=True, timeout=600,
    )

    out = tmp_path / "rigged.glb"
    result = BpyEngine().rig(
        fragmented, out, RigParams(height_m=1.6, preview=False), tmp_path, 600
    )

    weights = result.summary["weights"]
    assert weights["unweighted_vertices"] == 0
    assert weights["weighted_ratio"] == 1.0
    # 溶接されて頂点数が減っていること
    assert weights["vertices"] < 6528

    gltf = _read_glb_json(out)
    assert len(gltf["skins"][0]["joints"]) == 21
    # スキンを参照するノードがあること(これが無いと静的メッシュ扱いになる)
    assert any("skin" in node for node in gltf["nodes"])


def test_preserves_texture_and_uvs(tmp_path):
    """マテリアル・テクスチャ・UVがリグ処理を通り抜けること。

    リグのために頂点を溶接するが、Blender は UV を面コーナーごとに持つので
    テクスチャは壊れない。実運用では image-3d の `texture_mode=paint` 出力
    (色付きモデル)がこの経路を通る。
    """
    textured = tmp_path / "textured.glb"
    subprocess.run(
        [
            sys.executable, str(MAKE_GLB), "--shape", "humanoid",
            "--textured", "--output", str(textured),
        ],
        check=True, capture_output=True, timeout=600,
    )
    source = _read_glb_json(textured)
    assert source["materials"], "フィクスチャにマテリアルが付いていない"

    out = tmp_path / "rigged.glb"
    result = BpyEngine().rig(
        textured, out, RigParams(height_m=1.6, preview=False), tmp_path, 600
    )
    assert result.summary["weights"]["weighted_ratio"] == 1.0

    gltf = _read_glb_json(out)
    attributes = gltf["meshes"][0]["primitives"][0]["attributes"]
    assert "TEXCOORD_0" in attributes, "UVが失われた"
    assert "JOINTS_0" in attributes and "WEIGHTS_0" in attributes
    assert gltf["materials"], "マテリアルが失われた"
    assert gltf["images"], "テクスチャ画像が失われた"
    assert "baseColorTexture" in gltf["materials"][0]["pbrMetallicRoughness"]
    assert len(gltf["skins"][0]["joints"]) == 21
