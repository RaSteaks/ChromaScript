"""Schema parity and public HTTP behavior are tested without a specific model vendor."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from chromascript.api import create_app
from chromascript.mcp_server import create_mcp
from chromascript.operations import REGISTRY


def test_http_shared_contract_and_access(tmp_path: Path):
    with TestClient(create_app(tmp_path / "workspace"), base_url="http://127.0.0.1:8765") as client:
        session = client.get("/api/session").json()
        headers = {"Authorization": "Bearer " + session["token"]}
        assert client.post("/api/operations/list_templates", json={}).status_code == 401
        assert (
            client.post(
                "/api/operations/list_templates",
                json={},
                headers={**headers, "Origin": "https://example.invalid"},
            ).status_code
            == 403
        )
        p = client.post("/api/operations/create_project", json={"name": "HTTP"}, headers=headers)
        assert p.status_code == 200
        pid = p.json()["project"]["id"]
        created = client.post(
            "/api/operations/create_scene",
            json={"project_id": pid, "template_id": "gradient"},
            headers=headers,
        )
        assert created.status_code == 200
        invalid = client.post(
            "/api/operations/create_scene",
            json={"project_id": pid, "unexpected": True},
            headers=headers,
        )
        assert invalid.status_code == 422
        assert len(client.get("/api/operations").json()["operations"]) == len(REGISTRY)
        color_options = client.post(
            "/api/operations/get_delivery_options",
            json={
                "configuration": {"codec": "hevc", "color_space": "Rec.2020", "gamma": "hlg"},
                "changed_field": "gamma",
            },
            headers=headers,
        )
        assert color_options.status_code == 200
        # Scene-log remains discoverable but cannot be certified as tagged video.
        partial = client.post(
            "/api/operations/get_delivery_options",
            json={
                "configuration": {"codec": "hevc", "color_space": "acescct", "data_range": "limit"}
            },
            headers=headers,
        )
        assert partial.status_code == 200
        assert next(v for v in partial.json()["options"]["color_space"] if v["value"] == "ACEScct")["disabled"]
        blocked = client.post("/api/operations/validate_delivery_configuration", json={"configuration": {
            "codec": "hevc", "color_space": "ACEScct", "gamma": "acescct"}}, headers=headers)
        assert blocked.status_code != 200
        assert partial.json()["configuration"]["data_range"] == "limited"
        description = color_options.json()["color_encoding"]
        assert description["transfer_function_label"] == "HLG（ARIB STD-B67）"
        assert description["target_display_peak_nits"] == 1000
        config = color_options.json()["configuration"]
        assert (
            config["color_space"],
            config["gamma"],
            config["color_output"],
            config["bit_depth"],
        ) == ("Rec.2020", "hlg", "HLG", 10)
        # The HTTP and MCP schema share independent P3 gamma and Auto data levels.
        p3 = client.post(
            "/api/operations/get_delivery_options",
            headers=headers,
            json={
                "configuration": {
                    "container": "mov",
                    "codec": "prores",
                    "color_space": "P3-D65",
                    "gamma": "Gamma 2.4",
                    "data_range": "Auto",
                }
            },
        )
        assert p3.status_code == 200
        # Discovery suggests an exactly tagged alternative; submission never silently remaps it.
        assert p3.json()["configuration"]["gamma"] == "srgb"
        assert next(v for v in p3.json()["options"]["gamma"] if v["value"] == "gamma2.4")["disabled"]
        assert p3.json()["configuration"]["data_range"] == "auto"
        assert p3.json()["resolved_data_range"] == "limited"


def test_mcp_schema_resources_prompts(tmp_path: Path):
    async def check():
        async with Client(create_mcp(tmp_path / "workspace")) as client:
            tools = await client.list_tools()
            assert {t.name for t in tools.tools} == {"chromascript." + name for name in REGISTRY}
            assert all(t.input_schema and t.output_schema for t in tools.tools)
            # Both controls are exposed to model callers, with strict enum values.
            schema = next(
                t.input_schema for t in tools.tools if t.name == "chromascript.get_delivery_options"
            )
            properties = schema["$defs"]["DeliveryConfiguration"]["properties"]
            assert "Rec.2020" in properties["color_space"]["enum"]
            assert {
                "linear",
                "acescct",
                "gamma2.2",
                "gamma2.4",
                "gamma2.6",
                "srgb",
                "st2084",
                "hlg",
            } == set(properties["gamma"]["enum"])
            assert set(properties["data_range"]["enum"]) == {"auto", "full", "limited"}
            # HDR output selection is a strict shared model in MCP and HTTP/CLI.
            motion = next(
                t.input_schema for t in tools.tools if t.name == "chromascript.submit_motion_render"
            )
            output = motion["$defs"]["MotionOutput"]["properties"]
            assert output["video"]["enum"] == ["none", "pq", "hlg", "srgb", "rec709"]
            assert output["master_bit_depth"]["anyOf"][0]["enum"] == [16, 32]
            assert {
                "chromascript.render_motion_frame",
                "chromascript.get_motion_delivery_options",
            } <= {t.name for t in tools.tools}
            guidance = await client.read_resource("chromascript://docs/motion")
            assert "three-float-v1" in guidance.contents[0].text
            hdr_prompt = await client.get_prompt(
                "create_hdr_animation", {"description": "HDR light"}
            )
            assert "RGBA32F" in hdr_prompt.messages[0].content.text
            resources = await client.list_resource_templates()
            assert len(resources.resource_templates) == 6
            result = await client.read_resource("chromascript://docs/scene")
            assert "shade" in result.contents[0].text
            # Resource guidance must describe the same new contract as tool schemas.
            delivery_doc = await client.read_resource("chromascript://docs/delivery")
            assert "resolve-21-delivery-v1" in delivery_doc.contents[0].text
            assert "gamma2.6" in delivery_doc.contents[0].text
            assert "resolved_data_range" in delivery_doc.contents[0].text
            prompt = await client.get_prompt(
                "create_procedural_video", {"description": "gold particles"}
            )
            assert prompt.messages

    asyncio.run(check())


@pytest.mark.parametrize("operation", ["save_delivery_preset", "import_delivery_preset"])
def test_http_legacy_presets_reject_unmarked_exr_display_encoding(tmp_path: Path, operation):
    app = create_app(tmp_path / "workspace")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        session = client.get("/api/session").json()
        response = client.post(
            "/api/operations/" + operation,
            headers={"Authorization": "Bearer " + session["token"]},
            json={
                "preset": {
                    "id": "bad-exr",
                    "name": "unsupported",
                    "family": "test",
                    "container": "exr",
                    "codec": "exr",
                    "bit_depth": 32,
                    "color": {"output": "P3-Gamma2.2"},
                }
            },
        )
        # Reject before persistence or queueing; the generic CLI/MCP use this same model.
        assert response.status_code == 422
        assert "EXR delivery requires" in response.text
        assert app.state.service.store.list("preset") == []
        assert app.state.service.store.list("job") == []


def test_media_cookies_do_not_collide_between_local_ports(tmp_path):
    first = create_app(tmp_path / "first", port=8765)
    second = create_app(tmp_path / "second", port=8766)
    for app, value in [(first, "first"), (second, "second")]:
        store = app.state.service.store
        store.file("sample.txt").write_text(value)
        store.put(
            "artifact",
            "sample",
            {"relative_path": "sample.txt", "name": "sample.txt", "mime_type": "text/plain"},
        )
    with TestClient(first, base_url="http://127.0.0.1:8765") as a:
        with TestClient(second, base_url="http://127.0.0.1:8766") as b:
            a.get("/api/session")
            b.get("/api/session")
            # A browser shares cookies across localhost ports, like these merged jars.
            a.cookies.update(b.cookies)
            b.cookies.update(a.cookies)
            assert a.get("/api/artifacts/sample").text == "first"
            assert b.get("/api/artifacts/sample").text == "second"
