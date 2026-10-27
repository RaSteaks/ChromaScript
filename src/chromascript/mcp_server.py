"""MCP is a full transport adapter; all operations execute in the shared daemon."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from mcp.server import MCPServer
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import BaseModel, ValidationError

from .documentation import DOCUMENTS
from .operations import REGISTRY, catalogue
from .runtime import endpoint


def create_mcp(root: Path) -> MCPServer:
    server = MCPServer(
        "ChromaScript",
        log_level="WARNING",
        version="0.1.0",
        # Advertise the migration contract at connection time, not only in optional docs.
        instructions=DOCUMENTS["workflow"]
        + " Read chromascript://docs/scene before authoring WGSL or chromascript://docs/motion before authoring Three.js HDR animations. For native wide-gamut authoring or upgrading existing animations, discover chromascript.get_motion_capabilities and read chromascript://docs/motion-color. Use get_motion_scene then upgrade_motion_scene with the actual expected_revision_id to preserve old colors; changing source_color_space alone reinterprets RGB. Retain preserved_layers when saving the returned spec. HDR masters and sRGB preview are separate. Before creating audio, call chromascript.get_audio_guidelines(project_id) and read chromascript://docs/audio for local storage and production rules.",
    )

    async def private_cloud_validation(ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        params = ctx.params or {}
        name = params.get("name")
        if ctx.method == "tools/call" and name in {"chromascript.generate_speech", "chromascript.generate_music", "chromascript.analyze_media"}:
            # The SDK wraps function arguments in its own Pydantic model, whose
            # error formatter echoes values even if our nested model hides them.
            # Validate before that layer and return only a constant error message.
            arguments = params.get("arguments")
            try:
                if not isinstance(arguments, dict) or set(arguments) != {"request"}:
                    raise ValueError("Invalid wrapper")
                REGISTRY[name.removeprefix("chromascript.")].input_model.model_validate(arguments["request"])
            except (ValidationError, ValueError):
                return CallToolResult(content=[TextContent(type="text", text="Invalid cloud audio arguments; consult the tool schema")], is_error=True)
        return await call_next(ctx)

    server.middleware.append(private_cloud_validation)

    def register(name: str, model: type[BaseModel], description: str) -> None:
        async def invoke(request):
            info = endpoint(root)
            # Output frame reviews, audition preparation, generation and exports must not time out while their exclusive
            # destination is still being written by the shared service.
            timeout = httpx.Timeout(60, read=None) if name in {"render_motion_output_preview", "render_motion_frame", "export_artifact", "export_project_folder", "migrate_workspace_storage", "create_audition", "create_audio", "import_audio", "create_music", "inspect_soundfont", "review_audio", "generate_speech", "generate_music", "analyze_media"} else httpx.Timeout(60)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    info["url"] + "/api/operations/" + name,
                    # Keep nested explicit-field intent identical to direct HTTP.
                    json=request.model_dump(mode="json", exclude_unset=True),
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

    @server.resource("chromascript://motion-scenes/{scene_id}")
    async def motion_scene(scene_id: str) -> str:
        return await resource_call("get_motion_scene", {"scene_id": scene_id})

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

    @server.prompt(name="create_hdr_animation")
    def create_hdr_animation(description: str) -> str:
        # Model authors receive the actual float contract, not legacy React guidance.
        return f"Create this offline HDR animation: {description}\n" + DOCUMENTS["motion"] + "\n" + DOCUMENTS["motion-color"]

    @server.prompt(name="upgrade_animation_color_space")
    def upgrade_animation_color_space(scene_id: str, source_color_space: str = "Linear Rec.2020") -> str:
        # Returning a prompt does not mutate a revision; the agent must read it first.
        return (
            f"Preserve the colors of saved animation {scene_id} while upgrading its linear pipeline "
            f"to {source_color_space}. Read its actual revision before any mutation.\n"
            + DOCUMENTS["motion-color"]
        )

    return server
