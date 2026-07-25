"""リグ済みGLBにテストポーズを当てて正面プレビューを描く(目視検証用)。

自動ウェイトが「ボーンが正しい部位を掴んでいるか」は、ウェイト付与率だけでは
分からない。腕を下ろす・膝を曲げるといったポーズを実際に当てて、意図した部位が
意図した通りに動くことを確認するためのスクリプト。

実行:
    .venv-rig/bin/python tests/pose_preview.py rigged.glb --output pose.png
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.pose import quat_from_euler_degrees  # noqa: E402

# ポーズ = {ボーン名: (X回転, Y回転, Z回転)}(度、ボーンのローカル軸まわり)。
# 合成順は `server/pose.py` = three.js の "XYZ" 規約に揃える。
# Tポーズから見て「腕を下ろして肘と膝を曲げた」自然な立ち姿を作る。
POSES: dict[str, dict[str, tuple[float, float, float]]] = {
    "arms_down": {
        "LeftUpperArm": (0, 0, -70),
        "RightUpperArm": (0, 0, 70),
        "LeftLowerArm": (0, 0, -25),
        "RightLowerArm": (0, 0, 25),
    },
    "relaxed": {
        "LeftUpperArm": (0, 0, -65),
        "RightUpperArm": (0, 0, 65),
        "LeftLowerArm": (-20, 0, -20),
        "RightLowerArm": (-20, 0, 20),
        "LeftUpperLeg": (25, 0, 0),
        "RightUpperLeg": (25, 0, 0),
        "LeftLowerLeg": (-45, 0, 0),
        "RightLowerLeg": (-45, 0, 0),
        "Head": (-10, 0, 0),
        "Spine": (5, 0, 0),
    },
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path, help="リグ済み GLB")
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--pose", choices=sorted(POSES), default="relaxed")
    ap.add_argument("--view", choices=("front", "back", "side"), default="front")
    args = ap.parse_args()

    import bpy
    from mathutils import Quaternion, Vector

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(args.input))

    def real(o) -> bool:
        return not any(c.name == "glTF_not_exported" for c in o.users_collection)

    arms = [o for o in bpy.context.scene.objects if o.type == "ARMATURE" and real(o)]
    if not arms:
        print("アーマチュアが見つかりません")
        return 1
    arm = arms[0]

    applied, missing = [], []
    for name, degrees in POSES[args.pose].items():
        bone = arm.pose.bones.get(name)
        if bone is None:
            missing.append(name)
            continue
        # Blender の Euler "XYZ" は three.js/サーバ側と合成順が逆(Rz·Ry·Rx)。
        # 描画結果をブラウザのプレビューと一致させるため、`server/pose.py` の
        # 規約でクォータニオンを組んでから渡す。
        x, y, z, w = quat_from_euler_degrees(degrees)
        bone.rotation_mode = "QUATERNION"
        bone.rotation_quaternion = Quaternion((w, x, y, z))
        applied.append(name)
    print(f"pose={args.pose} applied={len(applied)} missing={missing}")
    bpy.context.view_layer.update()

    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH" and real(o)]
    height = max(
        (o.matrix_world @ v.co).z for o in meshes for v in o.data.vertices
    )

    scene = bpy.context.scene
    cam_data = bpy.data.cameras.new("Cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = height * 1.2
    cam = bpy.data.objects.new("Cam", cam_data)
    scene.collection.objects.link(cam)
    if args.view == "front":
        cam.location = (0.0, -height * 3, height / 2)
        cam.rotation_euler = (math.pi / 2, 0.0, 0.0)
    elif args.view == "back":
        cam.location = (0.0, height * 3, height / 2)
        cam.rotation_euler = (math.pi / 2, 0.0, math.pi)
    else:
        cam.location = (height * 3, 0.0, height / 2)
        cam.rotation_euler = (math.pi / 2, 0.0, math.pi / 2)
    scene.camera = cam

    # ライトは必ずカメラ側に置く。正面固定にすると背面ビューが真っ黒になり、
    # モデルの色が暗いのか陰になっているだけなのか区別できなくなる。
    sun_data = bpy.data.lights.new("Sun", type="SUN")
    sun_data.energy = 4.0
    sun = bpy.data.objects.new("Sun", sun_data)
    scene.collection.objects.link(sun)
    sun.location = cam.location + Vector((0.0, 0.0, height * 0.8))
    direction = Vector((0.0, 0.0, height / 2)) - sun.location
    sun.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()

    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 8
    scene.render.resolution_x = 384
    scene.render.resolution_y = 512
    scene.render.film_transparent = True
    scene.render.filepath = str(args.output)
    bpy.ops.render.render(write_still=True)
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
