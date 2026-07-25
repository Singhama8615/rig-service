"""同梱モーションクリップ(計画書 §7 Phase R3-2)。

外部のモーションアセットは持ち込まず、**手続き的に自前生成**する。
rig-service のリグは常に同じ21ボーン・同じ名前なので、クリップは
「ボーン名 → 時刻ごとの回転」だけで表現でき、**どのモデルにも使い回せる**。

bpy は使わない。クリップの実体はキーフレームの数値であり、Blender の
機能を必要としないため(純Pythonなら1〜2秒の起動もサブプロセスも要らず、
`server/vrm.py` / `server/pose.py` と同じくテストが速い)。

用途は2つ:

1. ブラウザのプレビュー再生 — `GET /api/motions` でクリップを配り、
   three.js 側で補間する
2. glTF アニメーションとして**GLB/VRMに焼き込む** — Godot 等でそのまま
   再生できるようにする(`bake_into_gltf`)

回転はすべて**レスト姿勢からの相対**で、合成順は `server/pose.py` と同じ
three.js の "XYZ" 規約。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

from .pose import Quaternion, quat_from_euler_degrees, quat_multiply
from .vrm import BONE_TO_VRM

# 時刻(秒)とオイラー角(度)のキーフレーム
Keyframe = tuple[float, tuple[float, float, float]]

# glTF accessor の componentType / type
_FLOAT = 5126
_ARRAY_BUFFER_NONE = None  # アニメーション用データは target を持たない


@dataclass
class Motion:
    """1クリップ。`tracks` はボーン名 → キーフレーム列。"""

    name: str
    label: str
    duration: float
    loop: bool
    tracks: dict[str, list[Keyframe]] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "duration": self.duration,
            "loop": self.loop,
            "bones": sorted(self.tracks),
        }

    def to_dict(self) -> dict[str, Any]:
        """ブラウザ向け。曖昧さを避けるため回転はクォータニオンで渡す。"""
        return {
            **self.summary(),
            "tracks": {
                bone: [
                    {"time": time, "rotation": list(quat_from_euler_degrees(degrees))}
                    for time, degrees in keys
                ]
                for bone, keys in self.tracks.items()
            },
        }


# --- クリップ定義 -----------------------------------------------------------

# Tポーズ(レスト)のままでは待機姿勢として不自然なので、各クリップは
# 「腕を下ろした自然な立ち姿」を基準にして動きを付ける。
_BASE = {
    "LeftUpperArm": (0.0, 0.0, -68.0),
    "RightUpperArm": (0.0, 0.0, 68.0),
    "LeftLowerArm": (-10.0, 0.0, -12.0),
    "RightLowerArm": (-10.0, 0.0, 12.0),
}


def _hold(bone: str, duration: float) -> list[Keyframe]:
    """基準姿勢のまま動かないトラック。"""
    return [(0.0, _BASE[bone]), (duration, _BASE[bone])]


def _offset(bone: str, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0):
    base = _BASE[bone]
    return (base[0] + dx, base[1] + dy, base[2] + dz)


def _idle() -> Motion:
    """呼吸と微かな重心移動だけの待機。"""
    d = 4.0
    return Motion(
        name="idle",
        label="待機(呼吸)",
        duration=d,
        loop=True,
        tracks={
            "Spine": [(0.0, (0, 0, 0)), (2.0, (2.0, 0, 0)), (d, (0, 0, 0))],
            "Chest": [(0.0, (0, 0, 0)), (2.0, (1.5, 0, 0)), (d, (0, 0, 0))],
            "Head": [(0.0, (0, 0, 0)), (2.0, (-2.0, 0, 0)), (d, (0, 0, 0))],
            "LeftUpperArm": [
                (0.0, _BASE["LeftUpperArm"]),
                (2.0, _offset("LeftUpperArm", dz=-2.5)),
                (d, _BASE["LeftUpperArm"]),
            ],
            "RightUpperArm": [
                (0.0, _BASE["RightUpperArm"]),
                (2.0, _offset("RightUpperArm", dz=2.5)),
                (d, _BASE["RightUpperArm"]),
            ],
            "LeftLowerArm": _hold("LeftLowerArm", d),
            "RightLowerArm": _hold("RightLowerArm", d),
        },
    )


def _wave() -> Motion:
    """左手を挙げて振る。右腕は下ろしたまま。"""
    d = 2.4
    raised = (0.0, 0.0, 25.0)
    forearm_out = (0.0, -35.0, 20.0)
    forearm_in = (0.0, -35.0, -15.0)
    return Motion(
        name="wave",
        label="手を振る",
        duration=d,
        loop=True,
        tracks={
            "LeftUpperArm": [
                (0.0, _BASE["LeftUpperArm"]),
                (0.5, raised),
                (2.0, raised),
                (d, _BASE["LeftUpperArm"]),
            ],
            "LeftLowerArm": [
                (0.0, _BASE["LeftLowerArm"]),
                (0.5, forearm_out),
                (0.8, forearm_in),
                (1.1, forearm_out),
                (1.4, forearm_in),
                (1.7, forearm_out),
                (2.0, forearm_in),
                (d, _BASE["LeftLowerArm"]),
            ],
            "RightUpperArm": _hold("RightUpperArm", d),
            "RightLowerArm": _hold("RightLowerArm", d),
            "Head": [(0.0, (0, 0, 0)), (0.6, (0, 12.0, 0)), (2.0, (0, 12.0, 0)), (d, (0, 0, 0))],
        },
    )


def _bow() -> Motion:
    """お辞儀して戻る。"""
    d = 3.0
    return Motion(
        name="bow",
        label="お辞儀",
        duration=d,
        loop=False,
        tracks={
            "Spine": [(0.0, (0, 0, 0)), (0.8, (22.0, 0, 0)), (1.8, (22.0, 0, 0)), (d, (0, 0, 0))],
            "Chest": [(0.0, (0, 0, 0)), (0.8, (12.0, 0, 0)), (1.8, (12.0, 0, 0)), (d, (0, 0, 0))],
            "Head": [(0.0, (0, 0, 0)), (0.8, (12.0, 0, 0)), (1.8, (12.0, 0, 0)), (d, (0, 0, 0))],
            "LeftUpperArm": [
                (0.0, _BASE["LeftUpperArm"]),
                (0.8, _offset("LeftUpperArm", dx=-18.0)),
                (1.8, _offset("LeftUpperArm", dx=-18.0)),
                (d, _BASE["LeftUpperArm"]),
            ],
            "RightUpperArm": [
                (0.0, _BASE["RightUpperArm"]),
                (0.8, _offset("RightUpperArm", dx=-18.0)),
                (1.8, _offset("RightUpperArm", dx=-18.0)),
                (d, _BASE["RightUpperArm"]),
            ],
            "LeftLowerArm": _hold("LeftLowerArm", d),
            "RightLowerArm": _hold("RightLowerArm", d),
        },
    )


def _walk() -> Motion:
    """その場歩き。ヒップの上下移動(ルートモーション)は持たない。"""
    d = 1.0
    half = d / 2

    def leg(swing_first: bool) -> tuple[list[Keyframe], list[Keyframe]]:
        forward, backward = (28.0, -22.0) if swing_first else (-22.0, 28.0)
        upper = [(0.0, (forward, 0, 0)), (half, (backward, 0, 0)), (d, (forward, 0, 0))]
        # 膝は振り出しの中間で最も曲がる
        bend_a, bend_b = (-8.0, -42.0) if swing_first else (-42.0, -8.0)
        lower = [(0.0, (bend_a, 0, 0)), (half, (bend_b, 0, 0)), (d, (bend_a, 0, 0))]
        return upper, lower

    left_upper, left_lower = leg(True)
    right_upper, right_lower = leg(False)

    def arm(bone: str, swing_first: bool) -> list[Keyframe]:
        a, b = (-22.0, 22.0) if swing_first else (22.0, -22.0)
        return [
            (0.0, _offset(bone, dx=a)),
            (half, _offset(bone, dx=b)),
            (d, _offset(bone, dx=a)),
        ]

    return Motion(
        name="walk",
        label="その場歩き",
        duration=d,
        loop=True,
        tracks={
            "LeftUpperLeg": left_upper,
            "LeftLowerLeg": left_lower,
            "RightUpperLeg": right_upper,
            "RightLowerLeg": right_lower,
            # 腕は脚と逆位相に振る
            "LeftUpperArm": arm("LeftUpperArm", False),
            "RightUpperArm": arm("RightUpperArm", True),
            "LeftLowerArm": _hold("LeftLowerArm", d),
            "RightLowerArm": _hold("RightLowerArm", d),
            "Spine": [(0.0, (0, -3.0, 0)), (half, (0, 3.0, 0)), (d, (0, -3.0, 0))],
        },
    )


MOTIONS: dict[str, Motion] = {m.name: m for m in (_idle(), _wave(), _bow(), _walk())}


def get(name: str) -> Motion:
    if name not in MOTIONS:
        raise KeyError(name)
    return MOTIONS[name]


def validate_bones() -> list[str]:
    """クリップが実在しないボーンを参照していないか(起動時の自己点検用)。"""
    unknown = set()
    for motion in MOTIONS.values():
        unknown |= set(motion.tracks) - set(BONE_TO_VRM)
    return sorted(unknown)


# --- glTF アニメーションとしての焼き込み --------------------------------------


def bake_into_gltf(
    gltf: dict[str, Any], binary: bytes, motion: Motion
) -> tuple[dict[str, Any], bytes]:
    """クリップを glTF アニメーションとして追加する(メッシュには触らない)。

    glTF のアニメーションチャンネルはノードの**絶対ローカル回転**を上書きする。
    一方クリップはレスト姿勢からの相対回転なので、焼き込む際に
    `R_rest * R_clip` を計算して入れる(`server/pose.py` と同じ合成)。
    """
    index_of = {
        node.get("name"): i
        for i, node in enumerate(gltf.get("nodes", []))
        if node.get("name")
    }
    missing = [bone for bone in motion.tracks if bone not in index_of]
    if missing:
        raise KeyError(f"モデルに存在しないボーンです: {', '.join(sorted(missing))}")

    buffer_data = bytearray(binary)
    buffer_views = gltf.setdefault("bufferViews", [])
    accessors = gltf.setdefault("accessors", [])
    samplers: list[dict[str, Any]] = []
    channels: list[dict[str, Any]] = []

    def add_accessor(values: list[float], count: int, kind: str) -> int:
        # bufferView は4バイト境界から始める必要がある
        while len(buffer_data) % 4:
            buffer_data.append(0)
        offset = len(buffer_data)
        buffer_data.extend(struct.pack(f"<{len(values)}f", *values))
        buffer_views.append(
            {"buffer": 0, "byteOffset": offset, "byteLength": len(values) * 4}
        )
        accessor: dict[str, Any] = {
            "bufferView": len(buffer_views) - 1,
            "componentType": _FLOAT,
            "count": count,
            "type": kind,
        }
        if kind == "SCALAR":
            # 時刻アクセサは min/max が必須
            accessor["min"] = [min(values)]
            accessor["max"] = [max(values)]
        accessors.append(accessor)
        return len(accessors) - 1

    for bone, keys in motion.tracks.items():
        node_index = index_of[bone]
        rest: Quaternion = tuple(  # type: ignore[assignment]
            gltf["nodes"][node_index].get("rotation", (0.0, 0.0, 0.0, 1.0))
        )
        times = [float(time) for time, _ in keys]
        rotations: list[float] = []
        for _, degrees in keys:
            rotations.extend(quat_multiply(rest, quat_from_euler_degrees(degrees)))

        samplers.append(
            {
                "input": add_accessor(times, len(times), "SCALAR"),
                "output": add_accessor(rotations, len(keys), "VEC4"),
                # クォータニオンの LINEAR 補間は仕様上 slerp になる
                "interpolation": "LINEAR",
            }
        )
        channels.append(
            {
                "sampler": len(samplers) - 1,
                "target": {"node": node_index, "path": "rotation"},
            }
        )

    gltf.setdefault("animations", []).append(
        {"name": motion.name, "samplers": samplers, "channels": channels}
    )
    # バッファ全体の長さを実データに合わせる
    buffers = gltf.setdefault("buffers", [{}])
    buffers[0]["byteLength"] = len(buffer_data)
    buffers[0].pop("uri", None)
    return gltf, bytes(buffer_data)
