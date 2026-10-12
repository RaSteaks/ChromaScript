"""Actual bitstreams and legal/full range ramps verify the encoding contract."""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from chromascript.encoding import check_probe, encode
from chromascript.models import RenderSettings
from chromascript.presets import builtins


@pytest.mark.integration
@pytest.mark.parametrize(
    "id", ["h264-review", "hdr-pq", "hdr-hlg", "prores-4", "dnxhr-hqx-mov", "dnxhr-lb-mxf"]
)
def test_streams_and_precision(tmp_path: Path, id):
    p = builtins()[id]
    s = RenderSettings(width=320, height=180, frames=2)
    pixels = np.ones((180, 320, 4), dtype=np.float32) * 0.5
    pixels[..., 3] = 1
    target = tmp_path / f"sample.{p.container}"
    info = encode(p, s, target, iter([pixels, pixels]), lambda n: None)
    result = check_probe(info, p, s)
    assert result["passed"], result["issues"]
    assert result["codec_input_precision"] == f"{p.bit_depth}-bit"


@pytest.mark.integration
@pytest.mark.parametrize(
    "id,depth,black,white", [("prores-4", 12, 256, 3760), ("dnxhr-hqx-mov", 10, 64, 940)]
)
def test_intra_codec_implicit_legal_range(tmp_path: Path, id, depth, black, white):
    p = builtins()[id]
    s = RenderSettings(width=320, height=180, frames=1)
    a = np.ones((180, 320, 4), dtype=np.float32)
    a[:, :160, :3] = 0
    target = tmp_path / "range.mov"
    encode(p, s, target, iter([a]), lambda n: None)
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(target),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            f"yuv444p{depth}le",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    y = np.frombuffer(result.stdout, dtype="<u2")[: 320 * 180].reshape(180, 320)
    assert float(y[40:140, 20:120].mean()) == pytest.approx(black, abs=4)
    assert float(y[40:140, 200:300].mean()) == pytest.approx(white, abs=4)


@pytest.mark.integration
def test_explicit_full_range_is_signaled(tmp_path):
    from chromascript.models import DeliveryPreset

    p = DeliveryPreset.model_validate(
        {**builtins()["h264-review"].model_dump(), "id": "h264-full", "data_range": "full"}
    )
    settings = RenderSettings(width=64, height=64, frames=2)
    pixels = np.ones((64, 64, 4), dtype=np.float32) * 0.5
    pixels[..., 3] = 1
    info = encode(p, settings, tmp_path / "full.mp4", iter([pixels, pixels]), lambda n: None)
    checked = check_probe(info, p, settings)
    assert checked["passed"], checked["issues"]
    assert checked["video"]["color_range"] == "pc"


def test_unknown_frame_rate_is_a_verification_failure():
    # Missing probe metadata is an actionable failed check, never Fraction(0, 0).
    info = {
        "streams": [
            {
                "codec_type": "video",
                "width": 320,
                "height": 180,
                "avg_frame_rate": "0/0",
                "r_frame_rate": "0/0",
                "nb_read_frames": "2",
            }
        ]
    }
    checked = check_probe(
        info, builtins()["h264-review"], RenderSettings(width=320, height=180, frames=2)
    )
    assert not checked["passed"]
    assert "frame-rate mismatch" in checked["issues"]


@pytest.mark.integration
@pytest.mark.parametrize("id", ["prores-4", "ffv1-rgb16"])
def test_codec_transparency_survives_decode(tmp_path, id):
    # Vary coverage independently of RGB so an opaque-only sample cannot pass.
    p = builtins()[id]
    if id == "ffv1-rgb16":
        # The built-in is RGB; this legal custom variant explicitly requests alpha.
        p = p.model_copy(update={"alpha": True, "pixel_format": "gbrap16le"})
    settings = RenderSettings(width=320, height=180, frames=1)
    pixels = np.ones((180, 320, 4), dtype=np.float32)
    pixels[..., :3] = [0.2, 0.4, 0.8]
    pixels[:, :100, 3] = 0
    pixels[:, 100:200, 3] = 0.25
    path = tmp_path / ("alpha." + p.container)
    encode(p, settings, path, iter([pixels]), lambda n: None)
    decoded = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgba64le",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    alpha = np.frombuffer(decoded.stdout, dtype="<u2").reshape(180, 320, 4)[..., 3] / 65535
    np.testing.assert_allclose(alpha, pixels[..., 3], atol=0.002)


def test_image_storage_check_rejects_incorrect_native_depth():
    from chromascript.encoding import check_image_metadata

    assert check_image_metadata({"storage": "uint8", "channels": 4}, builtins()["png-16"])
    assert not check_image_metadata(
        {"storage": "uint16", "channels": 4, "oiio:UnassociatedAlpha": 1}, builtins()["png-16"]
    )


@pytest.mark.integration
def test_nonzero_drop_frame_timecode_is_verified(tmp_path):
    from chromascript.models import Rational

    p = builtins()["h264-review"]
    settings = RenderSettings(
        width=64,
        height=64,
        frames=2,
        fps=Rational(numerator=30000, denominator=1001),
        timecode="10:20:30;15",
    )
    pixels = np.ones((64, 64, 4), dtype=np.float32) * 0.5
    pixels[..., 3] = 1
    info = encode(p, settings, tmp_path / "timecode.mp4", iter([pixels, pixels]), lambda n: None)
    checked = check_probe(info, p, settings)
    assert checked["passed"], checked["issues"]
    wrong = check_probe(info, p, settings.model_copy(update={"timecode": "10:20:30;16"}))
    assert "timecode mismatch" in wrong["issues"]


@pytest.mark.integration
def test_native_encoder_failure_cleans_partial_and_keeps_diagnostics(tmp_path):
    from chromascript.errors import ChromaError

    # Enough input to encounter a closed native encoder pipe, not just its exit status.
    p = builtins()["h264-review"].model_copy(update={"profile": "invalid-native-profile"})
    settings = RenderSettings(width=320, height=180, frames=30)
    pixels = np.ones((180, 320, 4), dtype=np.float32)
    target = tmp_path / "failed.mp4"
    with pytest.raises(ChromaError) as caught:
        encode(p, settings, target, (pixels for _ in range(30)), lambda n: None)
    assert caught.value.code == "encode_failed"
    assert "profile" in caught.value.message.lower()
    assert not target.exists() and not target.with_name("failed.partial.mp4").exists()


@pytest.mark.integration
def test_120fps_timecode_retains_three_digit_frame_number(tmp_path):
    from chromascript.models import Rational

    p = builtins()["h264-review"]
    settings = RenderSettings(
        width=64, height=64, frames=2, fps=Rational(numerator=120), timecode="00:00:00:110"
    )
    pixels = np.ones((64, 64, 4), dtype=np.float32)
    info = encode(
        p, settings, tmp_path / "hfr-timecode.mp4", iter([pixels, pixels]), lambda n: None
    )
    checked = check_probe(info, p, settings)
    assert checked["passed"], checked["issues"]
