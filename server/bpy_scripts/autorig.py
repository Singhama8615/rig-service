"""Tポーズ前提の自動リグ処理(bpy)。計画書 §7 Phase R1-2。

GLB を読み込み、座標系を正規化し、ヒューマノイドアーマチュアを配置して
ボーンヒート自動ウェイト(`ARMATURE_AUTO`)でスキニングし、
glTF 仕様準拠(Y-up・メートル系)のリグ済み GLB を書き出す。

**このファイルは単体で実行できるスタンドアロンスクリプトである**。
サーバ本体からはサブプロセスとして起動される(`server/engines/` 参照)。
2つの実行方式が同じスクリプトを共有する(計画書 §4 の結論):

    # 方式B: pip の bpy モジュール(第一実装)
    .venv-rig/bin/python server/bpy_scripts/autorig.py in.glb --output out.glb

    # 方式A: システム Blender のサブプロセス(フォールバック)
    blender --background --python server/bpy_scripts/autorig.py -- in.glb --output out.glb

結果(ボーン数・ウェイト統計・警告)は `--result` の JSON ファイルに書き出す。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import proportions  # noqa: E402  (同ディレクトリのスタンドアロンモジュール)

# glTF は 1頂点あたり最大4ボーン影響が標準(JOINTS_0/WEIGHTS_0 の1セット)。
MAX_BONE_INFLUENCES = 4


class RigError(RuntimeError):
    """リグ処理を継続できない入力・状態。"""


def _log(step: str, msg: str) -> None:
    print(f"[autorig:{step}] {msg}", flush=True)


def _real_objects(bpy, obj_type: str) -> list:
    """glTF インポータが作る非実体オブジェクトを除いてシーン内を数える。

    アーマチュア入りGLBを読むと、ボーン表示用 custom shape の `Icosphere` が
    `glTF_not_exported` コレクションに作られる(計画書 §10 の発見2)。
    """
    return [
        o
        for o in bpy.context.scene.objects
        if o.type == obj_type
        and not any(c.name == "glTF_not_exported" for c in o.users_collection)
    ]


def _world_vertices(obj) -> np.ndarray:
    """オブジェクトのローカル頂点座標を (N, 3) の numpy 配列で取り出す。

    呼び出し前に `transform_apply` 済み(ローカル=ワールド)であることを前提とする。
    """
    n = len(obj.data.vertices)
    flat = np.empty(n * 3, dtype=np.float64)
    obj.data.vertices.foreach_get("co", flat)
    return flat.reshape(n, 3)


def _import_and_join(bpy, input_path: Path):
    """GLB を読み込み、全メッシュを1つに結合してトランスフォームを適用する。"""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(input_path))

    meshes = _real_objects(bpy, "MESH")
    if not meshes:
        raise RigError("GLB にメッシュが含まれていません。")

    # 既存のアーマチュア/スキンは作り直すので取り除く
    for arm in _real_objects(bpy, "ARMATURE"):
        bpy.data.objects.remove(arm, do_unlink=True)

    bpy.ops.object.select_all(action="DESELECT")
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()
    mesh = bpy.context.view_layer.objects.active

    for modifier in list(mesh.modifiers):
        if modifier.type == "ARMATURE":
            mesh.modifiers.remove(modifier)
    mesh.vertex_groups.clear()
    if mesh.parent is not None:
        mesh.parent = None

    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

    before = len(mesh.data.vertices)
    _weld_vertices(bpy, mesh)
    _log(
        "import",
        f"meshes={len(meshes)} verts={before}->{len(mesh.data.vertices)} "
        f"tris={len(mesh.data.polygons)}",
    )
    return mesh


def _weld_vertices(bpy, mesh) -> None:
    """同一位置に重複している頂点を溶接する。

    **テクスチャ付きGLBはUVシームで頂点が複製され、メッシュが数千個の連結成分に
    分断されている**(実測: `texture_mode=paint` の出力で 140087頂点が5900成分。
    テクスチャ無しの同種モデルは1成分)。ボーンヒートは連結したメッシュ上で
    熱方程式を解くため、この状態では**ウェイトが1つも付かない**(実測: 付与率0%)。

    Blender では UV を面コーナー(loop)ごとに保持するので、頂点を溶接しても
    テクスチャは壊れない。

    閾値はモデルのスケールに比例させる(入力は高さ100単位や200単位などまちまち)。
    重複頂点は完全に同一座標なので、ごく小さい値で足りる。
    """
    coords = _world_vertices(mesh)
    extent = float(np.max(coords.max(axis=0) - coords.min(axis=0)))
    threshold = max(extent * 1e-5, 1e-9)

    bpy.ops.object.select_all(action="DESELECT")
    mesh.select_set(True)
    bpy.context.view_layer.objects.active = mesh
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=threshold)
    bpy.ops.object.mode_set(mode="OBJECT")


def _normalize(bpy, mesh, height_m: float, up_axis: str, facing: str) -> dict:
    """座標系・原点・スケールを正規化する(proportions の正規化座標へ)。"""
    from mathutils import Matrix

    matrix, info = proportions.normalize_matrix(
        _world_vertices(mesh), height_m=height_m, up_axis=up_axis, facing=facing
    )
    mesh.matrix_world = Matrix(matrix.tolist()) @ mesh.matrix_world
    bpy.context.view_layer.objects.active = mesh
    bpy.ops.object.select_all(action="DESELECT")
    mesh.select_set(True)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    _log("normalize", json.dumps(info))
    return info


def _build_armature(bpy, bones: list[proportions.BoneSpec]):
    """ボーン仕様からアーマチュアオブジェクトを作る。"""
    arm_data = bpy.data.armatures.new("Armature")
    arm = bpy.data.objects.new("Armature", arm_data)
    bpy.context.scene.collection.objects.link(arm)
    bpy.ops.object.select_all(action="DESELECT")
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    for spec in bones:
        bone = arm_data.edit_bones.new(spec.name)
        bone.head = spec.head
        bone.tail = spec.tail
        if spec.parent:
            bone.parent = arm_data.edit_bones[spec.parent]
            bone.use_connect = spec.connect
    bpy.ops.object.mode_set(mode="OBJECT")
    _log("armature", f"{len(arm_data.bones)} bones")
    return arm


def _auto_weight(bpy, mesh, arm) -> None:
    """ボーンヒート自動ウェイトでスキニングし、glTF 制約に合わせて整える。"""
    bpy.ops.object.select_all(action="DESELECT")
    mesh.select_set(True)
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    t = time.perf_counter()
    try:
        bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    except RuntimeError as exc:
        raise RigError(
            "ボーンヒート自動ウェイトが解けませんでした。メッシュが非多様体、"
            f"または極端に薄い可能性があります: {exc}"
        ) from exc
    _log("weights", f"bone heat {time.perf_counter() - t:.1f}s")

    # ボーンヒートは例外を出さずに「1頂点も解けなかった」で終わることがある。
    # そのまま書き出すと、JOINTS_0/WEIGHTS_0 は付くのに `skins` が無い
    # **不正なglTF**(スキンとして機能せず静的メッシュ扱いになる)が出来てしまう。
    # 気づけない成果物を返すより、ジョブとして失敗させる。
    if not any(v.groups for v in mesh.data.vertices):
        raise RigError(
            "自動ウェイトが1頂点にも付きませんでした。メッシュが多数の断片に"
            "分かれている(UVシームでの頂点分割など)か、ボーンがメッシュの外に"
            "ある可能性があります。"
        )

    # glTF は1頂点4ボーンまで。切り捨て後に正規化して合計1に戻す。
    bpy.ops.object.select_all(action="DESELECT")
    mesh.select_set(True)
    bpy.context.view_layer.objects.active = mesh
    bpy.ops.object.vertex_group_limit_total(limit=MAX_BONE_INFLUENCES)
    bpy.ops.object.vertex_group_normalize_all(lock_active=False)


def _weight_stats(mesh, bones: list[proportions.BoneSpec]) -> tuple[dict, list[str]]:
    """ウェイト付与状況を集計し、破綻の兆候を警告文にする。"""
    group_names = [g.name for g in mesh.vertex_groups]
    per_bone = {name: 0 for name in group_names}
    unweighted = 0
    for v in mesh.data.vertices:
        if not v.groups:
            unweighted += 1
            continue
        for g in v.groups:
            if g.weight > 0.0:
                per_bone[group_names[g.group]] += 1

    total = len(mesh.data.vertices)
    empty_bones = [b.name for b in bones if per_bone.get(b.name, 0) == 0]
    stats = {
        "vertices": total,
        "unweighted_vertices": unweighted,
        "weighted_ratio": round(1.0 - unweighted / total, 4) if total else 0.0,
        "vertices_per_bone": per_bone,
        "bones_without_weight": empty_bones,
    }

    warnings: list[str] = []
    if unweighted:
        warnings.append(
            f"ウェイトが付かなかった頂点が {unweighted}/{total} 個あります"
            "(その部分はポーズを付けても動きません)。"
        )
    if empty_bones:
        warnings.append(
            "対応する頂点が見つからなかったボーン: "
            + ", ".join(empty_bones)
            + "(Tポーズでない、または該当部位がメッシュに無い可能性があります)。"
        )
    return stats, warnings


def _export(bpy, output_path: Path) -> None:
    """glTF 仕様準拠(Y-up)のリグ済み GLB を書き出す。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.export_scene.gltf(
        filepath=str(output_path),
        export_format="GLB",
        export_skins=True,
        export_yup=True,
        use_selection=False,
    )
    _log("export", f"{output_path} ({output_path.stat().st_size / 1e6:.1f} MB)")


def _render_preview(bpy, preview_path: Path, height_m: float) -> None:
    """正面からの正射影プレビューを描く(向き・スケールの目視検証用)。

    EEVEE/Workbench は OpenGL コンテキストを要求し、ヘッドレスの bpy モジュールでは
    `epoxy_get_proc_address` でアボートする。CPU レンダラの Cycles を使う。
    """
    scene = bpy.context.scene
    cam_data = bpy.data.cameras.new("PreviewCam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = height_m * 1.15
    cam = bpy.data.objects.new("PreviewCam", cam_data)
    scene.collection.objects.link(cam)
    # 正面(-Y)側から +Y 方向を見る
    cam.location = (0.0, -height_m * 3, height_m / 2)
    cam.rotation_euler = (np.pi / 2, 0.0, 0.0)
    scene.camera = cam

    sun_data = bpy.data.lights.new("PreviewSun", type="SUN")
    sun_data.energy = 4.0
    sun = bpy.data.objects.new("PreviewSun", sun_data)
    sun.rotation_euler = (np.radians(60), 0.0, np.radians(-30))
    scene.collection.objects.link(sun)

    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 8
    scene.render.resolution_x = 384
    scene.render.resolution_y = 512
    scene.render.film_transparent = True
    scene.render.filepath = str(preview_path)
    bpy.ops.render.render(write_still=True)
    _log("preview", str(preview_path))


def run(
    input_path: Path,
    output_path: Path,
    height_m: float = 1.6,
    up_axis: str = "auto",
    facing: str = "auto",
    preview_path: Path | None = None,
) -> dict:
    """リグ処理一式を実行し、結果サマリを返す。"""
    t0 = time.perf_counter()
    import bpy  # noqa: PLC0415 — Blender CLI 実行時は起動後にしか存在しない

    mesh = _import_and_join(bpy, input_path)
    normalize_info = _normalize(bpy, mesh, height_m, up_axis, facing)

    verts = _world_vertices(mesh)
    measurements = proportions.measure(verts)
    _log("measure", json.dumps(measurements.to_dict()))
    bones = proportions.bone_layout(measurements)

    arm = _build_armature(bpy, bones)
    _auto_weight(bpy, mesh, arm)
    stats, warnings = _weight_stats(mesh, bones)

    if not normalize_info.get("facing_confident", True):
        warnings.append(
            "キャラクターの正面方向を確信を持って判定できませんでした。"
            "前後が逆になっている場合は facing パラメータで明示指定してください。"
        )
    if not measurements.t_pose:
        warnings.insert(
            0,
            "腕を水平に広げたTポーズと判定できませんでした。関節位置は人体標準比で"
            "代用しているため、リグの精度は大きく落ちます。Tポーズの立ち絵から"
            "生成したモデルを入力してください。",
        )
    if measurements.fallbacks:
        # 代用値は「人体標準比」とは限らない(例: 股下は実測できた肩の高さからの
        # 比例で出す)ため、標準比と決めつけない文言にする。
        warnings.append(
            "メッシュから実測できず推定値で代用した項目: " + ", ".join(measurements.fallbacks)
        )

    _export(bpy, output_path)
    if preview_path is not None:
        _render_preview(bpy, preview_path, height_m)

    return {
        "blender_version": bpy.app.version_string,
        "bones": [b.name for b in bones],
        "bone_count": len(bones),
        "normalize": normalize_info,
        "measurements": measurements.to_dict(),
        "weights": stats,
        "warnings": warnings,
        "elapsed_sec": round(time.perf_counter() - t0, 2),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path, help="入力 GLB")
    ap.add_argument("--output", type=Path, required=True, help="出力 GLB")
    ap.add_argument("--result", type=Path, help="結果サマリの書き出し先 JSON")
    ap.add_argument("--height-m", type=float, default=1.6, help="出力の全高(m)")
    ap.add_argument("--up-axis", default="auto", choices=("auto", "y", "z"),
                    help="入力GLBの上方向軸(image-3d の出力は z)")
    ap.add_argument("--facing", default="auto", choices=("auto", "+y", "-y"),
                    help="上方向を揃えた段階でキャラクターが向く軸")
    ap.add_argument("--preview", type=Path, help="正面プレビューPNGの書き出し先")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
        # `blender --background --python this.py -- ...` 形式では
        # `--` 以降が本スクリプトの引数になる。
        if "--" in argv:
            argv = argv[argv.index("--") + 1 :]
    args = parse_args(argv)

    try:
        result = run(
            args.input,
            args.output,
            height_m=args.height_m,
            up_axis=args.up_axis,
            facing=args.facing,
            preview_path=args.preview,
        )
        result["ok"] = True
        code = 0
    except (RigError, ValueError) as exc:
        result = {"ok": False, "error": str(exc)}
        code = 1
        _log("error", str(exc))

    if args.result:
        args.result.parent.mkdir(parents=True, exist_ok=True)
        args.result.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
