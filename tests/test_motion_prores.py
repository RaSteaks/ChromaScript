"""ProRes motion delivery verifies decoded MOV properties, not encoder names alone."""

import json
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from chromascript.cli import app
from chromascript.errors import ChromaError
from chromascript.models import MotionRate, MotionRenderSettings
from chromascript.motion import MotionOutput, prores_hq_frame_bits, resolve_motion_output, templates
from chromascript.operations import execute
from chromascript.service import Service
from chromascript.worker import run


def test_discovery_encoder_gate_and_cli_contract(tmp_path, monkeypatch):
    service = Service(tmp_path / "workspace")
    monkeypatch.setattr("chromascript.motion.available_encoders", lambda: {"libx264", "libx265"})
    monkeypatch.setattr("chromascript.motion.motion_environment", lambda: {"render_available": True})
    options = service.get_motion_delivery_options()
    choices = [v for v in options["videos"] if v["value"].startswith("prores-")]
    assert len(choices) == 4
    assert all(not v["available"] for v in choices)
    assert all("prores_ks" in v["reason"] for v in choices if v["value"].startswith("prores-hq-"))
    assert "VideoToolbox" in choices[-1]["reason"]
    project = service.create_project("missing encoder")["project"]["id"]
    scene = service.create_motion_scene(project)
    with pytest.raises(ChromaError, match="prores_ks"):
        execute(service, "submit_motion_render", {
            "scene_id": scene["record"]["id"], "expected_revision_id": scene["revision_id"],
            "idempotency_key": "missing-prores",
            "output": {"master_bit_depth": None, "video": "prores-hq-srgb"},
        })
    assert not service.store.list("job")
    monkeypatch.setattr("chromascript.motion.available_encoders", lambda: {"prores_ks"})
    monkeypatch.setattr("chromascript.cli.call", lambda root, op, arguments: execute(service, op, arguments))
    result = CliRunner().invoke(app, ["call", "get_motion_delivery_options", "--arguments", '{"engine":"remotion-sdr-v1"}'])
    assert result.exit_code == 0
    videos = json.loads(result.stdout)["videos"]
    assert [v["value"] for v in videos if v["available"]] == ["prores-hq-srgb"]
    assert next(v for v in videos if v["value"] == "prores-hq-srgb")["color_encoding"]["output_transform"] is None
    for video in ("prores-hq-pq", "prores-hq-hlg"):
        with pytest.raises(ChromaError, match="SDR"):
            resolve_motion_output("remotion-sdr-v1", MotionOutput(master_bit_depth=None, video=video))


def test_prores_budget_does_not_use_small_interframe_allowance(tmp_path):
    service = Service(tmp_path / "workspace")
    settings = MotionRenderSettings(width=3840, height=2160, frames=18000,
                                    fps=MotionRate(numerator=30000, denominator=1001))
    hevc = service.estimate_motion_render(settings, output=MotionOutput(master_bit_depth=None, video="pq"))
    direct = service.estimate_motion_render(settings, output=MotionOutput(master_bit_depth=None, video="prores-hq-pq"))
    master = service.estimate_motion_render(settings, output=MotionOutput(video="prores-hq-pq"))
    sdr = service.estimate_motion_render(settings, engine="remotion-sdr-v1",
        output=MotionOutput(master_bit_depth=None, video="prores-hq-srgb"))
    assert direct["master_uncompressed_bytes"] == 0
    assert direct["intermediate_budget_bytes"] == hevc["intermediate_budget_bytes"]
    # HQ remains larger than HEVC, without budgeting uncompressed frame storage.
    assert hevc["video_budget_bytes"] * 10 < direct["video_budget_bytes"] < hevc["video_budget_bytes"] * 25
    assert direct["video_budget_bytes"] == master["video_budget_bytes"] == sdr["video_budget_bytes"]


@pytest.mark.parametrize("width,height,bits_per_block", [
    (720, 576, 1566), (960, 720, 1216), (1440, 1080, 950),
    (1920, 1080, 950), (3840, 2160, 950),
])
def test_compressed_hq_target_accounts_for_padded_macroblocks(width, height, bits_per_block):
    # 1080-line pictures include a padded macroblock row, affecting tier selection.
    blocks = ((width + 15) // 16) * ((height + 15) // 16)
    assert prores_hq_frame_bits(width, height) == blocks * bits_per_block


@pytest.mark.parametrize("rate", [(60, 1), (60000, 1001), (1, 2)])
def test_4k_prores_budget_uses_frame_count_and_allows_screenshot_disk_space(tmp_path, monkeypatch, rate):
    service = Service(tmp_path / "workspace")
    # Regression: this 9m20s UHD60 clip previously required 1.22 TiB and
    # incorrectly refused the user's 421.78 GiB of available disk space.
    monkeypatch.setattr("chromascript.motion.shutil.disk_usage", lambda _: SimpleNamespace(free=421 * 1024**3))
    monkeypatch.setattr("chromascript.motion.motion_environment", lambda: {"render_available": True})
    monkeypatch.setattr("chromascript.motion.motion_identity", lambda: {"test": True})
    monkeypatch.setattr("chromascript.motion.psutil.virtual_memory", lambda: SimpleNamespace(available=64 * 1024**3))
    monkeypatch.setattr("chromascript.motion.available_encoders", lambda: {"prores_ks"})
    spec = templates()["radiance"].model_copy(update={
        "width": 3840, "height": 2160, "frames": 33600,
        "fps": MotionRate(numerator=rate[0], denominator=rate[1]),
    })
    output = MotionOutput(master_bit_depth=None, video="prores-hq-pq", render_scale=1)
    estimate = service.estimate_motion_render(spec.settings(), output=output)
    assert estimate["basis"] == "nominal-no-margin-v2"
    assert 120 * 1024**3 < estimate["destination_required_bytes"] < 122 * 1024**3
    assert estimate["master_uncompressed_bytes"] == 0 and estimate["sufficient"]
    expected_rate = Fraction(30_780_000 * rate[0], rate[1])
    assert estimate["video_planning_bps"] == -(-expected_rate.numerator // expected_rate.denominator)
    # Exact nominal frame payload; neither 20% nor a fixed 16 MiB mux reserve.
    assert estimate["video_budget_bytes"] == 129_276_000_000
    pid = service.create_project("compressed budget")["project"]["id"]
    saved = service.create_motion_scene(pid, spec=spec)
    job = service.submit_motion_render(saved["record"]["id"], saved["revision_id"], "budget", output=output)["job"]
    assert job["status"] == "queued"  # Do not dispatch or launch a browser.
    monkeypatch.setattr("chromascript.motion.shutil.disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3))
    with pytest.raises(ChromaError, match="足够空间"):
        service.submit_motion_render(saved["record"]["id"], saved["revision_id"], "too-small", output=output)


@pytest.mark.integration
@pytest.mark.parametrize("engine,video,depth", [
    ("three-float-v1", "prores-hq-pq", 16),
    ("three-float-v1", "prores-hq-hlg", 32),
    ("three-float-v1", "prores-hq-srgb", None),
    ("remotion-sdr-v1", "prores-hq-srgb", None),
])
def test_real_browser_prores_delivery(tmp_path, engine, video, depth):
    # Exercise real GPU/readback or screenshot input, native encoding and publication.
    service = Service(tmp_path / "workspace")
    pid = service.create_project("actual ProRes")["project"]["id"]
    spec = templates()["radiance" if engine == "three-float-v1" else "title"].model_copy(update={
        "width": 64, "height": 36, "frames": 2,
        "fps": MotionRate(numerator=30000, denominator=1001),
    })
    saved = service.create_motion_scene(pid, spec=spec)
    job = service.submit_motion_render(saved["record"]["id"], saved["revision_id"], video,
        export_directory=str(tmp_path / "delivery"),
        output=MotionOutput(master_bit_depth=depth, video=video, render_scale=1))["job"]
    run(service.store.root, job["id"])
    result = service.get_job(job["id"])
    assert result["job"]["status"] == "completed", result["job"]["error"]
    report = service.validate_delivery_output(job["id"])
    verification = report["verification"]
    native = verification["video"] if engine == "three-float-v1" else verification
    assert native["passed"] and native["native_color_tags_verified"]
    assert native["codec_input_precision"] == native["decoded_storage_precision"] == "10-bit"
    stream = next(s for s in report["probe"]["streams"] if s["codec_type"] == "video")
    assert (stream["codec_name"], stream["profile"], stream["pix_fmt"]) == ("prores", "HQ", "yuv422p10le")
    assert stream["r_frame_rate"] == "30000/1001" and int(stream["nb_read_frames"]) == 2
    transfer = {"prores-hq-pq": "smpte2084", "prores-hq-hlg": "arib-std-b67", "prores-hq-srgb": "iec61966-2-1"}[video]
    assert stream["color_transfer"] == transfer
    assert stream["color_primaries"] == ("bt709" if video.endswith("srgb") else "bt2020")
    assert stream["color_space"] == ("bt709" if video.endswith("srgb") else "bt2020nc")
    artifact = next(a for a in result["artifacts"] if a["kind"] == "video")
    assert artifact["mime_type"] == "video/quicktime"
    bundle = Path(result["job"]["export_path"])
    assert len(list(bundle.glob("*.mov"))) == 1 and not list(bundle.glob("*.mp4"))
    assert len(list(bundle.glob("masters/*.exr"))) == (2 if depth else 0)
    if engine == "remotion-sdr-v1":
        assert verification["source_precision"] == "8-bit browser screenshot"
        assert verification["color_encoding"]["output_transform"] is None
    else:
        assert verification["float_pipeline_verified"]
