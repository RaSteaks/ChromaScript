"""Linear authoring, revision-safe upgrades and native delivery without Chrome tests."""

import asyncio
import json

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from pydantic import ValidationError
from typer.testing import CliRunner

from chromascript.api import create_app
from chromascript.cli import app
from chromascript.color import render_output, transform
from chromascript.encoding import check_probe, encode
from chromascript.errors import ChromaError
from chromascript.images import read_image
from chromascript.mcp_server import create_mcp
from chromascript.models import MotionRate
from chromascript.motion import (
    MotionOutput,
    MotionSource,
    MotionSpec,
    blank_motion,
    compile_preview,
    motion_video_preset,
)
from chromascript.motion_color import (
    SOURCE_SPACE,
    SOURCE_SPACES,
    decode_float_frame,
    display_shader,
    working_space,
)
from chromascript.motion_worker import run_motion
from chromascript.operations import execute
from chromascript.service import Service

SPACES = [item["value"] for item in SOURCE_SPACES]


def test_legacy_default_and_strict_source_models():
    legacy = MotionSpec(code="source")
    assert legacy.source_color_space == SOURCE_SPACE
    assert blank_motion().source_color_space == "Linear Rec.2020"
    for invalid in ("PQ", "sRGB", "ACEScct", "BT2020", None):
        with pytest.raises(ValidationError):
            MotionSpec(code="source", source_color_space=invalid)
    with pytest.raises(ValidationError):
        MotionSpec(engine="remotion-sdr-v1", source_color_space="ACEScg", code="source")
    with pytest.raises(ValidationError):
        MotionSpec(engine="remotion-sdr-v1", code="source", preserved_layers=[MotionSource(code="old")])


@pytest.mark.parametrize("space", SPACES)
def test_linear_sampling_alpha_and_ocio_boundary(space):
    # One covered subpixel proves that averaging precedes unassociation, including
    # negative values, AP0/AP1 white adaptation and >1 radiance in every basis.
    raw = np.zeros((2, 2, 4), dtype="<f4")
    raw[0, 0] = [-0.25, 2.0, 0.06172835, 0.5]
    actual = decode_float_frame(raw.tobytes(), 1, 1, 2, space)
    source = np.array([[[-0.5, 4.0, 0.1234567, 0.125]]], np.float32)
    expected = transform(source, "ACEScg", source=space)
    np.testing.assert_allclose(actual, expected, atol=2e-6)
    assert actual[0, 0, 3] == 0.125
    # Linear adaptation is homogeneous; it must not insert a display curve/clamp.
    matrix = np.array(working_space(space)["to_linear_rec709"])
    values = np.array([0.12, 0.3, 4.0], np.float32)
    np.testing.assert_allclose(matrix @ values, transform(values, SOURCE_SPACE, source=space), atol=2e-6)
    assert display_shader(space)["working_space"]["source"] == space


def test_native_acescg_identity_is_exact():
    source = np.array([[[-0.125, 4.125, 0.1234567, 1]]], dtype="<f4")
    np.testing.assert_array_equal(decode_float_frame(source.tobytes(), 1, 1, source="ACEScg"), source)


def test_upgrade_preserves_source_revision_and_freezes_color_graph(tmp_path, monkeypatch):
    service = Service(tmp_path)
    pid = service.create_project("linear")["project"]["id"]
    old = service.create_motion_scene(pid, spec=MotionSpec(code="old entry", files={"part.ts": "old chapter"}))
    sid = old["record"]["id"]
    for target in ["Linear Rec.2020", "ACEScg", "ACES2065-1"]:
        updated = execute(service, "upgrade_motion_scene", {
            "scene_id": sid, "expected_revision_id": old["revision_id"], "source_color_space": target,
        })
        assert updated["scene"]["source_color_space"] == target
        assert updated["scene"]["preserved_layers"][0]["code"] == "old entry"
        assert updated["scene"]["preserved_layers"][0]["files"] == {"part.ts": "old chapter"}
        assert len(updated["scene"]["preserved_layers"]) == 1  # Empty foreground adds no phantom layer.
        assert service.get_motion_scene(sid, old["revision_id"])["scene"] == old["scene"]
        with pytest.raises(ChromaError, match="修订"):
            service.upgrade_motion_scene(sid, old["revision_id"], target)
        old = updated
    spec = MotionSpec.model_validate(updated["scene"])
    assert spec.source_digest() != spec.model_copy(update={"source_color_space": "ACEScg"}).source_digest()
    monkeypatch.setattr("chromascript.motion.motion_environment", lambda: {"render_available": True})
    monkeypatch.setattr("chromascript.motion.motion_identity", lambda: {"test": True})
    job = service.submit_motion_render(sid, updated["revision_id"], "wide", output=MotionOutput(render_scale=1))["job"]
    snapshot = service.store.get("snapshot", job["id"])
    assert snapshot["scene"] == spec.model_dump()
    assert snapshot["source_color_review"]["source_sha256"] == spec.source_digest()


@pytest.mark.parametrize("space", SPACES)
def test_compile_native_and_preserved_sources_without_browser(space):
    spec = blank_motion(source_color_space=space)
    spec.preserved_layers = [MotionSource(code=blank_motion().code)]
    compiled = compile_preview(spec.model_dump_json())
    assert "__chromaLayers" in compiled["html"]
    assert compiled["channel"] in compiled["html"]


@pytest.mark.parametrize("space", SPACES)
def test_native_pq_encoding_keeps_output_tags_and_rate(tmp_path, space):
    spec = MotionSpec(code="source", source_color_space=space, width=32, height=18, frames=1,
                      fps=MotionRate(numerator=30000, denominator=1001))
    raw = np.ones((18, 32, 4), dtype="<f4")
    raw[..., :3] = [0.08, 1.0, 0.04]
    master = decode_float_frame(raw.tobytes(), 32, 18, source=space)
    preset = motion_video_preset("pq")
    info = encode(preset, spec.settings(), tmp_path / "pq.mp4",
                  [render_output(master, "PQ", False)], lambda _: None)
    verified = check_probe(info, preset, spec.settings())
    assert verified["passed"], verified["issues"]
    assert verified["decode_verified"]
    assert verified["video"]["color_primaries"] == "bt2020"
    assert verified["video"]["color_transfer"] == "smpte2084"
    assert verified["verified_frame_rate"] == "30000/1001"


def test_cli_mcp_share_native_create_and_upgrade(tmp_path, monkeypatch):
    service = Service(tmp_path)
    pid = service.create_project("transports")["project"]["id"]
    monkeypatch.setattr("chromascript.cli.call", lambda root, op, args: execute(service, op, args))
    result = CliRunner().invoke(app, ["call", "create_motion_scene", "--arguments", json.dumps({
        "project_id": pid, "source_color_space": "ACEScg",
    })])
    assert result.exit_code == 0, result.output
    saved = json.loads(result.output)
    assert saved["scene"]["source_color_space"] == "ACEScg"
    web = create_app(tmp_path)
    real_client = httpx.AsyncClient
    monkeypatch.setattr("chromascript.mcp_server.httpx.AsyncClient",
                        lambda **kw: real_client(transport=httpx.ASGITransport(app=web), **kw))

    async def check():
        async with Client(create_mcp(tmp_path)) as client:
            tools = await client.list_tools()
            assert any(tool.name == "chromascript.upgrade_motion_scene" for tool in tools.tools)
            # Exercise discovery/resource/prompt through MCP itself: an agent must
            # learn how to preserve legacy content without guessing a field change.
            caps = await client.call_tool("chromascript.get_motion_capabilities", {"request": {}})
            guide_info = caps.structured_content["color_authoring"]
            assert guide_info["upgrade_operation"] == "chromascript.upgrade_motion_scene"
            assert guide_info["upgrade_preserves_colors"] is True
            guide = await client.read_resource(guide_info["documentation_resource"])
            shared = await client.call_tool("chromascript.get_documentation", {
                "request": {"topic": guide_info["documentation_topic"]},
            })
            assert guide.contents[0].text == shared.structured_content["text"]
            assert "Dropping preserved_layers removes" in guide.contents[0].text
            assert '"request"' in guide.contents[0].text
            prompt = await client.get_prompt("upgrade_animation_color_space", {
                "scene_id": saved["record"]["id"], "source_color_space": "ACES2065-1",
            })
            assert "expected_revision_id" in prompt.messages[0].content.text
            assert service.get_motion_scene(saved["record"]["id"])["revision_id"] == saved["revision_id"]
            result = await client.call_tool("chromascript.upgrade_motion_scene", {"request": {
                "scene_id": saved["record"]["id"], "expected_revision_id": saved["revision_id"],
                "source_color_space": "ACES2065-1",
            }})
            assert not result.is_error
            assert "ACES2065-1" in str(result)
    with TestClient(web) as client:
        token = client.get("/api/session").json()["token"]
        monkeypatch.setattr("chromascript.mcp_server.endpoint",
                            lambda root: {"url": "http://testserver", "token": token})
        asyncio.run(check())


@pytest.mark.parametrize("space,depth", [("ACEScg", 32), ("Linear Rec.2020", 16), ("ACES2065-1", None)])
def test_worker_master_and_direct_video_use_frozen_native_basis(tmp_path, monkeypatch, space, depth):
    # Browser pixels are supplied at the transport boundary; actual headless
    # WebKit GPU generation is verified separately by motion-linear-color.spec.ts.
    service = Service(tmp_path / "workspace")
    pid = service.create_project("worker")["project"]["id"]
    spec = MotionSpec(code="native source", source_color_space=space, width=32, height=18, frames=1,
                      preserved_layers=[MotionSource(code="retained source", files={"chapter.ts": "retained chapter"})])
    saved = service.create_motion_scene(pid, spec=spec)
    raw = np.ones((18,32,4), dtype="<f4")
    raw[..., :3] = [-0.02, 1.1, 0.3]
    gpu = {"source_space":space, "format":"RGBA32F", "renderer":"test-transport"}
    def frames(source, directory, indices, **kwargs):
        assert source.source_color_space == space
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "gpu.json").write_text(json.dumps(gpu))
        for index in indices:
            path = directory / f"frame-{index}.f32"
            path.write_bytes(raw.tobytes())
            try:
                yield index, path
            finally:
                path.unlink(missing_ok=True)
    def render(source, directory, indices, callback, **kwargs):
        for index, path in frames(source, directory, indices):
            callback(index,path)
        return gpu
    monkeypatch.setattr("chromascript.motion_worker.iter_float_frames", frames)
    monkeypatch.setattr("chromascript.motion_worker.render_float_frames", render)
    monkeypatch.setattr("chromascript.motion.motion_environment", lambda: {"render_available":True})
    monkeypatch.setattr("chromascript.motion.motion_identity", lambda: {"test":True})
    monkeypatch.setattr("chromascript.motion_worker.motion_identity", lambda: {"test":True})
    job = service.submit_motion_render(saved["record"]["id"], saved["revision_id"], "worker",
        export_directory=str(tmp_path / "output"), output=MotionOutput(master_bit_depth=depth, video="pq", render_scale=1))["job"]
    run_motion(service.store.root, job["id"])
    result = service.get_job(job["id"])["job"]
    assert result["status"] == "completed", result["error"]
    from pathlib import Path
    output = Path(result["export_path"])
    report = json.loads((output / "verification.json").read_text())
    assert report["verification"]["source_color_space"] == space
    assert report["verification"]["video"]["decode_verified"]
    assert (output / "preserved-layers/0/sources/chapter.ts").read_text() == "retained chapter"
    assert json.loads((output / "animation.json").read_text())["source_color_space"] == space
    if depth:
        actual, _ = read_image(output / "masters/00000000.exr")
        expected = decode_float_frame(raw.tobytes(), 32, 18, source=space)
        if depth == 16:
            expected = expected.astype(np.float16).astype(np.float32)
        np.testing.assert_array_equal(actual,expected)
    else:
        assert not (output / "masters").exists()
