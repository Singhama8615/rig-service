"""ポーズ適用(`server/pose.py`)の単体テスト。bpy 不要。"""
from __future__ import annotations

import math

import pytest

from server import pose, vrm
from tests.glb_fixture import rigged_glb, rigged_gltf


def _world(gltf, bone: str):
    index = next(i for i, n in enumerate(gltf["nodes"]) if n["name"] == bone)
    return vrm.world_translations(gltf)[index]


# --- クォータニオン ---------------------------------------------------------


def test_quat_from_euler_zero_is_identity():
    assert pose.quat_from_euler_degrees([0, 0, 0]) == pytest.approx(pose.IDENTITY)


def test_quat_from_euler_90deg_about_z():
    q = pose.quat_from_euler_degrees([0, 0, 90])
    assert q == pytest.approx((0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4)))


def test_quat_from_euler_matches_threejs_xyz_convention():
    """three.js の `Quaternion.setFromEuler(Euler(x,y,z,"XYZ"))` と一致すること。

    同じ「XYZ順」でも Blender は逆順(Rz·Ry·Rx)で合成する。ブラウザ側と
    食い違うと2軸以上回したボーンだけ静かにズレるので、閉じた式で固定する。
    """
    x, y, z = (math.radians(v) / 2 for v in (-20.0, 35.0, -20.0))
    c1, s1 = math.cos(x), math.sin(x)
    c2, s2 = math.cos(y), math.sin(y)
    c3, s3 = math.cos(z), math.sin(z)
    threejs = (
        s1 * c2 * c3 + c1 * s2 * s3,
        c1 * s2 * c3 - s1 * c2 * s3,
        c1 * c2 * s3 + s1 * s2 * c3,
        c1 * c2 * c3 - s1 * s2 * s3,
    )
    assert pose.quat_from_euler_degrees([-20.0, 35.0, -20.0]) == pytest.approx(threejs)


def test_euler_order_matters_for_multi_axis():
    """順序を取り違えると単軸では一致し、多軸でだけ食い違う(実装時に踏んだ罠)。"""
    single = pose.quat_from_euler_degrees([0, 0, -20])
    reversed_single = pose.quat_multiply(
        pose.quat_from_euler_degrees([0, 0, -20]), pose.IDENTITY
    )
    assert single == pytest.approx(reversed_single)

    multi = pose.quat_from_euler_degrees([-20, 0, -20])
    qx = pose.quat_from_euler_degrees([-20, 0, 0])
    qz = pose.quat_from_euler_degrees([0, 0, -20])
    assert multi == pytest.approx(pose.quat_multiply(qx, qz))
    assert multi != pytest.approx(pose.quat_multiply(qz, qx))


def test_quat_multiply_with_identity():
    q = pose.quat_from_euler_degrees([10, 20, 30])
    assert pose.quat_multiply(q, pose.IDENTITY) == pytest.approx(q)
    assert pose.quat_multiply(pose.IDENTITY, q) == pytest.approx(q)


# --- 入力の解釈 -------------------------------------------------------------


def test_parse_pose_accepts_quaternion_and_euler():
    parsed = pose.parse_pose({"Head": [0, 0, 0, 1], "Spine": [0, 0, 90]})
    assert parsed["Head"] == pytest.approx(pose.IDENTITY)
    assert parsed["Spine"] == pytest.approx(pose.quat_from_euler_degrees([0, 0, 90]))


def test_parse_pose_accepts_vrm_bone_names():
    """VRM の lowerCamelCase 名でも指定できること。"""
    parsed = pose.parse_pose({"leftUpperArm": [0, 0, 30]})
    assert "LeftUpperArm" in parsed
    assert "leftUpperArm" not in parsed


def test_parse_pose_normalizes_quaternion():
    parsed = pose.parse_pose({"Head": [0, 0, 0, 2]})
    assert parsed["Head"] == pytest.approx(pose.IDENTITY)


@pytest.mark.parametrize(
    "bad,match",
    [
        ({"NoSuchBone": [0, 0, 0]}, "未知のボーン"),
        ({"Head": [0, 0]}, "長さ4"),
        ({"Head": [0, 0, 0, 0, 0]}, "長さ4"),
        ({"Head": "0,0,0"}, "長さ4"),
        ({"Head": ["a", "b", "c"]}, "数値でない"),
        ({"Head": [0, 0, 0, 0]}, "長さが 0"),
    ],
)
def test_parse_pose_rejects_bad_input(bad, match):
    with pytest.raises(pose.PoseError, match=match):
        pose.parse_pose(bad)


def test_parse_pose_rejects_non_dict():
    with pytest.raises(pose.PoseError, match="オブジェクト"):
        pose.parse_pose(["Head"])


# --- 適用 -------------------------------------------------------------------


def test_apply_pose_composes_with_rest_rotation():
    """ポーズはレスト姿勢を置き換えるのではなく後ろに合成する。"""
    gltf = rigged_gltf()
    head = next(n for n in gltf["nodes"] if n["name"] == "Head")
    rest = pose.quat_from_euler_degrees([0, 0, 30])
    head["rotation"] = list(rest)

    pose.apply_pose(gltf, pose.parse_pose({"Head": [0, 0, 60]}))

    expected = pose.quat_multiply(rest, pose.quat_from_euler_degrees([0, 0, 60]))
    assert tuple(head["rotation"]) == pytest.approx(expected)


def test_apply_pose_returns_applied_bones():
    gltf = rigged_gltf()
    applied = pose.apply_pose(gltf, pose.parse_pose({"Head": [0, 0, 10], "Spine": [5, 0, 0]}))
    assert applied == ["Head", "Spine"]


def test_apply_pose_rejects_bone_missing_from_model():
    # Toes を持たないモデルに Toes のポーズを送る
    gltf = rigged_gltf()
    gltf["nodes"] = [n for n in gltf["nodes"] if n["name"] != "LeftToes"]
    with pytest.raises(pose.PoseError, match="モデルに存在しない"):
        pose.apply_pose(gltf, pose.parse_pose({"LeftToes": [0, 0, 10]}))


def test_apply_pose_moves_descendants():
    """肩を回すと手のワールド位置が動く(=スキンが追従する形になっている)。"""
    gltf = rigged_gltf()
    before = _world(gltf, "LeftHand")

    pose.apply_pose(gltf, pose.parse_pose({"LeftUpperArm": [0, 0, -70]}))
    after = _world(gltf, "LeftHand")

    assert after[1] < before[1], "腕を下ろしたのに手が下がっていない"
    assert abs(after[0]) < abs(before[0]), "腕を下ろしたのに手が内側に来ていない"


def test_apply_pose_does_not_move_unposed_bones():
    gltf = rigged_gltf()
    before = _world(gltf, "RightHand")
    pose.apply_pose(gltf, pose.parse_pose({"LeftUpperArm": [0, 0, -70]}))
    assert _world(gltf, "RightHand") == pytest.approx(before)


# --- ファイル書き出し -------------------------------------------------------


def test_pose_glb_writes_posed_file(tmp_path):
    src = tmp_path / "rigged.glb"
    src.write_bytes(rigged_glb())
    dst = tmp_path / "posed.glb"

    summary = pose.pose_glb(src, dst, {"LeftUpperArm": [0, 0, -70]})

    assert summary["applied_bones"] == ["LeftUpperArm"]
    assert dst.exists()
    posed, _ = vrm.read_glb(dst)
    original, _ = vrm.read_glb(src)
    assert _world(posed, "LeftHand")[1] < _world(original, "LeftHand")[1]


def test_pose_glb_keeps_vrm_extension(tmp_path):
    """VRMにポーズを当ててもVRMのままであること。"""
    src = tmp_path / "rigged.glb"
    src.write_bytes(rigged_glb())
    as_vrm = tmp_path / "model.vrm"
    vrm.convert(src, as_vrm, vrm.VrmMeta(name="t", authors=["a"]))

    posed = tmp_path / "posed.vrm"
    pose.pose_glb(as_vrm, posed, {"Head": [0, 10, 0]})

    gltf, _ = vrm.read_glb(posed)
    assert vrm.VRM_EXTENSION in gltf["extensionsUsed"]
    # ポーズで骨を回すと VRM の「+Zを向く」検証は当然変わりうるが、
    # 首を少し回した程度では仕様検証を壊さないこと
    assert vrm.validate(gltf) == []


def test_empty_pose_is_a_noop(tmp_path):
    src = tmp_path / "rigged.glb"
    src.write_bytes(rigged_glb())
    dst = tmp_path / "posed.glb"

    summary = pose.pose_glb(src, dst, {})

    assert summary["applied_bones"] == []
    assert vrm.read_glb(dst)[0]["nodes"] == vrm.read_glb(src)[0]["nodes"]
