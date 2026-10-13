"""Requested gamut choices must represent real pixels and native range/color signaling."""

import json
import subprocess

import numpy as np
import PyOpenColorIO as ocio
import pytest

from chromascript.color import config, output_metadata, transform
from chromascript.delivery import configured_output, delivery_options
from chromascript.encoding import check_image_metadata, check_probe, encode
from chromascript.errors import ChromaError
from chromascript.images import read_image, write_image
from chromascript.models import (
    EXR_OUTPUTS,
    OUTPUT_ENCODINGS,
    ColorPipeline,
    DeliveryConfiguration,
    DeliveryPreset,
    RenderSettings,
)


def choices(**fields):
    return delivery_options(DeliveryConfiguration(**fields))


def test_all_gamuts_and_ranges_remain_discoverable_with_honest_restrictions():
    result = choices()
    spaces = {item["value"]: item for item in result["options"]["color_space"]}
    assert {"P3-D65", "Rec.709", "Rec.2020", "ACEScct", "ACEScg"} <= spaces.keys()
    assert not spaces["P3-D65"]["disabled"]
    assert not spaces["ACEScct"]["disabled"]  # Selecting it promotes H.264 to 10-bit.
    assert spaces["ACEScg"]["disabled"] and "EXR" in spaces["ACEScg"]["reason"]
    ranges = result["options"]["data_range"]
    assert [item["value"] for item in ranges] == ["auto", "full", "limited"]
    assert not any(item["disabled"] for item in ranges)
    for container, codec, fixed in [("exr", "exr", "full"), ("mov", "prores", "limited")]:
        result = choices(container=container, codec=codec)
        options = {item["value"]: item for item in result["options"]["data_range"]}
        assert not options[fixed]["disabled"]
        blocked = "limited" if fixed == "full" else "full"
        assert options[blocked]["disabled"] and options[blocked]["reason"]


@pytest.mark.parametrize(
    "alias,canonical",
    [
        ("p3", "P3-D65"),
        ("709", "Rec.709"),
        ("bt2020", "Rec.2020"),
        ("acescct", "ACEScct"),
        ("aces cg", "ACEScg"),
    ],
)
def test_input_aliases_have_one_canonical_configuration(alias, canonical):
    value = DeliveryConfiguration(color_space=alias, data_range="limit")
    assert value.color_space == canonical
    assert value.data_range == "limited"


def test_log_selection_promotes_precision_and_float_selection_preserves_log():
    value = DeliveryConfiguration().model_copy(update={"color_space": "ACEScct"})
    result = delivery_options(value, "color_space")
    selected = DeliveryConfiguration.model_validate(result["configuration"])
    assert (selected.color_output, selected.gamma, selected.bit_depth) == ("ACEScct", "acescct", 10)
    assert configured_output(selected).color.output == "ACEScct"
    assert result["color_encoding"]["reference"] == "scene"
    assert result["color_encoding"]["dynamic_range"] == "scene-log"
    assert result["color_encoding"]["target_display_peak_nits"] is None
    result = delivery_options(selected.model_copy(update={"container": "exr"}), "container")
    assert result["configuration"]["color_output"] == "ACEScct"
    assert result["configuration"]["data_range"] == "auto"
    assert result["resolved_data_range"] == "full"
    assert not any(
        item["disabled"] for item in result["options"]["color_space"] if item["value"] == "ACEScg"
    )


def test_p3_matches_the_official_p3_view_instead_of_relabeling_709():
    pixels = np.array([[[0.7, 0.05, 0.01, 0.4], [0.03, 0.9, 0.02, 0.8]]], np.float32)
    for output, display, view in [
        ("Display-P3", "Display P3 - Display", "ACES 2.0 - SDR 100 nits (P3 D65)"),
        ("P3-PQ", "ST2084-P3-D65 - Display", "ACES 2.0 - HDR 1000 nits (P3 D65)"),
    ]:
        reference = (
            config()
            .getProcessor(ocio.DisplayViewTransform(src="ACEScg", display=display, view=view))
            .getDefaultCPUProcessor()
        )
        expected = np.array([reference.applyRGB(point[:3].tolist()) for point in pixels[0]])
        actual = transform(pixels, output)
        np.testing.assert_allclose(actual[0, :, :3], expected, rtol=2e-6, atol=2e-6)
        np.testing.assert_array_equal(actual[..., 3], pixels[..., 3])
        assert output_metadata(output)["limiting_gamut"] == "P3-D65"
    assert not np.allclose(
        transform(pixels, "Display-P3")[..., :3], transform(pixels, "sRGB")[..., :3]
    )


def test_p3_gamut_switch_retains_gamma_and_auto_freezes_the_codec_range():
    mov = choices(container="mov", codec="prores")
    draft = DeliveryConfiguration.model_validate(mov["configuration"])
    result = delivery_options(draft.model_copy(update={"color_space": "P3-D65"}), "color_space")
    selected = DeliveryConfiguration.model_validate(result["configuration"])
    assert (selected.gamma, selected.color_output) == ("gamma2.4", "P3-Gamma2.4")
    assert {item["value"] for item in result["options"]["gamma"] if not item["disabled"]} == {
        "gamma2.2",
        "gamma2.4",
        "gamma2.6",
        "srgb",
        "st2084",
    }
    assert selected.data_range == "auto" and result["resolved_data_range"] == "limited"
    assert configured_output(selected).data_range == "limited"
    exr = choices(container="exr")
    assert exr["configuration"]["data_range"] == "auto" and exr["resolved_data_range"] == "full"
    assert (
        configured_output(DeliveryConfiguration.model_validate(exr["configuration"])).data_range
        == "full"
    )
    assert result["file_tags"]["gamma"] == "未指定（按 Gamma 2.4 手动解读）"
    assert result["color_encoding"]["requires_manual_video_interpretation"]
    assert "手动" in result["color_note"]
    assert result["specification"] == "resolve-21-delivery-v1"


@pytest.mark.parametrize(
    "output,gamma,display,reference_gamma",
    [
        ("P3-Gamma2.2", 2.2, "P3-D65 - Display", 2.6),
        ("P3-Gamma2.4", 2.4, "P3-D65 - Display", 2.6),
        ("P3-Gamma2.6", 2.6, "P3-D65 - Display", 2.6),
        ("Rec.709-Gamma2.2", 2.2, "Rec.1886 Rec.709 - Display", 2.4),
        ("Rec.709-Gamma2.6", 2.6, "Rec.1886 Rec.709 - Display", 2.4),
    ],
)
def test_power_curves_preserve_the_same_rendered_linear_picture_and_alpha(
    output, gamma, display, reference_gamma
):
    # The official OCIO displays provide independent Gamma 2.6/2.4 references.
    # Re-encoding that result analytically must match, without a second ACES tone map.
    pixels = np.array([[[0.18, 0.18, 0.18, 0.37], [0.9, 0.03, 0.4, 0.8]]], np.float32)
    reference = (
        config()
        .getProcessor(
            ocio.DisplayViewTransform(
                src="ACEScg",
                display=display,
                view="ACES 2.0 - SDR 100 nits (P3 D65)"
                if output.startswith("P3-")
                else "ACES 2.0 - SDR 100 nits (Rec.709)",
            )
        )
        .getDefaultCPUProcessor()
    )
    encoded = np.array([reference.applyRGB(pixel[:3].tolist()) for pixel in pixels[0]])
    expected = np.sign(encoded) * np.abs(encoded) ** (reference_gamma / gamma)
    actual = transform(pixels, output)
    np.testing.assert_allclose(actual[0, :, :3], expected, atol=2e-5, rtol=2e-5)
    np.testing.assert_array_equal(actual[..., 3], pixels[..., 3])
    assert output_metadata(output)["target_display_peak_nits"] == 100


def test_unrepresentable_gamma_is_disabled_and_rejected_outside_mov():
    from pydantic import ValidationError

    result = choices(container="mp4", color_space="P3-D65")
    options = {item["value"]: item for item in result["options"]["gamma"]}
    for gamma in ["gamma2.4", "gamma2.6"]:
        assert options[gamma]["disabled"] and "MOV" in options[gamma]["reason"]
        with pytest.raises(ChromaError, match="不兼容"):
            configured_output(DeliveryConfiguration(color_space="P3-D65", gamma=gamma))
    # The legacy preset operation must not bypass the shared file-metadata limit.
    with pytest.raises(ValidationError, match="manual interpretation"):
        DeliveryPreset(
            id="bad-p3",
            name="invalid",
            family="test",
            container="mp4",
            codec="libx264",
            pixel_format="yuv420p10le",
            color={"output": "P3-Gamma2.6"},
        )


@pytest.mark.parametrize("output", [name for name in OUTPUT_ENCODINGS if name not in EXR_OUTPUTS])
def test_legacy_exr_presets_reject_display_encodings(output):
    from pydantic import ValidationError

    # Saving/importing a preset must enforce the same EXR boundary as direct delivery.
    with pytest.raises(ValidationError, match="EXR delivery requires"):
        DeliveryPreset(
            id="unsupported-exr",
            name="invalid",
            family="test",
            container="exr",
            codec="exr",
            bit_depth=32,
            color={"output": output},
        )


@pytest.mark.parametrize("output", EXR_OUTPUTS)
@pytest.mark.parametrize("depth", [16, 32])
def test_supported_exr_presets_retain_native_color_metadata(tmp_path, output, depth):
    preset = DeliveryPreset(
        id="supported-exr",
        name="scene encoding",
        family="test",
        container="exr",
        codec="exr",
        bit_depth=depth,
        alpha=True,
        color={"output": output},
    )
    # Preserve supported scene-linear/log encodings and their native AP0/AP1 declarations.
    pixels = np.full((16, 16, 4), 0.18, np.float32)
    pixels[..., 3] = 0.37
    path = tmp_path / "scene.exr"
    write_image(path, transform(pixels, output), space=output, depth=depth)
    _, metadata = read_image(path)
    assert metadata.get("chromaticities") is not None
    assert metadata.get("colorInteropID")
    assert check_image_metadata(metadata, preset) == []


@pytest.mark.parametrize("output", ["P3-Gamma2.2", "Rec.709-Gamma2.2"])
def test_unchecked_exr_display_encoding_never_passes_native_verification(tmp_path, output):
    preset = DeliveryPreset(
        id="unchecked-exr",
        name="unchecked",
        family="test",
        container="exr",
        codec="exr",
        bit_depth=32,
        alpha=True,
        color={"output": "ACEScg"},
    )
    # Replay an unchecked historical payload after ingress validation has been fixed.
    unchecked = preset.model_copy(update={"color": ColorPipeline(output=output)})
    pixels = np.full((16, 16, 4), 0.18, np.float32)
    pixels[..., 3] = 1
    path = tmp_path / "unsupported.exr"
    write_image(path, transform(pixels, output), space=output, depth=32)
    _, metadata = read_image(path)
    assert "Unsupported EXR color encoding" in check_image_metadata(metadata, unchecked)


@pytest.mark.integration
@pytest.mark.parametrize(
    "space,gamma,transfer",
    [
        ("P3-D65", "gamma2.2", 4),
        ("P3-D65", "gamma2.4", 2),
        ("P3-D65", "gamma2.6", 2),
        ("P3-D65", "srgb", 13),
        ("P3-D65", "st2084", 16),
        ("Rec.709", "gamma2.2", 4),
        ("Rec.709", "gamma2.4", 2),
        ("Rec.709", "gamma2.6", 2),
    ],
)
def test_prores_native_gamma_and_decoded_pixels(tmp_path, space, gamma, transfer):
    from chromascript.color import render_output

    result = choices(container="mov", codec="prores", color_space=space, gamma=gamma)
    preset = configured_output(DeliveryConfiguration.model_validate(result["configuration"]))
    settings = RenderSettings(width=320, height=180, frames=1)
    linear = np.full((180, 320, 4), 0.18, np.float32)
    linear[:, 160:, :3] = [0.8, 0.1, 0.3]
    linear[..., 3] = 1
    rendered = render_output(linear, preset.color.output, keep_alpha=False)
    path = tmp_path / "p3.mov"
    native = encode(preset, settings, path, [rendered], lambda _: None)
    checked = check_probe(native, preset, settings)
    assert checked["passed"], checked["issues"]
    assert native["container_color"][0]["primaries"] == (12 if space == "P3-D65" else 1)
    assert native["container_color"][0]["transfer"] == transfer
    assert "gamma" not in native["container_color"][0]
    if gamma in {"gamma2.4", "gamma2.6"}:
        # Native declarations preserve intent, not automatic decoder interpretation.
        from copy import deepcopy

        assert checked["color_encoding"]["requires_manual_video_interpretation"]
        declaration = json.loads(native["format"]["tags"]["comment"])
        assert declaration["primaries"] == space and declaration["transfer"] == gamma
        missing = deepcopy(native)
        missing["format"]["tags"].pop("comment")
        assert not check_probe(missing, preset, settings)["passed"]
        wrong = deepcopy(native)
        wrong["container_color"][0]["gamma"] = float(gamma[5:])
        assert (
            "Conflicting QuickTime colr/gama metadata"
            in check_probe(wrong, preset, settings)["issues"]
        )
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
            "rgb48le",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    pixels = np.frombuffer(decoded.stdout, dtype="<u2").reshape(180, 320, 3) / 65535
    for x in [40, 200]:
        np.testing.assert_allclose(
            pixels[40:140, x : x + 80].mean(axis=(0, 1)), rendered[80, x, :3], atol=0.006, rtol=0
        )


def test_acescct_matches_the_spec_and_float_exr_preserves_unbounded_codes(tmp_path):
    # AP1 is shared by source ACEScg and ACEScct, so the published curve alone is
    # an independent reference. Its linear toe includes negative scene values.
    values = np.array([-0.02, 0, 0.0078125, 0.18, 1, 65504], np.float32)
    pixels = np.ones((1, len(values), 4), np.float32)
    pixels[..., :3] = values[None, :, None]
    pixels[..., 3] = 0.37
    encoded = transform(pixels, "ACEScct")
    expected = np.where(
        values <= 0.0078125,
        10.5402377416545 * values + 0.0729055341958355,
        (np.log2(np.maximum(values, 1e-30)) + 9.72) / 17.52,
    )
    np.testing.assert_allclose(encoded[0, :, 0], expected, rtol=2e-6, atol=2e-6)
    assert encoded.min() < 0 and encoded.max() > 1
    np.testing.assert_array_equal(encoded[..., 3], pixels[..., 3])
    path = tmp_path / "acescct.exr"
    write_image(path, encoded, space="ACEScct", depth=32)
    restored, metadata = read_image(path)
    np.testing.assert_array_equal(restored, encoded)
    result = choices(container="exr", color_space="ACEScct", bit_depth=32, alpha_mode="straight")
    preset = configured_output(DeliveryConfiguration.model_validate(result["configuration"]))
    assert not check_image_metadata(metadata, preset)
    assert metadata["colorInteropID"] == "ocio:acescct_ap1_scene"
    assert not metadata.get("acesImageContainerFlag")


@pytest.mark.integration
@pytest.mark.parametrize(
    "space,gamma,codec,container,data_range",
    [
        ("P3-D65", "srgb", "h264", "mp4", "full"),
        ("P3-D65", "srgb", "h264", "mp4", "limited"),
        ("P3-D65", "st2084", "hevc", "mp4", "limited"),
        ("ACEScct", "acescct", "prores", "mov", "limited"),
        ("ACEScct", "acescct", "hevc", "mp4", "full"),
    ],
)
def test_native_encodings_and_range_levels(tmp_path, space, gamma, codec, container, data_range):
    result = choices(
        container=container, codec=codec, color_space=space, gamma=gamma, data_range=data_range
    )
    assert result["configuration"]["color_space"] == space
    assert result["configuration"]["gamma"] == gamma
    preset = configured_output(DeliveryConfiguration.model_validate(result["configuration"]))
    settings = RenderSettings(width=320, height=180, frames=1)
    frame = np.ones((180, 320, 4), np.float32)
    frame[:, :160, :3] = 0
    path = tmp_path / f"native.{container}"
    probed = encode(preset, settings, path, [frame], lambda n: None)
    checked = check_probe(probed, preset, settings)
    assert checked["passed"], checked["issues"]
    if space == "P3-D65":
        assert checked["video"]["color_primaries"] == "smpte432"
        assert probed["container_color"][0]["primaries"] == 12
    else:
        assert checked["color_encoding"]["requires_manual_video_interpretation"]
        declaration = json.loads(probed["format"]["tags"]["comment"])
        assert declaration["primaries"] == "AP1" and declaration["transfer"] == "ACEScct"
        assert probed["container_color"][0]["primaries"] == 2
    if codec == "h264":
        # Decode native luma samples: a tag alone cannot prove the actual range mapping.
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
                "yuv420p",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
        )
        y = np.frombuffer(decoded.stdout, np.uint8)[: 320 * 180].reshape(180, 320)
        black, white = (0, 255) if data_range == "full" else (16, 235)
        assert y[40:140, 20:120].mean() == pytest.approx(black, abs=2)
        assert y[40:140, 200:300].mean() == pytest.approx(white, abs=2)


def test_integer_acescct_cannot_silently_clip_code_values(tmp_path):
    result = choices(codec="hevc", color_space="ACEScct")
    preset = configured_output(DeliveryConfiguration.model_validate(result["configuration"]))
    settings = RenderSettings(width=32, height=32, frames=1)
    frame = np.ones((32, 32, 4), np.float32)
    frame[0, 0, 0] = -0.1
    with pytest.raises(ChromaError, match="float EXR"):
        encode(preset, settings, tmp_path / "out.mp4", [frame], lambda n: None)
    assert not (tmp_path / "out.partial.mp4").exists()
