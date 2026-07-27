"""リグ済みGLB / VRM にポーズを適用する(計画書 §7 Phase R3-3)。

ポーズは**各ボーンのレスト姿勢からの相対回転**として与える。glTF のノードは
ローカル TRS を持つので、適用は `R_new = R_rest * R_pose`(ボーンのローカル系で
レストの後ろに合成)になる。これは Blender のポーズボーンや VRM humanoid の
ポーズ表現と同じ意味づけで、`tests/pose_preview.py` の描画とも一致する。

`server/vrm.py` と同じく **bpy 非依存の純Python**。ブラウザ側のプレビューは
three.js でクライアント内で同じ回転を当てるため、サーバのこのAPIは
「ポーズ済みGLBを書き出す」用途に使う(数十MBを毎フレーム往復させない)。

VRMA / BVH の読み込みは未実装(計画書 §7 R3-3 の残り)。
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable

from . import vrm

Quaternion = tuple[float, float, float, float]  # glTF と同じ (x, y, z, w)

IDENTITY: Quaternion = (0.0, 0.0, 0.0, 1.0)

# VRM の lowerCamelCase 名でも指定できるようにする逆引き
_VRM_TO_BONE = {v: k for k, v in vrm.BONE_TO_VRM.items()}


class PoseError(ValueError):
    """ポーズ指定が不正、または適用先のボーンが無い。"""


def quat_multiply(a: Quaternion, b: Quaternion) -> Quaternion:
    """クォータニオンの積 a*b(a を適用したあと b を**ローカル系で**適用)。"""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def quat_from_euler_degrees(xyz: Iterable[float]) -> Quaternion:
    """オイラー角(度)をクォータニオンにする。**three.js の "XYZ" と同じ規約**。

    ここは取り違えやすい。同じ「XYZ順」という名前でも合成順が違う:

    - three.js `new THREE.Euler(x, y, z, "XYZ")` → 行列は **Rx·Ry·Rz**(qx*qy*qz)
    - Blender `mathutils.Euler((x, y, z), "XYZ")` → 行列は **Rz·Ry·Rx**(qz*qy*qx)

    ブラウザのプレビュー(three.js)とサーバのポーズ適用が食い違うと、
    2軸以上を同時に回したボーンだけ静かにズレる(実測: 前腕の位置が3mm)。
    glTF / three.js がこのプロジェクトの基準なので **three.js 側に揃える**。
    Blender を使う `tests/pose_preview.py` も同じ値になるよう
    クォータニオンで指定している。
    """
    x, y, z = (math.radians(v) / 2.0 for v in xyz)
    qx = (math.sin(x), 0.0, 0.0, math.cos(x))
    qy = (0.0, math.sin(y), 0.0, math.cos(y))
    qz = (0.0, 0.0, math.sin(z), math.cos(z))
    return quat_multiply(quat_multiply(qx, qy), qz)


# --- 解剖学的な向き -> ボーンローカルのオイラー角 -----------------------------
#
# ボーンのローカル系は「+Y が head→tail」で、残り2軸は Blender のロール規約で
# 決まる(`bpy_scripts/autorig.py` は head/tail だけを与え、ロールは既定)。
# そのため**ボーンの向きによってローカル軸の意味が変わる**:
#
#   - 体幹・頭・脚(垂直に伸びる) … ローカル軸はワールド軸とほぼ一致し、
#     ローカルX = 左右軸 = 前後に振る軸
#   - 腕(Tポーズで水平に ±X へ伸びる) … 肩ボーンが系を入れ替えるため
#     **ローカルX = ワールドZ(前後)、ローカルY = ワールドX(骨方向=ねじり)、
#     ローカルZ = ワールドY(上下)**
#
# つまり腕だけ「上下に振る軸」と「前後に振る軸」が体幹と入れ替わっている。
# ここを取り違えると *腕を下ろしたつもりが前へ突き出す*(実測・報告済み)。
# クリップやプリセットが生の (x, y, z) を書くと必ず間違えるので、
# 意味づけからオイラー角を作るのはこの関数だけにする。
#
# 符号はリグ済み実機(21ボーン)で測って決めた。落とし穴が2つある:
#
#   1. 腕の「下ろす」は**左右とも同符号**で、「前に出す」だけが左右で反転する
#      (骨方向が ±X と逆向きなため)
#   2. **上に伸びる骨(体幹)と下に伸びる骨(脚)は、同じローカルX回転でも
#      先端が逆向きに動く**。実測: Spine X+22 は前傾、UpperLeg X+28 は後ろ振り。

_ARM_BONES = frozenset(
    {"LeftUpperArm", "RightUpperArm", "LeftLowerArm", "RightLowerArm", "LeftHand", "RightHand"}
)
_DOWNWARD_BONES = frozenset(
    {
        "LeftUpperLeg", "RightUpperLeg",
        "LeftLowerLeg", "RightLowerLeg",
        "LeftFoot", "RightFoot",
        "LeftToes", "RightToes",
    }
)


def bone_euler(
    bone: str, *, down: float = 0.0, forward: float = 0.0, twist: float = 0.0
) -> tuple[float, float, float]:
    """解剖学的な指定をそのボーンのローカルオイラー角(度, XYZ)に変換する。

    Args:
        bone: リグのボーン名。
        down: 正で腕を**下ろす**(腕のボーンのみ。体幹を前に倒すのは `forward`)。
        forward: 正で先端を**前(+Z, キャラの正面)へ**振る。膝を曲げる(足首を
            後ろへ送る)は負の `forward`。
        twist: 骨の軸まわりのねじり。腕では正で**手のひらが内(体の側)を向く**
            (レストのTポーズでは手のひらが正面を向いているため、90 でほぼ真横=
            自然に腕を下ろした向きになる)。体幹・頭では正で左を向く。

    Returns:
        `quat_from_euler_degrees` に渡せる (x, y, z)。
    """
    if bone not in _VRM_TO_BONE.values() and bone not in vrm.BONE_TO_VRM:
        raise PoseError(f"未知のボーンです: {bone}")
    if bone in _ARM_BONES:
        # 腕: ローカルX=上下, ローカルY=ねじり, ローカルZ=前後。
        # 骨方向が左右で逆(±X)なので、**前後もねじりも左右で符号が反転する**
        # (下ろすだけが同符号)。反転を忘れると片手だけ手のひらが外を向く。
        side = -1.0 if bone.startswith("Left") else 1.0
        # **ねじりは振りのあとに掛ける**。XYZ順(Rx·Ry·Rz)のまま Y にねじりを
        # 入れると、続く Z(前後)の軸までねじられて前後の振りが上下に化ける
        # (実測: 腕の前後移動が 22cm から 3cm に落ちた)。振ってから骨軸まわりに
        # ねじる合成を作り、保存できる XYZ 角へ戻す。
        rotation = quat_multiply(
            quat_multiply(
                quat_from_euler_degrees((-down, 0.0, 0.0)),
                quat_from_euler_degrees((0.0, 0.0, side * forward)),
            ),
            quat_from_euler_degrees((0.0, -side * twist, 0.0)),
        )
        return quat_to_euler_degrees(rotation)
    if down:
        raise PoseError(f"{bone} に down は使えません(腕のボーンのみ)。forward を使ってください。")
    # 体幹・脚・頭: ローカルX=前後。下向きの骨は先端の動く向きが反転する。
    sign = -1.0 if bone in _DOWNWARD_BONES else 1.0
    return (sign * forward, twist, 0.0)


def bone_quat(bone: str, **kwargs: float) -> Quaternion:
    """`bone_euler` の結果をクォータニオンで返す。"""
    return quat_from_euler_degrees(bone_euler(bone, **kwargs))


# ブラウザのプリセット。**ここを唯一の定義元**にし、`GET /api/poses` で配る
# (viewer.js に生の角度を複製すると、軸の意味を取り違えたまま片方だけ直る)。
_POSE_INTENTS: dict[str, tuple[str, dict[str, dict[str, float]]]] = {
    "tpose": ("Tポーズ(レスト)", {}),
    "arms_down": (
        "腕を下ろす",
        {
            # twist=90: レストでは手のひらが正面を向いているので、下ろすだけだと
            # 手のひらが前を向いたままになる。内向きにして自然な立ち姿にする。
            "LeftUpperArm": {"down": 70.0, "twist": 90.0},
            "RightUpperArm": {"down": 70.0, "twist": 90.0},
            "LeftLowerArm": {"down": 25.0},
            "RightLowerArm": {"down": 25.0},
        },
    ),
    "relaxed": (
        "自然な立ち姿",
        {
            "LeftUpperArm": {"down": 65.0, "twist": 90.0},
            "RightUpperArm": {"down": 65.0, "twist": 90.0},
            "LeftLowerArm": {"down": 20.0, "forward": 20.0},
            "RightLowerArm": {"down": 20.0, "forward": 20.0},
            "LeftUpperLeg": {"forward": 5.0},
            "RightUpperLeg": {"forward": 5.0},
            "LeftLowerLeg": {"forward": -8.0},
            "RightLowerLeg": {"forward": -8.0},
            "Head": {"forward": -5.0},
            "Spine": {"forward": 5.0},
        },
    ),
    "wave": (
        "手を振る",
        {
            # 挙げた手は手のひらを前に向けたままにする(ねじり無し)
            "LeftUpperArm": {"down": -25.0},
            "LeftLowerArm": {"forward": 40.0, "twist": -20.0},
            "RightUpperArm": {"down": 70.0, "twist": 90.0},
            "RightLowerArm": {"down": 25.0},
            "Head": {"twist": 15.0},
        },
    ),
}


def presets() -> dict[str, dict[str, Any]]:
    """ポーズプリセットを {name: {label, pose}} で返す(pose はオイラー角・度)。"""
    return {
        name: {
            "label": label,
            "pose": {bone: list(bone_euler(bone, **intent)) for bone, intent in bones.items()},
        }
        for name, (label, bones) in _POSE_INTENTS.items()
    }


def quat_to_euler_degrees(q: Quaternion) -> tuple[float, float, float]:
    """クォータニオンを three.js "XYZ" 規約のオイラー角(度)に戻す。

    `quat_from_euler_degrees` の逆。クリップやAPIはオイラー角で角度を持つので、
    「振ってからねじる」のように XYZ 順では表せない合成を作ったあと、
    保存できる形に直すのに使う。
    """
    x, y, z, w = q
    # 回転行列の必要な成分だけを組む(R = Rx·Ry·Rz)
    r00 = 1 - 2 * (y * y + z * z)
    r01 = 2 * (x * y - z * w)
    r02 = 2 * (x * z + y * w)
    r12 = 2 * (y * z - x * w)
    r22 = 1 - 2 * (x * x + y * y)
    r11 = 1 - 2 * (x * x + z * z)
    r21 = 2 * (y * z + x * w)

    sy = max(-1.0, min(1.0, r02))
    ey = math.asin(sy)
    if abs(sy) < 0.9999999:
        ex = math.atan2(-r12, r22)
        ez = math.atan2(-r01, r00)
    else:
        # ジンバルロック: X と Z が同じ軸に潰れるので Z を 0 に寄せる
        ex = math.atan2(r21, r11)
        ez = 0.0
    return tuple(math.degrees(v) for v in (ex, ey, ez))  # type: ignore[return-value]


def normalize(q: Quaternion) -> Quaternion:
    length = math.sqrt(sum(v * v for v in q))
    if length == 0.0:
        raise PoseError("クォータニオンの長さが 0 です。")
    return tuple(v / length for v in q)  # type: ignore[return-value]


def parse_pose(pose: dict[str, Any]) -> dict[str, Quaternion]:
    """API入力のポーズ辞書を {ボーン名: クォータニオン} に正規化する。

    値は次のどちらでもよい:

    - 長さ4の配列 = クォータニオン `(x, y, z, w)`(glTFと同じ順序)
    - 長さ3の配列 = オイラー角(度, XYZ順)

    キーは rig-service のボーン名(`LeftUpperArm` 等)でも
    VRM の名前(`leftUpperArm` 等)でもよい。
    """
    if not isinstance(pose, dict):
        raise PoseError("pose はボーン名をキーにしたオブジェクトである必要があります。")

    parsed: dict[str, Quaternion] = {}
    for raw_name, value in pose.items():
        name = _VRM_TO_BONE.get(raw_name, raw_name)
        if name not in vrm.BONE_TO_VRM:
            raise PoseError(
                f"未知のボーンです: {raw_name}(指定可能: {sorted(vrm.BONE_TO_VRM)})"
            )
        if not isinstance(value, (list, tuple)) or len(value) not in (3, 4):
            raise PoseError(
                f"{raw_name} の値は長さ4のクォータニオンか長さ3のオイラー角(度)にしてください。"
            )
        if not all(isinstance(v, (int, float)) for v in value):
            raise PoseError(f"{raw_name} の値に数値でない要素があります。")
        parsed[name] = (
            normalize(tuple(float(v) for v in value))  # type: ignore[arg-type]
            if len(value) == 4
            else quat_from_euler_degrees(value)
        )
    return parsed


def apply_pose(gltf: dict[str, Any], pose: dict[str, Quaternion]) -> list[str]:
    """glTF のボーンノードにポーズを合成する(その場で書き換える)。

    Returns:
        適用できたボーン名の一覧。
    """
    index_of: dict[str, int] = {}
    for index, node in enumerate(gltf.get("nodes", [])):
        name = node.get("name")
        if name and name not in index_of:
            index_of[name] = index

    missing = [name for name in pose if name not in index_of]
    if missing:
        raise PoseError(f"モデルに存在しないボーンです: {', '.join(sorted(missing))}")

    applied: list[str] = []
    for name, rotation in pose.items():
        node = gltf["nodes"][index_of[name]]
        rest: Quaternion = tuple(node.get("rotation", IDENTITY))  # type: ignore[assignment]
        node["rotation"] = list(quat_multiply(rest, rotation))
        applied.append(name)
    return sorted(applied)


def pose_glb(src: Path, dst: Path, pose: dict[str, Any]) -> dict[str, Any]:
    """GLB/VRM にポーズを適用して書き出す。"""
    parsed = parse_pose(pose)
    gltf, binary = vrm.read_glb(src)
    applied = apply_pose(gltf, parsed)
    vrm.write_glb(dst, gltf, binary)
    return {
        "applied_bones": applied,
        "bone_count": len(applied),
        "size_bytes": dst.stat().st_size,
    }
