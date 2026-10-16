"""Video-only float delivery never materializes masters or renders a second pass."""

import json
from contextlib import closing
from types import SimpleNamespace

import numpy as np
import pytest

from chromascript import motion_worker
from chromascript.color import render_output
from chromascript.errors import ChromaError
from chromascript.exports import RenderBundle, motion_bundle
from chromascript.models import MotionRate, MotionRenderSettings
from chromascript.motion import MotionOutput, MotionSpec, resolve_motion_output, templates
from chromascript.motion_color import decode_float_frame
from chromascript.service import Service
from chromascript.worker import run


def make_job(tmp_path, video="pq", *, frames=3, scale=1):
    service = Service(tmp_path / "workspace")
    pid = service.create_project("video only")["project"]["id"]
    spec = templates()["radiance"].model_copy(update={
        "width": 32, "height": 18, "frames": frames,
        "fps": MotionRate(numerator=30000, denominator=1001),
    })
    saved = service.create_motion_scene(pid, spec=spec)
    job = service.submit_motion_render(saved["record"]["id"], saved["revision_id"], "video-only",
        export_directory=str(tmp_path / "delivery"),
        output=MotionOutput(master_bit_depth=None, video=video, render_scale=scale))["job"]
    return service, job, motion_bundle(job, service.store.get("snapshot", job["id"]))


@pytest.fixture
def transport(monkeypatch):
    consumed, closed = [], []
    for module in ("motion", "motion_worker", "service"):
        monkeypatch.setattr(f"chromascript.{module}.motion_identity", lambda: {"test": 1})
    monkeypatch.setattr("chromascript.motion.motion_environment", lambda: {"render_available": True})

    def frames(spec, directory, indices, *, render_scale=1):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "gpu.json").write_text(json.dumps({"format": "RGBA32F", "renderer": "test"}))
        try:
            for index in indices:
                assert not list(directory.glob("frame-*.f32"))
                raw = directory / f"frame-{index}.f32"
                pixels = np.empty((spec.height * render_scale, spec.width * render_scale, 4), dtype="<f4")
                pixels[:] = [-0.1, 2.1234567 + index, 0.2, 0.5]
                raw.write_bytes(pixels.tobytes())
                consumed.append(index)
                try:
                    yield index, raw
                finally:
                    raw.unlink(missing_ok=True)
        finally:
            closed.append(True)

    monkeypatch.setattr(motion_worker, "iter_float_frames", frames)
    monkeypatch.setattr(motion_worker, "render_float_frames", lambda *a, **k: pytest.fail("no EXR render pass"))
    monkeypatch.setattr(motion_worker, "read_image", lambda *a, **k: pytest.fail("no EXR readback"))
    writer = motion_worker.write_image

    def write(path, *args, **kwargs):
        assert path.suffix != ".exr", "video-only must not write EXR"
        return writer(path, *args, **kwargs)

    monkeypatch.setattr(motion_worker, "write_image", write)
    return consumed, closed


@pytest.mark.integration
@pytest.mark.parametrize("video,transfer", [
    ("pq", "smpte2084"), ("hlg", "arib-std-b67"), ("srgb", "iec61966-2-1"),
    ("prores-hq-pq", "smpte2084"), ("prores-hq-hlg", "arib-std-b67"),
    ("prores-hq-srgb", "iec61966-2-1"),
])
def test_direct_video_actual_codec_color_rate_and_single_pass(tmp_path, monkeypatch, transport, video, transfer):
    service, job, bundle = make_job(tmp_path, video, scale=2)
    encoder = motion_worker.encode
    seen = []

    def encode(preset, settings, path, frames, progress, **kwargs):
        def inspect():
            for index, frame in enumerate(frames):
                raw = np.empty((36, 64, 4), dtype="<f4")
                raw[:] = [-0.1, 2.1234567 + index, 0.2, 0.5]
                expected = render_output(decode_float_frame(raw.tobytes(), 32, 18, 2), preset.color.output, False)
                np.testing.assert_array_equal(frame, expected)
                seen.append(index)
                yield frame
        return encoder(preset, settings, path, inspect(), progress, **kwargs)

    monkeypatch.setattr(motion_worker, "encode", encode)
    run(service.store.root, job["id"])
    result = service.get_job(job["id"])
    assert result["job"]["status"] == "completed", result["job"]["error"]
    assert transport == ([0, 1, 2], [True]) and seen == [0, 1, 2]
    assert not list(tmp_path.rglob("*.exr")) and not list(tmp_path.rglob("*.f32"))
    assert not (bundle.target / "masters").exists()
    assert not bundle.staging.exists()
    assert not {"master-frame", "master-archive", "sequence-manifest"} & {a["kind"] for a in result["artifacts"]}
    report = service.validate_delivery_output(job["id"])
    verified = report["verification"]
    assert verified["passed"] and not verified["scene_linear_master"]
    assert verified["master_bit_depth"] is None
    assert verified["float_pipeline_verified"] and verified["video"]["passed"]
    assert verified["video"]["native_color_tags_verified"]
    stream = next(s for s in report["probe"]["streams"] if s["codec_type"] == "video")
    assert stream["color_transfer"] == transfer
    assert stream["r_frame_rate"] == "30000/1001"
    # Decode evidence, including HQ profile and MOV MIME, must match the new choice.
    if video.startswith("prores-"):
        assert (stream["codec_name"], stream["profile"], stream["pix_fmt"]) == ("prores", "HQ", "yuv422p10le")
        assert (bundle.target / f"animation-{video}.mov").is_file()
        assert next(a for a in result["artifacts"] if a["kind"] == "video")["mime_type"] == "video/quicktime"
    else:
        assert stream["pix_fmt"] == ("yuv420p" if video == "srgb" else "yuv420p10le")
    assert report["gpu"]["format"] == "RGBA32F"


def test_mode_validation_and_4k_budget_without_master_sequence(tmp_path):
    assert resolve_motion_output("three-float-v1", None).master_bit_depth == 32
    with pytest.raises(ChromaError, match="仅视频"):
        resolve_motion_output("three-float-v1", MotionOutput(master_bit_depth=None))
    with pytest.raises(ChromaError, match="SDR"):
        resolve_motion_output("remotion-sdr-v1", MotionOutput(master_bit_depth=None, video="pq"))
    service = Service(tmp_path / "workspace")
    settings = MotionRenderSettings(width=3840, height=2160, frames=36000, fps=MotionRate(numerator=60))
    output = MotionOutput(master_bit_depth=None, video="pq", render_scale=1)
    result = service.estimate_motion_render(settings, output=output)
    assert result["basis"] == "streaming-video-budget-v1"
    assert result["master_uncompressed_bytes"] == result["master_overhead_budget_bytes"] == 0
    assert result["intermediate_budget_bytes"] == 3840 * 2160 * 16
    assert result["video_planning_bps"] == 99_532_800
    assert result["destination_required_bytes"] < 10 * 1024**3
    assert service.get_motion_delivery_options()["video_only"]


@pytest.mark.parametrize("failure", ["encoder", "cancel", "disk"])
def test_failure_closes_transport_and_retry_restarts_without_exr(tmp_path, monkeypatch, transport, failure):
    service, job, bundle = make_job(tmp_path, "srgb")
    encoder = motion_worker.encode
    disk_usage = motion_worker.shutil.disk_usage

    def fail(preset, settings, path, frames, progress, **kwargs):
        if failure == "disk":
            monkeypatch.setattr(motion_worker.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
        next(iter(frames))
        if failure == "cancel":
            service.cancel_job(job["id"])
            progress(1)
        raise ChromaError("encode_failed", "injected encoding failure")

    monkeypatch.setattr(motion_worker, "encode", fail)
    run(service.store.root, job["id"])
    result = service.get_job(job["id"])["job"]
    assert result["status"] == ("cancelled" if failure == "cancel" else "failed")
    assert result["error"]["code"] == {"encoder": "encode_failed", "cancel": "cancelled", "disk": "disk_space"}[failure]
    assert transport == ([0], [True])
    assert not list(tmp_path.rglob("*.f32")) and not list(tmp_path.rglob("*.exr"))
    assert not bundle.target.exists()
    monkeypatch.setattr(motion_worker, "encode", encoder)
    monkeypatch.setattr(motion_worker.shutil, "disk_usage", disk_usage)
    service.resume_job(job["id"])
    run(service.store.root, job["id"])
    assert service.get_job(job["id"])["job"]["status"] == "completed"
    assert transport == ([0, 0, 1, 2], [True, True])


def test_published_video_recovers_without_phantom_archive(tmp_path, monkeypatch, transport):
    service, job, bundle = make_job(tmp_path, "srgb")
    publish = RenderBundle.publish

    def crash(*args, **kwargs):
        publish(*args, **kwargs)
        raise RuntimeError("crash after publication")

    monkeypatch.setattr(RenderBundle, "publish", crash)
    run(service.store.root, job["id"])
    assert service.get_job(job["id"])["job"]["status"] == "failed"
    assert bundle.target.exists()
    service.resume_job(job["id"])
    run(service.store.root, job["id"])
    result = service.get_job(job["id"])
    assert result["job"]["status"] == "completed"
    assert transport == ([0, 1, 2], [True])
    assert not any(a["kind"] == "master-archive" for a in result["artifacts"])


@pytest.mark.integration
def test_real_gpu_direct_video_and_generator_early_close(tmp_path, monkeypatch):
    # Exercise the real browser/ACK pipe as well as the native encoder. A separate
    # early close verifies that a suspended browser cannot retain its current frame.
    from chromascript.motion_render import iter_float_frames

    spec = MotionSpec.model_validate({**templates()["radiance"].model_dump(), "width": 32, "height": 18, "frames": 2})
    work = tmp_path / "early-close"
    with closing(iter_float_frames(spec, work, [0, 1])) as frames:
        _, raw = next(frames)
        assert raw.exists()
    assert not list(work.glob("*.f32"))
    consumed = []

    def frames(*args, **kwargs):
        with closing(iter_float_frames(*args, **kwargs)) as items:
            for index, raw in items:
                consumed.append(index)
                yield index, raw

    monkeypatch.setattr(motion_worker, "iter_float_frames", frames)
    service, job, bundle = make_job(tmp_path, "pq", frames=2)
    run(service.store.root, job["id"])
    result = service.get_job(job["id"])
    assert result["job"]["status"] == "completed", result["job"]["error"]
    assert consumed == [0, 1]
    assert not list(tmp_path.rglob("*.exr"))
    assert (bundle.target / "animation-pq.mp4").is_file()
