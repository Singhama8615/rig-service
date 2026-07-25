"""同梱モーションクリップ(`server/motions.py`)の単体テスト。bpy 不要。"""
from __future__ import annotations

import pytest

from server import motions, pose, vrm
from tests.glb_fixture import rigged_gltf


# --- クリップ定義 -----------------------------------------------------------


def test_all_clips_reference_existing_bones():
    """存在しないボーン名を書いてしまうと焼き込み時に初めて落ちるので先に弾く。"""
    assert motions.validate_bones() == []


@pytest.mark.parametrize("name", sorted(motions.MOTIONS))
def test_clip_is_well_formed(name):
    motion = motions.get(name)
    assert motion.duration > 0
    assert motion.tracks, "トラックが空のクリップは再生しても何も起きない"
    for bone, keys in motion.tracks.items():
        assert len(keys) >= 2, f"{bone}: キーフレームが1つだと補間できない"
        times = [t for t, _ in keys]
        assert times == sorted(times), f"{bone}: 時刻が昇順でない"
        assert times[0] == 0.0, f"{bone}: 0秒のキーが無い"
        assert times[-1] == pytest.approx(motion.duration), f"{bone}: 終端キーが duration と違う"


@pytest.mark.parametrize("name", ["idle", "wave", "walk"])
def test_looping_clips_start_and_end_at_same_pose(name):
    """ループするクリップは先頭と末尾が一致していないと繋ぎ目で跳ねる。"""
    motion = motions.get(name)
    assert motion.loop is True
    for bone, keys in motion.tracks.items():
        assert keys[0][1] == pytest.approx(keys[-1][1]), f"{bone}: ループが繋がらない"


def test_bow_is_not_looping():
    assert motions.get("bow").loop is False


def test_get_unknown_motion_raises():
    with pytest.raises(KeyError):
        motions.get("moonwalk")


# --- ブラウザ向けの出力 -----------------------------------------------------


def test_to_dict_emits_quaternions():
    """規約の取り違えを避けるため、ブラウザにはクォータニオンで渡す。"""
    data = motions.get("wave").to_dict()
    assert data["name"] == "wave"
    assert data["loop"] is True
    for keys in data["tracks"].values():
        for key in keys:
            assert len(key["rotation"]) == 4
            assert sum(v * v for v in key["rotation"]) == pytest.approx(1.0)


def test_to_dict_matches_pose_convention():
    motion = motions.get("bow")
    first_time, first_degrees = motion.tracks["Spine"][1]
    entry = motion.to_dict()["tracks"]["Spine"][1]
    assert entry["time"] == first_time
    assert entry["rotation"] == pytest.approx(
        list(pose.quat_from_euler_degrees(first_degrees))
    )


def test_summary_has_no_keyframes():
    summary = motions.get("idle").summary()
    assert "tracks" not in summary
    assert set(summary) == {"name", "label", "duration", "loop", "bones"}


# --- glTF への焼き込み ------------------------------------------------------


def _bake(name: str):
    gltf = rigged_gltf()
    return motions.bake_into_gltf(gltf, b"", motions.get(name))


@pytest.mark.parametrize("name", sorted(motions.MOTIONS))
def test_bake_creates_valid_animation(name):
    gltf, binary = _bake(name)
    animation = gltf["animations"][0]

    assert animation["name"] == name
    assert len(animation["channels"]) == len(motions.get(name).tracks)
    assert len(animation["samplers"]) == len(animation["channels"])

    node_count = len(gltf["nodes"])
    for channel in animation["channels"]:
        assert channel["target"]["path"] == "rotation"
        assert 0 <= channel["target"]["node"] < node_count
        assert 0 <= channel["sampler"] < len(animation["samplers"])

    for sampler in animation["samplers"]:
        assert sampler["interpolation"] == "LINEAR"
        time_accessor = gltf["accessors"][sampler["input"]]
        rotation_accessor = gltf["accessors"][sampler["output"]]
        assert time_accessor["type"] == "SCALAR"
        assert rotation_accessor["type"] == "VEC4"
        # 時刻アクセサは min/max が必須 (glTF 仕様)
        assert "min" in time_accessor and "max" in time_accessor
        assert time_accessor["count"] == rotation_accessor["count"]

    # bufferView は全て4バイト境界から始まり、バッファ長と整合すること
    for view in gltf["bufferViews"]:
        assert view["byteOffset"] % 4 == 0
        assert view["byteOffset"] + view["byteLength"] <= len(binary)
    assert gltf["buffers"][0]["byteLength"] == len(binary)


def test_bake_composes_rest_rotation():
    """glTFのチャンネルは絶対回転を上書きするので、レストを掛けてから焼く。"""
    import struct

    gltf = rigged_gltf()
    head_index = next(i for i, n in enumerate(gltf["nodes"]) if n["name"] == "Head")
    rest = pose.quat_from_euler_degrees([0, 0, 30])
    gltf["nodes"][head_index]["rotation"] = list(rest)

    motion = motions.get("bow")
    gltf, binary = motions.bake_into_gltf(gltf, b"", motion)

    channel = next(
        c for c in gltf["animations"][0]["channels"] if c["target"]["node"] == head_index
    )
    sampler = gltf["animations"][0]["samplers"][channel["sampler"]]
    accessor = gltf["accessors"][sampler["output"]]
    view = gltf["bufferViews"][accessor["bufferView"]]
    first = struct.unpack_from("<4f", binary, view["byteOffset"])

    expected = pose.quat_multiply(rest, pose.quat_from_euler_degrees(motion.tracks["Head"][0][1]))
    assert first == pytest.approx(expected, abs=1e-6)


def test_bake_rejects_bone_missing_from_model():
    gltf = rigged_gltf()
    gltf["nodes"] = [n for n in gltf["nodes"] if n["name"] != "LeftUpperLeg"]
    with pytest.raises(KeyError, match="モデルに存在しない"):
        motions.bake_into_gltf(gltf, b"", motions.get("walk"))


def test_bake_preserves_existing_buffer(tmp_path):
    """既存のBINチャンク(メッシュ)を壊さず、後ろに足すだけであること。"""
    gltf = rigged_gltf()
    original = b"\x01\x02\x03\x04" * 8
    gltf, binary = motions.bake_into_gltf(gltf, original, motions.get("idle"))
    assert binary.startswith(original)


def test_baked_glb_roundtrips(tmp_path):
    gltf, binary = _bake("walk")
    path = tmp_path / "animated.glb"
    vrm.write_glb(path, gltf, binary)

    loaded_gltf, loaded_binary = vrm.read_glb(path)
    assert loaded_gltf["animations"][0]["name"] == "walk"
    assert loaded_binary.startswith(binary)


def test_bake_twice_adds_two_animations():
    gltf = rigged_gltf()
    gltf, binary = motions.bake_into_gltf(gltf, b"", motions.get("idle"))
    gltf, binary = motions.bake_into_gltf(gltf, binary, motions.get("wave"))
    assert [a["name"] for a in gltf["animations"]] == ["idle", "wave"]
