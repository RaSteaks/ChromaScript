"""Native hardware output must prove stream/container tags and full decoding."""

import subprocess
from copy import deepcopy

import numpy as np
import pytest

from chromascript import video_hardware
from chromascript.encoding import check_probe, encode
from chromascript.errors import ChromaError
from chromascript.models import MotionRate, MotionRenderSettings
from chromascript.presets import builtins
from chromascript.video_transport import VideoFrame


@pytest.fixture
def isolated_probe_cache():
    video_hardware._probe.cache_clear()
    yield
    video_hardware._probe.cache_clear()


@pytest.mark.integration
@pytest.mark.parametrize("failure", [
    ChromaError("encode_failed", "VideoToolbox session busy"),
    OSError("Temporary encoder resource unavailable"),
    subprocess.TimeoutExpired("ffmpeg", 30),
], ids=["busy", "io", "timeout"])
def test_hardware_probe_retries_failure_and_caches_success(
    monkeypatch, isolated_probe_cache, failure,
):
    preset = builtins()["h264-srgb-preview"].model_copy(update={
        "codec": "h264_videotoolbox", "crf": None, "bitrate_kbps": 8000,
    })
    settings = MotionRenderSettings(width=64, height=64, frames=3)
    attempts = []

    def encode_after_failure(*args, **kwargs):
        attempts.append(args)
        if len(attempts) == 1:
            raise failure
        # Only the transient failure is simulated; recovery must pass the real
        # encoder, native color metadata and strict decoding checks.
        return encode(*args, **kwargs)

    monkeypatch.setattr(video_hardware, "encode", encode_after_failure)
    first = video_hardware.hardware_probe(preset, settings, "slow")
    assert first["available"] is False
    assert first["reason"] == str(failure)
    recovered = video_hardware.hardware_probe(preset, settings, "slow")
    assert recovered["available"] is True, recovered
    assert recovered["strict_decode_verified"] is True
    assert video_hardware.hardware_probe(preset, settings, "slow") == recovered
    assert len(attempts) == 2


@pytest.mark.integration
@pytest.mark.parametrize("identifier,transfer", [("h264-srgb-preview", 13), ("hdr-pq", 16), ("hdr-hlg", 18)])
def test_native_hardware_color_time_precision_and_decode(tmp_path, identifier, transfer):
    preset = builtins()[identifier]
    ten = preset.bit_depth == 10
    preset = preset.model_copy(update={"codec": "hevc_videotoolbox" if ten else "h264_videotoolbox",
                                      "crf": None, "bitrate_kbps": 8000})
    settings = MotionRenderSettings(width=64, height=64, frames=3, fps=MotionRate(numerator=30000, denominator=1001))
    pixel_format = "p010le" if ten else "nv12"
    values = np.full(64 * 64 * 3 // 2, 512 if ten else 128, dtype="<u2" if ten else "u1")
    values[:64 * 64] = np.tile(np.linspace(64 if ten else 16, 1000 if transfer == 18 else 940 if ten else 235, 64).astype(values.dtype), 64)
    if ten:
        values <<= 6
    frame = VideoFrame(values.tobytes(), pixel_format)
    path = tmp_path / "hardware.mp4"
    info = encode(preset, settings, path, [frame] * 3, lambda _: None, video_input=pixel_format)
    checked = check_probe(info, preset, settings)
    assert checked["passed"], checked["issues"]
    assert checked["decoded_storage_precision"] == ("10-bit" if ten else "8-bit")
    assert info["container_color"][0]["transfer"] == transfer
    headers = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-map", "0:v:0", "-c", "copy",
                              "-bsf:v", "trace_headers", "-f", "null", "-"], capture_output=True, text=True, check=True).stderr
    assert any("transfer_characteristics" in line and line.rstrip().endswith(f"= {transfer}") for line in headers.splitlines())
    invalid = deepcopy(info)
    invalid["container_color"][0]["transfer"] = 2
    assert not check_probe(invalid, preset, settings)["passed"]
    if transfer == 18:
        decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1",
                                  "-pix_fmt", "yuv420p10le", "-f", "rawvideo", "pipe:1"], capture_output=True, check=True).stdout
        luma = np.frombuffer(decoded, "<u2")[:64 * 64]
        assert luma.max() > 980, "Hardware must not clip legal HLG super-whites to nominal white"
