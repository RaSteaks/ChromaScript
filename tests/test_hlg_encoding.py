"""Native HLG video must retain saturated HDR light and independent coverage."""

import subprocess

import numpy as np
import pytest

from chromascript.color import render_output
from chromascript.encoding import check_probe, command, encode
from chromascript.errors import ChromaError
from chromascript.models import ColorPipeline, DeliveryPreset, RenderSettings
from chromascript.presets import builtins


def hlg_preset(identifier, **changes):
    return DeliveryPreset.model_validate({
        **builtins()[identifier].model_dump(),
        "color": ColorPipeline(output="HLG").model_dump(),
        **changes,
    })


def pq_nits(rgb):
    # Independent ST 2084 EOTF, used to recover the same ACES 1000-nit view
    # through its PQ encoding rather than using the HLG code being tested.
    power = np.maximum(np.asarray(rgb, np.float64), 0) ** (32 / 2523)
    return 10000 * (np.maximum(power - 3424 / 4096, 0)
                    / (2413 / 128 - 2392 / 128 * power)) ** (16384 / 2610)


def hlg_nits(rgb):
    # BT.2100 inverse OETF followed by the 1000-nit reference-display OOTF.
    # The 1.2 system gamma operates on scene luminance, never per-channel gamma.
    signal = np.maximum(np.asarray(rgb, np.float64), 0)
    a = 0.17883277
    b = 1 - 4 * a
    c = 0.5 - a * np.log(4 * a)
    linear = np.where(signal <= 0.5, signal**2 / 3, (np.exp((signal - c) / a) + b) / 12)
    luminance = linear @ np.array([0.2627, 0.6780, 0.0593])
    return 1000 * linear * luminance[..., None] ** 0.2


def native_yuv_samples(path, pixel_format, width, height, points):
    # Request the decoder's own planar format: no RGB conversion, range scaling
    # or chroma resampling may conceal the encoded values under test.
    formats = {"yuv420p10le": (10, 2, 2, False),
               "yuv422p10le": (10, 2, 1, False),
               "yuva444p12le": (12, 1, 1, True)}
    depth, sx, sy, alpha = formats[pixel_format]
    result = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0",
        "-frames:v", "1", "-pix_fmt", pixel_format, "-f", "rawvideo", "pipe:1",
    ], capture_output=True, check=True)
    values = np.frombuffer(result.stdout, "<u2").astype(np.float64)
    count, chroma_count = width * height, (width // sx) * (height // sy)
    assert len(values) == count * (2 if alpha else 1) + 2 * chroma_count
    y = values[:count].reshape(height, width)
    u = values[count:count + chroma_count].reshape(height // sy, width // sx)
    v = values[count + chroma_count:count + 2 * chroma_count].reshape(height // sy, width // sx)
    coverage = values[-count:].reshape(height, width) / ((1 << depth) - 1) if alpha else None
    samples = []
    scale = 1 << (depth - 8)
    for x, row in points:
        luma = (y[row, x] - 16 * scale) / (219 * scale)
        cb = (u[row // sy, x // sx] - 128 * scale) / (224 * scale)
        cr = (v[row // sy, x // sx] - 128 * scale) / (224 * scale)
        # Independent BT.2020 non-constant-luminance matrix reconstruction.
        red, blue = luma + 1.4746 * cr, luma + 1.8814 * cb
        green = (luma - 0.2627 * red - 0.0593 * blue) / 0.6780
        samples.append([red, green, blue, coverage[row, x] if alpha else 1])
    return np.asarray(samples)


@pytest.mark.integration
@pytest.mark.parametrize("identifier", [
    "hdr-hlg", "prores-3", "uncompressed-yuv10", "prores-4",
])
def test_saturated_aces_light_survives_hlg_video(tmp_path, identifier):
    preset = hlg_preset(identifier)
    settings = RenderSettings(width=128, height=64, frames=1)
    scene = np.ones((settings.height, settings.width, 4), np.float32)
    scene[:, :64, :3] = [16, 0, 0]
    scene[:, 64:, :3] = [0, 0, 16]
    if preset.alpha:
        # Non-8-bit-aligned coverage catches an alphamerge downgrade or applying
        # limited video range to Alpha while retaining HDR RGB in the other branch.
        scene[..., 3] = np.repeat([0.123, 0.287, 0.501, 0.877], 32)
    pixels = render_output(scene, "HLG", preset.alpha)
    expected = pq_nits(render_output(scene, "PQ", preset.alpha)[..., :3])
    assert pixels[..., :3].max() > 1.03
    target = tmp_path / f"{identifier}.{preset.container}"
    args = command(preset, settings, target)
    assert args[args.index("-pixel_format") + 1] == (
        "gbrapf32le" if preset.alpha else "gbrpf32le"
    )
    info = encode(preset, settings, target, [pixels], lambda _: None)
    verified = check_probe(info, preset, settings)
    assert verified["passed"], verified["issues"]
    assert verified["encoder_input_precision"] == "32-bit float RGB"
    assert verified["hlg_extended_range"]
    points = [(16, 32), (48, 32), (80, 32), (112, 32)]
    decoded = native_yuv_samples(
        target, verified["video"]["pix_fmt"], settings.width, settings.height, points,
    )
    actual = hlg_nits(decoded[:, :3])
    reference = np.asarray([expected[y, x] for x, y in points])
    # Quantization and lossy codecs allow a few nits; the old RGB clamp lost
    # hundreds of nits in these saturated primaries and fails by a wide margin.
    np.testing.assert_allclose(actual, reference, rtol=0.03, atol=3)
    assert np.min(actual.max(axis=1)) > 950
    np.testing.assert_allclose(
        decoded[:, 3], [scene[y, x, 3] for x, y in points], atol=0.0012,
    )


@pytest.mark.integration
@pytest.mark.parametrize("identifier", ["hdr-hlg", "ffv1-rgb16"])
def test_hlg_full_integer_outputs_reject_superwhite(tmp_path, identifier):
    preset = hlg_preset(identifier, data_range="full")
    settings = RenderSettings(width=64, height=64, frames=1)
    scene = np.zeros((64, 64, 4), np.float32)
    scene[...] = [0, 0, 16, 1]
    pixels = render_output(scene, "HLG", False)
    target = tmp_path / f"unrepresentable.{preset.container}"
    with pytest.raises(ChromaError) as error:
        encode(preset, settings, target, [pixels], lambda _: None)
    assert error.value.code == "hlg_storage_range"
    assert not target.exists()


@pytest.mark.integration
@pytest.mark.parametrize("rgb,component", [
    ([1.2, 1.2, 1.2], "Y"), ([0, 0, 2], "Cb"), ([2, 0, 0], "Cr"),
])
def test_hlg_limited_rejects_codes_outside_storage(tmp_path, rgb, component):
    preset = hlg_preset("hdr-hlg")
    settings = RenderSettings(width=64, height=64, frames=1)
    # These are already HLG-encoded test signals, deliberately beyond available
    # narrow-range headroom. Each independent Y/C component must be guarded.
    pixels = np.ones((64, 64, 4), np.float32)
    pixels[..., :3] = rgb
    target = tmp_path / "unrepresentable.mp4"
    with pytest.raises(ChromaError) as error:
        encode(preset, settings, target, [pixels], lambda _: None)
    assert error.value.code == "hlg_storage_range"
    assert error.value.details["component"] == component
    assert not target.exists()


@pytest.mark.integration
def test_hlg_full_rgb16_still_encodes_representable_values(tmp_path):
    preset = hlg_preset("ffv1-rgb16")
    settings = RenderSettings(width=96, height=32, frames=1)
    scene = np.ones((32, 96, 4), np.float32)
    scene[..., :3] = np.repeat([0, 0.18, 1], 32)[None, :, None]
    pixels = render_output(scene, "HLG", False)
    assert 0 <= pixels[..., :3].min() <= pixels[..., :3].max() <= 1
    target = tmp_path / "representable.mkv"
    info = encode(preset, settings, target, [pixels], lambda _: None)
    verified = check_probe(info, preset, settings)
    assert verified["passed"], verified["issues"]
    assert verified["encoder_input_precision"] == "16-bit RGB"
    assert not verified["hlg_extended_range"]
    assert verified["video"]["pix_fmt"] == "gbrp16le"
    result = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(target), "-frames:v", "1",
        "-pix_fmt", "gbrp16le", "-f", "rawvideo", "pipe:1",
    ], capture_output=True, check=True)
    decoded = np.frombuffer(result.stdout, "<u2").reshape(3, 32, 96)
    expected = np.rint(pixels[..., [1, 2, 0]].transpose(2, 0, 1) * 65535)
    np.testing.assert_allclose(decoded, expected, atol=1)
