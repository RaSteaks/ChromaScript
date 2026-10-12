"""Live proxies are bounded, revision-free and faithful to color/alpha/frame semantics."""

import base64
from io import BytesIO

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from chromascript.api import create_app
from chromascript.color import transform
from chromascript.errors import ChromaError
from chromascript.gpu import Renderer
from chromascript.models import Parameter, Rational, RenderSettings, SceneSpec
from chromascript.operations import REGISTRY, execute
from chromascript.preview import LivePreviewRequest, preview_image
from chromascript.service import Service
from chromascript.templates import TEMPLATES


@pytest.fixture(scope="module")
def renderer():
    return Renderer()


@pytest.mark.parametrize(
    "size, expected",
    [
        ((7680, 4320), (480, 270)),
        ((4320, 7680), (270, 480)),
        ((8192, 8192), (480, 480)),
        ((32, 16), (32, 16)),
        ((8192, 16), (480, 16)),
    ],
)
def test_preview_budget_and_rational_time(size, expected):
    request = LivePreviewRequest(
        spec=TEMPLATES["gradient"],
        width=size[0],
        height=size[1],
        fps=Rational(numerator=24000, denominator=1001),
        frame=120,
    )
    settings = request.settings()
    assert (settings.width, settings.height) == expected
    assert settings.samples == 1 and settings.frames == 1
    assert settings.fps.ffmpeg == "24000/1001"


@pytest.mark.parametrize(
    "extra",
    [
        {"width": 8193},
        {"height": 0},
        {"frame": -1},
        {"frame": 1.5},
        {"frame": 10_000_001},
        {"samples": 16},
        {"unexpected": True},
    ],
)
def test_preview_rejects_invalid_inputs(extra):
    with pytest.raises(ValidationError):
        LivePreviewRequest(spec=TEMPLATES["gradient"], **extra)
    assert "render_live_preview" in REGISTRY


def test_preview_encodes_display_copy_and_straight_alpha():
    source = np.array([[[-0.25, 8.0, 0.18, 0.375]]], dtype=np.float32)
    original = source.copy()
    result = preview_image(source)
    np.testing.assert_array_equal(source, original)
    with Image.open(BytesIO(base64.b64decode(result["data"]))) as png:
        assert png.info["srgb"] == 0
        assert png.mode == "RGBA"
        expected = np.rint(np.clip(transform(original, "sRGB"), 0, 1) * 255).astype(np.uint8)
        np.testing.assert_array_equal(np.asarray(png), expected)
        assert np.asarray(png)[0, 0, 3] == round(0.375 * 255)


@pytest.mark.gpu
def test_preview_draft_never_writes_revisions_jobs_or_artifacts(tmp_path, renderer):
    service = Service(tmp_path)
    service._renderer = renderer
    project = service.create_project("live draft")["project"]
    saved = service.create_scene(project["id"], template_id="gradient")
    draft = SceneSpec(
        name="unsaved draft",
        code="""fn shade(ctx: SceneContext) -> vec4<f32> {
            return vec4<f32>(ctx.params.exposure, ctx.time, 8.0, 0.375);
        }""",
        parameters=[Parameter(name="exposure", default=-0.25)],
    )
    tables = ["project", "scene", "revision", "job", "artifact"]
    before = {table: service.store.list(table) for table in tables}
    files = set(tmp_path.rglob("*"))
    args = {
        "request": {
            "spec": draft.model_dump(),
            "width": 7680,
            "height": 4320,
            "fps": {"numerator": 24000, "denominator": 1001},
            "frame": 12,
        }
    }
    result = execute(service, "render_live_preview", args)
    assert result["transient"] and result["samples"] == 1 and result["frame"] == 12
    with Image.open(BytesIO(base64.b64decode(result["images"][0]["data"]))) as png:
        assert png.size == (480, 270)
        expected = transform(
            np.array([[[-0.25, 12 * 1001 / 24000, 8.0, 0.375]]], np.float32), "sRGB"
        )
        np.testing.assert_array_equal(
            np.asarray(png)[135, 240], np.rint(np.clip(expected[0, 0], 0, 1) * 255).astype(np.uint8)
        )
    # Parameter/time edits reuse the compiled program without touching durable state.
    cached = list(renderer.cache.values())[-1]
    args["request"]["spec"]["values"] = {"exposure": 0.5}
    args["request"]["frame"] = 24
    changed = execute(service, "render_live_preview", args)
    assert changed["images"][0]["data"] != result["images"][0]["data"]
    assert list(renderer.cache.values())[-1] is cached
    assert {table: service.store.list(table) for table in tables} == before
    assert set(tmp_path.rglob("*")) == files
    assert service.get_scene(saved["record"]["id"])["revision_id"] == saved["revision_id"]


@pytest.mark.gpu
def test_reduced_raster_preserves_logical_pixel_coordinates(renderer):
    scene = SceneSpec(
        name="coordinates",
        code="""
        fn shade(ctx: SceneContext) -> vec4<f32> {
            return vec4<f32>(ctx.pixel.x / 1920.0, ctx.pixel.y / 1080.0,
                             ctx.resolution.x / 4000.0, ctx.uv.y);
        }""",
    )
    image = renderer.render(
        scene, RenderSettings(width=32, height=16, samples=1), 0, resolution=(1920, 1080)
    )
    for x, y in [(0, 0), (16, 8), (31, 15)]:
        np.testing.assert_allclose(
            image[y, x],
            [(x + 0.5) / 32, (y + 0.5) / 16, 1920 / 4000, 1 - (y + 0.5) / 16],
            rtol=0,
            atol=1e-6,
        )


@pytest.mark.gpu
def test_interactive_code_pipeline_cache_is_bounded(renderer):
    for index in range(18):
        renderer.validate(
            TEMPLATES["gradient"].model_copy(
                update={"code": TEMPLATES["gradient"].code + f"\n// edit {index}"}
            )
        )
    assert len(renderer.cache) == 16


@pytest.mark.gpu
def test_http_live_preview_uses_shared_strict_contract(tmp_path, renderer):
    app = create_app(tmp_path)
    app.state.service._renderer = renderer
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/api/session").json()["token"]
        headers = {"Authorization": "Bearer " + token}
        body = {"request": {"spec": TEMPLATES["gradient"].model_dump(), "width": 32, "height": 16}}
        assert client.post("/api/operations/render_live_preview", json=body).status_code == 401
        response = client.post("/api/operations/render_live_preview", json=body, headers=headers)
        assert response.status_code == 200
        assert response.json()["images"][0]["mime_type"] == "image/png"
        assert app.state.service.list_jobs()["total"] == 0
        body["request"]["samples"] = 16
        assert (
            client.post(
                "/api/operations/render_live_preview", json=body, headers=headers
            ).status_code
            == 422
        )
        with pytest.raises(ChromaError, match="extra"):
            execute(app.state.service, "render_live_preview", body)
