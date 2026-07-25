"""VRM 1.0 変換(`server/vrm.py`)の単体テスト。bpy 不要。"""
from __future__ import annotations

import pytest

from server import vrm
from tests.glb_fixture import BONES, rigged_glb, rigged_gltf


@pytest.fixture
def glb_path(tmp_path):
    path = tmp_path / "rigged.glb"
    path.write_bytes(rigged_glb())
    return path


@pytest.fixture
def meta():
    return vrm.VrmMeta(name="テストモデル", authors=["animede"])


# --- GLB の読み書き ---------------------------------------------------------


def test_glb_roundtrip_preserves_json_and_binary(tmp_path):
    gltf = rigged_gltf()
    binary = b"\x01\x02\x03"  # 4の倍数でない=パディングが要る長さ
    path = tmp_path / "out.glb"
    vrm.write_glb(path, gltf, binary)

    loaded_gltf, loaded_binary = vrm.read_glb(path)
    assert loaded_gltf == gltf
    # BINチャンクは4バイト境界に揃うのでゼロ埋めされる
    assert loaded_binary.startswith(binary)
    assert len(loaded_binary) % 4 == 0
    assert path.stat().st_size % 4 == 0


def test_read_glb_rejects_non_glb(tmp_path):
    path = tmp_path / "not.glb"
    path.write_bytes(b"this is not a glb file")
    with pytest.raises(vrm.VrmError, match="GLB ではありません"):
        vrm.read_glb(path)


# --- humanBones の構築 ------------------------------------------------------


def test_build_human_bones_maps_all_21_bones():
    human_bones = vrm.build_human_bones(rigged_gltf())
    assert len(human_bones) == 21
    assert set(human_bones) == set(vrm.BONE_TO_VRM.values())
    for bone in vrm.REQUIRED_VRM_BONES:
        assert bone in human_bones


def test_build_human_bones_points_at_correct_nodes():
    gltf = rigged_gltf()
    human_bones = vrm.build_human_bones(gltf)
    for bone_name, vrm_name in vrm.BONE_TO_VRM.items():
        node = gltf["nodes"][human_bones[vrm_name]["node"]]
        assert node["name"] == bone_name


def test_build_extension_rejects_missing_required_bone():
    without_hips = [b for b in BONES if b[0] != "Hips"]
    # Hips を消すと子が根なし子になるが、必須ボーン欠けとして弾かれることを見る
    gltf = rigged_gltf([(n, p if p != "Hips" else None, t) for n, p, t in without_hips])
    with pytest.raises(vrm.VrmError, match="必須ボーン"):
        vrm.build_extension(gltf, vrm.VrmMeta(name="x", authors=["y"]))


# --- メタ情報 ---------------------------------------------------------------


def test_meta_defaults_are_conservative():
    """権利者が不明な生成物のため、既定は最も制限的な設定であること。"""
    meta = vrm.VrmMeta()
    assert meta.avatar_permission == "onlyAuthor"
    assert meta.commercial_usage == "personalNonProfit"
    assert meta.modification == "prohibited"
    assert meta.allow_redistribution is False
    assert meta.credit_notation == "required"


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"name": "  "}, "name"),
        ({"authors": []}, "authors"),
        ({"authors": ["  "]}, "authors"),
        ({"avatar_permission": "anyone"}, "avatar_permission"),
        ({"commercial_usage": "free"}, "commercial_usage"),
        ({"credit_notation": "maybe"}, "credit_notation"),
        ({"modification": "yes"}, "modification"),
    ],
)
def test_meta_validation_rejects_bad_values(kwargs, match):
    meta = vrm.VrmMeta(**{"name": "ok", "authors": ["a"], **kwargs})
    with pytest.raises(vrm.VrmError, match=match):
        meta.validate()


# --- 変換 -------------------------------------------------------------------


def test_convert_writes_valid_vrm(glb_path, tmp_path, meta):
    out = tmp_path / "model.vrm"
    summary = vrm.convert(glb_path, out, meta)

    assert summary["spec_version"] == "1.0"
    assert summary["human_bone_count"] == 21
    assert out.exists()

    gltf, _ = vrm.read_glb(out)
    assert vrm.VRM_EXTENSION in gltf["extensionsUsed"]
    extension = gltf["extensions"][vrm.VRM_EXTENSION]
    assert extension["specVersion"] == "1.0"
    assert extension["meta"]["name"] == "テストモデル"
    assert extension["meta"]["authors"] == ["animede"]
    assert extension["meta"]["licenseUrl"] == vrm.DEFAULT_LICENSE_URL
    assert vrm.validate(gltf) == []


def test_convert_keeps_original_gltf_content(glb_path, tmp_path, meta):
    """VRM化はノードを書き換えず拡張を足すだけであること。"""
    before, _ = vrm.read_glb(glb_path)
    out = tmp_path / "model.vrm"
    vrm.convert(glb_path, out, meta)
    after, _ = vrm.read_glb(out)

    assert after["nodes"] == before["nodes"]
    assert after["scenes"] == before["scenes"]


def test_convert_is_idempotent(glb_path, tmp_path, meta):
    """既にVRM化されたファイルを再変換しても extensionsUsed が重複しない。"""
    first = tmp_path / "a.vrm"
    second = tmp_path / "b.vrm"
    vrm.convert(glb_path, first, meta)
    vrm.convert(first, second, meta)

    gltf, _ = vrm.read_glb(second)
    assert gltf["extensionsUsed"].count(vrm.VRM_EXTENSION) == 1
    assert vrm.validate(gltf) == []


# --- 検証 -------------------------------------------------------------------


def _converted(tmp_path, meta, bones=BONES):
    src = tmp_path / "src.glb"
    src.write_bytes(rigged_glb(bones))
    out = tmp_path / "out.vrm"
    vrm.convert(src, out, meta)
    return vrm.read_glb(out)[0]


def test_validate_detects_missing_extension():
    assert "extensionsUsed" in " ".join(vrm.validate(rigged_gltf()))


def test_validate_detects_broken_node_reference(tmp_path, meta):
    gltf = _converted(tmp_path, meta)
    gltf["extensions"][vrm.VRM_EXTENSION]["humanoid"]["humanBones"]["hips"]["node"] = 999
    assert any("ノード参照が不正" in e for e in vrm.validate(gltf))


def test_validate_detects_bone_outside_hips_hierarchy(tmp_path, meta):
    """VRM 1.0 は hips 以外の humanBone が hips の子孫であることを要求する。"""
    gltf = _converted(tmp_path, meta)
    leg = next(i for i, n in enumerate(gltf["nodes"]) if n["name"] == "LeftUpperLeg")
    # 変換後に脚を hips から切り離して根に移す(変換時点では validate を通るため)
    gltf["nodes"][0]["children"].remove(leg)
    gltf["scenes"][0]["nodes"].append(leg)

    errors = vrm.validate(gltf)
    assert any("hips の子孫" in e for e in errors)


def test_convert_rejects_model_that_would_be_invalid(tmp_path, meta):
    """検証に落ちる出力はファイルを書かずに例外にする。"""
    detached = [(n, None if n == "LeftUpperLeg" else p, t) for n, p, t in BONES]
    src = tmp_path / "detached.glb"
    src.write_bytes(rigged_glb(detached))
    out = tmp_path / "out.vrm"

    with pytest.raises(vrm.VrmError, match="hips の子孫"):
        vrm.convert(src, out, meta)
    assert not out.exists()


def test_validate_detects_upside_down_model(tmp_path, meta):
    gltf = _converted(tmp_path, meta)
    for node in gltf["nodes"]:
        node["translation"][1] *= -1
    assert any("Y-up" in e for e in vrm.validate(gltf))


def test_validate_detects_mirrored_model(tmp_path, meta):
    gltf = _converted(tmp_path, meta)
    for node in gltf["nodes"]:
        node["translation"][0] *= -1
    assert any("左右が反転" in e for e in vrm.validate(gltf))


def test_validate_detects_backwards_facing_model(tmp_path, meta):
    """VRM 1.0 はアバターが +Z を向くことを要求する。"""
    gltf = _converted(tmp_path, meta)
    for node in gltf["nodes"]:
        node["translation"][2] *= -1
    assert any("+Z を向いていない" in e for e in vrm.validate(gltf))


def test_world_translation_accounts_for_rotation(tmp_path, meta):
    """親の回転を無視して平行移動だけ累積すると前後判定を誤る(実装時に踏んだ罠)。"""
    gltf = _converted(tmp_path, meta)
    # Hips を Y軸まわりに180度回すと、子孫のワールド位置は前後左右が反転する
    gltf["nodes"][0]["rotation"] = [0.0, 1.0, 0.0, 0.0]
    world = vrm.world_translations(gltf)
    human_bones = gltf["extensions"][vrm.VRM_EXTENSION]["humanoid"]["humanBones"]
    toes = world[human_bones["leftToes"]["node"]]
    assert toes[2] < 0.0
    assert any("+Z を向いていない" in e for e in vrm.validate(gltf))
