"""ジョブ管理: 直列実行キュー + `data/jobs/` 永続化 (計画書 §5)。

image-3d の `server/jobs.py` と同じ実装パターンを踏襲する:

- ジョブは `data/jobs/<job_id>/` に `input.glb` / `rigged.glb` / `preview.png` /
  `meta.json` を保存する。
- asyncio ループ + 単一ワーカーで直列実行する(リグ処理はCPUを数コア使うため)。
- status遷移: queued -> rigging -> completed / failed
- サーバ再起動時は `data/jobs/` から履歴を再読込する。
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import config, vrm
from .engines import RigEngine, RigParams

logger = logging.getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_RIGGING = "rigging"
STATUS_EXPORTING = "exporting"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

# 計画書 §6 の download?format=glb|vrm
EXPORT_FORMATS = {"glb", "vrm"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    job_id: str
    status: str = STATUS_QUEUED
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    error: Optional[str] = None
    params: dict[str, Any] = field(default_factory=dict)
    engine: str = "bpy"
    original_filename: Optional[str] = None
    # autorig.py が返すサマリ(ボーン一覧・計測値・ウェイト統計・所要時間)
    summary: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    has_preview: bool = False
    # VRM 1.0 出力に成功したか(失敗してもリグ済みGLBは使えるので job は failed にしない)
    has_vrm: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def dir_path(self) -> Path:
        return config.JOBS_DIR / self.job_id

    def input_path(self) -> Path:
        return self.dir_path() / "input.glb"

    def model_path(self, fmt: str = "glb") -> Path:
        return self.dir_path() / f"rigged.{fmt}"

    def preview_path(self) -> Path:
        return self.dir_path() / "preview.png"

    def meta_path(self) -> Path:
        return self.dir_path() / "meta.json"

    def save_meta(self) -> None:
        self.dir_path().mkdir(parents=True, exist_ok=True)
        self.meta_path().write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load_meta(cls, meta_path: Path) -> "Job":
        return cls(**json.loads(meta_path.read_text(encoding="utf-8")))


class JobManager:
    """リグジョブの生成・永続化・直列実行キューを管理する。"""

    def __init__(self, engine: RigEngine) -> None:
        self.engine = engine
        self.jobs: dict[str, Job] = {}
        self._queue: Optional["asyncio.Queue[str]"] = None
        self._worker_task: Optional[asyncio.Task] = None

    # --- 永続化 -----------------------------------------------------------
    def load_history(self) -> None:
        config.ensure_dirs()
        for job_dir in sorted(config.JOBS_DIR.iterdir()):
            meta_path = job_dir / "meta.json"
            if not meta_path.exists():
                continue
            try:
                job = Job.load_meta(meta_path)
            except Exception:
                logger.exception("Failed to load job meta: %s", meta_path)
                continue
            # 再起動時に実行中だったジョブは実行状態を失うため failed 扱いにする
            if job.status in (STATUS_QUEUED, STATUS_RIGGING, STATUS_EXPORTING):
                job.status = STATUS_FAILED
                job.error = "サーバ再起動のため中断されました。"
                job.updated_at = _now_iso()
                job.save_meta()
            self.jobs[job.job_id] = job

    async def start_worker(self) -> None:
        # asyncio.Queue はイベントループにバインドされるため、ワーカー起動時に作る。
        self._queue = asyncio.Queue()
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop_worker(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        self._queue = None

    # --- ジョブ操作 ---------------------------------------------------------
    def list_jobs(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)

    def get_job(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)

    async def create_job(
        self,
        glb_bytes: bytes,
        params: RigParams,
        original_filename: Optional[str] = None,
    ) -> Job:
        job = Job(
            job_id=str(uuid.uuid4()),
            params=asdict(params),
            engine=self.engine.name,
            original_filename=original_filename,
        )
        job.dir_path().mkdir(parents=True, exist_ok=True)
        job.input_path().write_bytes(glb_bytes)
        job.save_meta()
        self.jobs[job.job_id] = job
        await self._queue.put(job.job_id)
        return job

    def delete_job(self, job_id: str) -> bool:
        job = self.jobs.pop(job_id, None)
        if job is None:
            return False
        shutil.rmtree(job.dir_path(), ignore_errors=True)
        return True

    def _export_vrm(self, job: Job, params: RigParams) -> None:
        """リグ済みGLBから VRM 1.0 を書き出す(同期・ワーカースレッドから呼ばれる)。

        失敗してもジョブは failed にしない。リグ済みGLB自体は Godot で使えるため、
        警告を残して VRM だけ諦める(image-3d のペイント失敗時と同じ考え方)。
        """
        try:
            meta = vrm.VrmMeta(**params.vrm_meta) if params.vrm_meta else vrm.VrmMeta()
            if not params.vrm_meta.get("name") and job.original_filename:
                meta.name = Path(job.original_filename).stem
            summary = vrm.convert(job.model_path("glb"), job.model_path("vrm"), meta)
            job.summary["vrm"] = summary
            job.has_vrm = True
        except Exception as exc:
            logger.exception("VRM export failed for job %s", job.job_id)
            job.warnings.append(f"VRM 1.0 の書き出しに失敗しました(GLBは利用できます): {exc}")
            job.has_vrm = False

    # --- ワーカー -----------------------------------------------------------
    async def _worker_loop(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._run_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unhandled error while running job %s", job_id)
            finally:
                self._queue.task_done()

    async def _run_job(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return

        loop = asyncio.get_running_loop()
        try:
            job.status = STATUS_RIGGING
            job.updated_at = _now_iso()
            job.save_meta()

            params = RigParams(**job.params)
            result = await loop.run_in_executor(
                None,
                self.engine.rig,
                job.input_path(),
                job.model_path("glb"),
                params,
                job.dir_path(),
                config.RIG_TIMEOUT_SEC,
            )

            job.summary = result.summary
            job.warnings = result.warnings
            job.has_preview = result.preview_path is not None

            if params.vrm:
                job.status = STATUS_EXPORTING
                job.updated_at = _now_iso()
                job.save_meta()
                await loop.run_in_executor(None, self._export_vrm, job, params)

            job.status = STATUS_COMPLETED
            job.updated_at = _now_iso()
            job.save_meta()
        except Exception as exc:
            # 例外はジョブ単位で捕捉し failed に落とす。サーバは落とさない。
            logger.exception("Job %s failed", job_id)
            job.status = STATUS_FAILED
            job.error = str(exc)
            job.updated_at = _now_iso()
            job.save_meta()
