"""Delivery folders preserve immutable scene/output identity and user-owned files."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from chromascript.cli import app
from chromascript.errors import ChromaError
from chromascript.exports import bundle_name, export_directory, publish_bundle
from chromascript.images import file_hash, read_image
from chromascript.models import DeliveryConfiguration, RenderSettings
from chromascript.operations import catalogue, execute
from chromascript.service import Service
from chromascript.worker import run


def test_directory_discovery_accepts_unregistered_writable_folders(tmp_path):
    root = tmp_path / "workspace"
    external = tmp_path / "external"
    external.mkdir()
    service = Service(root)
    options = service.get_delivery_export_options()
    assert options["directory"] == str(root / "exports")
    assert options["allowed_roots"] == [] and options["unrestricted"]
    assert not (root / "exports").exists()
    assert service.get_delivery_export_options(str(external / "新目录"))["directory"] == str(
        external / "新目录"
    )
    assert not (external / "新目录").exists()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    (root / "file").write_text("do not replace")
    assert service.get_delivery_export_options(str(outside))["directory"] == str(outside)
    assert service.get_delivery_export_options(str(root / "escape" / "video"))["directory"] == str(
        outside / "video"
    )
    for value in [
        "",
        "relative/path",
        "bad\x00path",
        str(root / "file" / "video"),
    ]:
        with pytest.raises(ChromaError):
            service.get_delivery_export_options(value)
    assert (root / "file").read_text() == "do not replace"
    assert bundle_name("../测试 / 场景:*", "abc") == "测试-场景-abc"


def test_destination_is_frozen_idempotent_and_shared_with_cli(tmp_path, monkeypatch):
    service = Service(tmp_path / "workspace")
    # Submission tests need a stable adapter identity, not rendered pixels.
    service._renderer = SimpleNamespace(info={"backend_type": "test"})
    project = service.create_project("exports")["project"]
    scene = service.create_scene(project["id"], template_id="gradient")
    config = service.get_delivery_options(DeliveryConfiguration(container="exr"))["configuration"]
    args = {
        "scene_id": scene["record"]["id"],
        "settings": {"width": 32, "height": 32, "frames": 1},
        "configurations": [config],
        "idempotency_key": "export-once",
        "export_directory": str(tmp_path / "选择的外部文件夹"),
    }
    first = execute(service, "submit_configured_delivery", args)["job"]
    again = execute(service, "submit_configured_delivery", args)["job"]
    assert again["id"] == first["id"] and again["export_path"] == first["export_path"]
    assert Path(first["export_path"]).parent == Path(args["export_directory"])
    assert not Path(first["export_path"]).exists() and not first["export_completed"]
    snapshot = service.store.get("snapshot", first["id"])
    assert snapshot["export_directory"] == args["export_directory"]
    assert snapshot["export_root"] == args["export_directory"]
    with pytest.raises(ChromaError, match="different render settings"):
        execute(
            service,
            "submit_configured_delivery",
            {**args, "export_directory": str(service.store.root / "other")},
        )
    with pytest.raises(ChromaError, match="绝对"):
        execute(
            service,
            "submit_configured_delivery",
            {**args, "idempotency_key": "invalid", "export_directory": "relative"},
        )
    assert len(service.store.list("job")) == 1
    monkeypatch.setattr(
        "chromascript.cli.call", lambda root, name, arguments: execute(service, name, arguments)
    )
    cli = CliRunner().invoke(
        app,
        [
            "render",
            scene["record"]["id"],
            "--container",
            "exr",
            "--width",
            "32",
            "--height",
            "32",
            "--frames",
            "1",
            "--export-directory",
            args["export_directory"],
        ],
    )
    assert cli.exit_code == 0, cli.output
    cli_job = json.loads(cli.output)["job"]
    assert Path(cli_job["export_path"]).parent == Path(args["export_directory"])
    assert cli_job["export_path"] != first["export_path"]
    schema = {op["name"]: op["inputSchema"] for op in catalogue()}
    for name in ["submit_delivery", "submit_configured_delivery", "get_delivery_export_options"]:
        assert "export_directory" in schema["chromascript." + name]["properties"]


def test_bundle_publication_retry_conflict_and_cancel(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"verified video contents")
    target = tmp_path / "exports" / "scene-job"
    files = {"video.mp4": source, "frames/00000000.exr": source}
    identity = {"job_id": "job", "snapshot_sha256": "immutable"}
    publish_bundle(files, target, tmp_path, identity, lambda: None)
    assert (target / "video.mp4").read_bytes() == source.read_bytes()
    manifest = json.loads((target / "delivery.json").read_text())
    assert manifest["files"]["video.mp4"]["sha256"] == file_hash(source)
    assert publish_bundle(files, target, tmp_path, identity, lambda: None) == target
    # A user's edited deliverable must survive retries, including intact other files.
    (target / "video.mp4").write_bytes(b"user edit")
    with pytest.raises(ChromaError, match="未覆盖"):
        publish_bundle(files, target, tmp_path, identity, lambda: None)
    assert (target / "video.mp4").read_bytes() == b"user edit"
    assert (target / "frames/00000000.exr").read_bytes() == source.read_bytes()
    empty = tmp_path / "exports" / "existing-empty"
    empty.mkdir()
    with pytest.raises(ChromaError, match="未覆盖"):
        publish_bundle(files, empty, tmp_path, identity, lambda: None)
    assert list(empty.iterdir()) == []
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ChromaError("cancelled", "cancelled")

    with pytest.raises(ChromaError, match="cancelled"):
        publish_bundle(files, tmp_path / "exports" / "cancelled", tmp_path, identity, cancelled)
    assert not (tmp_path / "exports" / "cancelled").exists()
    assert not list((tmp_path / "exports").glob(".chromascript-*"))


def test_publication_rechecks_changed_directory(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    parent = root / "destination"
    assert export_directory(str(parent)) == parent
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    parent.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ChromaError, match="路径发生变化"):
        publish_bundle({}, parent / "bundle", root, {"job_id": "job"}, lambda: None)
    assert not list(elsewhere.iterdir())


@pytest.mark.gpu
@pytest.mark.integration
def test_worker_exports_frozen_code_thumbnail_video_and_sequence(tmp_path, monkeypatch):
    external = tmp_path / "外部交付"
    external.mkdir()
    service = Service(tmp_path / "workspace")
    project = service.create_project("bundle")["project"]
    scene = service.create_scene(project["id"], template_id="gradient")
    job = service.submit_delivery(
        scene["record"]["id"],
        RenderSettings(width=32, height=32, frames=2, start_frame=12, samples=1),
        ["h264-review", "exr-16-zip"],
        "bundle",
        export_directory=str(external / "子文件夹"),
    )["job"]
    # Editing after queueing cannot change the code or parameters in the delivered bundle.
    spec = service.scene(scene["record"]["id"])[1]
    service.update_scene(
        scene["record"]["id"],
        spec.model_copy(update={"code": spec.code + "\n// New draft"}),
        scene["revision_id"],
    )

    # An export failure must not discard verified workspace outputs or claim success.
    def unavailable(*args, **kwargs):
        raise ChromaError("export_failed", "temporary destination failure")

    with monkeypatch.context() as patch:
        patch.setattr("chromascript.worker.publish_bundle", unavailable)
        with pytest.raises(ChromaError, match="temporary destination"):
            run(service.store.root, job["id"])
    failed = service.get_job(job["id"])
    assert failed["job"]["status"] == "failed" and not failed["job"]["export_completed"]
    assert any(a["kind"] == "video" for a in failed["artifacts"])
    assert not Path(job["export_path"]).exists()
    service.resume_job(job["id"])
    run(service.store.root, job["id"])
    completed = service.get_job(job["id"])
    assert completed["job"]["status"] == "completed" and completed["job"]["export_completed"]
    target = Path(job["export_path"])
    assert (target / "shader.wgsl").read_text() == scene["scene"]["code"]
    assert json.loads((target / "scene.json").read_text()) == scene["scene"]
    assert (target / "h264-review.mp4").stat().st_size > 0
    assert (target / "exr-16-zip/00000012.exr").exists()
    assert (target / "exr-16-zip/00000013.exr").exists()
    assert (target / "exr-16-zip/sequence.json").exists()
    preview = next(
        a
        for a in completed["artifacts"]
        if a["kind"] == "preview-image" and a["metadata"]["frame"] == 12
    )
    assert file_hash(target / "thumbnail.png") == file_hash(
        service.store.file(preview["relative_path"])
    )
    pixels, _ = read_image(target / "thumbnail.png")
    assert pixels.shape == (32, 32, 4)
    assert json.loads((target / "verification.json").read_text())["passed"]
    previous = {str(p.relative_to(target)): file_hash(p) for p in target.rglob("*") if p.is_file()}
    # Simulate retry after publishing but before persisting the completed job state.
    run(service.store.root, job["id"])
    assert previous == {
        str(p.relative_to(target)): file_hash(p) for p in target.rglob("*") if p.is_file()
    }
