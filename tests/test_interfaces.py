"""Schema parity and public HTTP behavior are tested without a specific model vendor."""

import asyncio
from pathlib import Path

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
        # Partial requests retain their intent through HTTP revalidation; the
        # implicit 8-bit default must not turn scene-log grading data into SDR.
        partial = client.post(
            "/api/operations/get_delivery_options",
            json={
                "configuration": {"codec": "hevc", "color_space": "acescct", "data_range": "limit"}
            },
            headers=headers,
        )
        assert partial.status_code == 200
        assert partial.json()["configuration"]["color_output"] == "ACEScct"
        assert partial.json()["configuration"]["bit_depth"] == 10
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
            assert {"linear", "acescct", "gamma2.4", "srgb", "st2084", "hlg"} == set(
                properties["gamma"]["enum"]
            )
            resources = await client.list_resource_templates()
            assert len(resources.resource_templates) == 5
            result = await client.read_resource("chromascript://docs/scene")
            assert "shade" in result.contents[0].text
            prompt = await client.get_prompt(
                "create_procedural_video", {"description": "gold particles"}
            )
            assert prompt.messages

    asyncio.run(check())


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
