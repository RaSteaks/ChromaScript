"""MCP is a full transport adapter; all operations execute in the shared daemon."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from mcp.server import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import BaseModel

from .documentation import DOCUMENTS
from .operations import REGISTRY, catalogue
from .runtime import endpoint


def create_mcp(root: Path) -> MCPServer:
    server = MCPServer(
        "ChromaScript",
        log_level="WARNING",
        version="0.1.0",
        instructions=DOCUMENTS["workflow"]
        + " Read chromascript://docs/scene before authoring shaders.",
    )

    def register(name: str, model: type[BaseModel], description: str) -> None:
        async def invoke(request):
            info = endpoint(root)
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    info["url"] + "/api/operations/" + name,
                    json=request.model_dump(mode="json"),
                    headers={"Authorization": "Bearer " + info["token"]},
                )
            data = response.json()
            images = data.pop("images", [])
            content: list[Any] = [
                TextContent(type="text", text=json.dumps(data, ensure_ascii=False))
            ]
            content += [
                ImageContent(type="image", data=image["data"], mime_type=image["mime_type"])
                for image in images
            ]
            return CallToolResult(
                content=content, structured_content=data, is_error=not response.is_success
            )

        # Explicit dynamic annotations preserve each operation's real JSON Schema.
        invoke.__name__ = name
        invoke.__annotations__ = {"request": model, "return": dict[str, Any]}
        server.tool(name="chromascript." + name, description=description, structured_output=True)(
            invoke
        )

    catalogue()
    for op in REGISTRY.values():
        register(op.name, op.input_model, op.description)

    @server.resource("chromascript://docs/{topic}")
    def documentation(topic: str) -> str:
        return DOCUMENTS[topic]

    @server.resource("chromascript://projects/{project_id}")
    async def project(project_id: str) -> str:
        return await resource_call("get_project", {"project_id": project_id})

    @server.resource("chromascript://scenes/{scene_id}")
    async def scene(scene_id: str) -> str:
        return await resource_call("get_scene", {"scene_id": scene_id})

    @server.resource("chromascript://jobs/{job_id}")
    async def job(job_id: str) -> str:
        return await resource_call("get_job", {"job_id": job_id})

    @server.resource("chromascript://artifacts/{artifact_id}")
    async def artifact(artifact_id: str) -> str:
        return await resource_call("get_artifact", {"artifact_id": artifact_id})

    async def resource_call(name: str, args: dict[str, Any]) -> str:
        info = endpoint(root)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                info["url"] + "/api/operations/" + name,
                json=args,
                headers={"Authorization": "Bearer " + info["token"]},
            )
            response.raise_for_status()
        return json.dumps(response.json(), ensure_ascii=False)

    @server.prompt(name="create_procedural_video")
    def create_procedural_video(description: str) -> str:
        return (
            f"Create this video with ChromaScript: {description}\n"
            + DOCUMENTS["workflow"]
            + "\n"
            + DOCUMENTS["scene"]
        )

    return server
