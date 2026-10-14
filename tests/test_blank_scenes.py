"""Blank creation has no dependency on examples and remains editable on every transport."""

import asyncio
from pathlib import Path

import numpy as np
import pytest
from mcp import Client

from chromascript.errors import ChromaError
from chromascript.gpu import Renderer
from chromascript.mcp_server import create_mcp
from chromascript.models import RenderSettings, SceneSpec
from chromascript.motion import MotionSpec
from chromascript.operations import execute
from chromascript.service import Service


def test_wgsl_blank_is_the_default_and_renders_transparent(tmp_path, monkeypatch):
    service = Service(tmp_path)
    project = service.create_project("blank")["project"]["id"]
    monkeypatch.setattr(
        service, "get_template", lambda *_: pytest.fail("blank must not load a template")
    )
    result = execute(service, "create_scene", {"project_id": project})
    spec = SceneSpec.model_validate(result["scene"])
    assert spec.name == "空白场景" and not spec.parameters and not spec.values
    pixels = Renderer().render(spec, RenderSettings(width=16, height=16, frames=1), 0)
    np.testing.assert_array_equal(pixels, np.zeros((16, 16, 4), dtype=np.float32))
    explicit = execute(
        service,
        "create_scene",
        {
            "project_id": project,
            "spec": spec.model_copy(update={"name": "MCP source"}).model_dump(),
        },
    )
    assert explicit["scene"]["name"] == "MCP source"


@pytest.mark.integration
@pytest.mark.parametrize("engine", ["remotion-sdr-v1", "three-float-v1"])
def test_blank_animation_needs_no_template_and_is_previewable(tmp_path, monkeypatch, engine):
    service = Service(tmp_path)
    project = service.create_project("blank animation")["project"]["id"]
    monkeypatch.setattr(
        "chromascript.motion.templates", lambda: pytest.fail("blank must not load templates")
    )
    args = {"project_id": project}
    if engine != "remotion-sdr-v1":
        args["engine"] = engine
    result = execute(service, "create_motion_scene", args)
    assert result["scene"]["engine"] == engine
    spec = MotionSpec.model_validate(result["scene"]).model_copy(
        update={"width": 32, "height": 18, "frames": 1}
    )
    preview = service.render_motion_frame(spec)
    assert preview["images"][0]["mime_type"] == "image/png"
    if engine == "three-float-v1":
        assert preview["minimum"] == preview["maximum"] == [0, 0, 0, 0]
    updated = service.update_motion_scene(
        result["record"]["id"],
        spec.model_copy(update={"code": spec.code + "\n// MCP edit"}),
        result["revision_id"],
    )
    assert updated["revision_id"] != result["revision_id"]
    assert updated["scene"]["code"].endswith("// MCP edit")


def test_explicit_motion_engine_must_match_template_or_source(tmp_path):
    service = Service(tmp_path)
    project = service.create_project("mismatch")["project"]["id"]
    with pytest.raises(ChromaError, match="引擎"):
        service.create_motion_scene(project, template_id="title", engine="three-float-v1")
    assert not service.store.list("motion_scene")


def test_mcp_creation_schemas_make_templates_optional(tmp_path: Path):
    async def check():
        async with Client(create_mcp(tmp_path)) as client:
            tools = (await client.list_tools()).tools
            for name in ["create_scene", "create_motion_scene"]:
                tool = next(t for t in tools if t.name == "chromascript." + name)
                definitions = tool.input_schema["$defs"]
                request = next(
                    v for v in definitions.values() if "project_id" in v.get("properties", {})
                )
                assert request["properties"]["template_id"]["default"] is None
                assert "template_id" not in request["required"]

    asyncio.run(check())
