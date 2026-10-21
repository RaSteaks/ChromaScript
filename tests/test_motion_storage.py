"""Single-copy motion storage, recovery and on-demand ZIP transport contracts."""

import asyncio
import errno
import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from starlette.requests import ClientDisconnect
from typer.testing import CliRunner

from chromascript.api import ArchiveResponse, create_app
from chromascript.artifacts import archive_members, artifact_path, stream_zip
from chromascript.cli import app as cli
from chromascript.errors import ChromaError
from chromascript.exports import RenderBundle, motion_bundle
from chromascript.images import file_hash
from chromascript.mcp_server import create_mcp
from chromascript.motion import MotionOutput, templates
from chromascript.operations import execute
from chromascript.service import Service
from chromascript.worker import run


@pytest.fixture
def float_job(tmp_path, monkeypatch):
    # Failure injection uses deterministic float transport. Real GPU/codec
    # precision remains covered by test_motion's native integration scenarios.
    # Import before patching the source module so teardown restores the real
    # worker identity, independently of which integration test ran first.
    __import__("chromascript.motion_worker")
    for module in ("motion", "motion_worker", "service"):
        monkeypatch.setattr(f"chromascript.{module}.motion_identity", lambda: {"test": 1})
    monkeypatch.setattr(
        "chromascript.motion.motion_environment", lambda: {"render_available": True}
    )
    service = Service(tmp_path / "workspace")
    project = service.create_project("single copy")["project"]["id"]
    spec = templates()["radiance"].model_copy(update={"width": 16, "height": 16, "frames": 3})
    saved = service.create_motion_scene(project, spec=spec)
    job = service.submit_motion_render(
        saved["record"]["id"],
        saved["revision_id"],
        "single-copy",
        export_directory=str(tmp_path / "export"),
        output=MotionOutput(video="none"),
    )["job"]
    bundle = motion_bundle(job, service.store.get("snapshot", job["id"]))
    calls = []
    gpu = {"format": "RGBA32F", "renderer": "test"}

    def render(spec, directory, indices, on_frame, expected_gpu=None, **kwargs):
        calls.append(list(indices))
        if expected_gpu is not None and expected_gpu != gpu:
            raise ChromaError("gpu_changed", "GPU 后端已改变")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "gpu.json").write_text(json.dumps(gpu))
        for index in indices:
            # Model the actual transport raster, including final supersampling.
            scale = kwargs.get("render_scale", 1)
            pixels = np.empty((spec.height * scale, spec.width * scale, 4), dtype="<f4")
            # Choose negative radiance that remains negative after the Rec.709→AP1 matrix.
            pixels[:] = [-4.125, 4.125 + index, 0.1234567, 0.5]
            raw = directory / f"frame-{index}.f32"
            raw.write_bytes(pixels.tobytes())
            on_frame(index, raw)
            raw.unlink(missing_ok=True)
        return gpu

    monkeypatch.setattr("chromascript.motion_worker.render_float_frames", render)
    return service, job, bundle, calls, gpu


def archive_of(service, job):
    return next(a for a in service.get_job(job["id"])["artifacts"] if a["kind"] == "master-archive")


def test_single_copy_and_original_artifacts_outside_workspace(float_job):
    service, job, bundle, _, _ = float_job
    run(service.store.root, job["id"])
    result = service.get_job(job["id"])
    assert result["job"]["status"] == "completed", result["job"]["error"]
    assert len(list(bundle.target.rglob("*.exr"))) == 3
    assert not list(service.store.root.glob("jobs/**/*.exr"))
    assert not list(bundle.target.parent.rglob("*.zip"))
    assert not bundle.staging.exists()
    master = next(a for a in result["artifacts"] if a["kind"] == "master-frame")
    assert service.inspect_frame(master["id"])["negative_rgb_values"] > 0
    assert service.sample_pixels(master["id"], [[0, 0]])["color_space"] == "ACEScg"
    assert service.compare_frames(master["id"], master["id"])["max_absolute_error"] == 0
    assert service.validate_delivery_output(job["id"])["verification"]["passed"]
    archive = archive_of(service, job)
    assert archive["representation"] == "zip-stream" and archive["size_bytes"] is None
    assert service.get_artifact(archive["id"])["local_path"] is None


def test_publication_does_not_change_the_destination_request_identity(float_job):
    service, job, bundle, _, _ = float_job
    run(service.store.root, job["id"])
    assert service.get_job(job["id"])["job"]["status"] == "completed"
    # Creating the formerly absent destination must not change the frozen volume
    # identity or turn a transport retry into a conflicting/new render request.
    repeated = service.submit_motion_render(job["scene_id"], job["revision_id"], "single-copy",
        export_directory=str(bundle.target.parent), output=MotionOutput(video="none"))["job"]
    assert repeated["id"] == job["id"]


def test_failed_publication_reuses_only_valid_masters(float_job, monkeypatch):
    service, job, bundle, calls, _ = float_job
    publish = RenderBundle.publish

    def full_disk(*args, **kwargs):
        raise OSError(errno.ENOSPC, "test export disk full")

    monkeypatch.setattr(RenderBundle, "publish", full_disk)
    run(service.store.root, job["id"])
    assert service.get_job(job["id"])["job"]["status"] == "failed"
    assert not bundle.target.exists()
    retained = bundle.output / "masters/00000000.exr"
    retained_hash, retained_time = file_hash(retained), retained.stat().st_mtime_ns
    # Stored pixel inspection works even while an export is resumable in staging.
    master = next(
        a
        for a in service.get_job(job["id"])["artifacts"]
        if a["kind"] == "master-frame" and a["metadata"]["frame"] == 0
    )
    assert artifact_path(service.store, master) == retained
    assert service.inspect_frame(master["id"])["high_rgb_values"] > 0
    (bundle.output / "masters/00000001.exr").write_bytes(b"corrupted cached frame")
    monkeypatch.setattr(RenderBundle, "publish", publish)
    service.resume_job(job["id"])
    run(service.store.root, job["id"])
    assert service.get_job(job["id"])["job"]["status"] == "completed"
    assert calls == [[0, 1, 2], [1]]
    retained = bundle.target / "masters/00000000.exr"
    assert file_hash(retained) == retained_hash and retained.stat().st_mtime_ns == retained_time
    assert artifact_path(service.store, master) == retained


def test_cancel_retains_verified_frame_and_checks_gpu_before_resume(float_job, monkeypatch):
    service, job, bundle, _, gpu = float_job
    from chromascript import motion_worker

    original = motion_worker.render_float_frames

    def cancel(spec, directory, indices, on_frame, **kwargs):
        original(spec, directory, [indices[0]], on_frame, **kwargs)
        service.cancel_job(job["id"])
        raise ChromaError("cancelled", "test cancelled")

    monkeypatch.setattr(motion_worker, "render_float_frames", cancel)
    run(service.store.root, job["id"])
    assert service.get_job(job["id"])["job"]["status"] == "cancelled"
    assert (bundle.output / "masters/00000000.exr").is_file()
    assert not bundle.target.exists()
    gpu["renderer"] = "changed"
    monkeypatch.setattr(motion_worker, "render_float_frames", original)
    service.resume_job(job["id"])
    run(service.store.root, job["id"])
    assert service.get_job(job["id"])["job"]["error"]["code"] == "gpu_changed"
    gpu["renderer"] = "test"
    service.resume_job(job["id"])
    run(service.store.root, job["id"])
    assert service.get_job(job["id"])["job"]["status"] == "completed"


def test_crash_after_rename_recovers_without_rewriting_outputs(float_job, monkeypatch):
    service, job, bundle, _, _ = float_job
    publish = RenderBundle.publish

    def crash_after_publish(*args, **kwargs):
        publish(*args, **kwargs)
        raise RuntimeError("test crash between rename and job completion")

    monkeypatch.setattr(RenderBundle, "publish", crash_after_publish)
    run(service.store.root, job["id"])
    before = service.get_job(job["id"])
    assert before["job"]["status"] == "failed" and bundle.target.is_dir()
    report = bundle.target / "verification.json"
    report_hash, report_time = file_hash(report), report.stat().st_mtime_ns
    service.resume_job(job["id"])
    monkeypatch.setattr(
        "chromascript.motion_worker.render_float_frames",
        lambda *a, **k: pytest.fail("published sequence must not re-enter the renderer"),
    )
    run(service.store.root, job["id"])
    after = service.get_job(job["id"])
    assert after["job"]["status"] == "completed"
    assert set(before["job"]["artifacts"]).issubset(after["job"]["artifacts"])
    assert file_hash(report) == report_hash and report.stat().st_mtime_ns == report_time
    assert archive_of(service, job)["representation"] == "zip-stream"
    assert not bundle.staging.exists()


def test_missing_gpu_identity_cannot_certify_existing_cache(float_job, monkeypatch):
    service, job, bundle, calls, _ = float_job

    def fail_publish(*args, **kwargs):
        raise OSError(errno.ENOSPC, "test export disk full")

    monkeypatch.setattr(RenderBundle, "publish", fail_publish)
    run(service.store.root, job["id"])
    (bundle.work / "gpu-verified.json").unlink()
    service.resume_job(job["id"])
    run(service.store.root, job["id"])
    assert service.get_job(job["id"])["job"]["error"]["code"] == "gpu_changed"
    assert calls == [[0, 1, 2]]
    assert len(list((bundle.output / "masters").glob("*.exr"))) == 3


def test_zip64_stream_http_cli_and_mcp_without_server_archive(float_job, tmp_path, monkeypatch):
    service, job, bundle, _, _ = float_job
    run(service.store.root, job["id"])
    artifact = archive_of(service, job)
    web = create_app(service.store.root)
    client = TestClient(web)
    session = client.get("/api/session").json()
    url = f"/api/artifacts/{artifact['id']}?download=1"
    response = client.get(url)
    assert response.status_code == 200 and response.headers["accept-ranges"] == "none"
    assert "content-length" not in response.headers
    assert client.get(url, headers={"Range": "bytes=0-1023"}).status_code == 416

    def verify(data):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert archive.testzip() is None
            assert len(archive.namelist()) == 4
            assert all(info.extract_version >= 45 for info in archive.infolist())
            for name in archive.namelist():
                assert archive.read(name) == (bundle.target / name).read_bytes()

    verify(response.content)
    monkeypatch.setattr(
        "chromascript.cli.call", lambda root, name, args: execute(service, name, args)
    )
    target = tmp_path / "cli-download.zip"
    result = CliRunner().invoke(
        cli,
        [
            "call",
            "export_artifact",
            "--arguments",
            json.dumps(
                {
                    "artifact_id": artifact["id"],
                    "destination": str(target),
                }
            ),
        ],
    )
    assert result.exit_code == 0, result.output
    verify(target.read_bytes())

    # Exercise the real MCP adapter against the same ASGI service contract.
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "chromascript.mcp_server.endpoint",
        lambda root: {
            "url": "http://testserver",
            "token": session["token"],
        },
    )
    monkeypatch.setattr(
        "chromascript.mcp_server.httpx.AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.ASGITransport(app=web),
            **kwargs,
        ),
    )
    mcp_target = tmp_path / "mcp-download.zip"

    async def export():
        async with Client(create_mcp(service.store.root)) as mcp:
            reply = await mcp.call_tool(
                "chromascript.export_artifact",
                {
                    "request": {
                        "artifact_id": artifact["id"],
                        "destination": str(mcp_target),
                    }
                },
            )
            assert not reply.is_error, reply

    asyncio.run(export())
    verify(mcp_target.read_bytes())
    assert not list(bundle.target.parent.rglob("*.zip"))
    assert not list(service.store.root.rglob("*.zip"))
    with pytest.raises(ChromaError, match="new destination"):
        service.export_artifact(artifact["id"], str(target))


def test_modified_or_linked_delivery_is_never_overwritten(float_job, tmp_path):
    service, job, bundle, _, _ = float_job
    run(service.store.root, job["id"])
    artifact = archive_of(service, job)
    members = archive_members(service.store, artifact)
    path = bundle.target / "masters/00000000.exr"
    contents = path.read_bytes()
    path.write_bytes(contents[:-1] + bytes([contents[-1] ^ 1]))
    with pytest.raises(ChromaError, match="下载时发生变化"):
        b"".join(stream_zip(members))
    with pytest.raises(ChromaError):
        service.export_artifact(artifact["id"], str(tmp_path / "broken.zip"))
    assert not (tmp_path / "broken.zip").exists()
    with pytest.raises(ChromaError, match="未覆盖"):
        bundle.prepare()
    assert path.read_bytes() != contents
    path.unlink()
    elsewhere = tmp_path / "outside.exr"
    elsewhere.write_bytes(contents)
    path.symlink_to(elsewhere)
    with pytest.raises(ChromaError, match="符号链接"):
        archive_members(service.store, artifact)


def test_destination_conflict_and_owned_staging_checks(float_job, tmp_path):
    service, job, bundle, _, _ = float_job
    bundle.target.mkdir(parents=True)
    (bundle.target / "user.txt").write_text("keep user data")
    run(service.store.root, job["id"])
    assert service.get_job(job["id"])["job"]["error"]["code"] == "export_conflict"
    assert (bundle.target / "user.txt").read_text() == "keep user data"
    other = RenderBundle(tmp_path / "other/bundle", tmp_path / "other", bundle.identity)
    other.staging.mkdir(parents=True)
    (other.staging / "owner.json").write_text('{"job_id":"somebody-else"}')
    with pytest.raises(ChromaError, match="身份不符"):
        other.prepare()


def test_disk_precheck_uses_destination_volume(float_job, monkeypatch):
    service, job, bundle, _, _ = float_job
    bundle.target.parent.mkdir()
    seen = []

    def disk_usage(path):
        seen.append(path)
        return SimpleNamespace(free=0 if path == bundle.target.parent else 10**12)

    monkeypatch.setattr("chromascript.motion.shutil.disk_usage", disk_usage)
    with pytest.raises(ChromaError, match="导出盘"):
        service.submit_motion_render(
            job["scene_id"],
            job["revision_id"],
            "full-export-volume",
            export_directory=str(bundle.target.parent),
            output=MotionOutput(video="none"),
        )
    assert bundle.target.parent in seen
    assert len(service.store.list("job")) == 1


def test_zip_stream_is_bounded_and_closes_on_disconnect(tmp_path, monkeypatch):
    path = tmp_path / "frame.exr"
    path.write_bytes(b"x" * (3 * 1024**2))
    chunks = stream_zip([("frame.exr", path, file_hash(path))])
    assert max(map(len, chunks)) <= 1024**2
    closed = False

    def source():
        nonlocal closed
        try:
            yield b"header"
            pytest.fail("a disconnected response must not read the next frame")
        finally:
            closed = True

    async def disconnected():
        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        with pytest.raises(ClientDisconnect):
            await ArchiveResponse(source(), "masters.zip")(
                {"type": "http", "asgi": {"spec_version": "2.4"}},
                receive,
                send,
            )

    asyncio.run(disconnected())
    assert closed
    # Exercise the ZIP64 archive directory with small files, without allocating GiBs.
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 1024)
    data = b"".join(stream_zip([("frame.exr", path, file_hash(path))]))
    assert b"PK\x06\x06" in data
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert hashlib.sha256(archive.read("frame.exr")).hexdigest() == file_hash(path)


def test_thumbnail_no_hidden_reserve(float_job, monkeypatch):
    service, job, _, _, _ = float_job
    # Enough for this tiny sequence, below the unrelated image API's 128 MiB reserve.
    monkeypatch.setattr("shutil.disk_usage", lambda _: SimpleNamespace(free=100 * 1024**2))
    run(service.store.root, job["id"])
    result = service.get_job(job["id"])
    assert result["job"]["status"] == "completed", result["job"]["error"]
