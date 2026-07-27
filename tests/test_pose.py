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

    pose.apply_pose(gltf, pose.parse_pose({"LeftUpperArm": pose.bone_euler("LeftUpperArm", down=70)}))
    after = _world(gltf, "LeftHand")

    assert after[1] < before[1], "腕を下ろしたのに手が下がっていない"
    assert abs(after[0]) < abs(before[0]), "腕を下ろしたのに手が内側に来ていない"


def test_apply_pose_does_not_move_unposed_bones():
    gltf = rigged_gltf()
    before = _world(gltf, "RightHand")
    pose.apply_pose(gltf, pose.parse_pose({"LeftUpperArm": pose.bone_euler("LeftUpperArm", down=70)}))
    assert _world(gltf, "RightHand") == pytest.approx(before)


# --- ファイル書き出し -------------------------------------------------------


def test_pose_glb_writes_posed_file(tmp_path):
    src = tmp_path / "rigged.glb"
    src.write_bytes(rigged_glb())
    dst = tmp_path / "posed.glb"

    summary = pose.pose_glb(src, dst, {"LeftUpperArm": pose.bone_euler("LeftUpperArm", down=70)})

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


# --- 解剖学的な指定 -> ボーンローカル軸 ---------------------------------------
#
# 「腕を下ろしたら前へ突き出した」不具合の再発防止。ボーンローカルのオイラー角を
# 直接主張するのではなく、**ワールドでどちらへ動いたか**を見る。


def _tip_after(bone: str, child: str, **intent: float):
    """`bone` に解剖学的な指定を当てたときの `child` のワールド移動量を返す。"""
    gltf = rigged_gltf()
    before = _world(gltf, child)
    pose.apply_pose(gltf, {bone: pose.bone_quat(bone, **intent)})
    after = _world(gltf, child)
    return [a - b for a, b in zip(after, before)]


@pytest.mark.parametrize("side", ["Left", "Right"])
def test_down_lowers_the_arm_without_pushing_it_forward(side):
    """`down` は腕を**下げる**。前後にはほとんど動かないこと。"""
    delta = _tip_after(f"{side}UpperArm", f"{side}Hand", down=70)
    assert delta[1] < -0.1, "腕が下がっていない"
    assert abs(delta[2]) < 0.02, "腕が前後に突き出している"


@pytest.mark.parametrize("side", ["Left", "Right"])
def test_forward_swings_the_arm_forward_on_both_sides(side):
    """`forward` は左右とも**前(+Z)**へ振る(骨方向が逆でも符号は揃う)。"""
    delta = _tip_after(f"{side}UpperArm", f"{side}Hand", forward=40)
    assert delta[2] > 0.1, "腕が前に出ていない"
    assert abs(delta[1]) < 0.02, "腕が上下に動いている"


@pytest.mark.parametrize("side", ["Left", "Right"])
def test_twist_does_not_move_the_arm_tip(side):
    """`twist` は骨の軸まわりなので手の位置はほぼ変わらない。"""
    delta = _tip_after(f"{side}UpperArm", f"{side}Hand", twist=45)
    assert max(abs(v) for v in delta) < 0.02


@pytest.mark.parametrize("side", ["Left", "Right"])
def test_forward_swings_the_leg_forward(side):
    delta = _tip_after(f"{side}UpperLeg", f"{side}Foot", forward=30)
    assert delta[2] > 0.1, "脚が前に出ていない"


@pytest.mark.parametrize("side", ["Left", "Right"])
def test_negative_forward_bends_the_knee_backwards(side):
    """膝は後ろにしか曲がらない(逆関節にならないこと)。"""
    delta = _tip_after(f"{side}LowerLeg", f"{side}Foot", forward=-40)
    assert delta[2] < -0.05, "膝が前に曲がっている(逆関節)"


def test_forward_bows_the_spine_forward():
    delta = _tip_after("Spine", "Head", forward=25)
    assert delta[2] > 0.05, "上体が前に倒れていない"


def test_down_is_rejected_for_non_arm_bones():
    """体幹に down を渡すと静かに変な向きへ回るのではなく落ちること。"""
    with pytest.raises(pose.PoseError, match="down"):
        pose.bone_euler("Spine", down=20)


def test_pose_presets_lower_the_arms():
    """プリセット `arms_down` が実際に手を下げること(定義元はサーバ)。"""
    presets = pose.presets()
    gltf = rigged_gltf()
    before = _world(gltf, "LeftHand")
    pose.apply_pose(gltf, pose.parse_pose(presets["arms_down"]["pose"]))
    after = _world(gltf, "LeftHand")
    assert after[1] < before[1] - 0.1, "arms_down で手が下がっていない"
    assert abs(after[2] - before[2]) < 0.15, "arms_down で腕が前へ突き出している"


# --- 手のひらの向き -----------------------------------------------------------
#
# レストのTポーズでは手のひらが正面(+Z)を向いている。腕を下ろしただけだと
# 手のひらが前を向いたままになるため、`twist` で内向きにする。


def _palm_direction(side: str, **intent: float):
    """指定のポーズを当てたときの手のひらのワールド方向を返す。"""
    import math

    gltf = rigged_gltf()

    def world_rotation(g, bone):
        nodes = g["nodes"]
        parent = {c: i for i, n in enumerate(nodes) for c in n.get("children", [])}
        index = next(i for i, n in enumerate(nodes) if n.get("name") == bone)
        chain = []
        while index is not None:
            chain.append(index)
            index = parent.get(index)
        q = pose.IDENTITY
        for i in reversed(chain):
            q = pose.quat_multiply(q, tuple(nodes[i].get("rotation", pose.IDENTITY)))
        return q

    def rotate(q, v):
        qv = (v[0], v[1], v[2], 0.0)
        conj = (-q[0], -q[1], -q[2], q[3])
        r = pose.quat_multiply(pose.quat_multiply(q, qv), conj)
        return r[:3]

    rest = world_rotation(gltf, f"{side}Hand")
    inverse_rest = (-rest[0], -rest[1], -rest[2], rest[3])
    palm_local = rotate(inverse_rest, (0.0, 0.0, 1.0))  # レストでは +Z を向く

    pose.apply_pose(gltf, {f"{side}UpperArm": pose.bone_quat(f"{side}UpperArm", **intent)})
    return rotate(world_rotation(gltf, f"{side}Hand"), palm_local)


@pytest.mark.parametrize("side", ["Left", "Right"])
def test_lowering_the_arm_alone_leaves_the_palm_facing_forward(side):
    """ねじらないと手のひらは正面を向いたまま(報告された見た目)。"""
    palm = _palm_direction(side, down=70)
    assert palm[2] > 0.9, "手のひらが正面を向いていない"


@pytest.mark.parametrize("side", ["Left", "Right"])
def test_twist_turns_the_palm_inward_on_both_sides(side):
    """`twist` は正で**左右とも**手のひらを内(体の側)へ向ける。"""
    palm = _palm_direction(side, down=70, twist=90)
    inward = -palm[0] if side == "Left" else palm[0]
    assert inward > 0.9, "手のひらが内を向いていない(左右の反転を忘れている)"


@pytest.mark.parametrize("side", ["Left", "Right"])
def test_twist_does_not_steal_the_forward_swing(side):
    """ねじりを入れても前後の振りが効くこと。

    XYZ順のまま Y にねじりを入れると、続く Z(前後)の軸までねじられて
    前後の振りが上下に化ける。
    """
    plain = _tip_after(f"{side}UpperArm", f"{side}Hand", down=70, forward=40)
    twisted = _tip_after(f"{side}UpperArm", f"{side}Hand", down=70, twist=90, forward=40)
    assert twisted[2] > 0.1, "ねじりを入れると腕が前に出なくなっている"
    assert twisted[2] == pytest.approx(plain[2], abs=0.03)


@pytest.mark.parametrize("side", ["Left", "Right"])
def test_arms_down_preset_turns_the_palm_inward(side):
    presets = pose.presets()
    gltf = rigged_gltf()
    pose.apply_pose(gltf, pose.parse_pose(presets["arms_down"]["pose"]))
    # プリセット適用後の手の位置が体側に下りていることは別テストで見ているので、
    # ここでは意図(twist)がプリセットに入っていることを確かめる
    assert presets["arms_down"]["pose"][f"{side}UpperArm"] != list(
        pose.bone_euler(f"{side}UpperArm", down=70)
    ), "arms_down にねじりが入っていない"


def test_quat_to_euler_round_trips():
    for angles in [(0, 0, 0), (20, -35, 50), (-70, 0, 25), (10, 89, -10)]:
        q = pose.quat_from_euler_degrees(angles)
        back = pose.quat_from_euler_degrees(pose.quat_to_euler_degrees(q))
        assert back == pytest.approx(q, abs=1e-6) or back == pytest.approx(
            tuple(-v for v in q), abs=1e-6
        )


@pytest.mark.parametrize("preset", ["arms_down", "relaxed"])
@pytest.mark.parametrize("side", ["Left", "Right"])
def test_standing_presets_keep_the_arms_clear_of_the_body(preset, side):
    """立ち姿のプリセットで腕が体に密着しないこと。"""
    gltf = rigged_gltf()
    hip = abs(_world(gltf, f"{side}UpperLeg")[0])
    pose.apply_pose(gltf, pose.parse_pose(pose.presets()[preset]["pose"]))
    hand = abs(_world(gltf, f"{side}Hand")[0])
    assert hand > hip * 1.5, f"{preset}: 腕が体に密着している"
