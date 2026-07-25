"""bpy を使わずに「リグ済みGLB」相当のバイト列を組み立てるテスト用ヘルパ。

VRM 変換(`server/vrm.py`)はノード階層しか見ないので、メッシュもBINチャンクも
不要な JSON だけの GLB で十分に検証できる。bpy を回さないぶんテストが速い。
"""
from __future__ import annotations

import json
import struct

CHUNK_JSON = 0x4E4F534A

# (ボーン名, 親, 親からのローカル平行移動)
# +Y上 / +X が本人の左 / +Z が正面 という rig-service の出力規約に合わせてある。
BONES: list[tuple[str, str | None, tuple[float, float, float]]] = [
    ("Hips", None, (0.0, 0.53, 0.0)),
    ("Spine", "Hips", (0.0, 0.10, 0.0)),
    ("Chest", "Spine", (0.0, 0.10, 0.0)),
    ("Neck", "Chest", (0.0, 0.10, 0.0)),
    ("Head", "Neck", (0.0, 0.05, 0.0)),
]
for _side, _sign in (("Left", 1.0), ("Right", -1.0)):
    BONES += [
        (f"{_side}Shoulder", "Chest", (_sign * 0.02, 0.05, 0.0)),
        (f"{_side}UpperArm", f"{_side}Shoulder", (_sign * 0.10, 0.0, 0.0)),
        (f"{_side}LowerArm", f"{_side}UpperArm", (_sign * 0.25, 0.0, 0.0)),
        (f"{_side}Hand", f"{_side}LowerArm", (_sign * 0.20, 0.0, 0.0)),
        (f"{_side}UpperLeg", "Hips", (_sign * 0.10, 0.0, 0.0)),
        (f"{_side}LowerLeg", f"{_side}UpperLeg", (0.0, -0.25, 0.0)),
        (f"{_side}Foot", f"{_side}LowerLeg", (0.0, -0.25, 0.0)),
        (f"{_side}Toes", f"{_side}Foot", (0.0, -0.05, 0.15)),
    ]


def rigged_gltf(bones=BONES) -> dict:
    """21ボーンのノード階層だけを持つ glTF ドキュメントを作る。"""
    index_of = {name: i for i, (name, _, _) in enumerate(bones)}
    nodes: list[dict] = [
        {"name": name, "translation": list(translation)}
        for name, _, translation in bones
    ]
    for name, parent, _ in bones:
        if parent is not None:
            nodes[index_of[parent]].setdefault("children", []).append(index_of[name])

    roots = [index_of[name] for name, parent, _ in bones if parent is None]
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
