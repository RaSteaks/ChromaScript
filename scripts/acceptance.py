"""Real MCP acceptance: visual feedback, durable jobs, formats, and large outputs."""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx2
from mcp import Client, ClientSession
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from chromascript.runtime import endpoint, ensure_daemon


async def main(args):
    root = args.workspace.resolve()
    ensure_daemon(root)
    summary = {"checks": {}, "jobs": [], "workspace": str(root)}
    started = time.time()
    params = StdioServerParameters(
        command=str(Path(sys.executable).with_name("chromascript")),
        args=["--workspace", str(root), "mcp"],
    )
    async with Client(params) as client:
        tools = await client.list_tools()
        assert len(tools.tools) >= 49
        assert all(t.input_schema and t.output_schema for t in tools.tools)
        summary["checks"]["stdio_tools"] = len(tools.tools)

        async def call(operation, **arguments):
            r = await client.call_tool("chromascript." + operation, {"request": arguments})
            if r.is_error:
                raise RuntimeError(str(r.structured_content))
            return r.structured_content

        async def wait(job, label):
            before = ""
            deadline = time.monotonic() + 7200
            while time.monotonic() < deadline:
                v = await call("get_job", job_id=job["id"])
                j = v["job"]
                marker = f"{j['stage']} {int(j['progress'] * 10) * 10}%"
                if marker != before:
                    print(label, marker, flush=True)
                    before = marker
                if j["status"] in {"completed", "failed", "interrupted", "cancelled"}:
                    summary["jobs"].append({"label": label, **j})
                    if j["status"] != "completed":
                        raise RuntimeError(json.dumps(j["error"], ensure_ascii=False))
                    return v
                await asyncio.sleep(1)
            raise TimeoutError(label)

        p = (await call("create_project", name="MCP 实际验收"))["project"]
        s = await call("create_scene", project_id=p["id"], template_id="gradient")
        sid = s["record"]["id"]
        await call(
            "set_parameters",
            scene_id=sid,
            values={"exposure": 0.5},
            expected_revision_id=s["revision_id"],
        )
        await call("validate_scene", scene_id=sid)
        small = {"width": 320, "height": 180, "frames": 8, "samples": 4}
        options = {
            "scene_id": sid,
            "settings": small,
            "preset_ids": ["exr-16-zip", "prores-4", "h264-review", "hdr-pq", "hdr-hlg"],
            "idempotency_key": p["id"] + ":small",
        }
        job = (await call("submit_delivery", **options))["job"]
        assert (await call("submit_delivery", **options))["job"]["id"] == job["id"]
        done = await wait(job, "MCP small")
        preview = next(a for a in done["artifacts"] if a["kind"] == "preview-image")
        result = await client.call_tool(
            "chromascript.get_artifact",
            {"request": {"artifact_id": preview["id"], "include_image": True}},
        )
        assert any(b.type == "image" for b in result.content)
        master = next(a for a in done["artifacts"] if a["kind"] == "master-frame")
        stats = await call("inspect_frame", artifact_id=master["id"])
        assert stats["non_finite"] == 0 and stats["high_rgb_values"] > 0
        await call("sample_pixels", artifact_id=master["id"], points=[[10, 10]])
        assert (await call("validate_delivery_output", job_id=job["id"]))["passed"]
        summary["checks"].update(native_preview=True, idempotency=True, color_inspection=True)
        job = (
            await call(
                "render_preview",
                scene_id=sid,
                settings={**small, "frames": 30},
                idempotency_key=p["id"] + ":cancel",
            )
        )["job"]
        await call("cancel_job", job_id=job["id"])
        assert (await call("get_job", job_id=job["id"]))["job"]["status"] == "cancelled"
        await asyncio.sleep(0.5)
        await call("resume_job", job_id=job["id"])
        await wait(job, "MCP resume")
        summary["checks"]["cancel_resume"] = True
        # A batch freezes one scene revision but supports independently sized/time-based jobs.
        batch_args = {
            "scene_id": sid,
            "deliveries": [
                {
                    "settings": {
                        "width": 320,
                        "height": 180,
                        "frames": 2,
                        "fps": {"numerator": 24, "denominator": 1},
                        "samples": 1,
                    },
                    "preset_ids": ["png-16"],
                },
                {
                    "settings": {
                        "width": 256,
                        "height": 144,
                        "frames": 3,
                        "fps": {"numerator": 30000, "denominator": 1001},
                        "samples": 1,
                    },
                    "preset_ids": ["h264-review"],
                },
            ],
            "idempotency_key": p["id"] + ":batch",
        }
        batch = await call("submit_batch", **batch_args)
        again = await call("submit_batch", **batch_args)
        assert [j["id"] for j in batch["jobs"]] == [j["id"] for j in again["jobs"]]
        for item in batch["jobs"]:
            await wait(item, "MCP batch " + item["id"][:8])
            assert (await call("validate_delivery_output", job_id=item["id"]))["passed"]
        summary["checks"]["mixed_delivery_batch"] = True
        if args.full:
            job = (
                await call(
                    "submit_delivery",
                    scene_id=sid,
                    settings={"width": 3840, "height": 2160, "frames": 300, "samples": 4},
                    preset_ids=["exr-16-zip", "prores-4", "h264-review", "hdr-pq"],
                    idempotency_key=p["id"] + ":4k",
                )
            )["job"]
            await wait(job, "4K60 / 300 frames")
            summary["checks"]["4k60"] = await call("validate_delivery_output", job_id=job["id"])
        if args.formats:
            presets = (await call("list_delivery_presets"))["presets"]
            for preset in [p for p in presets if p["builtin"]]:
                geometry = preset.get("settings") or {"width": 1920, "height": 1080}
                job = (
                    await call(
                        "submit_delivery",
                        scene_id=sid,
                        settings={
                            "width": geometry["width"],
                            "height": geometry["height"],
                            "frames": 2,
                            "samples": 1,
                        },
                        preset_ids=[preset["id"]],
                        idempotency_key=p["id"] + ":format:" + preset["id"],
                    )
                )["job"]
                await wait(job, "1080p " + preset["id"])
            summary["checks"]["format_presets"] = len(presets)
        if args.high:
            job = (
                await call(
                    "render_frame",
                    scene_id=sid,
                    width=7680,
                    height=4320,
                    samples=1,
                    idempotency_key=p["id"] + ":8k",
                )
            )["job"]
            await wait(job, "8K still")
            job = (
                await call(
                    "render_preview",
                    scene_id=sid,
                    settings={
                        "width": 1920,
                        "height": 1080,
                        "frames": 120,
                        "fps": {"numerator": 120, "denominator": 1},
                        "samples": 1,
                    },
                    idempotency_key=p["id"] + ":120",
                )
            )["job"]
            await wait(job, "120 fps")
            summary["checks"]["8k_and_120fps"] = True
    info = endpoint(root)
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + info["token"]}) as http:
        async with streamable_http_client(info["url"] + "/mcp/", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
                r = await session.call_tool("chromascript.list_templates", {"request": {}})
                assert not r.is_error
                summary["checks"]["http_tools"] = len(tools.tools)
    summary["elapsed_seconds"] = time.time() - started
    Path("reports").mkdir(exist_ok=True)
    # Concurrent validation workspaces must retain separate evidence reports.
    report = args.report or Path("reports") / f"acceptance-{root.name}.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print("Acceptance passed", summary["checks"].keys(), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path("workspace/acceptance"))
    parser.add_argument("--report", type=Path)
    for flag in ["full", "formats", "high"]:
        parser.add_argument("--" + flag, action="store_true")
    asyncio.run(main(parser.parse_args()))
