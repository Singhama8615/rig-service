"""テスト用の素体Tポーズ GLB を bpy で生成する。

計画書 §7 R1-2 の検証基準「mockメッシュ+実生成メッシュでウェイト破綻なし」の
mockメッシュ側にあたる。**image-3d の出力慣習をわざと再現する**:

- Z-up(glTF仕様違反。計画書 §10 の発見1)
- 原点=足元、全高 100 単位
- キャラクターの正面は -Y

実行:
    .venv-rig/bin/python tests/make_tpose_glb.py --output /tmp/tpose.glb
"""
from __future__ import annotations

import argparse
from pathlib import Path

def _humanoid() -> list[tuple[str, tuple, tuple]]:
    """7.5頭身の人体標準比に近い素体。全高100基準。"""
    parts = [
        ("head", (0, 0, 93.5), (13, 13, 13)),
        ("neck", (0, 0, 86.5), (5, 5, 4)),
        ("chest", (0, 0, 76), (22, 12, 18)),
        ("waist", (0, 0, 62), (16, 10, 12)),
        ("pelvis", (0, 0, 53), (19, 11, 8)),
    ]
    for sign, side in ((1, "l"), (-1, "r")):
        parts += [
            # 腕: 肩(x=11)から指先(x=50)まで。肩の高さ 81。
            (f"upper_arm_{side}", (sign * 18.5, 0, 81), (15, 8, 8)),
            (f"lower_arm_{side}", (sign * 33, 0, 81), (14, 6.5, 6.5)),
            (f"hand_{side}", (sign * 45, 0, 81), (10, 5, 3)),
            # 脚: 中心 x=7.5、股下 49 から足首 5.5 まで
            (f"thigh_{side}", (sign * 7.5, 0, 38), (10, 10, 24)),
            (f"shin_{side}", (sign * 7.5, 0, 16), (8, 8, 22)),
            (f"foot_{side}", (sign * 7.5, -4, 3), (8, 15, 6)),
        ]
    return parts


def _chibi() -> list[tuple[str, tuple, tuple]]:
    """頭が全高の4割を占めるデフォルメ体型(ぬいぐるみ・動物キャラ)。

    実運用の入力(ぬいぐるみキャラのTポーズ立ち絵)の比率に合わせてある。
    肩が全高の 49%、股下が 16% と、人体標準比から大きく外れるのが要点。
    """
    parts = [
        ("head", (0, 0, 78), (40, 36, 40)),
        ("neck", (0, 0, 55), (12, 12, 6)),
        ("body", (0, 0, 38), (32, 22, 30)),
    ]
    for sign, side in ((1, "l"), (-1, "r")):
        parts += [
            (f"upper_arm_{side}", (sign * 25, 0, 49), (24, 12, 12)),
            (f"lower_arm_{side}", (sign * 43, 0, 49), (16, 10, 10)),
            (f"paw_{side}", (sign * 51, 0, 49), (8, 9, 9)),
            (f"leg_{side}", (sign * 11, 0, 14), (16, 14, 20)),
            (f"foot_{side}", (sign * 11, -4, 4), (15, 22, 8)),
        ]
    return parts


SHAPES = {"humanoid": _humanoid, "chibi": _chibi}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--shape", choices=sorted(SHAPES), default="humanoid")
    args = ap.parse_args()
    parts = SHAPES[args.shape]()

    import bpy

    bpy.ops.wm.read_factory_settings(use_empty=True)
    for name, center, size in parts:
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=center)
        obj = bpy.context.active_object
        obj.name = name
        obj.scale = size
        # 継ぎ目が滑らかなほうがボーンヒートが解けやすいので分割しておく
        bpy.ops.object.modifier_add(type="SUBSURF")
        obj.modifiers["Subdivision"].levels = 2
        obj.modifiers["Subdivision"].render_levels = 2
        bpy.ops.object.modifier_apply(modifier="Subdivision")

    bpy.ops.object.select_all(action="SELECT")
    bpy.context.view_layer.objects.active = bpy.context.scene.objects[0]
    bpy.ops.object.join()
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

    # 連結した1つの多様体にする(ボーンヒートは非連結パーツでも解けるが、
    # 生成メッシュに近づけるため結合しておく)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=0.001)
    bpy.ops.object.mode_set(mode="OBJECT")
    # フラットシェーディングのままだと glTF エクスポータが法線ごとに頂点を分割し、
    # 再インポート時に面単位のバラバラなメッシュになる(=実際の生成メッシュと
    # 性質が違うテストデータになってしまう)。スムーズシェードで溶接を保つ。
    bpy.ops.object.shade_smooth()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # export_yup=False で Blender の Z-up をそのまま書き出す(=image-3d 慣習)
    bpy.ops.export_scene.gltf(
        filepath=str(args.output), export_format="GLB", export_yup=False
    )
    print(f"wrote {args.output} ({args.output.stat().st_size / 1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
