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
