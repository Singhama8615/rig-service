"""bpy を使わずに「リグ済みGLB」相当のバイト列を組み立てるテスト用ヘルパ。

VRM 変換(`server/vrm.py`)はノード階層しか見ないので、メッシュもBINチャンクも
不要な JSON だけの GLB で十分に検証できる。bpy を回さないぶんテストが速い。
"""
from __future__ import annotations

import json
import struct

CHUNK_JSON = 0x4E4F534A

# (ボーン名, 親, 親からのローカル平行移動, ローカル回転)
# +Y上 / +X が本人の左 / +Z が正面 という rig-service の出力規約に合わせてある。
#
# **回転まで実機に合わせてあることが重要**。Blender のボーンはローカル+Y が
# head→tail なので、肩と股関節がローカル系を入れ替え、腕・脚の軸の意味が体幹と
# 変わる。ここを単位回転・±X平行移動で近似すると、軸を取り違えたクリップ
# (腕を下ろしたつもりが前へ突き出す)をテストが素通りさせる。値は実際に
# リグ化した21ボーンGLBから採った。
_IDENTITY = (0.0, 0.0, 0.0, 1.0)
BoneSpec = tuple[str, "str | None", tuple[float, float, float], tuple[float, float, float, float]]
BONES: list[BoneSpec] = [
    ("Hips", None, (0.0, 0.515, 0.0), _IDENTITY),
    ("Spine", "Hips", (0.0, 0.127, 0.0), _IDENTITY),
    ("Chest", "Spine", (0.0, 0.127, 0.0), _IDENTITY),
    ("Neck", "Chest", (0.0, 0.109, 0.0), _IDENTITY),
    ("Head", "Neck", (0.0, 0.05, 0.0), _IDENTITY),
]
for _side, _sign in (("Left", 1.0), ("Right", -1.0)):
    # 肩: ローカルY=骨方向(±X)、ローカルX=前後(ワールドZ)、ローカルZ=上下(ワールドY)
    _shoulder = (-0.5, -0.5, -0.5, 0.5) if _side == "Left" else (-0.5, 0.5, 0.5, 0.5)
    BONES += [
        (f"{_side}Shoulder", "Chest", (_sign * 0.033, 0.076, 0.0), _shoulder),
        (f"{_side}UpperArm", f"{_side}Shoulder", (0.0, 0.189, 0.0), _IDENTITY),
        (f"{_side}LowerArm", f"{_side}UpperArm", (0.0, 0.236, 0.0), _IDENTITY),
        (f"{_side}Hand", f"{_side}LowerArm", (0.0, 0.203, 0.0), _IDENTITY),
        # 股関節は180°回って骨が下を向く(ローカル+Y が下)
        (f"{_side}UpperLeg", "Hips", (_sign * 0.155, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0)),
        (f"{_side}LowerLeg", f"{_side}UpperLeg", (0.0, 0.259, 0.0), _IDENTITY),
        (f"{_side}Foot", f"{_side}LowerLeg", (0.0, 0.165, 0.0), (-0.563, 0.0, 0.0, 0.826)),
        (f"{_side}Toes", f"{_side}Foot", (0.0, 0.161, 0.0), (0.0, -0.982, 0.186, 0.0)),
    ]


def rigged_gltf(bones=BONES) -> dict:
    """21ボーンのノード階層だけを持つ glTF ドキュメントを作る。"""
    index_of = {name: i for i, spec in enumerate(bones) for name in (spec[0],)}
    nodes: list[dict] = [
        {"name": name, "translation": list(translation), "rotation": list(rotation)}
        for name, _, translation, rotation in bones
    ]
    for name, parent, _, _ in bones:
        if parent is not None:
            nodes[index_of[parent]].setdefault("children", []).append(index_of[name])

    roots = [index_of[name] for name, parent, _, _ in bones if parent is None]
    return {
        "asset": {"version": "2.0", "generator": "rig-service test fixture"},
        "scene": 0,
        "scenes": [{"nodes": roots}],
        "nodes": nodes,
    }


def to_glb(gltf: dict) -> bytes:
    """glTF ドキュメントを JSON チャンクだけの GLB にする。"""
    payload = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    payload += b" " * (-len(payload) % 4)
    chunk = struct.pack("<II", len(payload), CHUNK_JSON) + payload
    return b"glTF" + struct.pack("<II", 2, 12 + len(chunk)) + chunk


def rigged_glb(bones=BONES) -> bytes:
    return to_glb(rigged_gltf(bones))
