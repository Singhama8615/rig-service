"""リグ済みGLBを VRM 1.0 に変換する(計画書 §7 Phase R2)。

VRM 1.0 は **glTF 2.0 + `VRMC_vrm` 拡張**にすぎない。R1-4 で確認した通り
rig-service の出力GLBは既に VRM humanoid の要求(Y-up・原点=足元・メートル系・
キャラクターは +Z を向く・標準ボーン名)を満たしているので、
GLBのJSONチャンクに `meta` と `humanoid.humanBones` を足すだけで VRM になる。

そのため Blender アドオン(VRM Add-on for Blender)は使わず自前で書いている:

- bpy に依存しないので **単体テストが速く、リグ処理と分離できる**
- 第三者コードをサービスのプロセス内で実行しない
- 計画書 §8 のリスク「VRM Add-onのヘッドレス動作不成立」が消える

代わりに MToon マテリアル・表情(expressions)・スプリングボーンは扱わない
(いずれも現状スコープ外。必要になれば `VRMC_springBone` 等を同じ方式で足せる)。

仕様: https://github.com/vrm-c/vrm-specification/tree/master/specification/VRMC_vrm-1.0
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GLB_MAGIC = b"glTF"
GLB_VERSION = 2
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942

VRM_EXTENSION = "VRMC_vrm"
VRM_SPEC_VERSION = "1.0"
DEFAULT_LICENSE_URL = "https://vrm.dev/licenses/1.0/"

# rig-service のボーン名(Godot `SkeletonProfileHumanoid` 準拠)→ VRM humanoid ボーン名。
# VRM 側は lowerCamelCase で名前が決まっている。
BONE_TO_VRM = {
    "Hips": "hips",
    "Spine": "spine",
    "Chest": "chest",
    "Neck": "neck",
    "Head": "head",
    "LeftShoulder": "leftShoulder",
    "LeftUpperArm": "leftUpperArm",
    "LeftLowerArm": "leftLowerArm",
    "LeftHand": "leftHand",
    "LeftUpperLeg": "leftUpperLeg",
    "LeftLowerLeg": "leftLowerLeg",
    "LeftFoot": "leftFoot",
    "LeftToes": "leftToes",
    "RightShoulder": "rightShoulder",
    "RightUpperArm": "rightUpperArm",
    "RightLowerArm": "rightLowerArm",
    "RightHand": "rightHand",
    "RightUpperLeg": "rightUpperLeg",
    "RightLowerLeg": "rightLowerLeg",
    "RightFoot": "rightFoot",
    "RightToes": "rightToes",
}

# VRM 1.0 が必須とする humanoid ボーン(これが揃わないとVRMとして成立しない)
REQUIRED_VRM_BONES = (
    "hips",
    "spine",
    "head",
    "leftUpperArm",
    "leftLowerArm",
    "leftHand",
    "rightUpperArm",
    "rightLowerArm",
    "rightHand",
    "leftUpperLeg",
    "leftLowerLeg",
    "leftFoot",
    "rightUpperLeg",
    "rightLowerLeg",
    "rightFoot",
)

_AVATAR_PERMISSIONS = {"onlyAuthor", "onlySeparatelyLicensedPerson", "everyone"}
_COMMERCIAL_USAGE = {"personalNonProfit", "personalProfit", "corporation"}
_CREDIT_NOTATION = {"required", "unnecessary"}
_MODIFICATION = {"prohibited", "allowModification", "allowModificationRedistribution"}


class VrmError(RuntimeError):
    """VRM への変換を継続できない入力・状態。"""


@dataclass
class VrmMeta:
    """`VRMC_vrm.meta`(計画書 §7 R2-2 の「メタ情報パラメータ」)。

    既定値は「作者だけがアバターとして使用可・非営利・改変再配布不可」という
    最も保守的な設定にしてある。生成物の権利者が誰かはサービス側では判断
    できないため、緩める場合は呼び出し側が明示的に指定する。
    """

    name: str = "Untitled"
    version: str = ""
    authors: list[str] = field(default_factory=lambda: ["Unknown"])
    copyright_information: str = ""
    contact_information: str = ""
    references: list[str] = field(default_factory=list)
    third_party_licenses: str = ""
    license_url: str = DEFAULT_LICENSE_URL
    avatar_permission: str = "onlyAuthor"
    allow_excessively_violent_usage: bool = False
    allow_excessively_sexual_usage: bool = False
    commercial_usage: str = "personalNonProfit"
    allow_political_or_religious_usage: bool = False
    allow_antisocial_or_hate_usage: bool = False
    credit_notation: str = "required"
    allow_redistribution: bool = False
    modification: str = "prohibited"
    other_license_url: str = ""

    def validate(self) -> None:
        if not self.name.strip():
            raise VrmError("VRMメタの name は必須です。")
        if not self.authors or not any(a.strip() for a in self.authors):
            raise VrmError("VRMメタの authors は1件以上必要です。")
        for value, allowed, label in (
            (self.avatar_permission, _AVATAR_PERMISSIONS, "avatar_permission"),
            (self.commercial_usage, _COMMERCIAL_USAGE, "commercial_usage"),
            (self.credit_notation, _CREDIT_NOTATION, "credit_notation"),
            (self.modification, _MODIFICATION, "modification"),
        ):
            if value not in allowed:
                raise VrmError(f"{label} は {sorted(allowed)} のいずれかである必要があります。")

    def to_gltf(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "authors": [a for a in self.authors if a.strip()],
            "copyrightInformation": self.copyright_information,
            "contactInformation": self.contact_information,
            "references": self.references,
            "thirdPartyLicenses": self.third_party_licenses,
            "licenseUrl": self.license_url,
            "avatarPermission": self.avatar_permission,
            "allowExcessivelyViolentUsage": self.allow_excessively_violent_usage,
            "allowExcessivelySexualUsage": self.allow_excessively_sexual_usage,
            "commercialUsage": self.commercial_usage,
            "allowPoliticalOrReligiousUsage": self.allow_political_or_religious_usage,
            "allowAntisocialOrHateUsage": self.allow_antisocial_or_hate_usage,
            "creditNotation": self.credit_notation,
            "allowRedistribution": self.allow_redistribution,
            "modification": self.modification,
            "otherLicenseUrl": self.other_license_url,
        }


# --- GLB の読み書き ---------------------------------------------------------


def read_glb(path: Path) -> tuple[dict[str, Any], bytes]:
    """GLB を (glTF JSON, BINチャンク) に分解する。"""
    data = path.read_bytes()
    if len(data) < 12 or data[:4] != GLB_MAGIC:
        raise VrmError(f"GLB ではありません: {path}")

    gltf: dict[str, Any] | None = None
    binary = b""
    offset = 12
    while offset + 8 <= len(data):
        length, chunk_type = struct.unpack_from("<II", data, offset)
        payload = data[offset + 8 : offset + 8 + length]
        if chunk_type == CHUNK_JSON:
            gltf = json.loads(payload.decode("utf-8"))
        elif chunk_type == CHUNK_BIN:
            binary = payload
        offset += 8 + length

    if gltf is None:
        raise VrmError(f"GLB に JSON チャンクがありません: {path}")
    return gltf, binary


def write_glb(path: Path, gltf: dict[str, Any], binary: bytes) -> None:
    """glTF JSON と BINチャンクから GLB を書き出す。

    チャンクは4バイト境界に揃える必要がある(JSONは空白、BINはゼロで埋める)。
    """
    json_bytes = json.dumps(gltf, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    json_bytes += b" " * (-len(json_bytes) % 4)
    chunks = [struct.pack("<II", len(json_bytes), CHUNK_JSON) + json_bytes]
    if binary:
        padded = binary + b"\x00" * (-len(binary) % 4)
        chunks.append(struct.pack("<II", len(padded), CHUNK_BIN) + padded)

    body = b"".join(chunks)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(GLB_MAGIC + struct.pack("<II", GLB_VERSION, 12 + len(body)) + body)


# --- ノード階層のユーティリティ ----------------------------------------------


def _node_children(gltf: dict[str, Any]) -> dict[int, list[int]]:
    return {i: list(n.get("children", [])) for i, n in enumerate(gltf.get("nodes", []))}


def _node_parents(gltf: dict[str, Any]) -> dict[int, int]:
    parents: dict[int, int] = {}
    for i, node in enumerate(gltf.get("nodes", [])):
        for child in node.get("children", []):
            parents[child] = i
    return parents


def _is_descendant_of(node: int, ancestor: int, parents: dict[int, int]) -> bool:
    current = parents.get(node)
    while current is not None:
        if current == ancestor:
            return True
        current = parents.get(current)
    return False


Matrix = list[list[float]]

_IDENTITY: Matrix = [
    [1.0, 0.0, 0.0, 0.0],
    [0.0, 1.0, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.0],
    [0.0, 0.0, 0.0, 1.0],
]


def _local_matrix(node: dict[str, Any]) -> Matrix:
    """glTFノードのローカル変換行列を作る。

    **回転を無視して平行移動だけ累積してはいけない**。Blender の glTF
    エクスポータはボーンをローカルY軸方向に向けて出すため、骨格ノードには
    必ず回転が入っており、無視すると骨の前後関係が逆に出る。
    """
    if "matrix" in node:
        # glTF の matrix は列優先の16要素
        m = node["matrix"]
        return [[m[column * 4 + row] for column in range(4)] for row in range(4)]

    tx, ty, tz = node.get("translation", [0.0, 0.0, 0.0])
    x, y, z, w = node.get("rotation", [0.0, 0.0, 0.0, 1.0])
    sx, sy, sz = node.get("scale", [1.0, 1.0, 1.0])
    rotation = (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )
    scale = (sx, sy, sz)
    translation = (tx, ty, tz)
    return [
        [rotation[row][col] * scale[col] for col in range(3)] + [translation[row]]
        for row in range(3)
    ] + [[0.0, 0.0, 0.0, 1.0]]


def _matmul(a: Matrix, b: Matrix) -> Matrix:
    return [
        [sum(a[row][k] * b[k][col] for k in range(4)) for col in range(4)]
        for row in range(4)
    ]


def world_translations(gltf: dict[str, Any]) -> dict[int, tuple[float, float, float]]:
    """各ノードのワールド位置(TRSを親から累積した結果)を求める。"""
    nodes = gltf.get("nodes", [])
    parents = _node_parents(gltf)
    cache: dict[int, Matrix] = {}

    def resolve(index: int) -> Matrix:
        if index not in cache:
            parent = parents.get(index)
            base = resolve(parent) if parent is not None else _IDENTITY
            cache[index] = _matmul(base, _local_matrix(nodes[index]))
        return cache[index]

    return {
        i: (resolve(i)[0][3], resolve(i)[1][3], resolve(i)[2][3])
        for i in range(len(nodes))
    }


# --- VRM 拡張の構築 ---------------------------------------------------------


def build_human_bones(gltf: dict[str, Any]) -> dict[str, dict[str, int]]:
    """glTFのノード名から VRM humanoid の humanBones を組み立てる。

    ボーン名は R1-2 で `SkeletonProfileHumanoid` に合わせてあるので、
    名前の対応表を引くだけで済む。
    """
    name_to_index: dict[str, int] = {}
    for index, node in enumerate(gltf.get("nodes", [])):
        name = node.get("name")
        # 同名ノードがあれば最初のものを使う(骨格ノードは一意な想定)
        if name and name not in name_to_index:
            name_to_index[name] = index

    human_bones: dict[str, dict[str, int]] = {}
    for bone_name, vrm_name in BONE_TO_VRM.items():
        index = name_to_index.get(bone_name)
        if index is not None:
            human_bones[vrm_name] = {"node": index}
    return human_bones


def build_extension(gltf: dict[str, Any], meta: VrmMeta) -> dict[str, Any]:
    meta.validate()
    human_bones = build_human_bones(gltf)
    missing = [b for b in REQUIRED_VRM_BONES if b not in human_bones]
    if missing:
        raise VrmError(
            "VRM humanoid の必須ボーンが見つかりません: " + ", ".join(missing)
        )
    return {
        "specVersion": VRM_SPEC_VERSION,
        "meta": meta.to_gltf(),
        "humanoid": {"humanBones": human_bones},
    }


def convert(glb_path: Path, vrm_path: Path, meta: VrmMeta) -> dict[str, Any]:
    """リグ済みGLBを VRM 1.0 として書き出し、検証結果つきのサマリを返す。"""
    gltf, binary = read_glb(glb_path)

    extension = build_extension(gltf, meta)
    gltf.setdefault("extensionsUsed", [])
    if VRM_EXTENSION not in gltf["extensionsUsed"]:
        gltf["extensionsUsed"].append(VRM_EXTENSION)
    gltf.setdefault("extensions", {})[VRM_EXTENSION] = extension

    errors = validate(gltf)
    if errors:
        raise VrmError("VRM として不正な出力になりました: " + " / ".join(errors))

    write_glb(vrm_path, gltf, binary)
    return {
        "spec_version": VRM_SPEC_VERSION,
        "human_bones": sorted(extension["humanoid"]["humanBones"]),
        "human_bone_count": len(extension["humanoid"]["humanBones"]),
        "meta_name": meta.name,
        "size_bytes": vrm_path.stat().st_size,
    }


# --- 検証 -------------------------------------------------------------------


def validate(gltf: dict[str, Any]) -> list[str]:
    """VRM 1.0 として成立しているかを検査し、問題を日本語で列挙する。

    godot-vrm 等の実装に読ませる前に、仕様側の必須条件を機械的に潰すためのもの。
    """
    errors: list[str] = []
    if VRM_EXTENSION not in gltf.get("extensionsUsed", []):
        errors.append(f"extensionsUsed に {VRM_EXTENSION} がありません")
    extension = gltf.get("extensions", {}).get(VRM_EXTENSION)
    if not extension:
        return errors + [f"extensions.{VRM_EXTENSION} がありません"]

    if extension.get("specVersion") != VRM_SPEC_VERSION:
        errors.append(f"specVersion が {VRM_SPEC_VERSION} ではありません")

    meta = extension.get("meta") or {}
    for key in ("name", "licenseUrl"):
        if not meta.get(key):
            errors.append(f"meta.{key} が空です")
    if not meta.get("authors"):
        errors.append("meta.authors が空です")

    human_bones = (extension.get("humanoid") or {}).get("humanBones") or {}
    node_count = len(gltf.get("nodes", []))
    for bone in REQUIRED_VRM_BONES:
        if bone not in human_bones:
            errors.append(f"必須の humanBone がありません: {bone}")

    for bone, entry in human_bones.items():
        index = entry.get("node")
        if not isinstance(index, int) or not (0 <= index < node_count):
            errors.append(f"humanBone {bone} のノード参照が不正です: {index}")

    if errors:
        return errors

    # hips 以外はすべて hips の子孫でなければならない(VRM 1.0 の階層制約)
    parents = _node_parents(gltf)
    hips = human_bones["hips"]["node"]
    for bone, entry in human_bones.items():
        if bone == "hips":
            continue
        if not _is_descendant_of(entry["node"], hips, parents):
            errors.append(f"humanBone {bone} が hips の子孫になっていません")

    errors.extend(_validate_orientation(gltf, human_bones))
    return errors


def _validate_orientation(
    gltf: dict[str, Any], human_bones: dict[str, dict[str, int]]
) -> list[str]:
    """VRM 1.0 が要求する「アバターは +Z を向き、+Y が上」を骨の位置から確かめる。"""
    errors: list[str] = []
    world = world_translations(gltf)

    head = world[human_bones["head"]["node"]]
    hips_pos = world[human_bones["hips"]["node"]]
    if head[1] <= hips_pos[1]:
        errors.append("head が hips より上にありません(Y-up になっていない)")

    left_hand = world[human_bones["leftHand"]["node"]]
    right_hand = world[human_bones["rightHand"]["node"]]
    if left_hand[0] <= right_hand[0]:
        errors.append("leftHand が rightHand より +X 側にありません(左右が反転している)")

    # つま先は任意ボーンなので、ある場合だけ正面方向を確認する
    if "leftToes" in human_bones and "leftFoot" in human_bones:
        toes = world[human_bones["leftToes"]["node"]]
        foot = world[human_bones["leftFoot"]["node"]]
        if toes[2] <= foot[2]:
            errors.append("つま先が足首より +Z 側にありません(アバターが +Z を向いていない)")

    return errors
