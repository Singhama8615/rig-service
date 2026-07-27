"""FastAPI エントリポイント (計画書 §6 API仕様)。"""
from __future__ import annotations

import json
import logging
import platform
from contextlib import asynccontextmanager
from dataclasses import fields
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, motions, pose, vrm
from .engines import RigParams, build_engine
from .jobs import EXPORT_FORMATS, STATUS_COMPLETED, JobManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# GLB のマジックナンバー(glTF 2.0 バイナリ)
GLB_MAGIC = b"glTF"

# params.vrm_meta で指定できるキー(`server/vrm.py` の VrmMeta と同じ)
_VRM_META_FIELDS = {f.name for f in fields(vrm.VrmMeta)}

# VRM は中身が GLB なので glTF バイナリとして返す
_DOWNLOAD_MEDIA_TYPES = {"glb": "model/gltf-binary", "vrm": "model/gltf-binary"}

job_manager = JobManager(build_engine())


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    job_manager.load_history()
    await job_manager.start_worker()
    yield
    await job_manager.stop_worker()


app = FastAPI(title="rig-service", lifespan=lifespan)


def _parse_params(params_json: Optional[str]) -> RigParams:
    data: dict = {}
    if params_json:
        try:
            data = json.loads(params_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"paramsのJSONが不正です: {exc}") from exc
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=400, detail="paramsはJSONオブジェクトである必要があります。"
            )

    height_m = data.get("height_m", config.DEFAULT_HEIGHT_M)
    up_axis = data.get("up_axis", "auto")
    facing = data.get("facing", "auto")
    bone_set = data.get("bone_set", "standard")
    arm_down = data.get("arm_down", pose.STANDING_ARM_DOWN)
    preview = data.get("preview", config.DEFAULT_PREVIEW)
    export_vrm = data.get("vrm", config.DEFAULT_VRM)
    vrm_meta = data.get("vrm_meta", {})

    if not isinstance(height_m, (int, float)) or not (0.05 <= height_m <= 100):
        raise HTTPException(
            status_code=400, detail="height_mは0.05〜100の数値である必要があります。"
        )
    if up_axis not in config.ALLOWED_UP_AXIS:
        raise HTTPException(
            status_code=400,
            detail=f"up_axisは{sorted(config.ALLOWED_UP_AXIS)}のいずれかである必要があります。",
        )
    if facing not in config.ALLOWED_FACING:
        raise HTTPException(
            status_code=400,
            detail=f"facingは{sorted(config.ALLOWED_FACING)}のいずれかである必要があります。",
        )
    if not isinstance(arm_down, (int, float)) or not (0 <= arm_down <= 90):
        raise HTTPException(
            status_code=400, detail="arm_downは0〜90の数値である必要があります(90=体に密着)。"
        )
    if bone_set not in config.ALLOWED_BONE_SETS:
        raise HTTPException(
            status_code=400,
            detail=f"bone_setは{sorted(config.ALLOWED_BONE_SETS)}のいずれかである必要があります。",
        )
    if not isinstance(vrm_meta, dict):
        raise HTTPException(status_code=400, detail="vrm_metaはJSONオブジェクトである必要があります。")
    unknown = sorted(set(vrm_meta) - _VRM_META_FIELDS)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"vrm_metaに未知のキーがあります: {unknown}(指定可能: {sorted(_VRM_META_FIELDS)})",
        )
    try:
        # 値の型・列挙値の検証はここで済ませ、ジョブ実行時に落ちないようにする
        vrm.VrmMeta(**{**vrm_meta, "name": vrm_meta.get("name") or "Untitled"}).validate()
    except (TypeError, vrm.VrmError) as exc:
        raise HTTPException(status_code=400, detail=f"vrm_metaが不正です: {exc}") from exc

    return RigParams(
        height_m=float(height_m),
        up_axis=up_axis,
        facing=facing,
        bone_set=bone_set,
        arm_down=float(arm_down),
        preview=bool(preview),
        vrm=bool(export_vrm),
        vrm_meta=vrm_meta,
    )


async def _read_glb(model: UploadFile) -> bytes:
    data = await model.read()
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="空のファイルです。")
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"ファイルサイズが上限({config.MAX_UPLOAD_BYTES // (1024 * 1024)}MB)を超えています。",
        )
    if data[:4] != GLB_MAGIC:
        raise HTTPException(
            status_code=400,
            detail="GLB(バイナリglTF)ではありません。image-3d の model.glb を指定してください。",
        )
    return data


def _arm_down_for(job_id: Optional[str], arm_down: Optional[float]) -> Optional[float]:
    """腕の開きを決める。明示指定 > ジョブの設定 > 既定。"""
    if arm_down is not None:
        return arm_down
    if job_id:
        job = job_manager.get_job(job_id)
        if job is not None:
            return job.params.get("arm_down")
    return None


def _require_job(job_id: str):
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません。")
    return job


@app.post("/api/rig")
async def create_rig_job(
    model: UploadFile = File(...),
    params: Optional[str] = Form(None),
):
    data = await _read_glb(model)
    rig_params = _parse_params(params)
    job = await job_manager.create_job(data, rig_params, original_filename=model.filename)
    return {"job_id": job.job_id}


@app.get("/api/rig/jobs")
async def list_rig_jobs():
    return [job.to_dict() for job in job_manager.list_jobs()]


@app.get("/api/rig/jobs/{job_id}")
async def get_rig_job(job_id: str):
    return _require_job(job_id).to_dict()


@app.get("/api/motions")
async def list_motions():
    """同梱モーションクリップの一覧 (計画書 §6)。"""
    return [m.summary() for m in motions.MOTIONS.values()]


@app.get("/api/poses")
async def list_poses(job: Optional[str] = None, arm_down: Optional[float] = None):
    """ポーズプリセット。ボーンごとに軸の意味が違うため定義元はサーバに置く。

    腕の開きはジョブごとに変えられるので、`job` か `arm_down` で指定できる。
    """
    return pose.presets(_arm_down_for(job, arm_down))


@app.get("/api/motions/{name}")
async def get_motion(name: str, job: Optional[str] = None, arm_down: Optional[float] = None):
    """クリップのキーフレーム(ブラウザ再生用)。"""
    try:
        return motions.get(name, _arm_down_for(job, arm_down)).to_dict()
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"モーションが見つかりません: {name}(利用可能: {sorted(motions.MOTIONS)})",
        ) from exc


@app.get("/api/rig/jobs/{job_id}/download")
async def download_rig_job(job_id: str, format: str = "glb", motion: Optional[str] = None):
    """リグ済みモデルを返す。

    `motion` を指定すると同梱クリップを **glTF アニメーションとして焼き込んで**
    返す(Godot 等でそのまま再生できる)。
    """
    fmt = format.lower()
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"formatは{sorted(EXPORT_FORMATS)}のいずれかである必要があります。",
        )
    job = _require_job(job_id)
    if job.status != STATUS_COMPLETED:
        raise HTTPException(status_code=409, detail=f"ジョブは未完了です(status={job.status})。")
    path = job.model_path(fmt)
    if not path.exists():
        detail = "モデルファイルが見つかりません。"
        if fmt == "vrm":
            detail = (
                "VRMが生成されていません。params.vrm=false で実行されたか、"
                "書き出しに失敗しています(ジョブの warnings を確認してください)。"
            )
        raise HTTPException(status_code=404, detail=detail)

    if motion is None:
        return FileResponse(
            path, media_type=_DOWNLOAD_MEDIA_TYPES[fmt], filename=f"{job_id}_rigged.{fmt}"
        )

    try:
        clip = motions.get(motion, job.params.get("arm_down"))
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"モーションが見つかりません: {motion}(利用可能: {sorted(motions.MOTIONS)})",
        ) from exc

    import asyncio

    loop = asyncio.get_running_loop()
    destination = job.dir_path() / f"animated_{clip.name}.{fmt}"

    def bake() -> None:
        gltf, binary = vrm.read_glb(path)
        gltf, binary = motions.bake_into_gltf(gltf, binary, clip)
        vrm.write_glb(destination, gltf, binary)

    try:
        await loop.run_in_executor(None, bake)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except vrm.VrmError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return FileResponse(
        destination,
        media_type=_DOWNLOAD_MEDIA_TYPES[fmt],
        filename=f"{job_id}_{clip.name}.{fmt}",
    )


@app.post("/api/rig/jobs/{job_id}/pose")
async def pose_rig_job(job_id: str, body: dict):
    """ポーズを適用したGLB/VRMを返す (計画書 §6 / §7 R3-3)。

    リクエスト: `{"pose": {"LeftUpperArm": [x,y,z,w] | [x,y,z度], ...},
                  "format": "glb" | "vrm"}`

    ブラウザのプレビューは three.js 側で同じ回転をクライアント内で当てるため、
    このAPIは**ポーズ済みファイルの書き出し**に使う(数十MBを毎フレーム
    往復させない)。
    """
    fmt = str(body.get("format", "glb")).lower()
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"formatは{sorted(EXPORT_FORMATS)}のいずれかである必要があります。",
        )
    job = _require_job(job_id)
    if job.status != STATUS_COMPLETED:
        raise HTTPException(status_code=409, detail=f"ジョブは未完了です(status={job.status})。")
    source = job.model_path(fmt)
    if not source.exists():
        raise HTTPException(status_code=404, detail="モデルファイルが見つかりません。")

    import asyncio

    loop = asyncio.get_running_loop()
    destination = job.dir_path() / f"posed.{fmt}"
    try:
        await loop.run_in_executor(
            None, pose.pose_glb, source, destination, body.get("pose", {})
        )
    except pose.PoseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except vrm.VrmError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return FileResponse(
        destination,
        media_type=_DOWNLOAD_MEDIA_TYPES[fmt],
        filename=f"{job_id}_posed.{fmt}",
    )


@app.get("/api/rig/jobs/{job_id}/preview.png")
async def get_rig_job_preview(job_id: str):
    job = _require_job(job_id)
    path = job.preview_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="プレビュー画像がありません。")
    return FileResponse(path, media_type="image/png")


@app.delete("/api/rig/jobs/{job_id}")
async def delete_rig_job(job_id: str):
    if not job_manager.delete_job(job_id):
        raise HTTPException(status_code=404, detail="ジョブが見つかりません。")
    return {"deleted": True}


@app.get("/api/health")
async def health():
    ok, detail = job_manager.engine.available()
    return {
        "status": "ok",
        "engine": job_manager.engine.name,
        "engine_available": ok,
        "engine_detail": detail,
        "python_version": platform.python_version(),
        # VRM 1.0 は純Python実装(server/vrm.py)なので外部依存なしで常に使える
        "vrm_export_available": True,
        "vrm_spec_version": vrm.VRM_SPEC_VERSION,
    }


# --- 静的フロントエンド配信 (計画書 §6 `GET /`) -------------------------------
class RevalidatingStaticFiles(StaticFiles):
    """静的ファイルに `Cache-Control: no-cache` を付けて必ず再検証させる。

    Starlette の StaticFiles は ETag / Last-Modified は返すが Cache-Control を
    付けない。するとブラウザは**ヒューリスティックキャッシュ**(最終更新からの
    経過時間の約10%)を使い、その間はサーバに問い合わせずに古いファイルを
    使い続ける。コードを直したのに画面が変わらない、という混乱の原因になる。

    `no-cache` は「保存してよいが使う前に必ず再検証せよ」の意味なので、
    毎回 ETag による条件付きGETが走り、変更が無ければ 304 で終わる
    (転送量はほとんど増えない)。
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


app.mount("/", RevalidatingStaticFiles(directory=str(config.WEB_DIR), html=True), name="web")
