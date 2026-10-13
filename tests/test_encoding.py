"""Actual bitstreams and legal/full range ramps verify the encoding contract."""

import subprocess
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest

from chromascript.encoding import check_image_metadata, check_probe, encode
from chromascript.images import read_image, write_image
from chromascript.models import RenderSettings
from chromascript.presets import builtins


@pytest.mark.integration
@pytest.mark.parametrize(
    "id",
    [
        "h264-review",
        "h264-mov",
        "hevc-mov",
        "hdr-pq",
        "hdr-hlg",
        "prores-4",
        "dnxhr-hqx-mov",
        "dnxhr-lb-mxf",
        "uncompressed-rgb10",
        "uncompressed-yuv10",
    ],
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
    assert result["color_encoding"]["resolve_import_verified"] is False
    if p.container in {"mp4", "mov"}:
        assert len(info["container_color"]) == 1
        # Codec VUI must not mask stripped or wrong QuickTime/MP4 tags.
        stripped = deepcopy(info)
        stripped["container_color"] = []
        assert "QuickTime/MP4 colr mismatch or missing" in check_probe(stripped, p, s)["issues"]
        wrong = deepcopy(info)
        wrong["container_color"][0]["primaries"] = 12
        assert not check_probe(wrong, p, s)["passed"]
        if p.container == "mov":
            # colr takes precedence over gama; automatic pure-gamma import is not certified.
            assert "gamma" not in info["container_color"][0]
            assert info["container_color"][0]["transfer"] == 2
            assert result["color_encoding"]["requires_manual_video_interpretation"]


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
def test_decoded_profile_and_sampling_must_match_the_requested_delivery(tmp_path):
    # One real 4:2:2 file supplies independent evidence for both negative cases.
    p = builtins()["prores-2"]
    settings = RenderSettings(width=64, height=64, frames=1)
    info = encode(
        p, settings, tmp_path / "422.mov", [np.ones((64, 64, 4), np.float32)], lambda _: None
    )
    assert check_probe(info, p, settings)["passed"]
    assert check_probe(info, p.model_copy(update={"profile": "standard"}), settings)["passed"]
    assert check_probe(info, p.model_copy(update={"profile": "auto"}), settings)["passed"]
    wrong_profile = p.model_copy(update={"profile": "3"})
    assert any(
        "profile mismatch" in issue
        for issue in check_probe(info, wrong_profile, settings)["issues"]
    )
    wrong_sampling = p.model_copy(update={"pixel_format": "yuv444p10le"})
    assert "chroma sampling mismatch" in check_probe(info, wrong_sampling, settings)["issues"]
    missing = deepcopy(info)
    missing["streams"][0].pop("profile")
    assert not check_probe(missing, p, settings)["passed"]


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


def test_image_storage_check_rejects_incorrect_native_depth(tmp_path):
    p = builtins()["png-16"]
    path = tmp_path / "native.png"
    write_image(path, np.ones((16, 16, 4), dtype=np.float32), space="sRGB", depth=16)
    _, metadata = read_image(path)
    assert not check_image_metadata(metadata, p)
    assert check_image_metadata({**metadata, "storage": "uint8"}, p)


@pytest.mark.parametrize(
    "id",
    [
        "exr-16-zip",
        "exr-32-piz",
        "aces2065-archive",
        "png-8",
        "png-16",
        "tiff-8",
        "tiff-16",
        "dpx-10",
        "dpx-12",
        "dpx-16",
    ],
)
def test_native_sequence_color_metadata_survives_without_sidecar(tmp_path, id):
    import json

    p = builtins()[id]
    path = tmp_path / f"frame.{p.container}"
    # Non-opaque coverage and unequal RGB catch unwanted color/alpha processing.
    pixels = np.broadcast_to(np.array([0.125, 0.5, 0.75, 0.5], dtype=np.float32), (16, 16, 4))
    if not p.alpha:
        pixels = pixels[..., :3]
    write_image(path, pixels, space=p.color.output, depth=p.bit_depth, compression=p.compression)
    actual, metadata = read_image(path)
    assert not check_image_metadata(metadata, p)
    json.dumps(metadata, allow_nan=False)  # ICC evidence must remain usable by MCP/reports.
    tolerance = 2 / (2**p.bit_depth - 1)
    np.testing.assert_allclose(actual, pixels, atol=tolerance, rtol=0)
    if p.container == "tiff":
        from PIL import Image

        # Confirm tag 34675 through an independent reader, not an OIIO space guess.
        with Image.open(path) as native:
            profile = native.tag_v2[34675]
            assert profile[36:40] == b"acsp"
        assert metadata["ICCProfile"]["matches_srgb"]
        metadata.pop("ICCProfile")
    elif p.container == "png":
        metadata.pop("png:sRGB")
    elif p.container == "dpx":
        import struct

        header = path.read_bytes()[:2048]
        order = ">" if header[:4] == b"SDPX" else "<"
        assert header[801:803] == bytes([0, 6])  # User-defined TRC, BT.709 primaries.
        assert struct.unpack_from(order + "f", header, 1948)[0] == pytest.approx(2.4)
        metadata["dpx:Gamma"] = 2.2
    else:
        metadata.pop("colorInteropID")
    assert check_image_metadata(metadata, p)


def test_untagged_png_is_not_certified_from_reader_default(tmp_path):
    from PIL import Image

    path = tmp_path / "untagged.png"
    Image.new("RGB", (16, 16)).save(path)
    _, metadata = read_image(path)
    # OIIO reports assumed sRGB; a physical sRGB/gAMA/cHRM header is still required.
    assert metadata["oiio:ColorSpace"] == "srgb_rec709_scene"
    p = builtins()["png-8"].model_copy(update={"alpha": False})
    assert "missing PNG sRGB chunk" in check_image_metadata(metadata, p)


@pytest.mark.integration
@pytest.mark.parametrize(
    "container,output,expected",
    [
        ("mov", "sRGB", (1, 13, 1)),
        ("mov", "PQ", (9, 16, 9)),
        ("mov", "HLG", (9, 18, 9)),
        ("mp4", "sRGB", (1, 13, 1)),
    ],
)
def test_gamma_choices_write_standard_container_tags(tmp_path, container, output, expected):
    from chromascript.models import ColorPipeline

    p = builtins()["hevc-mov"].model_copy(
        update={
            "container": container,
            "color": ColorPipeline(output=output),
        }
    )
    settings = RenderSettings(width=64, height=64, frames=1)
    pixels = np.ones((64, 64, 4), dtype=np.float32) * 0.5
    pixels[..., 3] = 1
    info = encode(p, settings, tmp_path / f"gamma.{container}", iter([pixels]), lambda n: None)
    checked = check_probe(info, p, settings)
    assert checked["passed"], checked["issues"]
    native = info["container_color"][0]
    assert tuple(native[k] for k in ("primaries", "transfer", "matrix")) == expected
    assert "gamma" not in native  # No power-law gamma tag on sRGB/PQ/HLG.


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
