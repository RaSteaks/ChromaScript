"""Exercise linked choices and direct multi-format delivery through real MCP transports."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx2
from mcp import Client, ClientSession
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from chromascript.runtime import endpoint, ensure_daemon


async def main(args):
    root = args.workspace.resolve()
    ensure_daemon(root)
    params = StdioServerParameters(
        command=str(Path(sys.executable).with_name("chromascript")),
        args=["--workspace", str(root), "mcp"],
    )
    checks = {}
    async with Client(params) as client:

        async def call(operation, **arguments):
            result = await client.call_tool("chromascript." + operation, {"request": arguments})
            assert not result.is_error, result.structured_content
            return result.structured_content

        tools = await client.list_tools()
        assert all(tool.input_schema and tool.output_schema for tool in tools.tools)
        checks["stdio_tools"] = len(tools.tools)
        configurations = []
        for value, field in [
            ({}, None),
            (
                {
                    "container": "exr",
                    "bit_depth": 32,
                    "alpha_mode": "straight",
                    "compression": "zip",
                },
                "bit_depth",
            ),
            ({"container": "mov", "codec": "prores", "alpha_mode": "straight"}, "alpha_mode"),
            ({"codec": "hevc", "color_output": "PQ"}, "color_output"),
        ]:
            options = await call("get_delivery_options", configuration=value, changed_field=field)
            configurations.append(options["configuration"])
        assert configurations[2]["profile"] == "4444" and configurations[3]["bit_depth"] == 10
        checks["linked_choices"] = True
        project = (await call("create_project", name="直接交付 MCP 验收"))["project"]
        scene = await call("create_scene", project_id=project["id"], template_id="shapes")
        settings = {
            "width": 320,
            "height": 180,
            "frames": 3,
            "fps": {"numerator": 30, "denominator": 1},
            "samples": 4,
        }
        for configuration in configurations:
            validated = await call(
                "validate_delivery_configuration", configuration=configuration, settings=settings
            )
            assert validated["valid"] and "output" in validated
        request = {
            "scene_id": scene["record"]["id"],
            "revision_id": scene["revision_id"],
            "settings": settings,
            "configurations": configurations,
            "idempotency_key": project["id"] + ":direct",
        }
        job = (await call("submit_configured_delivery", **request))["job"]
        # A different client's subsequent edit must not change this frozen delivery.
        await call(
            "set_parameters",
            scene_id=scene["record"]["id"],
            expected_revision_id=scene["revision_id"],
            values={"color": [0.1, 0.4, 1.0]},
        )
        assert (await call("submit_configured_delivery", **request))["job"]["id"] == job["id"]
        checks["frozen_revision_and_idempotency"] = True
        for _ in range(180):
            result = await call("get_job", job_id=job["id"])
            if result["job"]["status"] == "completed":
                break
            assert result["job"]["status"] not in {"failed", "interrupted", "cancelled"}, result[
                "job"
            ]
            await asyncio.sleep(0.5)
        else:
            raise TimeoutError(job["id"])
        report = await call("validate_delivery_output", job_id=job["id"])
        assert report["passed"] and len(report["outputs"]) == 4
        preview = next(a for a in result["artifacts"] if a["kind"] == "preview-image")
        image = await client.call_tool(
            "chromascript.get_artifact",
            {"request": {"artifact_id": preview["id"], "include_image": True}},
        )
        assert any(block.type == "image" for block in image.content)
        master = next(a for a in result["artifacts"] if a["kind"] == "master-frame")
        inspected = await call("inspect_frame", artifact_id=master["id"])
        assert (
            inspected["alpha_present"]
            and inspected["minimum"][3] == 0
            and inspected["maximum"][3] == 1
        )
        presets = (await call("list_delivery_presets"))["presets"]
        assert all(p["builtin"] for p in presets)
        checks.update(
            native_four_format_delivery=True,
            native_image=True,
            alpha_inspection=True,
            no_saved_preset=True,
        )
        invalid = await client.call_tool(
            "chromascript.submit_configured_delivery",
            {
                "request": {
                    **request,
                    "configurations": [{"container": "mp4", "codec": "prores"}],
                    "idempotency_key": "invalid",
                }
            },
        )
        assert (
            invalid.is_error
            and invalid.structured_content["error"]["code"] == "delivery_combination"
        )
        checks["invalid_combination_rejected"] = True
        comparison = await call(
            "get_delivery_options",
            configuration={"container": "png", "alpha_mode": "premultiplied"},
            changed_field="container",
        )
    descriptor = endpoint(root)
    async with httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + descriptor["token"]}
    ) as http:
        async with streamable_http_client(descriptor["url"] + "/mcp/", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
                response = await session.call_tool(
                    "chromascript.get_delivery_options",
                    {
                        "request": {
                            "configuration": {"container": "png", "alpha_mode": "premultiplied"},
                            "changed_field": "container",
                        }
                    },
                )
                assert not response.is_error and response.structured_content == comparison
                assert response.structured_content["configuration"]["alpha_mode"] == "straight"
                checks["http_tools"] = len(tools.tools)
                checks["transport_choices_identical"] = True
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(
            {
                "passed": True,
                "checks": checks,
                "workspace": str(root),
                "job": result["job"],
                "report": report,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print("Configured delivery acceptance passed:", checks, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path("workspace/acceptance-configured"))
    parser.add_argument("--report", type=Path, default=Path("reports/configured-acceptance.json"))
    asyncio.run(main(parser.parse_args()))
