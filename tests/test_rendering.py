"""GPU output is checked against independent values and storage round trips."""

from pathlib import Path

import numpy as np
import pytest

from chromascript.color import input_rgb, transform
from chromascript.errors import ChromaError
from chromascript.gpu import Renderer
from chromascript.images import read_image, resize_srgb, write_image
from chromascript.models import Parameter, Rational, RenderSettings, SceneSpec
from chromascript.templates import TEMPLATES


@pytest.fixture(scope="module")
def renderer():
    return Renderer()


@pytest.mark.gpu
def test_linear_values_time_and_alpha(renderer):
    scene = SceneSpec(
        name="reference",
        code="fn shade(ctx: SceneContext) -> vec4<f32> { return vec4<f32>(-0.25, 8.0, ctx.time, 0.5); }",
    )
    settings = RenderSettings(width=32, height=16, frames=1, samples=4, fps=Rational(numerator=24))
    a = renderer.render(scene, settings, 48)
    np.testing.assert_allclose(
        a, np.broadcast_to([-0.25, 8.0, 2.0, 0.5], a.shape), rtol=0, atol=1e-6
    )


@pytest.mark.gpu
@pytest.mark.parametrize("space,expected", [("ACEScg", 0.5), ("sRGB", 0.21404114)])
def test_parameter_encoding_does_not_change_shader_literal_space(renderer, space, expected):
    # The sRGB midpoint decodes to linear 0.21404114 before the shader sees it;
    # a shader literal remains 0.5 ACEScg in the same frame. Neither is display-baked.
    scene = SceneSpec(
        name="fixed generation space",
        code="""fn shade(ctx: SceneContext) -> vec4<f32> {
            if ctx.pixel.x < 8.0 { return vec4<f32>(ctx.params.tint, 0.375); }
            return vec4<f32>(0.5, 0.5, 0.5, 0.375);
        }""",
        parameters=[Parameter(name="tint", kind="color", default=[0.5] * 3, color_space=space)],
    )
    image = renderer.render(scene, RenderSettings(width=16, height=16, frames=1, samples=4), 0)
    np.testing.assert_allclose(image[0, 0, :3], [expected] * 3, rtol=2e-4, atol=1e-6)
    np.testing.assert_array_equal(image[0, 8], [0.5, 0.5, 0.5, 0.375])
    np.testing.assert_array_equal(image[..., 3], 0.375)


@pytest.mark.gpu
def test_transparent_samples_do_not_bleed(renderer):
    scene = SceneSpec(
        name="coverage",
        code="""
fn shade(ctx: SceneContext) -> vec4<f32> {
 if fract(ctx.pixel.x) < 0.5 { return vec4<f32>(1.0, 0.0, 0.0, 1.0); }
 return vec4<f32>(0.0, 0.0, 100.0, 0.0);
}""",
    )
    a = renderer.render(scene, RenderSettings(width=16, height=16, frames=1, samples=4), 0)
    np.testing.assert_allclose(a, np.broadcast_to([1, 0, 0, 0.5], a.shape), rtol=0, atol=1e-6)


@pytest.mark.gpu
@pytest.mark.parametrize("samples", [1, 4, 16])
def test_sample_reduction_keeps_finite_extremes_and_coverage(renderer, samples):
    # A constant's mean must not depend on sample count; include negative maxima,
    # tiny coverage, signed cancellation and hidden RGB in one small GPU dispatch.
    scene = SceneSpec(
        name="finite averages",
        code="""
fn shade(ctx: SceneContext) -> vec4<f32> {
    let maximum = bitcast<f32>(0x7f7fffffu);
    if ctx.pixel.y < 4.0 { return vec4<f32>(maximum, -maximum, 1e38, 1.0); }
    if ctx.pixel.y < 8.0 { return vec4<f32>(1e38, -1e38, 1.0, 1e-20); }
    if ctx.pixel.y < 12.0 {
        let sign = select(-1.0, 1.0, fract(ctx.pixel.x) < 0.5);
        return vec4<f32>(sign * maximum, 0.0, 0.0, 1.0);
    }
    return vec4<f32>(maximum, -maximum, 1e38, 0.0);
}""",
    )
    image = renderer.render(
        scene, RenderSettings(width=16, height=16, frames=1, samples=samples), 0
    )
    maximum = np.finfo(np.float32).max
    np.testing.assert_allclose(image[0, 0], [maximum, -maximum, 1e38, 1], rtol=2e-6, atol=0)
    np.testing.assert_allclose(image[4, 0], [1e38, -1e38, 1, 1e-20], rtol=2e-6, atol=0)
    assert image[8, 0, 0] == (-maximum if samples == 1 else 0)
    np.testing.assert_array_equal(image[12:, :, :], 0)


@pytest.mark.parametrize(
    "pair,expected",
    [
        ([[0, 0, 0, 1], [1, 1, 1, 1]], [0.73535698, 0.73535698, 0.73535698, 1]),
        ([[1, 0, 0, 1], [0, 0, 1, 0]], [1, 0, 0, 0.5]),
        ([[1, 0, 0, 0.25], [0, 1, 0, 0.75]], [0.53709873, 0.88082502, 0, 0.5]),
        ([[1, 0, 0, 0], [0, 1, 0, 0]], [0, 0, 0, 0]),
    ],
)
def test_srgb_thumbnail_averages_light_and_coverage_before_quantization(tmp_path, pair, expected):
    # Published sRGB code values for linear 1/2, 1/4 and 3/4 are independent
    # references; opaque and transparent inputs catch different ordering errors.
    source = np.tile(np.array(pair, np.float32), (2, 2, 1))
    original = source.copy()
    resized = resize_srgb(source, 2)
    assert resized.shape == (1, 2, 4)
    np.testing.assert_allclose(resized, np.broadcast_to(expected, resized.shape), atol=2e-7, rtol=0)
    np.testing.assert_array_equal(source, original)
    path = tmp_path / "thumbnail.png"
    write_image(path, resized, space="sRGB", depth=8)
    decoded, _ = read_image(path)
    np.testing.assert_allclose(
        decoded, np.broadcast_to(expected, resized.shape), atol=1 / 255, rtol=0
    )


@pytest.mark.gpu
def test_tile_boundaries_and_seed(renderer):
    s = RenderSettings(width=300, height=160, frames=1, samples=4, tile_size=128)
    a = renderer.render(TEMPLATES["noise"], s, 12)
    b = renderer.render(TEMPLATES["noise"], s.model_copy(update={"tile_size": 256}), 12)
    np.testing.assert_array_equal(a, b)


@pytest.mark.gpu
def test_batched_readback_keeps_edge_tiles_and_frame_uniforms(renderer, monkeypatch):
    # Nine uneven tiles cross two batch boundaries. Independent pixel/time values
    # catch overlapping storage slots and uniforms overwritten before submission.
    scene = SceneSpec(
        name="batch reference",
        code="""fn shade(ctx: SceneContext) -> vec4<f32> {
            return vec4<f32>(ctx.pixel.x, -ctx.pixel.y, ctx.time, 0.375);
        }""",
    )
    settings = RenderSettings(
        width=259, height=267, frames=1, tile_size=128, samples=4,
        fps=Rational(numerator=24000, denominator=1001),
    )
    reads = []
    read_buffer = renderer.device.queue.read_buffer

    def read(*args):
        reads.append(args[2])
        return read_buffer(*args)

    monkeypatch.setattr(renderer.device.queue, "read_buffer", read)
    completed = []
    for frame in [12, 24]:
        actual = renderer.render(scene, settings, frame, lambda: completed.append(True))
        y, x = np.mgrid[:settings.height, :settings.width]
        expected = np.stack([
            x + 0.5, -(y + 0.5), np.full_like(x, frame * 1001 / 24000, dtype=float),
            np.full_like(x, 0.375, dtype=float),
        ], axis=-1).astype(np.float32)
        np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-6)
    assert len(reads) == 6  # Three synchronization points per nine-tile frame.
    assert max(reads) <= 4 * 128 * 128 * 16
    assert len(completed) == 18


@pytest.mark.gpu
def test_cancellation_does_not_submit_another_tile_batch(renderer):
    # A failed progress callback exits before the next bounded GPU submission.
    def cancel():
        raise ChromaError("cancelled", "cancelled at tile boundary")

    with pytest.raises(ChromaError, match="cancelled at tile boundary"):
        renderer.render(
            TEMPLATES["gradient"],
            RenderSettings(width=1024, height=1024, tile_size=128, frames=1),
            0, cancel,
        )
    # Cancellation cannot poison the reusable uniform/output buffers.
    result = renderer.render(
        TEMPLATES["gradient"], RenderSettings(width=16, height=16, frames=1), 1
    )
    assert np.isfinite(result).all()


@pytest.mark.gpu
def test_all_templates_and_diagnostics(renderer):
    for scene in TEMPLATES.values():
        renderer.validate(scene)
    bad = SceneSpec(
        name="bad", code="fn shade(ctx: SceneContext) -> vec4<f32> { return nonsense; }"
    )
    with pytest.raises(ChromaError, match="nonsense"):
        renderer.validate(bad)


def test_aces_input_and_output_alpha():
    np.testing.assert_allclose(
        input_rgb([0.5, 0.5, 0.5], "sRGB"), [0.21404114] * 3, rtol=2e-4, atol=1e-6
    )
    image = np.array([[[0.18, 0.18, 0.18, 0.375], [16, 16, 16, 0.75]]], dtype=np.float32)
    for output in ["sRGB", "Rec.709", "PQ", "HLG", "ACES2065-1"]:
        result = transform(image, output)
        np.testing.assert_array_equal(result[..., 3], image[..., 3])
        assert np.isfinite(result).all()
        assert np.all(result[0, 1, :3] > result[0, 0, :3])


@pytest.mark.parametrize("depth", [16, 32])
def test_exr_precision_and_chromaticities(tmp_path: Path, depth):
    values = np.random.default_rng(1).uniform(-0.25, 32, (16, 32, 4)).astype(np.float32)
    values[..., 3] = 0.5
    path = tmp_path / f"master-{depth}.exr"
    write_image(path, values, depth=depth)
    actual, meta = read_image(path)
    expected = values.astype(np.float16).astype(np.float32) if depth == 16 else values
    np.testing.assert_array_equal(actual, expected)
    assert meta["chromascript:color_space"] == "ACEScg"
    assert meta["chromaticities"][0] == pytest.approx(0.713)
    assert not meta.get("acesImageContainerFlag")


def test_half_overflow_and_integer_quantization(tmp_path: Path):
    with pytest.raises(ChromaError, match="half"):
        write_image(
            tmp_path / "overflow.exr", np.full((16, 16, 4), 70000, dtype=np.float32), depth=16
        )
    values = np.full((16, 16, 4), 0.25, dtype=np.float32)
    values[..., 3] = 1
    p = tmp_path / "display.png"
    write_image(p, values, space="sRGB", depth=16)
    a, _ = read_image(p)
    np.testing.assert_allclose(a, values, atol=1 / 65535, rtol=0)


def test_premultiplied_storage_roundtrip(tmp_path: Path):
    values = np.zeros((16, 16, 4), dtype=np.float32)
    values[:] = [2, 0.5, 0.25, 0.5]
    p = tmp_path / "associated.exr"
    write_image(p, values, alpha_mode="premultiplied")
    raw, _ = read_image(p, straight=False)
    np.testing.assert_allclose(raw[0, 0], [1, 0.25, 0.125, 0.5])
    actual, _ = read_image(p)
    np.testing.assert_allclose(actual, values)


@pytest.mark.parametrize("extension", ["png", "tiff"])
def test_straight_integer_alpha_is_not_implicitly_associated(tmp_path, extension):
    values = np.zeros((16, 16, 4), dtype=np.float32)
    values[:] = [0.25, 0.5, 0.75, 0.5]
    path = tmp_path / ("alpha." + extension)
    write_image(path, values, space="sRGB", depth=16)
    actual, _ = read_image(path)
    np.testing.assert_allclose(actual, values, rtol=0, atol=1 / 65535)


def test_nonalpha_output_composites_before_display_transform():
    from chromascript.color import render_output

    values = np.array([[[2.0, 1.0, 0.5, 0.25]]], dtype=np.float32)
    expected = transform(np.array([[[0.5, 0.25, 0.125, 1.0]]], dtype=np.float32), "sRGB")
    actual = render_output(values, "sRGB", False)
    np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-6)


@pytest.mark.gpu
def test_invalid_samples_cannot_hide_in_antialias_average(renderer):
    bad_alpha = SceneSpec(
        name="invalid alpha",
        code="fn shade(ctx: SceneContext) -> vec4<f32> { return vec4<f32>(1.0, 0.0, 0.0, select(-1.0, 2.0, fract(ctx.pixel.x)<0.5)); }",
    )
    with pytest.raises(ChromaError, match="alpha"):
        renderer.render(bad_alpha, RenderSettings(width=16, height=16, frames=1, samples=4), 0)
    bad_rgb = SceneSpec(
        name="hidden NaN",
        code="fn shade(ctx: SceneContext) -> vec4<f32> { return vec4<f32>(bitcast<f32>(0x7fc00000u), 0.0, 0.0, 0.0); }",
    )
    with pytest.raises(ChromaError, match="NaN"):
        renderer.render(bad_rgb, RenderSettings(width=16, height=16, frames=1, samples=4), 0)


def test_exr_has_exact_rational_playback_rate(tmp_path):
    path = tmp_path / "timebase.exr"
    write_image(path, np.ones((16, 16, 4), dtype=np.float32), fps=(60000, 1001), frame_index=12)
    _, meta = read_image(path)
    assert tuple(meta["FramesPerSecond"]) == (60000, 1001)
    assert meta["chromascript:frame_index"] == 12


def test_grading_space_conversion_roundtrip():
    values = np.array([[[0.18, 2.0, 0.01]]], dtype=np.float32)
    encoded = transform(values, "ACEScct")
    restored = transform(encoded, "ACEScg", source="ACEScct")
    np.testing.assert_allclose(restored, values, rtol=2e-5, atol=1e-6)


def test_standard_aces_archive_remains_strict(tmp_path):
    from chromascript.presets import builtins

    p = builtins()["aces2065-archive"]
    assert p.compression == "none" and p.bit_depth == 16
    path = tmp_path / "archive.exr"
    write_image(
        path,
        np.ones((16, 16, 4), dtype=np.float32),
        space="ACES2065-1",
        depth=p.bit_depth,
        compression=p.compression,
    )
    _, meta = read_image(path)
    assert meta["acesImageContainerFlag"] == 1
    assert meta["compression"] == "none"


def test_associated_tiff_uses_standard_alpha_metadata(tmp_path):
    # TIFF drops custom EXR attributes; ExtraSamples is its authoritative association.
    values = np.full((16, 16, 4), 0.5, dtype=np.float32)
    values[..., :3] = [0.25, 0.5, 0.75]
    path = tmp_path / "associated.tiff"
    write_image(path, values, space="sRGB", depth=16, alpha_mode="premultiplied")
    raw, metadata = read_image(path, straight=False)
    assert metadata["chromascript:alpha_mode"] == "premultiplied"
    np.testing.assert_allclose(
        raw[..., :3], values[..., :3] * values[..., 3:4], rtol=0, atol=1 / 65535
    )
    actual, _ = read_image(path)
    np.testing.assert_allclose(actual, values, rtol=0, atol=3 / 65535)
