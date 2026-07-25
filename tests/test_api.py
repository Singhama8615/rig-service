"""API のライフサイクル検証。

実際に bpy を回すと1ケース数秒かかるため、リグエンジンをスタブに差し替えて
ジョブのキュー・永続化・ダウンロードだけを検証する
(image-3d が mock ジェネレータでAPIを検証しているのと同じ考え方)。
bpy を含む本物の経路は `test_autorig_e2e.py` が担当する。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from server import main, vrm
from server.engines.base import RigEngine, RigEngineError, RigResult
from tests.glb_fixture import rigged_glb

# アップロードするダミー入力(GLBのマジックナンバーだけ満たせばよい)
GLB_HEADER = b"glTF" + b"\x02\x00\x00\x00" + b"\x00" * 12
# スタブエンジンが「リグ結果」として書き出す、21ボーンを持つ最小のGLB。
# これで VRM 変換まで含めたジョブの流れを bpy 抜きで検証できる。
RIGGED_GLB = rigged_glb()


class StubEngine(RigEngine):
    """autorig.py を実行せず、それらしい成果物だけを書くエンジン。"""

    name = "stub"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    def command(self, args):  # pragma: no cover — スタブでは使わない
        return ["true", *args]

    def available(self):
        return True, "stub"

    def rig(self, input_glb, output_glb, params, work_dir, timeout_sec):
        self.calls.append({"input": input_glb, "params": params})
        if self.fail:
            raise RigEngineError("スタブの失敗")
        output_glb.write_bytes(RIGGED_GLB)
        preview = work_dir / "preview.png"
        preview.write_bytes(b"\x89PNG\r\n\x1a\n")
        return RigResult(
            output_path=output_glb,
            preview_path=preview,
            summary={"bone_count": 21, "weights": {"weighted_ratio": 1.0}},
            warnings=["テスト警告"],
        )


@pytest.fixture
def client(monkeypatch):
    engine = StubEngine()
    monkeypatch.setattr(main.job_manager, "engine", engine)
    main.job_manager.jobs.clear()
    with TestClient(main.app) as c:
        c.engine = engine
        yield c


def _wait_completed(client, job_id, tries=100):
    """直列ワーカーがジョブを処理し終えるまでポーリングする。"""
    for _ in range(tries):
        job = client.get(f"/api/rig/jobs/{job_id}").json()
        if job["status"] in ("completed", "failed"):
            return job
    raise AssertionError(f"ジョブが終わりません: {job}")


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["engine"] == "stub"
    assert body["engine_available"] is True
    # VRM 出力は純Python実装なので外部依存なしで常に使える
    assert body["vrm_export_available"] is True
    assert body["vrm_spec_version"] == "1.0"


def test_rig_job_lifecycle(client):
    res = client.post("/api/rig", files={"model": ("m.glb", GLB_HEADER, "model/gltf-binary")})
    assert res.status_code == 200
    job_id = res.json()["job_id"]

    job = _wait_completed(client, job_id)
    assert job["status"] == "completed"
    assert job["summary"]["bone_count"] == 21
    assert job["warnings"] == ["テスト警告"]
    assert job["has_preview"] is True
    assert job["has_vrm"] is True
    assert job["original_filename"] == "m.glb"

    download = client.get(f"/api/rig/jobs/{job_id}/download?format=glb")
    assert download.status_code == 200
    assert download.content.startswith(b"glTF")

    assert client.get(f"/api/rig/jobs/{job_id}/preview.png").status_code == 200
    assert any(j["job_id"] == job_id for j in client.get("/api/rig/jobs").json())

    assert client.delete(f"/api/rig/jobs/{job_id}").json() == {"deleted": True}
    assert client.get(f"/api/rig/jobs/{job_id}").status_code == 404


def test_params_are_passed_to_engine(client):
    params = {"height_m": 1.2, "up_axis": "z", "facing": "-y", "preview": False}
    res = client.post(
        "/api/rig",
        files={"model": ("m.glb", GLB_HEADER, "model/gltf-binary")},
        data={"params": json.dumps(params)},
    )
    _wait_completed(client, res.json()["job_id"])
    called = client.engine.calls[-1]["params"]
    assert called.height_m == 1.2
    assert called.up_axis == "z"
    assert called.facing == "-y"
    assert called.preview is False


def test_failed_job_records_error(client, monkeypatch):
    monkeypatch.setattr(client.engine, "fail", True)
    res = client.post("/api/rig", files={"model": ("m.glb", GLB_HEADER, "model/gltf-binary")})
    job = _wait_completed(client, res.json()["job_id"])
    assert job["status"] == "failed"
    assert "スタブの失敗" in job["error"]
    # 未完了ジョブのダウンロードは 409
    assert client.get(f"/api/rig/jobs/{job['job_id']}/download").status_code == 409


@pytest.mark.parametrize(
    "content,expected",
    [
        (b"", 400),  # 空ファイル
        (b"not a glb at all", 400),  # マジックナンバー不一致
    ],
)
def test_rejects_invalid_upload(client, content, expected):
    res = client.post("/api/rig", files={"model": ("m.glb", content, "model/gltf-binary")})
    assert res.status_code == expected


@pytest.mark.parametrize(
    "params,expected",
    [
        ({"height_m": 0}, 400),
        ({"height_m": "tall"}, 400),
        ({"up_axis": "x"}, 400),
        ({"facing": "+x"}, 400),
        ({"bone_set": "fingers"}, 400),
        ({"vrm_meta": "momo"}, 400),  # オブジェクトでない
        ({"vrm_meta": {"title": "momo"}}, 400),  # 未知のキー
        ({"vrm_meta": {"authors": []}}, 400),  # authors は1件以上
        ({"vrm_meta": {"commercial_usage": "free"}}, 400),  # 列挙値外
    ],
)
def test_rejects_invalid_params(client, params, expected):
    res = client.post(
        "/api/rig",
        files={"model": ("m.glb", GLB_HEADER, "model/gltf-binary")},
        data={"params": json.dumps(params)},
    )
    assert res.status_code == expected


def test_malformed_params_json(client):
    res = client.post(
        "/api/rig",
        files={"model": ("m.glb", GLB_HEADER, "model/gltf-binary")},
        data={"params": "{not json"},
    )
    assert res.status_code == 400


def test_vrm_is_generated_and_downloadable(client):
    params = {"vrm_meta": {"name": "momo", "authors": ["animede"]}}
    res = client.post(
        "/api/rig",
        files={"model": ("m.glb", GLB_HEADER, "model/gltf-binary")},
        data={"params": json.dumps(params)},
    )
    job = _wait_completed(client, res.json()["job_id"])
    assert job["status"] == "completed"
    assert job["has_vrm"] is True
    assert job["summary"]["vrm"]["human_bone_count"] == 21

    download = client.get(f"/api/rig/jobs/{job['job_id']}/download?format=vrm")
    assert download.status_code == 200
    # VRM の中身は GLB
    assert download.content.startswith(b"glTF")

    gltf, _ = vrm.read_glb(
        main.job_manager.get_job(job["job_id"]).model_path("vrm")
    )
    assert vrm.validate(gltf) == []
    assert gltf["extensions"]["VRMC_vrm"]["meta"]["name"] == "momo"


def test_vrm_name_defaults_to_uploaded_filename(client):
    res = client.post(
        "/api/rig", files={"model": ("momo_tpose.glb", GLB_HEADER, "model/gltf-binary")}
    )
    job = _wait_completed(client, res.json()["job_id"])
    gltf, _ = vrm.read_glb(main.job_manager.get_job(job["job_id"]).model_path("vrm"))
    assert gltf["extensions"]["VRMC_vrm"]["meta"]["name"] == "momo_tpose"


def test_vrm_can_be_disabled(client):
    res = client.post(
        "/api/rig",
        files={"model": ("m.glb", GLB_HEADER, "model/gltf-binary")},
        data={"params": json.dumps({"vrm": False})},
    )
    job = _wait_completed(client, res.json()["job_id"])
    assert job["has_vrm"] is False
    assert client.get(f"/api/rig/jobs/{job['job_id']}/download?format=vrm").status_code == 404


def test_vrm_failure_does_not_fail_the_job(client, monkeypatch):
    """VRM化に失敗してもリグ済みGLBは使えるので job は completed のままにする。"""
    def broken_rig(input_glb, output_glb, params, work_dir, timeout_sec):
        output_glb.write_bytes(GLB_HEADER)  # ボーンが無い=VRMにできない
        return RigResult(output_path=output_glb, preview_path=None, summary={}, warnings=[])

    monkeypatch.setattr(client.engine, "rig", broken_rig)
    res = client.post("/api/rig", files={"model": ("m.glb", GLB_HEADER, "model/gltf-binary")})
    job = _wait_completed(client, res.json()["job_id"])

    assert job["status"] == "completed"
    assert job["has_vrm"] is False
    assert any("VRM" in w for w in job["warnings"])
    # GLB のダウンロードは引き続きできる
    assert client.get(f"/api/rig/jobs/{job['job_id']}/download?format=glb").status_code == 200


def test_unknown_download_format(client):
    res = client.post("/api/rig", files={"model": ("m.glb", GLB_HEADER, "model/gltf-binary")})
    job_id = res.json()["job_id"]
    _wait_completed(client, job_id)
    assert client.get(f"/api/rig/jobs/{job_id}/download?format=fbx").status_code == 400


def test_unknown_job_returns_404(client):
    assert client.get("/api/rig/jobs/does-not-exist").status_code == 404
    assert client.delete("/api/rig/jobs/does-not-exist").status_code == 404


def test_history_is_reloaded_from_disk(client):
    res = client.post("/api/rig", files={"model": ("m.glb", GLB_HEADER, "model/gltf-binary")})
    job_id = res.json()["job_id"]
    _wait_completed(client, job_id)

    main.job_manager.jobs.clear()
    main.job_manager.load_history()
    assert job_id in main.job_manager.jobs
    assert main.job_manager.jobs[job_id].status == "completed"


# --- ポーズ適用 (計画書 §7 R3-3) ---------------------------------------------


def _completed(client, params=None):
    res = client.post(
        "/api/rig",
        files={"model": ("m.glb", GLB_HEADER, "model/gltf-binary")},
        data={"params": json.dumps(params)} if params else None,
    )
    return _wait_completed(client, res.json()["job_id"])


def test_pose_endpoint_returns_posed_glb(client):
    job = _completed(client)
    res = client.post(
        f"/api/rig/jobs/{job['job_id']}/pose",
        json={"pose": {"LeftUpperArm": [0, 0, -70]}},
    )
    assert res.status_code == 200
    assert res.content.startswith(b"glTF")

    # 元GLBより手が下がっていること(ポーズが実際に効いている)
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        posed_path = Path(tmp) / "posed.glb"
        posed_path.write_bytes(res.content)
        posed, _ = vrm.read_glb(posed_path)
        original, _ = vrm.read_glb(main.job_manager.get_job(job["job_id"]).model_path("glb"))

    def hand_y(gltf):
        index = next(i for i, n in enumerate(gltf["nodes"]) if n["name"] == "LeftHand")
        return vrm.world_translations(gltf)[index][1]

    assert hand_y(posed) < hand_y(original)


def test_pose_endpoint_supports_vrm_format(client):
    job = _completed(client)
    res = client.post(
        f"/api/rig/jobs/{job['job_id']}/pose",
        json={"pose": {"Head": [0, 15, 0]}, "format": "vrm"},
    )
    assert res.status_code == 200
    assert res.content.startswith(b"glTF")


@pytest.mark.parametrize(
    "body",
    [
        {"pose": {"NoSuchBone": [0, 0, 0]}},
        {"pose": {"Head": [1, 2]}},
        {"pose": "not-an-object"},
    ],
)
def test_pose_endpoint_rejects_bad_pose(client, body):
    job = _completed(client)
    res = client.post(f"/api/rig/jobs/{job['job_id']}/pose", json=body)
    assert res.status_code == 400


def test_pose_endpoint_rejects_bad_format(client):
    job = _completed(client)
    res = client.post(
        f"/api/rig/jobs/{job['job_id']}/pose", json={"pose": {}, "format": "fbx"}
    )
    assert res.status_code == 400


def test_pose_endpoint_unknown_job(client):
    assert client.post("/api/rig/jobs/nope/pose", json={"pose": {}}).status_code == 404


# --- 同梱モーション (計画書 §7 R3-2) -----------------------------------------


def test_list_motions(client):
    body = client.get("/api/motions").json()
    names = {m["name"] for m in body}
    assert {"idle", "wave", "bow", "walk"} <= names
    for m in body:
        assert m["duration"] > 0
        assert m["bones"]
        assert "tracks" not in m  # 一覧にキーフレームは含めない


def test_get_motion_returns_keyframes(client):
    body = client.get("/api/motions/wave").json()
    assert body["name"] == "wave"
    assert body["tracks"]["LeftLowerArm"][0]["rotation"]


def test_get_unknown_motion(client):
    assert client.get("/api/motions/moonwalk").status_code == 404


def test_download_with_motion_bakes_animation(client):
    job = _completed(client)
    res = client.get(f"/api/rig/jobs/{job['job_id']}/download?format=glb&motion=walk")
    assert res.status_code == 200

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "animated.glb"
        path.write_bytes(res.content)
        gltf, _ = vrm.read_glb(path)

    assert gltf["animations"][0]["name"] == "walk"
    assert gltf["animations"][0]["channels"]


def test_download_without_motion_has_no_animation(client):
    job = _completed(client)
    res = client.get(f"/api/rig/jobs/{job['job_id']}/download?format=glb")
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "plain.glb"
        path.write_bytes(res.content)
        gltf, _ = vrm.read_glb(path)
    assert "animations" not in gltf


def test_download_with_unknown_motion(client):
    job = _completed(client)
    res = client.get(f"/api/rig/jobs/{job['job_id']}/download?format=glb&motion=moonwalk")
    assert res.status_code == 404
