"""座標正規化・計測・骨格レイアウトの単体テスト(bpy不要)。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server" / "bpy_scripts"))

import proportions  # noqa: E402


def _box(center, size, n=400, rng=None) -> np.ndarray:
    """直方体の表面付近をランダムサンプリングした点群。"""
    rng = rng or np.random.default_rng(0)
    c = np.asarray(center, dtype=float)
    s = np.asarray(size, dtype=float) / 2
    pts = rng.uniform(-1, 1, size=(n, 3))
    # いずれか1軸を面に貼り付けて「表面っぽい」点群にする
    axis = rng.integers(0, 3, size=n)
    pts[np.arange(n), axis] = np.sign(pts[np.arange(n), axis])
    return c + pts * s


def tpose_cloud(
    height: float = 1.6,
    shoulder: float = 0.82,
    crotch: float = 0.50,
    arm_half: float = 0.50,
    head_base: float = 0.87,
    leg_x: float = 0.075,
    facing: int = -1,
) -> np.ndarray:
    """正規化座標(+Z上・足元z=0)のTポーズ点群。比率はすべて全高比で指定する。"""
    h = height
    foot_y = 0.09 * h * facing
    parts = [
        # 頭・首・胴
        ((0, 0, (head_base + 1.0) / 2 * h), (0.16 * h, 0.16 * h, (1.0 - head_base) * h)),
        ((0, 0, (shoulder + head_base) / 2 * h), (0.05 * h, 0.05 * h, (head_base - shoulder) * h)),
        ((0, 0, (crotch + shoulder) / 2 * h), (0.26 * h, 0.14 * h, (shoulder - crotch) * h)),
        # 腕(水平)
        ((arm_half / 2 * h, 0, shoulder * h), (arm_half * h, 0.08 * h, 0.08 * h)),
        ((-arm_half / 2 * h, 0, shoulder * h), (arm_half * h, 0.08 * h, 0.08 * h)),
        # 脚
        ((leg_x * h, 0, crotch / 2 * h), (0.10 * h, 0.10 * h, crotch * h)),
        ((-leg_x * h, 0, crotch / 2 * h), (0.10 * h, 0.10 * h, crotch * h)),
        # 足(つま先が正面側へ出る)
        ((leg_x * h, foot_y / 2, 0.02 * h), (0.09 * h, abs(foot_y), 0.04 * h)),
        ((-leg_x * h, foot_y / 2, 0.02 * h), (0.09 * h, abs(foot_y), 0.04 * h)),
    ]
    rng = np.random.default_rng(42)
    cloud = np.vstack([_box(c, s, 600, rng) for c, s in parts])
    # 足元をちょうど z=0、頭頂をちょうど z=height に合わせる
    cloud[:, 2] -= cloud[:, 2].min()
    cloud[:, 2] *= height / cloud[:, 2].max()
    return cloud


# --- 上方向軸の判定 ---------------------------------------------------------


def test_detect_up_axis_y_up_source():
    """仕様準拠(Y-up)のGLBはインポート後 Blender の +Z 方向に立つ。"""
    verts = tpose_cloud()  # Z が最長 = Y-up 由来
    assert proportions.detect_source_up_axis(verts) == "y"


def test_detect_up_axis_z_up_source():
    """image-3d の Z-up GLB はインポート後 Blender の -Y 方向に倒れる。"""
    v = tpose_cloud()
    # glTF(Z-up) -> Blender: (x, y, z) -> (x, -z, y)
    verts = np.stack([v[:, 0], -v[:, 2], v[:, 1]], axis=1)
    assert proportions.detect_source_up_axis(verts) == "z"


def test_detect_up_axis_not_confused_by_tpose_arm_span():
    """腕を広げて左右幅が身長を超えても、上方向の判定は左右軸に引きずられない。"""
    verts = tpose_cloud(arm_half=0.62)  # 指先間の幅 > 身長
    assert verts[:, 0].ptp() > verts[:, 2].ptp()
    assert proportions.detect_source_up_axis(verts) == "y"


# --- 正面方向の判定 ---------------------------------------------------------


@pytest.mark.parametrize("facing", [-1, 1])
def test_detect_facing(facing):
    verts = tpose_cloud(facing=facing)
    sign, _ = proportions.detect_facing(verts)
    assert sign == facing


# --- 正規化 -----------------------------------------------------------------


def test_normalize_matrix_from_z_up_source():
    """Z-up・高さ100 の入力が Y-up換算・原点足元・指定身長に揃うこと。"""
    v = tpose_cloud(height=100.0)
    blender = np.stack([v[:, 0], -v[:, 2], v[:, 1]], axis=1)

    matrix, info = proportions.normalize_matrix(blender, height_m=1.6)
    out = blender @ matrix[:3, :3].T + matrix[:3, 3]

    assert info["up_axis"] == "z"
    assert info["facing"] == "-y"
    assert out[:, 2].min() == pytest.approx(0.0, abs=1e-6)
    assert out[:, 2].max() == pytest.approx(1.6, abs=1e-6)
    # 左右は原点対称
    assert out[:, 0].mean() == pytest.approx(0.0, abs=0.02)


def test_normalize_matrix_rotates_when_facing_plus_y():
    """正面が +Y のモデルは 180° 回して -Y 向きに揃えられる。"""
    v = tpose_cloud(facing=1)
    matrix, info = proportions.normalize_matrix(v, height_m=1.6, up_axis="y")
    out = v @ matrix[:3, :3].T + matrix[:3, 3]
    assert info["facing"] == "+y"
    foot = out[out[:, 2] <= 0.08 * 1.6]
    # 回転後はつま先が -Y 側に来る
    assert abs(foot[:, 1].min()) > abs(foot[:, 1].max())


def test_normalize_matrix_rejects_flat_mesh():
    verts = np.random.default_rng(0).uniform(-1, 1, size=(100, 3))
    verts[:, 2] = 0.0
    with pytest.raises(ValueError):
        proportions.normalize_matrix(verts, height_m=1.6, up_axis="y")


# --- 計測 -------------------------------------------------------------------


def test_measure_human_proportions():
    m = proportions.measure(tpose_cloud())
    assert m.t_pose is True
    assert m.fallbacks == []
    assert m.shoulder_z == pytest.approx(0.82 * 1.6, rel=0.06)
    assert m.crotch_z == pytest.approx(0.50 * 1.6, rel=0.10)
    assert m.arm_half_span == pytest.approx(0.50 * 1.6, rel=0.05)
    assert m.leg_center_x == pytest.approx(0.075 * 1.6, rel=0.20)
    assert m.head_base_z == pytest.approx(0.87 * 1.6, rel=0.06)


def test_measure_chibi_proportions():
    """頭身の低いデフォルメ体型(ぬいぐるみ・動物キャラ)でも実測が効くこと。

    人体標準比へ倒してしまうと関節位置が丸ごとズレるので、
    妥当範囲は人体標準よりかなり広く取ってある。
    """
    cloud = tpose_cloud(shoulder=0.49, crotch=0.20, head_base=0.56, arm_half=0.53)
    m = proportions.measure(cloud)
    assert m.t_pose is True
    assert "shoulder_z" not in m.fallbacks
    assert "crotch_z" not in m.fallbacks
    assert m.shoulder_z == pytest.approx(0.49 * 1.6, rel=0.08)
    assert m.crotch_z == pytest.approx(0.20 * 1.6, rel=0.25)


def test_measure_neck_not_confused_by_tapering_top():
    """頭頂の突起(帽子の先など)は首より細い。首の探索を上まで広げると誤認する。

    実機再現: ぬいぐるみキャラ(サンタ帽)で頭頂 half=0.127h < 首 half=0.181h となり、
    首の高さが 0.98h と判定されて人体標準比へ倒れていた。
    """
    h = 1.6
    rng = np.random.default_rng(7)
    cloud = tpose_cloud(shoulder=0.45, crotch=0.20, head_base=0.55, arm_half=0.53)
    # 首(半幅 0.025h)より細い突起を頭頂に足す
    tip = _box((0, 0, 0.97 * h), (0.03 * h, 0.03 * h, 0.06 * h), 400, rng)
    m = proportions.measure(np.vstack([cloud, tip]))

    assert "head_base_z" not in m.fallbacks
    assert m.head_base_z == pytest.approx(0.55 * h, rel=0.12)


def test_crotch_falls_back_to_shoulder_ratio_when_legs_are_merged():
    """脚が繋がって股下を検出できないとき、全高比ではなく肩の高さから比例で出す。

    実機再現: 肩が 0.45h のぬいぐるみキャラで、人体標準比(0.50h)に倒すと
    股下が実際の倍以上になり、脚のボーンが胴体の中に入ってしまっていた。
    """
    h = 1.6
    rng = np.random.default_rng(11)
    cloud = tpose_cloud(shoulder=0.45, crotch=0.20, head_base=0.55, arm_half=0.53)
    # 脚の間を埋めて左右の隙間を無くす
    filler = _box((0, 0, 0.10 * h), (0.26 * h, 0.12 * h, 0.20 * h), 2000, rng)
    m = proportions.measure(np.vstack([cloud, filler]))

    assert "crotch_z" in m.fallbacks
    assert m.crotch_z == pytest.approx(proportions.CROTCH_TO_SHOULDER_RATIO * m.shoulder_z)
    # 人体標準比(0.50h)に倒れていないこと
    assert m.crotch_z < 0.35 * h


def test_crotch_falls_back_to_height_ratio_when_shoulder_is_unknown():
    """肩も実測できていないなら比例の基準が無いので全高比へ倒す。"""
    h = 1.6
    rng = np.random.default_rng(13)
    # 腕を下ろした縦長の塊(Tポーズでない=肩が実測できない)
    cloud = _box((0, 0, 0.5 * h), (0.3 * h, 0.2 * h, 1.0 * h), 4000, rng)
    cloud[:, 2] -= cloud[:, 2].min()
    cloud[:, 2] *= h / cloud[:, 2].max()
    m = proportions.measure(cloud)

    assert "shoulder_z" in m.fallbacks
    assert "crotch_z" in m.fallbacks
    assert m.crotch_z == pytest.approx(proportions.DEFAULT_CROTCH_Z * h)


def test_measure_flags_non_tpose():
    """腕を下ろした(=縦に長い幅広部を持つ)モデルはTポーズと判定しない。"""
    h = 1.6
    rng = np.random.default_rng(1)
    # スカート状に 0.1h〜0.9h が一様に広い形状
    cloud = np.vstack(
        [
            _box((0, 0, 0.5 * h), (0.7 * h, 0.4 * h, 0.8 * h), 3000, rng),
            _box((0, 0, 0.95 * h), (0.2 * h, 0.2 * h, 0.1 * h), 600, rng),
        ]
    )
    cloud[:, 2] -= cloud[:, 2].min()
    cloud[:, 2] *= h / cloud[:, 2].max()
    m = proportions.measure(cloud)
    assert m.t_pose is False
    assert "shoulder_z" in m.fallbacks


# --- 骨格レイアウト ---------------------------------------------------------


def test_bone_layout_structure():
    bones = proportions.bone_layout(proportions.measure(tpose_cloud()))
    names = [b.name for b in bones]

    assert len(bones) == 21
    assert names[0] == "Hips"
    # VRM 1.0 humanoid の必須ボーンが揃っていること
    required = ["Hips", "Spine", "Head"]
    for side in ("Left", "Right"):
        required += [f"{side}{part}" for part in
                     ("UpperArm", "LowerArm", "Hand", "UpperLeg", "LowerLeg", "Foot")]
    assert set(required) <= set(names)

    by_name = {b.name: b for b in bones}
    # 親は自分より前に定義されていること(アーマチュア構築順の前提)
    for i, bone in enumerate(bones):
        if bone.parent:
            assert names.index(bone.parent) < i
    # Hips 以外はすべて Hips に辿り着く(VRMはHipsが唯一の根)
    for bone in bones:
        node = bone
        while node.parent:
            node = by_name[node.parent]
        assert node.name == "Hips"


def test_bone_layout_is_left_right_symmetric():
    bones = {b.name: b for b in proportions.bone_layout(proportions.measure(tpose_cloud()))}
    for name, left in bones.items():
        if not name.startswith("Left"):
            continue
        right = bones["Right" + name[len("Left"):]]
        for point_l, point_r in ((left.head, right.head), (left.tail, right.tail)):
            assert point_l[0] == pytest.approx(-point_r[0])
            assert point_l[1] == pytest.approx(point_r[1])
            assert point_l[2] == pytest.approx(point_r[2])


def test_bone_layout_left_is_plus_x():
    """VRM/glTF 換算でキャラクターの左が +X であること。"""
    bones = {b.name: b for b in proportions.bone_layout(proportions.measure(tpose_cloud()))}
    assert bones["LeftHand"].tail[0] > 0
    assert bones["RightHand"].tail[0] < 0


def test_bone_layout_chain_is_ordered_bottom_up():
    m = proportions.measure(tpose_cloud())
    bones = {b.name: b for b in proportions.bone_layout(m)}
    chain = ["Hips", "Spine", "Chest", "Neck", "Head"]
    heights = [bones[n].head[2] for n in chain]
    assert heights == sorted(heights)
    assert bones["Head"].tail[2] == pytest.approx(m.height)
    # 脚は下向き、つま先は正面(-Y)側
    assert bones["LeftUpperLeg"].tail[2] < bones["LeftUpperLeg"].head[2]
    assert bones["LeftToes"].tail[1] < 0


def test_detect_facing_prefers_head_cue_on_disagreement():
    """足と後頭部が食い違ったら後頭部を採る(実測で後頭部のほうが当たる)。

    Pixal3D のぬいぐるみ体型は大きな前足が前後に張り出し、足の手がかりが
    ほぼ機能しない(実測 3/8)。後頭部は 8/8 で正しかった。
    """
    h = 1.6
    rng = np.random.default_rng(3)
    # つま先は +Y 側(足の手がかりは「正面は +Y」と言う)
    cloud = tpose_cloud(facing=1)
    # 後頭部の膨らみ(髪)も +Y 側に置く(頭の手がかりは「背面が +Y」= 正面は -Y)
    # 髪は薄くする(大きすぎるとbbox中心ごと動いて足の手がかりまで反転する)
    hair = _box((0, 0.045 * h, 0.95 * h), (0.15 * h, 0.05 * h, 0.08 * h), 1200, rng)
    cloud = np.vstack([cloud, hair])

    sign, confident = proportions.detect_facing(cloud)
    assert confident is False, "手がかりの食い違いが検出されていない"
    assert sign == -1, "後頭部の手がかりを採れていない"
