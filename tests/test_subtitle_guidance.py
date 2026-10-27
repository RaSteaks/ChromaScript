"""Subtitle authoring rules stay discoverable and identical across public transports."""

import asyncio
import json

import httpx
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner

from chromascript.api import create_app
from chromascript.cli import app
from chromascript.documentation import SUBTITLE_GUIDE
from chromascript.mcp_server import create_mcp
from chromascript.operations import execute


def test_subtitle_guidance_transports_and_prompts(tmp_path, monkeypatch):
    web = create_app(tmp_path)
    real_client = httpx.AsyncClient
    with TestClient(web, base_url="http://127.0.0.1:8765") as http:
        session = http.get("/api/session").json()
        response = http.post(
            "/api/operations/get_documentation",
            headers={"Authorization": "Bearer " + session["token"]},
            json={"topic": "subtitles"},
        )
        assert response.status_code == 200
        expected = {"topic": "subtitles", "text": SUBTITLE_GUIDE}
        assert response.json() == expected
        assert "Never burn subtitles into video frames" in expected["text"]
        spec = {
            "language": "zh-en", "fps": {"numerator": 30000, "denominator": 1001},
            "frames": 300,
            "cues": [{"start_frame": 0, "end_frame": 60, "zh": "你好。", "en": "Hello."}],
        }
        expected_srt = "1\n00:00:00,000 --> 00:00:02,002\n你好。\nHello.\n\n"
        exported = http.post(
            "/api/operations/export_subtitles",
            headers={"Authorization": "Bearer " + session["token"]},
            json={"spec": spec, "destination": str(tmp_path / "http.srt")},
        )
        assert exported.status_code == 200
        assert (tmp_path / "http.srt").read_text(encoding="utf-8") == expected_srt

        # Replace only the transport: MCP still executes the actual HTTP operation.
        monkeypatch.setattr(
            "chromascript.mcp_server.endpoint",
            lambda root: {"url": "http://testserver", "token": session["token"]},
        )
        monkeypatch.setattr(
            "chromascript.mcp_server.httpx.AsyncClient",
            lambda **kwargs: real_client(transport=httpx.ASGITransport(app=web), **kwargs),
        )

        async def check():
            server = create_mcp(tmp_path)
            assert "chromascript://docs/subtitles" in server.instructions
            async with Client(server) as client:
                tools = await client.list_tools()
                for name in ("get_documentation", "create_motion_scene", "update_motion_scene", "export_subtitles", "get_project_subtitles", "set_project_subtitles"):
                    tool = next(t for t in tools.tools if t.name == "chromascript." + name)
                    assert "topic=subtitles" in tool.description
                result = await client.call_tool(
                    "chromascript.get_documentation", {"request": {"topic": "subtitles"}}
                )
                assert not result.is_error
                assert result.structured_content == expected
                exported = await client.call_tool(
                    "chromascript.export_subtitles",
                    {"request": {"spec": spec, "destination": str(tmp_path / "mcp.srt")}},
                )
                assert not exported.is_error
                assert (tmp_path / "mcp.srt").read_text(encoding="utf-8") == expected_srt
                resource = await client.read_resource("chromascript://docs/subtitles")
                assert resource.contents[0].text == SUBTITLE_GUIDE
                for name in ("create_procedural_video", "create_hdr_animation"):
                    prompt = await client.get_prompt(name, {"description": "添加中英双语字幕"})
                    assert SUBTITLE_GUIDE in prompt.messages[0].content.text
                project = await client.call_tool("chromascript.create_project", {"request": {"name": "字幕归档"}})
                pid = project.structured_content["project"]["id"]
                selected = await client.call_tool("chromascript.set_project_subtitles", {"request": {
                    "project_id": pid, "expected_revision_id": "0", "subtitles": {"tracks": [spec]},
                }})
                assert not selected.is_error
                current = await client.call_tool("chromascript.get_project_subtitles", {"request": {"project_id": pid}})
                assert current.structured_content == selected.structured_content
                return pid, selected.structured_content

        pid, selected = asyncio.run(check())
        monkeypatch.setattr(
            "chromascript.cli.call",
            lambda root, op, args: execute(web.state.service, op, args),
        )
        reply = CliRunner().invoke(
            app, ["call", "get_documentation", "--arguments", '{"topic":"subtitles"}']
        )
        assert reply.exit_code == 0
        assert json.loads(reply.stdout) == expected
        exported_cli = CliRunner().invoke(app, [
            "call", "export_subtitles", "--arguments",
            json.dumps({"spec": spec, "destination": str(tmp_path / "cli.srt")}),
        ])
        assert exported_cli.exit_code == 0
        assert (tmp_path / "cli.srt").read_text(encoding="utf-8") == expected_srt
        current_http = http.post("/api/operations/get_project_subtitles", json={"project_id": pid},
                                headers={"Authorization": "Bearer " + session["token"]})
        assert current_http.json() == selected
        current_cli = CliRunner().invoke(app, ["call", "get_project_subtitles", "--arguments", json.dumps({"project_id": pid})])
        assert current_cli.exit_code == 0
        assert json.loads(current_cli.stdout) == selected
        # Only explicit creation above owns a project; standalone export never creates one.
        assert web.state.service.list_projects()["total"] == 1
