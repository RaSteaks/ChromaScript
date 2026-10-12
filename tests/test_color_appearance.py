"""Picker accuracy, lossless inspection and transport parity against real OCIO."""

import asyncio
import json
from itertools import product

import numpy as np
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from pydantic import ValidationError
from typer.testing import CliRunner

from chromascript.api import create_app
from chromascript.appearance import (
    PICK_PREVIEW_TOLERANCE,
    ColorAppearanceRequest,
    OklchColor,
    gamut_map,
    oklch_to_linear,
    resolve_color,
    srgb_to_oklch,
)
from chromascript.cli import app
from chromascript.color import input_rgb, transform
from chromascript.errors import ChromaError
from chromascript.mcp_server import create_mcp
from chromascript.operations import execute
from chromascript.service import Service


@pytest.mark.parametrize(
    "rgb,expected",
    [
        ([1, 0, 0], [0.62795536, 0.25768331, 29.23388519]),
        ([0, 1, 0], [0.86643961, 0.29482724, 142.49533889]),
        ([0, 0, 1], [0.45201372, 0.31321437, 264.05202064]),
        ([1, 1, 1], [1, 0, 0]),
        ([0, 0, 0], [0, 0, 0]),
    ],
)
def test_oklch_d65_reference_colors(rgb, expected):
    # Published Oklab D65 matrices, independent primary/neutral golden values.
    appearance = srgb_to_oklch(rgb)
    assert list(appearance.values()) == pytest.approx(expected, abs=2e-6)
    linear = oklch_to_linear(**appearance)
    assert linear == pytest.approx(rgb, abs=2e-7)


def test_mapping_reduces_only_chroma_and_reaches_srgb_boundary():
    color = OklchColor(lightness=0.7, chroma=0.4, hue=40)
    rgb, actual, mapped = gamut_map(color)
    assert mapped
    assert actual["lightness"] == color.lightness
    assert actual["hue"] == color.hue
    assert 0 < actual["chroma"] < color.chroma
    assert all(0 <= v <= 1 for v in rgb)
    assert max(rgb) == pytest.approx(1, abs=2e-6)
    assert srgb_to_oklch(rgb)["hue"] == pytest.approx(color.hue, abs=1e-4)


@pytest.mark.parametrize("space", ["ACEScg", "sRGB"])
def test_aces_view_round_trip_including_gamut_boundaries(space):
    maximum_error = 0.0
    for lightness, chroma, hue in product(
        [0, 0.001, 0.1, 0.5, 0.8, 0.999, 1], [0, 0.1, 0.4], range(0, 360, 30)
    ):
        color = OklchColor(lightness=lightness, chroma=chroma, hue=hue)
        picked = resolve_color(ColorAppearanceRequest(oklch=color, color_space=space))
        target = gamut_map(color)[0]
        # Render through the SAME parameter input conversion used by the shader.
        scene_rgb = input_rgb(picked["rgb"], space)
        rendered = transform(np.array(scene_rgb, dtype=np.float32), "sRGB")
        # ACES's numerical inverse is least exact near display white. The picker
        # promises a display appearance match, bounded by half an 8-bit code value.
        assert np.clip(rendered, 0, 1) == pytest.approx(target, abs=PICK_PREVIEW_TOLERANCE)
        maximum_error = max(maximum_error, picked["round_trip_error"])
    assert maximum_error < PICK_PREVIEW_TOLERANCE


@pytest.mark.parametrize("space", ["ACEScg", "sRGB"])
def test_inspection_preserves_original_extended_floats_and_never_adds_revisions(tmp_path, space):
    service = Service(tmp_path)
    project = service.create_project("Picker")
    scene = service.create_scene(project["project"]["id"], template_id="gradient")
    original = [-0.123456789123, 16.987654321, 0.0000000123456789]
    request = {"request": {"rgb": original, "color_space": space}}
    result = execute(service, "resolve_color", request)
    assert result["rgb"] == original
    assert request["request"]["rgb"] == original
    assert result["extended_range"]
    assert all(0 <= v <= 1 for v in result["display_rgb"])
    assert service.get_scene(scene["record"]["id"]) == scene
    assert service.list_revisions(scene["record"]["id"])["total"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"rgb": [0, 0]},
        {"rgb": [0, 0, 0, 1]},
        {"rgb": [True, 0, 0]},
        {"rgb": ["0.5", 0, 0]},
        {"rgb": [float("nan"), 0, 0]},
        {"rgb": [float("inf"), 0, 0]},
        {"rgb": [3.5e38, 0, 0]},
        {"rgb": [0, 0, 0], "alpha": 1},
        {"rgb": [0, 0, 0], "color_space": "PQ"},
        {"rgb": [0, 0, 0], "oklch": {"lightness": 0.5, "chroma": 0.1, "hue": 30}},
        {"oklch": {"lightness": 1.1, "chroma": 0, "hue": 0}},
        {"oklch": {"lightness": 0.5, "chroma": -0.1, "hue": 0}},
        {"oklch": {"lightness": 0.5, "chroma": 0.5, "hue": 0}},
        {"oklch": {"lightness": 0.5, "chroma": 0.1, "hue": 361}},
        {"oklch": {"lightness": 0.5, "chroma": 0.1, "hue": float("inf")}},
    ],
)
def test_picker_rejects_ambiguous_nonfinite_unsupported_and_alpha_inputs(payload):
    with pytest.raises(ValidationError):
        ColorAppearanceRequest.model_validate(payload)


def test_http_cli_and_mcp_share_picker_contract(tmp_path, monkeypatch):
    application = create_app(tmp_path)
    arguments = {"request": {"oklch": {"lightness": 0.65, "chroma": 0.15, "hue": 270}}}
    with TestClient(application, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/api/session").json()["token"]
        headers = {"Authorization": "Bearer " + token}
        response = client.post("/api/operations/resolve_color", json=arguments, headers=headers)
        assert response.status_code == 200
        assert response.json()["model"] == "OKLCH"
        bad = client.post("/api/operations/resolve_color", json={"request": {}}, headers=headers)
        assert bad.status_code == 422
        assert client.post("/api/operations/resolve_color", json=arguments).status_code == 401

    # Generic CLI calls use the same registry validation without starting a daemon.
    monkeypatch.setattr(
        "chromascript.cli.call",
        lambda root, name, args: execute(application.state.service, name, args),
    )
    invocation = CliRunner().invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "call",
            "resolve_color",
            "--arguments",
            json.dumps(arguments),
        ],
    )
    assert invocation.exit_code == 0, invocation.output
    assert json.loads(invocation.output) == response.json()
    with pytest.raises(ChromaError, match="exactly one"):
        execute(application.state.service, "resolve_color", {"request": {}})

    async def schema_check():
        async with Client(create_mcp(tmp_path)) as client:
            tools = await client.list_tools()
            tool = next(t for t in tools.tools if t.name == "chromascript.resolve_color")
            assert tool.output_schema
            definitions = tool.input_schema["$defs"]
            assert definitions["OklchColor"]["properties"]["chroma"]["maximum"] == 0.4
            assert definitions["ColorAppearanceRequest"]["properties"]["color_space"]["enum"] == [
                "ACEScg",
                "sRGB",
            ]

    asyncio.run(schema_check())
