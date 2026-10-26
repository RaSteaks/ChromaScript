"""Acceleration decisions are explicit, measured and frozen across all interfaces."""

import pytest
from pydantic import ValidationError

from chromascript import motion, motion_video_plan
from chromascript.errors import ChromaError
from chromascript.models import MotionRenderSettings
from chromascript.motion import MotionOutput, blank_motion
from chromascript.operations import execute
from chromascript.project_storage import migrate
from chromascript.service import Service


@pytest.mark.parametrize("field,value", [("video_processing", "fast"), ("video_encoder", True), ("video_quality", "ultra")])
def test_acceleration_models_reject_unknown_modes(field, value):
    with pytest.raises(ValidationError):
        MotionOutput(**{field: value})


def test_automatic_fallback_and_explicit_hardware_failure(monkeypatch):
    monkeypatch.setattr(motion_video_plan, "available_encoders", lambda: {"h264_videotoolbox"})
    monkeypatch.setattr(motion_video_plan, "hardware_probe", lambda *args: {"available": False, "reason": "test unavailable"})
    settings = MotionRenderSettings(width=1920, height=1080, frames=3)
    output = MotionOutput(master_bit_depth=None, video="srgb")
    plan = motion_video_plan.resolve_video_execution("three-float-v1", output, settings)
    assert plan.processing == "gpu" and plan.pixel_format == "nv12"
    assert plan.preset.codec == "libx264" and plan.fallback_reason == "test unavailable"
    with pytest.raises(ChromaError, match="test unavailable"):
        motion_video_plan.resolve_video_execution("three-float-v1", output.model_copy(update={"video_encoder": "hardware"}), settings)


def test_master_processing_and_reference_quality_remain_explicit():
    settings = MotionRenderSettings(width=1920, height=1080, frames=3)
    for quality, speed, crf in [("quality", "slow", 18), ("balanced", "medium", 20), ("speed", "fast", 23)]:
        output = MotionOutput(video="srgb", video_encoder="software", video_quality=quality)
        plan = motion_video_plan.resolve_video_execution("three-float-v1", output, settings)
        assert plan.processing == "reference" and plan.pixel_format is None
        assert (plan.speed, plan.preset.crf) == (speed, crf)
    output = MotionOutput(master_bit_depth=None, video="srgb", video_processing="reference", video_encoder="software")
    assert motion_video_plan.resolve_video_execution("three-float-v1", output, settings).processing == "reference"


def test_hdr_and_prores_gpu_formats_do_not_reduce_selected_precision():
    settings = MotionRenderSettings(width=64, height=64, frames=3)
    for video, pixel_format in [("pq", "p010le"), ("hlg", "p010le"), ("prores-hq-srgb", "p210le"), ("prores-4444-acescct", "p416le")]:
        output = MotionOutput(master_bit_depth=None, video=video, video_encoder="software")
        plan = motion_video_plan.resolve_video_execution("three-float-v1", output, settings)
        assert plan.pixel_format == pixel_format


@pytest.mark.parametrize("partitioned", [False, True])
@pytest.mark.parametrize("initial_available,retry_available", [(False, False), (False, True), (True, False)])
def test_submission_reuses_frozen_video_execution_after_restart(
    tmp_path, monkeypatch, partitioned, initial_available, retry_available,
):
    monkeypatch.setattr(motion, "motion_environment", lambda: {"render_available": True})
    monkeypatch.setattr(motion, "motion_identity", lambda: {"test_runtime": 1})
    for module in (motion, motion_video_plan):
        monkeypatch.setattr(module, "available_encoders", lambda: {"libx264", "h264_videotoolbox"})
    available = initial_available
    probes = []

    def probe(*args):
        probes.append(args)
        # Native diagnostics change between runs even for the same failure.
        return {"available": available, "reason": f"[h264_videotoolbox @ 0x{len(probes):x}] busy"}

    monkeypatch.setattr(motion_video_plan, "hardware_probe", probe)
    service = Service(tmp_path / "workspace")
    project = service.create_project("Frozen video plan")["project"]["id"]
    if partitioned:
        migrate(service.store)
    spec = blank_motion().model_copy(update={"width": 64, "height": 64, "frames": 3})
    saved = service.create_motion_scene(project, spec=spec)
    args = {
        "scene_id": saved["record"]["id"], "expected_revision_id": saved["revision_id"],
        "idempotency_key": "same-request", "export_directory": str(tmp_path / "delivery"),
        "output": MotionOutput(master_bit_depth=None, video="srgb").model_dump(),
    }
    original = execute(service, "submit_motion_render", args)
    snapshot = service.store.get("snapshot", original["job"]["id"])
    if partitioned:
        # Project-local requests remain authoritative if the derived global
        # index was lost during restart or restoring a portable project.
        with service.store.connect() as db:
            db.execute("DELETE FROM requests WHERE key=?", (args["idempotency_key"],))
    available = retry_available
    restarted = Service(service.store.root)
    retried = execute(restarted, "submit_motion_render", args)
    assert retried == original
    assert retried["video_execution"]["hardware"] is initial_available
    assert restarted.store.get("snapshot", original["job"]["id"]) == snapshot
    assert len(probes) == 1

    # Reusing native evidence must not bypass the rest of the frozen recipe.
    for changes in (
        {"output": {**args["output"], "video_quality": "speed"}},
        {"export_directory": str(tmp_path / "other-delivery")},
    ):
        with pytest.raises(ChromaError) as conflict:
            execute(restarted, "submit_motion_render", {**args, **changes})
        assert conflict.value.code == "idempotency_conflict"
    assert len(probes) == 1
    assert restarted.list_jobs(project_id=project)["total"] == 1

    fresh = execute(restarted, "submit_motion_render", {**args, "idempotency_key": "new-request"})
    assert fresh["job"]["id"] != original["job"]["id"]
    assert fresh["video_execution"]["hardware"] is retry_available
    assert len(probes) == 2
