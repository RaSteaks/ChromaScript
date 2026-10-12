"""Exercise real MCP disconnection, daemon/worker restart, corruption and encoder failure."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

import numpy as np
import psutil
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from chromascript.images import file_hash, read_image
from chromascript.runtime import endpoint, ensure_daemon


async def main(root: Path) -> None:
    root = root.resolve()
    ensure_daemon(root)
    params = StdioServerParameters(
        command=str(Path(sys.executable).with_name("chromascript")),
        args=["--workspace", str(root), "mcp"],
    )
    evidence = {"workspace": str(root), "checks": {}, "jobs": []}

    async def call(client, operation, **args):
        result = await client.call_tool("chromascript." + operation, {"request": args})
        if result.is_error:
            raise RuntimeError(str(result.structured_content))
        return result.structured_content

    async def until(client, job_id, predicate, timeout=600):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = await call(client, "get_job", job_id=job_id)
            job = value["job"]
            if predicate(job):
                return value
            if job["status"] in {"failed", "cancelled", "interrupted", "completed"}:
                raise AssertionError(f"Unexpected terminal state: {job}")
            await asyncio.sleep(0.1)
        raise TimeoutError(job_id)

    def stop_daemon():
        # Signals are limited to this disposable workspace's own verified daemon.
        proc = psutil.Process(endpoint(root)["pid"])
        command = proc.cmdline()
        assert "chromascript.cli" in command and str(root) in command
        proc.terminate()
        proc.wait(timeout=15)

    async with Client(params) as client:
        project = (await call(client, "create_project", name="恢复与故障实际验收"))["project"]
        scene = await call(client, "create_scene", project_id=project["id"], template_id="noise")
        sid = scene["record"]["id"]
        options = {
            "scene_id": sid,
            "settings": {"width": 1920, "height": 1080, "frames": 60, "samples": 4},
            "preset_ids": ["h264-review"],
            "idempotency_key": project["id"] + ":disconnect",
        }
        job = (await call(client, "submit_delivery", **options))["job"]
        live = await until(client, job["id"], lambda j: j["completed_frames"] >= 2)
        worker = psutil.Process(live["job"]["pid"])
    # Closing stdio must not transfer ownership of the long-running job to the client.
    assert worker.is_running()
    evidence["checks"]["stdio_disconnect_preserves_worker"] = True
    stop_daemon()
    assert worker.is_running()
    ensure_daemon(root)
    async with Client(params) as client:
        live = await call(client, "get_job", job_id=job["id"])
        assert live["job"]["pid"] == worker.pid
        done = await until(client, job["id"], lambda j: j["status"] == "completed")
        evidence["checks"]["daemon_restart_preserves_worker"] = True
        evidence["jobs"].append(done["job"])
        print("MCP disconnect and daemon restart passed", flush=True)

        # Extend the same scene to reuse existing frames, then interrupt during rendering.
        second = (
            await call(
                client,
                "submit_delivery",
                **{
                    **options,
                    "settings": {**options["settings"], "frames": 90},
                    "idempotency_key": project["id"] + ":interrupt",
                },
            )
        )["job"]
        live = await until(client, second["id"], lambda j: j["completed_frames"] >= 61)
        worker = psutil.Process(live["job"]["pid"])
        assert abs(worker.create_time() - live["job"]["process_started"]) < 0.1
        os.killpg(worker.pid, signal.SIGKILL)
        await until(client, second["id"], lambda j: j["status"] == "interrupted")
        manifests = list((root / "cache").glob("*/manifest.json"))
        assert len(manifests) == 1
        cache = manifests[0].parent / "frames"
        first, retained = cache / "00000000.exr", cache / "00000001.exr"
        expected_pixels, _ = read_image(first)
        retained_hash, retained_time = file_hash(retained), retained.stat().st_mtime_ns
        first.write_bytes(b"deliberate disposable-cache corruption")
        await call(client, "resume_job", job_id=second["id"])
        done = await until(client, second["id"], lambda j: j["status"] == "completed")
        repaired, _ = read_image(first)
        assert np.array_equal(expected_pixels, repaired)
        assert file_hash(retained) == retained_hash and retained.stat().st_mtime_ns == retained_time
        assert (await call(client, "validate_delivery_output", job_id=second["id"]))["passed"]
        evidence["checks"].update(
            worker_crash_resume=True, corrupt_frame_repaired=True, valid_frame_retained=True
        )
        evidence["jobs"].append(done["job"])
        print("Worker interruption, checksum repair and retained frames passed", flush=True)

        # An accepted custom encoder profile can still fail at native codec initialization.
        preset = (await call(client, "get_delivery_preset", preset_id="h264-review"))["preset"]
        preset.update(
            id="failure-" + project["id"], builtin=False, profile="deliberately-invalid-profile"
        )
        await call(client, "save_delivery_preset", preset=preset)
        failed = (
            await call(
                client,
                "submit_delivery",
                scene_id=sid,
                settings={"width": 320, "height": 180, "frames": 2, "samples": 1},
                preset_ids=[preset["id"]],
                idempotency_key=project["id"] + ":encode-failure",
            )
        )["job"]
        done = await until(client, failed["id"], lambda j: j["status"] == "failed")
        assert done["job"]["error"]["code"] == "encode_failed", done["job"]["error"]
        logs = await call(client, "get_job_log", job_id=failed["id"])
        assert logs
        evidence["checks"]["native_encoder_failure_persisted"] = True
        evidence["jobs"].append(done["job"])
        print("Native encoder failure reporting passed", flush=True)
    report = Path("reports/recovery-acceptance.json")
    report.parent.mkdir(exist_ok=True)
    report.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    stop_daemon()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path("workspace/recovery"))
    asyncio.run(main(parser.parse_args().workspace))
