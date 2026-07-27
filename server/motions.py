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

from .pose import Quaternion, bone_euler, quat_from_euler_degrees, quat_multiply
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
#
# 角度は必ず `pose.bone_euler` の**解剖学的な指定**(down / forward / twist)で
# 書くこと。生の (x, y, z) を書くと腕と体幹で軸の意味が違うため必ず取り違える
# (腕を下ろしたつもりが前へ突き出す)。
_BASE_INTENT: dict[str, dict[str, float]] = {
    "LeftUpperArm": {"down": 68.0},
    "RightUpperArm": {"down": 68.0},
    "LeftLowerArm": {"down": 12.0, "forward": 10.0},
    "RightLowerArm": {"down": 12.0, "forward": 10.0},
}
_BASE = {bone: bone_euler(bone, **intent) for bone, intent in _BASE_INTENT.items()}


def _hold(bone: str, duration: float) -> list[Keyframe]:
    """基準姿勢のまま動かないトラック。"""
    return [(0.0, _BASE[bone]), (duration, _BASE[bone])]


def _offset(bone: str, **delta: float):
    """基準姿勢に解剖学的なオフセットを足した角度。"""
    intent = dict(_BASE_INTENT[bone])
    for key, value in delta.items():
        intent[key] = intent.get(key, 0.0) + value
    return bone_euler(bone, **intent)


def _idle() -> Motion:
    """呼吸と微かな重心移動だけの待機。"""
    d = 4.0
    return Motion(
        name="idle",
        label="待機(呼吸)",
        duration=d,
        loop=True,
        tracks={
            "Spine": [(0.0, (0, 0, 0)), (2.0, bone_euler("Spine", forward=2.0)), (d, (0, 0, 0))],
            "Chest": [(0.0, (0, 0, 0)), (2.0, bone_euler("Chest", forward=1.5)), (d, (0, 0, 0))],
            "Head": [(0.0, (0, 0, 0)), (2.0, bone_euler("Head", forward=-2.0)), (d, (0, 0, 0))],
            # 呼吸に合わせて腕をわずかに開閉する
            "LeftUpperArm": [
                (0.0, _BASE["LeftUpperArm"]),
                (2.0, _offset("LeftUpperArm", down=-2.5)),
                (d, _BASE["LeftUpperArm"]),
            ],
            "RightUpperArm": [
                (0.0, _BASE["RightUpperArm"]),
                (2.0, _offset("RightUpperArm", down=-2.5)),
                (d, _BASE["RightUpperArm"]),
            ],
            "LeftLowerArm": _hold("LeftLowerArm", d),
            "RightLowerArm": _hold("RightLowerArm", d),
        },
    )


def _wave() -> Motion:
    """左手を挙げて振る。右腕は下ろしたまま。"""
    d = 2.4
    # 腕はほぼ真横〜やや上、前腕を前に出して左右に振る
    raised = bone_euler("LeftUpperArm", down=-25.0)
    forearm_out = bone_euler("LeftLowerArm", forward=35.0, twist=-20.0)
    forearm_in = bone_euler("LeftLowerArm", forward=35.0, twist=15.0)
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
            "Head": [
                (0.0, (0, 0, 0)),
                (0.6, bone_euler("Head", twist=12.0)),
                (2.0, bone_euler("Head", twist=12.0)),
                (d, (0, 0, 0)),
            ],
        },
    )


def _bend(bone: str, degrees: float, duration: float) -> list[Keyframe]:
    """前傾して戻るトラック(お辞儀用)。"""
    forward = bone_euler(bone, forward=degrees)
    return [(0.0, (0, 0, 0)), (0.8, forward), (1.8, forward), (duration, (0, 0, 0))]


def _bow() -> Motion:
    """お辞儀して戻る。"""
    d = 3.0
    return Motion(
        name="bow",
        label="お辞儀",
        duration=d,
        loop=False,
        tracks={
            "Spine": _bend("Spine", 22.0, d),
            "Chest": _bend("Chest", 12.0, d),
            "Head": _bend("Head", 12.0, d),
            # 上体を倒すぶん、腕は体から離れないよう後ろへ送る
            "LeftUpperArm": [
                (0.0, _BASE["LeftUpperArm"]),
                (0.8, _offset("LeftUpperArm", forward=-18.0)),
                (1.8, _offset("LeftUpperArm", forward=-18.0)),
                (d, _BASE["LeftUpperArm"]),
            ],
            "RightUpperArm": [
                (0.0, _BASE["RightUpperArm"]),
                (0.8, _offset("RightUpperArm", forward=-18.0)),
                (1.8, _offset("RightUpperArm", forward=-18.0)),
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

    def leg(side: str, swing_first: bool) -> tuple[list[Keyframe], list[Keyframe]]:
        a, b = (28.0, -22.0) if swing_first else (-22.0, 28.0)
        upper = [
            (0.0, bone_euler(f"{side}UpperLeg", forward=a)),
            (half, bone_euler(f"{side}UpperLeg", forward=b)),
            (d, bone_euler(f"{side}UpperLeg", forward=a)),
        ]
        # 膝は振り出しの中間で最も曲がる。曲げ = 足首を後ろへ送る = forward が負。
        bend_a, bend_b = (-8.0, -42.0) if swing_first else (-42.0, -8.0)
        lower = [
            (0.0, bone_euler(f"{side}LowerLeg", forward=bend_a)),
            (half, bone_euler(f"{side}LowerLeg", forward=bend_b)),
            (d, bone_euler(f"{side}LowerLeg", forward=bend_a)),
        ]
        return upper, lower

    left_upper, left_lower = leg("Left", True)
    right_upper, right_lower = leg("Right", False)

    def arm(bone: str, leg_swings_first: bool) -> list[Keyframe]:
        """同じ側の脚と**逆位相**に振る(引数は脚側の位相を渡す)。"""
        a, b = (-22.0, 22.0) if leg_swings_first else (22.0, -22.0)
        return [
            (0.0, _offset(bone, forward=a)),
            (half, _offset(bone, forward=b)),
            (d, _offset(bone, forward=a)),
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
            # 腕は同じ側の脚と逆位相(脚の位相をそのまま渡す)
            "LeftUpperArm": arm("LeftUpperArm", True),
            "RightUpperArm": arm("RightUpperArm", False),
            "LeftLowerArm": _hold("LeftLowerArm", d),
            "RightLowerArm": _hold("RightLowerArm", d),
            # 歩調に合わせて上体をわずかにひねる
            "Spine": [
                (0.0, bone_euler("Spine", twist=-3.0)),
                (half, bone_euler("Spine", twist=3.0)),
                (d, bone_euler("Spine", twist=-3.0)),
            ],
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
