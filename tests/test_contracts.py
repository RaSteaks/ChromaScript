"""Contracts protect time, revisions, precision, and workspace boundaries."""

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from chromascript.errors import ChromaError
from chromascript.models import ColorPipeline, DeliveryPreset, Rational, RenderSettings, SceneSpec
from chromascript.service import Service
from chromascript.templates import TEMPLATES


@pytest.fixture
def service(tmp_path: Path) -> Service:
    return Service(tmp_path / "workspace")


@pytest.fixture
def scene(service: Service):
    project = service.create_project("test")["project"]
    return service.create_scene(project["id"], template_id="gradient")


def test_rational_and_timecode():
    assert Rational(numerator=120000, denominator=2002).ffmpeg == "60000/1001"
    s = RenderSettings(frames=300, fps=Rational(numerator=60000, denominator=1001))
    assert s.duration == pytest.approx(5.005)
    with pytest.raises(ValidationError):
        RenderSettings(timecode="00:01:00;00", fps=Rational(numerator=30000, denominator=1001))
    with pytest.raises(ValidationError):
        RenderSettings(timecode="00:00:00;00")
    assert RenderSettings(timecode="00:10:00;00", fps=Rational(numerator=30000, denominator=1001))


def test_shader_only_and_parameter_validation():
    spec = TEMPLATES["gradient"].model_dump()
    spec["code"] = "@compute @workgroup_size(1) fn shade() {}"
    with pytest.raises(ValidationError):
        SceneSpec.model_validate(spec)
    spec = TEMPLATES["gradient"].model_dump()
    spec["values"] = {"exposure": float("nan")}
    with pytest.raises(ValidationError):
        SceneSpec.model_validate(spec)
    spec["values"] = {"unknown": 1}
    with pytest.raises(ValidationError):
        SceneSpec.model_validate(spec)


def test_invalid_delivery_combinations():
    with pytest.raises(ValidationError):
        DeliveryPreset(
            id="bad",
            name="bad",
            family="test",
            container="mp4",
            codec="libx264",
            pixel_format="yuv420p",
            bit_depth=8,
            color=ColorPipeline(output="PQ"),
        )
    with pytest.raises(ValidationError):
        DeliveryPreset(
            id="bad",
            name="bad",
            family="test",
            container="png",
            codec="png",
            color=ColorPipeline(output="ACEScg"),
        )
    with pytest.raises(ValidationError):
        DeliveryPreset(
            id="bad",
            name="bad",
            family="test",
            container="mp4",
            codec="libx264",
            pixel_format="yuv420p",
            crf=18,
            bitrate_kbps=1000,
        )


def test_fixed_working_space_is_independent_of_delivery(service, scene):
    # All transports validate the same model; changing output cannot redefine code RGB.
    project_id = scene["record"]["project_id"]
    for output in ("sRGB", "Rec.709", "PQ", "ACEScct"):
        result = service.set_color_pipeline(project_id, ColorPipeline(output=output))
        assert result["pipeline"]["working_space"] == "ACEScg"
        info = result["configuration"]
        assert info["code_space"] == info["working_space"] == "ACEScg"
        assert info["working_transfer"] == "linear"
        assert info["preview"]["display"] == "sRGB"
    with pytest.raises(ValidationError):
        ColorPipeline(working_space="sRGB")
    for field in ("input_space", "working_space"):
        with pytest.raises(ValidationError):
            SceneSpec.model_validate({**TEMPLATES["gradient"].model_dump(), field: "sRGB"})


def test_revision_conflict_has_no_phantom_history(service, scene):
    original = scene["revision_id"]
    sid = scene["record"]["id"]
    changed = service.set_parameters(sid, {"exposure": 1.0}, original)
    with pytest.raises(ChromaError, match="latest"):
        service.set_parameters(sid, {"exposure": 2.0}, original)
    assert service.list_revisions(sid)["total"] == 2
    assert service.get_scene(sid)["scene"]["values"]["exposure"] == 1.0
    restored = service.restore_revision(sid, original, changed["revision_id"])
    assert restored["scene"]["values"] == {}


def test_idempotency_snapshot_freeze_and_conflict(service, scene):
    sid = scene["record"]["id"]
    settings = RenderSettings(width=32, height=32, frames=2)
    a = service.submit_delivery(sid, settings, ["exr-16-zip"], "same")["job"]
    b = service.submit_delivery(sid, settings, ["exr-16-zip"], "same")["job"]
    assert a["id"] == b["id"]
    with pytest.raises(ChromaError, match="different"):
        service.submit_delivery(
            sid, settings.model_copy(update={"frames": 3}), ["exr-16-zip"], "same"
        )
    service.set_parameters(sid, {"exposure": 2.0}, scene["revision_id"])
    assert service.store.get("snapshot", a["id"])["scene"]["values"] == {}


def test_frame_preview_retains_caller_timebase(service, scene):
    job = service.render_frame(
        scene["record"]["id"], frame=48, width=32, height=32, fps=Rational(numerator=24)
    )["job"]
    assert job["settings"]["fps"]["numerator"] == 24
    assert job["settings"]["start_frame"] == 48


def test_archive_and_cancel_resume(service, scene):
    sid = scene["record"]["id"]
    pid = scene["record"]["project_id"]
    job = service.render_frame(sid, width=32, height=32)["job"]
    with pytest.raises(ChromaError, match="cancel"):
        service.archive_project(pid)
    assert service.cancel_job(job["id"])["job"]["status"] == "cancelled"
    assert service.resume_job(job["id"])["job"]["status"] == "queued"
    service.cancel_job(job["id"])
    service.archive_project(pid)
    with pytest.raises(ChromaError, match="Restore"):
        service.create_scene(pid)
    service.archive_project(pid, False)


def test_workspace_and_disk_guards(service, scene, tmp_path):
    with pytest.raises(ChromaError):
        service.store.file("../outside")
    outside = tmp_path / "external"
    outside.mkdir()
    (service.store.root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ChromaError):
        service.store.file("link/secret")
    usage = __import__("shutil").disk_usage(service.store.root)
    with patch(
        "chromascript.service.shutil.disk_usage",
        return_value=type(usage)(usage.total, usage.total, 0),
    ):
        with pytest.raises(ChromaError, match="space"):
            service.render_frame(scene["record"]["id"], width=32, height=32)


@pytest.mark.parametrize(
    "operation", ["render_frame", "render_keyframes", "render_preview", "render_sequence"]
)
def test_render_entrypoints_honor_explicit_revision(service, scene, operation):
    # A model can export an approved revision after another client changes the scene.
    sid, original = scene["record"]["id"], scene["revision_id"]
    service.set_parameters(sid, {"exposure": 2.0}, original)
    args = {"scene_id": sid, "revision_id": original}
    if operation == "render_frame":
        args.update(width=32, height=32)
    else:
        args["settings"] = RenderSettings(width=32, height=32, frames=2)
    if operation == "render_keyframes":
        args["frame_indices"] = [0, 1]
    job = getattr(service, operation)(**args)["job"]
    assert job["revision_id"] == original
    assert service.store.get("snapshot", job["id"])["scene"]["values"] == {}


@pytest.mark.parametrize("container,depth", [("png", 32), ("tiff", 12), ("dpx", 8)])
def test_integer_sequence_rejects_illegal_depth(container, depth):
    with pytest.raises(ValidationError, match="bit depth"):
        DeliveryPreset(
            id="invalid-depth",
            name="invalid",
            family="integer",
            container=container,
            codec=container,
            bit_depth=depth,
            color=ColorPipeline(output="sRGB"),
        )
