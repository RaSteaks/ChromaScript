"""No-browser tests of scene-log encoding and sampling-aware disk admission."""

import json
import os
import platform
import time
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from chromascript.color import render_output
from chromascript.encoding import check_probe, command, encode
from chromascript.errors import ChromaError
from chromascript.models import DeliveryPreset, MotionRate, MotionRenderSettings
from chromascript.motion import MotionOutput, motion_video_preset, templates
from chromascript.motion_sampling import (
    checked_storage_sample,
    close_samples,
    recover_samples,
    sample_indices,
)
from chromascript.motion_storage import estimate_storage
from chromascript.prores import prores_capability
from chromascript.service import Service


@pytest.fixture
def context(tmp_path, monkeypatch):
    # Keep source execution out of Chrome. Codec tests still use real FFmpeg.
    for module in ("motion", "motion_sampling", "motion_worker", "service"):
        monkeypatch.setattr(f"chromascript.{module}.motion_identity", lambda: {"test": 1})
    monkeypatch.setattr("chromascript.motion.motion_environment", lambda: {"render_available": True})
    service = Service(tmp_path / "workspace")
    project = service.create_project("sample storage")["project"]["id"]
    spec = templates()["radiance"].model_copy(update={
        "width": 64, "height": 32, "frames": 24, "source_color_space": "ACEScg",
        "fps": MotionRate(numerator=24000, denominator=1001),
    })

    def frames(spec, directory, indices, render_scale=1, cancelled=None, **kwargs):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "gpu.json").write_text('{"format":"RGBA32F","renderer":"synthetic"}')
        (directory / "renderer.log").write_text("Synthetic transport; no browser launched.")
        for index in indices:
            if cancelled and cancelled():
                raise ChromaError("cancelled", "cancelled")
            raw = directory / f"frame-{index}.f32"
            image = np.ones((spec.height * render_scale, spec.width * render_scale, 4), "<f4")
            image[..., :3] = 0.18
            if index % 2:
                image[..., :3] = np.random.default_rng(index).uniform(0, 4, image[..., :3].shape)
            raw.write_bytes(image.tobytes())
            try:
                yield index, raw
            finally:
                raw.unlink(missing_ok=True)

    monkeypatch.setattr("chromascript.motion_sampling.iter_float_frames", frames)
    yield service, project, spec, tmp_path / "destination", frames
    close_samples(service.store)


def wait_sample(service, id):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        record = service.get_motion_render_estimate(id)["estimate"]
        if record["status"] != "running":
            return record
        time.sleep(0.02)
    pytest.fail("sampling did not finish")


def test_narrow_declaration_contract():
    preset = motion_video_preset("prores-4444-acescct")
    args = command(preset, MotionRenderSettings(width=64, height=32), Path("/tmp/unused.mov"))
    assert args[args.index("-c:v") + 1] == "prores_videotoolbox"
    assert args[args.index("-pix_fmt") + 1] == "p416le"
    assert "gbrp16le" in args[args.index("-vf") + 1]
    assert "yuv444p10le" not in " ".join(args)
    for update in ({"alpha": True}, {"bit_depth": 10}, {"profile": "5"}, {"data_range": "full"}):
        with pytest.raises(ValidationError):
            DeliveryPreset.model_validate({**preset.model_dump(), **update})
    # Ordinary legacy ACEScct recipes are still rejected by the native-tag gate.
    legacy = preset.model_copy(update={"color_metadata_policy": "standard"})
    with pytest.raises(ChromaError, match="标准视频标签"):
        command(legacy, MotionRenderSettings(), Path("/tmp/unused.mov"))


def test_no_percentage_reserves_or_duplicate_movie(context):
    service, project, _, destination, _ = context
    settings = MotionRenderSettings(width=3840, height=2160, frames=14400,
                                    fps=MotionRate(numerator=24))
    output = MotionOutput(master_bit_depth=None, video="prores-4444-acescct", render_scale=2)
    result = service.estimate_motion_render(settings, output=output, export_directory=str(destination))
    # Apple nominal payload scaled by exact frame count, not a padded percentage.
    expected = -(-Fraction(330_000_000 * 1001 * 4 * 14400, 30000 * 8).numerator //
                 Fraction(330_000_000 * 1001 * 4 * 14400, 30000 * 8).denominator)
    assert result["video_budget_bytes"] == expected
    assert result["master_overhead_budget_bytes"] == result["auxiliary_budget_bytes"] == 0
    assert result["estimated_output_bytes"] == expected
    assert result["destination_required_bytes"] == expected + result["workspace_required_bytes"]
    reference = service.estimate_motion_render(settings, output=output.model_copy(update={"video_processing": "reference"}), export_directory=str(destination))
    assert reference["destination_required_bytes"] == expected + 3840 * 2160 * 16 * 4 + reference["workspace_required_bytes"]
    assert not destination.exists()


def test_sample_uses_mean_not_max_and_counts_headers_once(context):
    service, project, spec, destination, _ = context
    output = MotionOutput(master_bit_depth=16, video="prores-hq-pq", render_scale=1)
    measured = {
        "sample_count": 2, "master_bytes": [100, 300], "video_packet_bytes": [20, 80],
        "video_table_bytes": 8, "video_fixed_bytes": 1000,
    }
    result = estimate_storage(service.store, spec.settings(), spec.engine, output, destination, project, measured)
    assert result["compressed_master_bytes"] == 200 * spec.frames
    assert result["video_budget_bytes"] == 50 * spec.frames + 4 * spec.frames + 1000
    assert result["estimated_output_bytes"] == 254 * spec.frames + 1000
    assert result["master_overhead_budget_bytes"] == 0


def test_full_resolution_sampling_cleanup_staleness_and_submit(context, monkeypatch):
    service, project, spec, destination, _ = context
    output = MotionOutput(master_bit_depth=16, render_scale=2)
    saved = service.create_motion_scene(project, spec=spec)
    reply = service.start_motion_render_estimate(project, spec, output, str(destination), "sample")
    id = reply["estimate"]["id"]
    result = wait_sample(service, id)
    assert result["status"] == "completed", result
    record = service.store.get("motion_estimate", id)
    values = record["measurements"]["master_bytes"]
    assert len(values) == 12 and len(set(values)) > 1
    assert result["budget"]["compressed_master_bytes"] == sum(values) * 2
    assert record["measurements"]["frame_indices"] == sample_indices(spec.frames)
    # Completion may be observed just before finally removes disposable files.
    close_samples(service.store)
    assert not list(destination.glob(".chromascript-sample-*"))
    assert service.store.list("job") == [] and service._motion_scene(saved["record"]["id"])[1] == spec
    assert checked_storage_sample(service.store, id, spec.model_copy(update={"name": "rename"}),
                                  output, destination, project)
    for changed in (
        spec.model_copy(update={"code": spec.code + "\n// changed"}),
        spec.model_copy(update={"frames": 25}),
        spec.model_copy(update={"source_color_space": "Linear Rec.2020"}),
    ):
        with pytest.raises(ChromaError, match="重新采样"):
            checked_storage_sample(service.store, id, changed, output, destination, project)
    assert service.start_motion_render_estimate(project, spec, output, str(destination), "sample")["estimate"]["id"] == id
    with pytest.raises(ChromaError):
        service.start_motion_render_estimate(project, spec.model_copy(update={"frames": 25}),
                                             output, str(destination), "sample")
    available = result["budget"]["destination_required_bytes"]
    monkeypatch.setattr("chromascript.motion_storage.shutil.disk_usage",
                        lambda _: SimpleNamespace(free=available))
    assert not service.estimate_motion_render(spec.settings(), output=output)["sufficient"]
    job = service.submit_motion_render(saved["record"]["id"], saved["revision_id"], "render",
        str(destination), output, storage_estimate_id=id)["job"]
    assert job["status"] == "queued"
    assert service.store.get("snapshot", job["id"])["storage_estimate"]["id"] == id


def test_cancel_and_busy_keep_drafts(context, monkeypatch):
    service, project, spec, destination, _ = context

    def blocked(spec, directory, indices, cancelled=None, **kwargs):
        while not cancelled():
            time.sleep(0.01)
        raise ChromaError("cancelled", "cancelled")
        yield  # Generator contract, without launching any renderer.

    monkeypatch.setattr("chromascript.motion_sampling.iter_float_frames", blocked)
    output = MotionOutput(master_bit_depth=16)
    id = service.start_motion_render_estimate(project, spec, output, str(destination), "blocked")["estimate"]["id"]
    with pytest.raises(ChromaError, match="已有采样"):
        service.start_motion_render_estimate(project, spec, output, str(destination), "second")
    service.cancel_motion_render_estimate(id)
    assert wait_sample(service, id)["status"] == "cancelled"
    close_samples(service.store)
    assert not list(destination.glob(".chromascript-sample-*"))
    assert not service.store.list("job")


@pytest.mark.parametrize("local", [False, True])
def test_recover_interrupted_samples_removes_only_owned_temporary_directory(context, local):
    from chromascript.exports import volume_identity
    service, _, _, destination, _ = context
    destination.mkdir()
    temporary = destination / ".chromascript-sample-interrupted"
    temporary.mkdir()
    (temporary / "owner").write_text("interrupted")
    (temporary / "frame.f32").write_bytes(b"disposable")
    unrelated = destination / "keep.mov"
    unrelated.write_bytes(b"user data")
    service.store.put("motion_estimate", "interrupted", {
        "id": "interrupted", "status": "running", "pid": os.getpid() if local else 99999999,
        "process_started": __import__("psutil").Process().create_time() if local else -1,
        "temporary": str(temporary), "destination": str(destination),
        "volume": volume_identity(destination),
    })
    recover_samples(service.store)
    assert service.store.get("motion_estimate", "interrupted")["status"] == "interrupted"
    assert not temporary.exists() and unrelated.read_bytes() == b"user data"
    assert sample_indices(1) == [0]
    assert sample_indices(2) == [0, 1]
    assert sample_indices(2_147_483_647)[-1] == 2_147_483_646


@pytest.mark.integration
def test_native_scene_log_precision_tags_and_sampling(context, monkeypatch):
    service, project, spec, destination, frames = context
    capability = prores_capability(service.store.root)
    if not capability["available"]:
        if os.environ.get("CHROMASCRIPT_REQUIRE_NATIVE_PRORES"):
            pytest.fail(str(capability))
        pytest.skip("Apple native encoder unavailable in this environment")
    assert platform.system() == "Darwin"
    assert capability["max_code_error"] == 0
    preset = motion_video_preset("prores-4444-acescct")
    pixels = np.ones((32, 64, 4), np.float32)
    pixels[..., :3] = np.linspace(0, 16, 64)[None, :, None]
    settings = MotionRenderSettings(width=64, height=32, frames=1)
    info = encode(preset, settings, destination / "actual.mov",
                  iter([render_output(pixels, "ACEScct", False)]), lambda _: None)
    report = check_probe(info, preset, settings)
    assert report["passed"], report["issues"]
    assert report["color_declaration_verified"] and not report["native_color_tags_verified"]
    assert report["decoded_storage_precision"] == "12-bit"
    assert info["container_color"] == [{"type": "nclc", "primaries": 2, "transfer": 2, "matrix": 1}]
    broken = json.loads(json.dumps(info))
    broken["format"]["tags"].pop("comment")
    assert not check_probe(broken, preset, settings)["passed"]
    # Invalid log codes must fail, rather than being silently clipped to integers.
    pixels[..., 0] = 1.01
    with pytest.raises(ChromaError) as error:
        encode(preset, settings, destination / "invalid.mov", iter([pixels]), lambda _: None)
    assert error.value.code == "log_storage_range" and error.value.details["frame"] == 0
    assert not (destination / "invalid.mov").exists()
    output = MotionOutput(master_bit_depth=None, video="prores-4444-acescct", render_scale=1, video_processing="reference")
    id = service.start_motion_render_estimate(project, spec, output, str(destination), "native")["estimate"]["id"]
    result = wait_sample(service, id)
    assert result["status"] == "completed", result
    assert result["budget"]["master_uncompressed_bytes"] == 0
    assert result["budget"]["sample_count"] == 12
    from chromascript.worker import run
    monkeypatch.setattr("chromascript.motion_worker.iter_float_frames", frames)
    saved = service.create_motion_scene(project, spec=spec)
    job = service.submit_motion_render(saved["record"]["id"], saved["revision_id"], "native-render",
        str(destination), output, storage_estimate_id=id)["job"]
    run(service.store.root, job["id"])
    completed = service.get_job(job["id"])
    assert completed["job"]["status"] == "completed", completed["job"]["error"]
    bundle = Path(completed["job"]["export_path"])
    declaration = json.loads((bundle / "color-encoding.json").read_text())
    assert declaration["transfer"] == "ACEScct"
    assert declaration["display_transform_applied"] is False
    assert declaration["resolve"]["import_verified"] is False
    report = service.validate_delivery_output(job["id"])["verification"]
    assert report["video"]["color_declaration_verified"]
    assert not list(bundle.rglob("*.exr")) and not list(destination.rglob("*.f32"))


def test_sampling_initialization_failure_releases_slot(context, monkeypatch):
    service, project, spec, destination, _ = context
    original = Path.mkdir

    def mkdir(path, *args, **kwargs):
        if path.parent == service.store.root / "estimates":
            raise OSError("disk full")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    output = MotionOutput(master_bit_depth=16, video="none", render_scale=1)
    first = service.start_motion_render_estimate(project, spec, output, str(destination), "fails")["estimate"]
    assert wait_sample(service, first["id"])["status"] == "failed"
    monkeypatch.setattr(Path, "mkdir", original)
    second = service.start_motion_render_estimate(project, spec, output, str(destination), "works")["estimate"]
    assert wait_sample(service, second["id"])["status"] == "completed"


def test_cancel_kills_term_resistant_late_process(tmp_path):
    import subprocess
    import sys

    from chromascript.motion_sampling import SamplingTask

    ready = tmp_path / "ready"
    child = subprocess.Popen([sys.executable, "-c",
        "import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).touch(); time.sleep(30)"])
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        task = SamplingTask()
        task.cancel()
        task.register(child)  # Cancellation also owns processes registered late.
        task.cancel()
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)


@pytest.mark.parametrize("failure", ["mkdir", "write_bytes", "write_text"])
def test_capability_diagnostics_failure_is_unavailable(tmp_path, monkeypatch, failure):
    import chromascript.prores as module

    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.shutil, "which", lambda _: "/fake/ffmpeg")
    monkeypatch.setattr(module, "prores_identity", lambda: failure)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no codec")))
    original = getattr(Path, failure)

    def fail(path, *args, **kwargs):
        if "capabilities" in path.parts:
            raise PermissionError("diagnostics unwritable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, failure, fail)
    assert module.prores_capability(tmp_path)["available"] is False
    monkeypatch.setattr("chromascript.motion.available_encoders", lambda: {
        "libx264", "libx265", "prores_ks", "prores_videotoolbox",
    })
    options = Service(tmp_path / "workspace").get_motion_delivery_options()
    choices = {item["value"]: item["available"] for item in options["videos"]}
    assert not choices["prores-4444-acescct"]
    assert all(choices[key] for key in ("srgb", "pq", "prores-hq-srgb"))
