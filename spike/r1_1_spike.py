"""R1-1 技術スパイク: bpy モジュールでヘッドレスにリグ処理が通るかを検証する。

検証項目 (計画書 §7 Phase R1-1 の検証基準「bpyでGLB入出力がヘッドレスで動く」):

1. `import bpy` がヘッドレス(Xなし)で成功するか
2. GLB をインポートできるか / Blender 側で見える座標系・スケールはどうなるか
3. アーマチュアを配置し `ARMATURE_AUTO`(ボーンヒート自動ウェイト)が通るか
4. スキン付き GLB としてエクスポートできるか
5. 書き出した GLB を再インポートしてスキン(頂点グループ/ボーン)が保持されているか

使い方:
    .venv-rig/bin/python spike/r1_1_spike.py <input.glb> [--out out.glb] [--faces N]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def log(step: str, msg: str = "") -> None:
    print(f"[{step}] {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path, help="入力 GLB")
    ap.add_argument("--out", type=Path, default=Path("spike_rigged.glb"))
    ap.add_argument(
        "--faces",
        type=int,
        default=0,
        help="0 以外なら Decimate でこの面数まで簡略化してから自動ウェイトを試す",
    )
    args = ap.parse_args()

    t0 = time.perf_counter()
    import bpy  # noqa: PLC0415 — bpy のインポート自体が検証対象

    log("1-import", f"bpy {bpy.app.version_string} / python {sys.version.split()[0]} "
                    f"({time.perf_counter() - t0:.1f}s)")
    log("1-import", f"background={bpy.app.background}")

    # --- 2. GLB インポート ------------------------------------------------
    bpy.ops.wm.read_factory_settings(use_empty=True)
    t = time.perf_counter()
    bpy.ops.import_scene.gltf(filepath=str(args.input))
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    log("2-glb-in", f"{len(meshes)} mesh object(s) in {time.perf_counter() - t:.1f}s")
    if not meshes:
        log("2-glb-in", "FAIL: メッシュが見つからない")
        return 1

    # 複数メッシュなら結合しておく(自動ウェイトは単一メッシュのほうが素直)
    bpy.ops.object.select_all(action="DESELECT")
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()
    mesh = bpy.context.view_layer.objects.active

    # トランスフォームを適用してワールド座標=ローカル座標にする
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

    bb = [mesh.matrix_world @ v.co for v in mesh.data.vertices]
    lo = [min(p[i] for p in bb) for i in range(3)]
    hi = [max(p[i] for p in bb) for i in range(3)]
    size = [hi[i] - lo[i] for i in range(3)]
    up_axis = "XYZ"[max(range(3), key=lambda i: size[i])]
    log("2-coords", f"verts={len(mesh.data.vertices)} tris={len(mesh.data.polygons)}")
    log("2-coords", f"min={[round(v, 3) for v in lo]} max={[round(v, 3) for v in hi]}")
    log("2-coords", f"size={[round(v, 3) for v in size]} -> 最長軸(=上方向と推定)={up_axis}")

    # --- 任意: 簡略化して自動ウェイトのコストを測る ----------------------
    if args.faces and len(mesh.data.polygons) > args.faces:
        t = time.perf_counter()
        mod = mesh.modifiers.new("decimate", "DECIMATE")
        mod.ratio = args.faces / len(mesh.data.polygons)
        bpy.ops.object.modifier_apply(modifier=mod.name)
        log("2-decimate", f"-> {len(mesh.data.polygons)} tris in {time.perf_counter() - t:.1f}s")

    # --- 3. アーマチュア配置(スパイクなので最小構成) --------------------
    # 上方向は実測した最長軸を使う。左右は残り2軸のうち広いほう。
    up = "XYZ".index(up_axis)
    rest = [i for i in range(3) if i != up]
    side = max(rest, key=lambda i: size[i])
    depth = [i for i in rest if i != side][0]
    log("3-axes", f"up={'XYZ'[up]} side={'XYZ'[side]} depth={'XYZ'[depth]}")

    def pt(up_frac: float, side_frac: float = 0.0, depth_frac: float = 0.0):
        """高さ比 / 左右比 / 前後比(いずれも 0..1, 中央=0.5)から座標を作る。"""
        p = [0.0, 0.0, 0.0]
        p[up] = lo[up] + size[up] * up_frac
        p[side] = lo[side] + size[side] * (0.5 + side_frac)
        p[depth] = lo[depth] + size[depth] * (0.5 + depth_frac)
        return p

    arm_data = bpy.data.armatures.new("Armature")
    arm = bpy.data.objects.new("Armature", arm_data)
    bpy.context.scene.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")

    # スパイク用の最小骨格(本実装 R1-2 で 15〜20 ボーンに拡張する)
    spec = [
        # name,          head,             tail,             parent
        ("hips",         pt(0.50), pt(0.58), None),
        ("spine",        pt(0.58), pt(0.70), "hips"),
        ("head",         pt(0.85), pt(1.00), "spine"),
        ("upper_arm.L",  pt(0.80, +0.06), pt(0.80, +0.25), "spine"),
        ("lower_arm.L",  pt(0.80, +0.25), pt(0.80, +0.49), "upper_arm.L"),
        ("upper_arm.R",  pt(0.80, -0.06), pt(0.80, -0.25), "spine"),
        ("lower_arm.R",  pt(0.80, -0.25), pt(0.80, -0.49), "upper_arm.R"),
        ("upper_leg.L",  pt(0.50, +0.10), pt(0.27, +0.10), "hips"),
        ("lower_leg.L",  pt(0.27, +0.10), pt(0.03, +0.10), "upper_leg.L"),
        ("upper_leg.R",  pt(0.50, -0.10), pt(0.27, -0.10), "hips"),
        ("lower_leg.R",  pt(0.27, -0.10), pt(0.03, -0.10), "upper_leg.R"),
    ]
    for name, head, tail, parent in spec:
        b = arm_data.edit_bones.new(name)
        b.head, b.tail = head, tail
        if parent:
            b.parent = arm_data.edit_bones[parent]
            b.use_connect = False
    bpy.ops.object.mode_set(mode="OBJECT")
    log("3-armature", f"{len(arm_data.bones)} bones placed")

    # --- 4. ボーンヒート自動ウェイト -------------------------------------
    bpy.ops.object.select_all(action="DESELECT")
    mesh.select_set(True)
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    t = time.perf_counter()
    try:
        bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    except RuntimeError as exc:
        log("4-weights", f"FAIL: ボーンヒートが解けなかった: {exc}")
        return 2
    dt = time.perf_counter() - t

    vg = [g.name for g in mesh.vertex_groups]
    weighted = sum(1 for v in mesh.data.vertices if v.groups)
    log("4-weights", f"{dt:.1f}s / vertex_groups={len(vg)} / "
                     f"ウェイト付き頂点={weighted}/{len(mesh.data.vertices)}")
    missing = [n for n, *_ in spec if n not in vg]
    if missing:
        log("4-weights", f"WARN: 頂点グループが作られなかったボーン: {missing}")

    # --- 5. スキン付き GLB エクスポート ----------------------------------
    out = args.out.resolve()
    t = time.perf_counter()
    bpy.ops.export_scene.gltf(
        filepath=str(out),
        export_format="GLB",
        export_skins=True,
        export_yup=True,
        use_selection=False,
    )
    log("5-glb-out", f"{out} ({out.stat().st_size / 1e6:.1f} MB) in {time.perf_counter() - t:.1f}s")

    # --- 6. 再インポートしてスキンが残っているか確認 ---------------------
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(out))
    # glTF インポータはボーン表示用の custom shape (Icosphere) を作り、
    # `glTF_not_exported` コレクションに入れる。実メッシュではないので除外する。
    def real(o) -> bool:
        return not any(c.name == "glTF_not_exported" for c in o.users_collection)

    arms = [o for o in bpy.context.scene.objects if o.type == "ARMATURE" and real(o)]
    ms = [o for o in bpy.context.scene.objects if o.type == "MESH" and real(o)]
    if not arms or not ms:
        log("6-verify", f"FAIL: armature={len(arms)} mesh={len(ms)}")
        return 3
    skinned = [o for o in ms if any(m.type == "ARMATURE" for m in o.modifiers)]
    log("6-verify", f"armature bones={len(arms[0].data.bones)} / "
                     f"skinned meshes={len(skinned)}/{len(ms)} / "
                     f"vertex_groups={len(ms[0].vertex_groups)}")
    if not skinned:
        log("6-verify", "FAIL: 再インポートしたメッシュにアーマチュアモディファイアが無い")
        return 3

    log("RESULT", f"OK — 全ステップ通過 (総時間 {time.perf_counter() - t0:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
