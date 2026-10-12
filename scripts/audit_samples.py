"""Independently inspect already completed samples through MCP with the current checks."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from chromascript.encoding import check_image_metadata, check_probe
from chromascript.models import DeliveryPreset, RenderSettings


async def main(args):
    summary = json.loads(args.acceptance.read_text())
    root = args.workspace.resolve()
    params = StdioServerParameters(
        command=str(Path(sys.executable).with_name("chromascript")),
        args=["--workspace", str(root), "mcp"],
    )
    evidence = {"workspace": str(root), "images": [], "videos": []}
    async with Client(params) as client:

        async def call(operation, **arguments):
            result = await client.call_tool("chromascript." + operation, {"request": arguments})
            assert not result.is_error, result.structured_content
            return result.structured_content

        for job in summary["jobs"]:
            if not job["label"].startswith(args.label):
                continue
            done = await call("get_job", job_id=job["id"])
            settings = RenderSettings.model_validate(job["settings"])
            for spec in job["outputs"]:
                preset = DeliveryPreset.model_validate(spec)
                if preset.container in {"exr", "png", "tiff", "dpx"}:
                    artifact = next(
                        a
                        for a in done["artifacts"]
                        if a["kind"] == "delivered-frame"
                        and a["metadata"]["preset_id"] == preset.id
                    )
                    inspected = await call("inspect_frame", artifact_id=artifact["id"])
                    issues = check_image_metadata(inspected["metadata"], preset)
                    assert not issues and inspected["non_finite"] == 0, (preset.id, issues)
                    if preset.container == "exr":
                        assert inspected["metadata"]["FramesPerSecond"] == [
                            settings.fps.numerator,
                            settings.fps.denominator,
                        ]
                    evidence["images"].append(
                        {
                            "preset_id": preset.id,
                            "job_id": job["id"],
                            "passed": True,
                            "metadata": inspected["metadata"],
                        }
                    )
                else:
                    artifact = next(
                        a
                        for a in done["artifacts"]
                        if a["kind"] == "video" and a["metadata"]["preset_id"] == preset.id
                    )
                    probe = (await call("probe_media", artifact_id=artifact["id"]))["probe"]
                    checked = check_probe(probe, preset, settings)
                    assert checked["passed"], (preset.id, checked["issues"])
                    evidence["videos"].append(
                        {"preset_id": preset.id, "job_id": job["id"], **checked}
                    )
    args.output.parent.mkdir(exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(
        f"Native file audit passed: {len(evidence['images'])} image presets, "
        f"{len(evidence['videos'])} video presets",
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--acceptance", type=Path, required=True)
    parser.add_argument("--label", default="1080p")
    parser.add_argument("--output", type=Path, default=Path("reports/native-file-audit.json"))
    asyncio.run(main(parser.parse_args()))
