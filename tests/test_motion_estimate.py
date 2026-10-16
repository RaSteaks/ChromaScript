"""Read-only storage budgets share the submission gate without promising ZIP ratios."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner

from chromascript.api import create_app
from chromascript.audio import AudioClip, AudioSynthesis, AudioTimeline
from chromascript.cli import app
from chromascript.errors import ChromaError
from chromascript.mcp_server import create_mcp
from chromascript.models import MotionRate, MotionRenderSettings
from chromascript.motion import MotionOutput
from chromascript.operations import execute
from chromascript.service import Service


def test_master_budget_depth_sampling_and_read_only(tmp_path):
    service = Service(tmp_path / "workspace")
    target = tmp_path / "not-created" / "delivery"
    settings = MotionRenderSettings(width=3840, height=2160, frames=300)
    before = set(service.store.root.iterdir())
    full = service.estimate_motion_render(settings, export_directory=str(target))
    half = service.estimate_motion_render(
        settings, output=MotionOutput(master_bit_depth=16), export_directory=str(target)
    )
    sampled = service.estimate_motion_render(
        settings, output=MotionOutput(render_scale=4), export_directory=str(target)
    )
    assert full["master_uncompressed_bytes"] == 3840 * 2160 * 4 * 4 * 300
    assert full["master_frame_bytes"] == 3840 * 2160 * 16
    assert half["master_uncompressed_bytes"] * 2 == full["master_uncompressed_bytes"]
    assert sampled["master_uncompressed_bytes"] == full["master_uncompressed_bytes"]
    assert sampled["intermediate_budget_bytes"] == 4 * full["intermediate_budget_bytes"]
    parts = [
        "master_uncompressed_bytes",
        "master_overhead_budget_bytes",
        "intermediate_budget_bytes",
        "video_budget_bytes",
        "audio_bytes",
        "auxiliary_budget_bytes",
        "workspace_required_bytes",
    ]
    assert full["same_volume"]
    assert full["destination_required_bytes"] == sum(full[k] for k in parts)
    assert full["compressed_master_bytes"] is None
    assert full["video_budget_bytes"] == 0
    assert not target.parent.exists()
    assert set(service.store.root.iterdir()) == before
    assert service.store.list("job") == []


def test_rational_audio_and_video_budget(tmp_path):
    service = Service(tmp_path / "workspace")
    pid = service.create_project("soundtrack")["project"]["id"]
    asset = service.create_audio(pid, "sound", AudioSynthesis(duration_seconds=1))["asset"]
    service.set_project_audio(pid, "0", AudioTimeline(clips=[AudioClip(asset_id=asset["id"])]))
    settings = MotionRenderSettings(
        width=32, height=16, frames=30, fps=MotionRate(numerator=30000, denominator=1001)
    )
    result = service.estimate_motion_render(
        settings, output=MotionOutput(video="pq"), project_id=pid
    )
    assert result["audio_bytes"] == 48048 * 8 + 44
    assert result["video_budget_bytes"] == 32 * 16 * 4 * 30 + result["audio_bytes"]
    silent = service.estimate_motion_render(settings)
    assert silent["audio_bytes"] == 0
    sdr = service.estimate_motion_render(settings, engine="remotion-sdr-v1")
    assert sdr["master_uncompressed_bytes"] == sdr["master_frame_bytes"] == 0
    assert sdr["intermediate_budget_bytes"] == 32 * 16 * 4 * 4 * 30
    assert sdr["video_budget_bytes"] > 0
    with pytest.raises(ChromaError, match="SDR"):
        service.estimate_motion_render(settings, engine="remotion-sdr-v1", output=MotionOutput())
    with pytest.raises(ChromaError, match="母版"):
        service.estimate_motion_render(settings, output=MotionOutput(master_bit_depth=None))


def test_separate_volumes_and_submission_use_identical_budget(tmp_path, monkeypatch):
    service = Service(tmp_path / "workspace")
    target = tmp_path / "external"
    target.mkdir()
    # Model separate volume IDs without requiring an actual mounted test disk.
    original_stat = Path.stat

    def stat(path, **kwargs):
        result = original_stat(path, **kwargs)
        if path == target:
            values = list(result)
            values[2] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "stat", stat)
    available = {"workspace": 1024**3, "external": 1024**3}
    monkeypatch.setattr(
        "chromascript.motion.shutil.disk_usage",
        lambda path: SimpleNamespace(
            free=available["workspace" if Path(path) == service.store.root else "external"]
        ),
    )
    monkeypatch.setattr(
        "chromascript.motion.motion_environment", lambda: {"render_available": True}
    )
    monkeypatch.setattr("chromascript.motion.motion_identity", lambda: {"test": True})
    pid = service.create_project("budget")["project"]["id"]
    saved = service.create_motion_scene(pid)
    from chromascript.motion import MotionSpec

    spec = MotionSpec.model_validate(saved["scene"])
    estimate = service.estimate_motion_render(
        spec.settings(), export_directory=str(target), project_id=pid
    )
    assert not estimate["same_volume"]
    available["external"] = estimate["destination_required_bytes"] - 1
    failed = service.estimate_motion_render(
        spec.settings(), export_directory=str(target), project_id=pid
    )
    assert not failed["destination_sufficient"]
    with pytest.raises(ChromaError) as error:
        service.submit_motion_render(
            saved["record"]["id"], saved["revision_id"], "low-space", export_directory=str(target)
        )
    assert error.value.code == "disk_space"
    assert error.value.details["required_bytes"] == estimate["destination_required_bytes"]
    assert service.store.list("job") == []
    available["external"] = estimate["destination_required_bytes"]
    available["workspace"] = estimate["workspace_required_bytes"] - 1
    assert not service.estimate_motion_render(spec.settings(), export_directory=str(target))[
        "workspace_sufficient"
    ]
    available["workspace"] += 1
    assert service.estimate_motion_render(spec.settings(), export_directory=str(target))[
        "sufficient"
    ]
    job = service.submit_motion_render(
        saved["record"]["id"], saved["revision_id"], "enough", export_directory=str(target)
    )["job"]
    # Dynamic free-space readings must not enter the immutable request identity.
    available["external"] += 1024**3
    assert (
        service.submit_motion_render(
            saved["record"]["id"], saved["revision_id"], "enough", export_directory=str(target)
        )["job"]["id"]
        == job["id"]
    )


@pytest.mark.parametrize("output,master_bytes", [({"master_bit_depth": 16}, 1920 * 1080 * 8 * 150), ({"master_bit_depth": None, "video": "pq"}, 0)])
def test_strict_schema_cli_and_mcp_transport(tmp_path, monkeypatch, output, master_bytes):
    web = create_app(tmp_path / "workspace")
    service = web.state.service
    args = {
        "settings": {"width": 1920, "height": 1080, "frames": 150},
        "output": output,
    }
    for settings in [{"width": True}, {"frames": 0}, {"width": "1920"}, {"unknown": 1}]:
        with pytest.raises(ChromaError):
            execute(service, "estimate_motion_render", {"settings": settings})
    with pytest.raises(ChromaError):
        execute(service, "estimate_motion_render", {**args, "export_directory": "relative"})
    monkeypatch.setattr(
        "chromascript.cli.call", lambda root, op, arguments: execute(service, op, arguments)
    )
    result = CliRunner().invoke(
        app, ["call", "estimate_motion_render", "--arguments", json.dumps(args)]
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["master_uncompressed_bytes"] == master_bytes
    with TestClient(web) as client:
        session = client.get("/api/session").json()
        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            "chromascript.mcp_server.endpoint",
            lambda root: {"url": "http://testserver", "token": session["token"]},
        )
        monkeypatch.setattr(
            "chromascript.mcp_server.httpx.AsyncClient",
            lambda **kw: real_client(transport=httpx.ASGITransport(app=web), **kw),
        )

        async def check():
            async with Client(create_mcp(service.store.root)) as mcp:
                result = await mcp.call_tool(
                    "chromascript.estimate_motion_render", {"request": args}
                )
                assert not result.is_error
                assert (
                    result.structured_content["master_uncompressed_bytes"] == master_bytes
                )

        asyncio.run(check())
