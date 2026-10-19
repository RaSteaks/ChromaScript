"""Delivery evidence must reject concealed decode errors, not only missing frames."""

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from chromascript.encoding import encode, probe, strict_video_decode_evidence
from chromascript.errors import ChromaError
from chromascript.models import RenderSettings
from chromascript.presets import builtins


@pytest.fixture(scope="module")
def hevc_delivery(tmp_path_factory):
    root = tmp_path_factory.mktemp("strict-video-decode")
    path = root / "valid.mp4"
    settings = RenderSettings(width=320, height=180, frames=2)
    pixels = np.ones((180, 320, 4), np.float32)
    pixels[:, :160, :3] = 0
    info = encode(builtins()["hdr-pq"], settings, path, [pixels, pixels], lambda _: None)
    return path, info


@pytest.fixture(scope="module")
def concealed_hevc_error(hevc_delivery):
    path, _ = hevc_delivery
    packets = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_packets", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout)["packets"]
    packet = packets[1]
    start, size = int(packet["pos"]), int(packet["size"])
    corrupted = path.with_name("concealed-error.mp4")
    # Change only compressed slice bytes, leaving the container, tags and packet
    # count intact. A small candidate set accommodates native encoder versions;
    # the regression specifically needs a decoder error with exit status zero.
    for fraction in (0.2, 1 / 3, 0.5, 2 / 3, 0.8):
        data = bytearray(path.read_bytes())
        position = start + max(8, int(size * fraction))
        length = min(8, start + size - position)
        data[position:position + length] = b"\xff" * length
        corrupted.write_bytes(data)
        native = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-of", "json", str(corrupted)],
            capture_output=True, text=True,
        )
        if native.returncode == 0 and native.stderr:
            streams = json.loads(native.stdout)["streams"]
            video = next(stream for stream in streams if stream["codec_type"] == "video")
            if video.get("nb_read_frames") == "2":
                return corrupted, native.stderr
    pytest.fail("Could not produce the concealed HEVC decode-error regression sample")


@pytest.mark.integration
def test_complete_video_decode_is_recorded_separately_from_metadata(hevc_delivery):
    _, info = hevc_delivery
    evidence = info["video_decode_verification"]
    assert evidence["passed"] and evidence["complete"]
    assert evidence["method"] == "ffmpeg-strict-full-decode"
    assert info["streams"][0]["nb_read_frames"] == "2"


@pytest.mark.integration
def test_metadata_only_probe_does_not_claim_full_decode(hevc_delivery, monkeypatch):
    path, _ = hevc_delivery

    def unexpected_decode(_: Path):
        pytest.fail("Metadata-only inspection must not fully decode the video")

    monkeypatch.setattr("chromascript.encoding.strict_video_decode_evidence", unexpected_decode)
    info = probe(path, count_frames=False)
    assert "video_decode_verification" not in info
    assert "nb_read_frames" not in info["streams"][0]
    assert info["container_color"][0]["transfer"] == 16


@pytest.mark.integration
def test_probe_rejects_nonfatal_decoder_errors(concealed_hevc_error):
    path, diagnostic = concealed_hevc_error
    assert diagnostic
    with pytest.raises(ChromaError) as caught:
        probe(path)
    assert caught.value.code == "probe_failed"
    assert caught.value.details["return_code"] == 0


@pytest.mark.integration
def test_strict_decode_rejects_concealed_frames(concealed_hevc_error):
    path, _ = concealed_hevc_error
    with pytest.raises(ChromaError) as caught:
        strict_video_decode_evidence(path)
    assert caught.value.code == "video_decode_verification"
    assert caught.value.details["diagnostic"]
