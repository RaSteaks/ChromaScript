"""Local glTF resources are immutable scene data, never renderer filesystem paths."""

import asyncio
import base64
import json
import shutil
import struct
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from pydantic import ValidationError
from typer.testing import CliRunner

from chromascript.api import create_app
from chromascript.cli import app
from chromascript.errors import ChromaError
from chromascript.mcp_server import create_mcp
from chromascript.motion import MotionSpec
from chromascript.motion_assets import import_gltf
from chromascript.motion_sources import source_hash, write_sources
from chromascript.operations import catalogue, execute
from chromascript.project_storage import migrate
from chromascript.service import Service

FIXTURE = Path(__file__).parent / "fixtures" / "motion-assets"


@pytest.mark.parametrize("binary", [False, True])
def test_import_freezes_model_dependencies_and_exports_portably(tmp_path, binary):
    source = tmp_path / "local"
    shutil.copytree(FIXTURE, source)
    path = source / "triangle.gltf"
    original = json.loads(path.read_text())
    if binary:
        del original["buffers"][0]["uri"]
        payload = json.dumps(original).encode()
        payload += b" " * (-len(payload) % 4)
        geometry = (source / "triangle.bin").read_bytes()
        geometry += b"\0" * (-len(geometry) % 4)
        chunks = struct.pack("<II", len(payload), 0x4E4F534A) + payload
        chunks += struct.pack("<II", len(geometry), 0x004E4942) + geometry
        path = source / "triangle.glb"
        path.write_bytes(struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)) + chunks)
    frozen = import_gltf(path)
    document = json.loads(frozen)
    assert base64.b64decode(document["buffers"][0]["uri"].split(",")[1]) == (FIXTURE / "triangle.bin").read_bytes()
    assert base64.b64decode(document["images"][0]["uri"].split(",")[1]) == (FIXTURE / "color.png").read_bytes()
    assert document["animations"] == original["animations"]
    shutil.rmtree(source)
    spec = MotionSpec(code="export default function(){}", assets={"horse": frozen})
    paths = write_sources(tmp_path / "export", spec.engine, spec.code, spec.files, spec.assets)
    asset = tmp_path / "export" / "assets" / "horse.gltf"
    assert asset in paths and import_gltf(asset) == frozen
    assert spec.source_digest() != source_hash(spec.code, spec.files)
    assert source_hash(spec.code, spec.files, {}) == source_hash(spec.code, spec.files)


@pytest.mark.parametrize("uri", [
    "https://example.invalid/model.bin", "file:///tmp/model.bin", "/tmp/model.bin",
    "../escape.bin", "%2e%2e/escape.bin", "model.bin?version=1", "missing.bin",
])
def test_import_rejects_external_or_missing_dependencies(tmp_path, uri):
    document = json.loads((FIXTURE / "triangle.gltf").read_text())
    document["buffers"][0]["uri"] = uri
    path = tmp_path / "bad.gltf"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        import_gltf(path)


def test_import_rejects_symlink_escape(tmp_path):
    source = tmp_path / "model"
    shutil.copytree(FIXTURE, source)
    (source / "triangle.bin").unlink()
    (source / "triangle.bin").symlink_to(FIXTURE / "triangle.bin")
    with pytest.raises(ValueError, match="模型目录"):
        import_gltf(source / "triangle.gltf")


@pytest.mark.parametrize("mutation", [
    lambda doc: doc["buffers"][0].update(uri="https://example.invalid/data"),
    lambda doc: doc["buffers"][0].update(byteLength=1),
    lambda doc: doc["buffers"][0].update(uri="data:application/octet-stream;base64,%%%"),
    lambda doc: doc["bufferViews"][0].update(byteLength=100000),
    lambda doc: doc["images"][0].update(uri="file:///tmp/image.png"),
    lambda doc: doc.update(extensionsRequired=["KHR_draco_mesh_compression"]),
    lambda doc: doc.update(extensionsRequired=["KHR_meshopt_compression"]),
    lambda doc: doc.update(extensionsRequired=["unsupported_required_extension"]),
    lambda doc: doc.update(asset={"version": "1.0"}),
])
def test_direct_specs_cannot_bypass_asset_validation(mutation):
    document = json.loads(import_gltf(FIXTURE / "triangle.gltf"))
    mutation(document)
    with pytest.raises(ValidationError):
        MotionSpec(code="export {};", assets={"horse": json.dumps(document)})


@pytest.mark.parametrize("names", [["../escape"], ["/absolute"], ["horse", "HORSE"], ["x" * 81]])
def test_asset_names_are_portable(names):
    frozen = import_gltf(FIXTURE / "triangle.gltf")
    with pytest.raises(ValidationError):
        MotionSpec(code="export {};", assets=dict.fromkeys(names, frozen))


def test_asset_size_limits_and_invalid_glb(tmp_path, monkeypatch):
    monkeypatch.setattr("chromascript.motion_assets.MAX_ASSET_BYTES", 10)
    with pytest.raises(ValueError, match="64 MiB"):
        import_gltf(FIXTURE / "triangle.gltf")
    with pytest.raises(ValidationError, match="64 MiB"):
        MotionSpec(code="export {};", assets={"horse": '{"asset":{"version":"2.0"}}'})
    path = tmp_path / "bad.glb"
    path.write_bytes(b"bad")
    with pytest.raises(ValueError, match="GLB 2"):
        import_gltf(path)


def test_registered_import_preserves_revisions_and_frozen_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr("chromascript.motion.motion_identity", lambda: {"test": 1})
    monkeypatch.setattr("chromascript.motion.motion_environment", lambda: {"render_available": True})
    service = Service(tmp_path / "workspace")
    project = service.create_project("local model")["project"]["id"]
    spec = MotionSpec(code="export default function(){}", frames=2)
    saved = service.create_motion_scene(project, spec=spec)
    args = {"scene_id": saved["record"]["id"], "expected_revision_id": saved["revision_id"], "name": "horse", "path": str(FIXTURE / "triangle.gltf")}
    imported = execute(service, "import_motion_gltf", args)
    assert "chromascript.import_motion_gltf" in {op["name"] for op in catalogue()}
    assert imported["scene"]["code"] == spec.code and imported["scene"]["frames"] == 2
    assert service.get_motion_scene(args["scene_id"], saved["revision_id"])["scene"]["assets"] == {}
    with pytest.raises(ChromaError, match="修订已改变"):
        execute(service, "import_motion_gltf", args)
    reviewed = service.record_motion_color_review(args["scene_id"], imported["revision_id"], "test", "asset review")
    job = service.submit_motion_render(args["scene_id"], imported["revision_id"], "asset-snapshot")["job"]
    updated = service.update_motion_scene(args["scene_id"], spec, imported["revision_id"])
    assert updated["scene"]["assets"] == {}
    assert service.store.get("snapshot", job["id"])["scene"]["assets"] == imported["scene"]["assets"]
    assert service.get_motion_scene(args["scene_id"], imported["revision_id"])["color_review"] == reviewed["color_review"]


def test_color_upgrade_keeps_assets_in_the_original_rec709_layer(tmp_path):
    service = Service(tmp_path / "workspace")
    project = service.create_project("asset color")["project"]["id"]
    frozen = import_gltf(FIXTURE / "triangle.gltf")
    spec = MotionSpec(code="export default function(){}", assets={"horse": frozen})
    with pytest.raises(ValidationError, match="不能直接重标记"):
        MotionSpec.model_validate({**spec.model_dump(), "source_color_space": "ACEScg"})
    saved = service.create_motion_scene(project, spec=spec)
    upgraded = service.upgrade_motion_scene(saved["record"]["id"], saved["revision_id"], "ACEScg")
    assert upgraded["scene"]["assets"] == {}
    assert upgraded["scene"]["preserved_layers"][0]["assets"] == spec.assets


def test_cli_and_mcp_import_use_the_same_revision_contract(tmp_path, monkeypatch):
    service = Service(tmp_path)
    project = service.create_project("asset transports")["project"]["id"]
    saved = service.create_motion_scene(project, engine="remotion-sdr-v1")
    args = {"scene_id": saved["record"]["id"], "expected_revision_id": saved["revision_id"],
            "name": "horse", "path": str(FIXTURE / "triangle.gltf")}
    monkeypatch.setattr("chromascript.cli.call", lambda root, op, arguments: execute(service, op, arguments))
    reply = CliRunner().invoke(app, ["call", "import_motion_gltf", "--arguments", json.dumps(args)])
    assert reply.exit_code == 0, reply.output
    imported = json.loads(reply.output)
    inspection = CliRunner().invoke(app, ["call", "get_motion_asset_info", "--arguments", json.dumps({
        "scene_id": args["scene_id"], "name": "horse",
    })])
    assert inspection.exit_code == 0, inspection.output
    expected_info = json.loads(inspection.output)
    assert "base64," not in inspection.output
    args["expected_revision_id"] = imported["revision_id"]
    web = create_app(tmp_path)
    real_client = httpx.AsyncClient
    monkeypatch.setattr("chromascript.mcp_server.httpx.AsyncClient",
                        lambda **kw: real_client(transport=httpx.ASGITransport(app=web), **kw))

    async def check():
        async with Client(create_mcp(tmp_path)) as client:
            reply = await client.call_tool("chromascript.import_motion_gltf", {"request": args})
            assert not reply.is_error
            assert reply.structured_content["scene"]["assets"] == imported["scene"]["assets"]
            assert reply.structured_content["revision_id"] != imported["revision_id"]
            inspected = await client.call_tool("chromascript.get_motion_asset_info", {"request": {
                "scene_id": args["scene_id"], "name": "horse", "revision_id": imported["revision_id"],
            }})
            assert not inspected.is_error
            assert inspected.structured_content == expected_info
    with TestClient(web) as client:
        token = client.get("/api/session").json()["token"]
        monkeypatch.setattr("chromascript.mcp_server.endpoint", lambda root: {"url": "http://testserver", "token": token})
        asyncio.run(check())


def test_project_views_export_assets_without_overwriting_manual_edits(tmp_path):
    service = Service(tmp_path)
    project = service.create_project("asset projection")["project"]["id"]
    frozen = import_gltf(FIXTURE / "triangle.gltf")
    spec = MotionSpec(code="export default function(){}", assets={"horse": frozen})
    saved = service.create_motion_scene(project, spec=spec)
    migrate(service.store)
    path = next(service.store.project_directory(project).glob("作品/*/资源/horse.gltf"))
    assert path.read_text() == frozen
    path.write_text("manual edit")
    document = json.loads(frozen)
    document["asset"]["generator"] = "changed model"
    changed = MotionSpec.model_validate({**spec.model_dump(), "assets": {"horse": json.dumps(document)}})
    assert spec.source_digest() != changed.source_digest()
    service.update_motion_scene(saved["record"]["id"], changed, saved["revision_id"])
    assert service.sync_project_folder(project)["conflicts"]
    assert path.read_text() == "manual edit"


@pytest.mark.parametrize("engine,filename", [("three-float-v1", "float.ts"), ("remotion-sdr-v1", "sdr.tsx")])
def test_documented_examples_compile_with_the_frozen_model(tmp_path, engine, filename):
    example = Path(__file__).resolve().parents[1] / "examples" / "motion-assets" / filename
    spec = MotionSpec(engine=engine, code=example.read_text(), assets={"horse": import_gltf(FIXTURE / "triangle.gltf")})
    preview = Service(tmp_path).compile_motion_preview(spec)
    assert "connect-src data: blob:" in preview["html"]
