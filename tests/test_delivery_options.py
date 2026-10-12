"""Linked selections and immutable direct deliveries share one capability contract."""

from pathlib import Path

import numpy as np
import pytest

from chromascript.delivery import configured_output, delivery_options
from chromascript.encoding import check_probe, encode
from chromascript.errors import ChromaError
from chromascript.models import DeliveryConfiguration, RenderSettings
from chromascript.service import Service


def change(config, field, value):
    return DeliveryConfiguration.model_validate(
        delivery_options(config.model_copy(update={field: value}), field)["configuration"]
    )


def test_container_codec_and_alpha_linkage():
    config = change(DeliveryConfiguration(), "container", "mov")
    config = change(config, "codec", "prores")
    assert (config.profile, config.bit_depth, config.sampling) == ("hq", 10, "422")
    config = change(config, "alpha_mode", "straight")
    assert (config.profile, config.sampling, config.alpha_mode) == ("4444", "444", "straight")
    config = change(config, "profile", "proxy")
    assert config.alpha_mode == "none" and config.sampling == "422"
    config = change(config, "container", "mp4")
    assert config.codec == "h264" and config.alpha_mode == "none"
    choices = delivery_options(config)["options"]
    assert {v["value"] for v in choices["codec"]} == {"h264", "hevc"}
    assert [v["value"] for v in choices["alpha_mode"]] == ["none"]


def test_hdr_depth_and_profile_update_in_both_directions():
    config = change(DeliveryConfiguration(), "codec", "hevc")
    config = change(config, "color_output", "PQ")
    assert (config.profile, config.bit_depth, config.color_output) == ("main10", 10, "PQ")
    config = change(config, "bit_depth", 8)
    assert (config.profile, config.color_output) == ("main", "Rec.709")
    config = change(config, "bit_depth", 10)
    assert config.profile == "main10"


def test_image_storage_and_alpha_choices():
    config = change(DeliveryConfiguration(), "container", "exr")
    assert (
        config.codec,
        config.color_output,
        config.bit_depth,
        config.compression,
        config.data_range,
    ) == ("exr", "ACEScg", 16, "zip", "full")
    config = change(config, "bit_depth", 32)
    config = change(config, "compression", "piz")
    assert configured_output(config).bit_depth == 32
    config = change(config, "alpha_mode", "premultiplied")
    config = change(config, "container", "png")
    assert config.bit_depth == 16 and config.alpha_mode == "straight"
    assert "premultiplied" not in [
        x["value"] for x in delivery_options(config)["options"]["alpha_mode"]
    ]
    config = change(config, "container", "dpx")
    assert config.bit_depth == 16 and config.alpha_mode == "none"


def test_mxf_rates_quality_and_strict_submission():
    config = change(DeliveryConfiguration(), "container", "mxf")
    assert config.codec == "dnxhr" and config.quality_mode == "fixed" and config.crf is None
    rates = delivery_options(config)["frame_rates"]
    assert {r["numerator"] / r["denominator"] for r in rates}.isdisjoint({120, 100, 48})
    bad = DeliveryConfiguration(container="mp4", codec="prores")
    with pytest.raises(ChromaError, match="不兼容"):
        configured_output(bad)
    config = change(DeliveryConfiguration(), "quality_mode", "bitrate")
    assert config.bitrate_kbps == 20000 and config.crf is None
    assert configured_output(config).bitrate_kbps == 20000


def test_configured_submission_never_requires_saved_presets(tmp_path: Path):
    service = Service(tmp_path / "workspace")
    project = service.create_project("direct")["project"]
    scene = service.create_scene(project["id"], template_id="gradient")
    config = change(DeliveryConfiguration(), "container", "exr")
    settings = RenderSettings(width=32, height=32, frames=2)
    args = dict(
        scene_id=scene["record"]["id"],
        settings=settings,
        configurations=[config],
        idempotency_key="direct",
        revision_id=scene["revision_id"],
    )
    first = service.submit_configured_delivery(**args)["job"]
    assert service.submit_configured_delivery(**args)["job"]["id"] == first["id"]
    assert service.store.list("preset") == []
    output = first["outputs"][0]
    assert output["container"] == "exr" and output["bit_depth"] == 16 and not output["builtin"]
    assert service.store.get("snapshot", first["id"])["outputs"][0] == output
    assert (
        service.estimate_delivery(settings, [config])["image_outputs_upper_bound_bytes"]
        == 32 * 32 * 2 * 3 * 2
    )
    with pytest.raises(ChromaError, match="重复"):
        service.submit_configured_delivery(
            **{**args, "configurations": [config, config], "idempotency_key": "duplicate"}
        )
    mxf = change(DeliveryConfiguration(), "container", "mxf")
    with pytest.raises(ChromaError, match="timebase|broadcast"):
        service.validate_delivery_configuration(
            mxf, RenderSettings(width=320, height=180, frames=2, fps={"numerator": 120})
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "container,codec,field,value",
    [
        ("mp4", "h264", "bit_depth", 10),
        ("mov", "prores", "alpha_mode", "straight"),
        ("mxf", "dnxhr", "profile", "hqx"),
        ("mkv", "ffv1", "bit_depth", 8),
    ],
)
def test_direct_configuration_encodes_actual_native_combination(
    tmp_path, container, codec, field, value
):
    config = change(DeliveryConfiguration(), "container", container)
    config = change(config, "codec", codec)
    config = change(config, field, value)
    output = configured_output(config)
    settings = RenderSettings(width=320, height=180, frames=2)
    pixels = np.ones((180, 320, 4), dtype=np.float32) * 0.5
    pixels[..., 3] = 0.5 if output.alpha else 1
    info = encode(
        output, settings, tmp_path / ("direct." + container), iter([pixels, pixels]), lambda n: None
    )
    result = check_probe(info, output, settings)
    assert result["passed"], result["issues"]


def test_native_unavailable_format_is_not_silently_replaced(monkeypatch):
    monkeypatch.setattr("chromascript.delivery.available_encoders", lambda: set())
    assert delivery_options()["configuration"]["container"] == "exr"
    with pytest.raises(ChromaError, match="没有可用编码器"):
        delivery_options(DeliveryConfiguration())


def test_cli_direct_fields_and_explicit_conflicts(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from chromascript.cli import app
    from chromascript.operations import execute

    service = Service(tmp_path / "workspace")
    project = service.create_project("cli")["project"]
    scene = service.create_scene(project["id"], template_id="gradient")
    monkeypatch.setattr(
        "chromascript.cli.call", lambda root, name, args: execute(service, name, args)
    )
    runner = CliRunner()
    common = [
        "--workspace",
        str(service.store.root),
        "render",
        scene["record"]["id"],
        "--width",
        "320",
        "--height",
        "180",
        "--frames",
        "2",
    ]
    result = runner.invoke(
        app, [*common, "--container", "exr", "--bit-depth", "32", "--compression", "piz"]
    )
    assert result.exit_code == 0, result.output
    output = service.store.list("job")[0]["outputs"][0]
    assert (
        output["container"] == "exr"
        and output["bit_depth"] == 32
        and output["compression"] == "piz"
    )
    result = runner.invoke(app, [*common, "--codec", "hevc", "--color-output", "PQ"])
    assert result.exit_code == 0, result.output
    output = service.store.list("job")[0]["outputs"][0]
    assert output["codec"] == "libx265" and output["bit_depth"] == 10
    before = len(service.store.list("job"))
    result = runner.invoke(
        app,
        [
            *common,
            "--container",
            "mov",
            "--codec",
            "prores",
            "--profile",
            "proxy",
            "--alpha-mode",
            "straight",
        ],
    )
    assert result.exit_code == 1 and "互不兼容" in result.output
    assert len(service.store.list("job")) == before
