"""Listening decisions preserve immutable excerpts, history and transport parity."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner

from chromascript.api import create_app
from chromascript.audio import AudioClip, AudioEvent, AudioSynthesis, AudioTimeline
from chromascript.cli import app
from chromascript.errors import ChromaError
from chromascript.mcp_server import create_mcp
from chromascript.operations import REGISTRY, execute
from chromascript.service import Service


@pytest.fixture
def listening(tmp_path):
    service = Service(tmp_path / "workspace")
    pid = service.create_project("Listening")["project"]["id"]
    assets = [service.create_audio(pid, "Secret source " + str(i), AudioSynthesis(
        duration_seconds=2, events=[AudioEvent(duration_seconds=2, frequency_hz=440 + i * 220, gain_db=gain)]
    ))["asset"] for i, gain in enumerate([-6, -24])]
    args = {"project_id": pid, "title": "Direction", "duration_seconds": 1,
            "candidates": [{"asset_id": a["id"], "source_start_seconds": 0.25} for a in assets],
            "idempotency_key": "first", "blind": True}
    return service, pid, assets, args


def test_blind_excerpt_normalization_idempotency_and_no_timeline_mutation(listening):
    service, pid, assets, args = listening
    before = service.get_project_audio(pid)
    round = execute(service, "create_audition", args)
    assert round["duration_seconds"] == 1
    assert round["baseline"]["playback_artifact_id"] is None
    assert all(a["id"] not in json.dumps(round) and a["name"] not in json.dumps(round) for a in assets)
    assert execute(service, "create_audition", args) == round
    with pytest.raises(ChromaError, match="幂等"):
        execute(service, "create_audition", {**args, "title": "Changed"})
    raw = service.store.get("audition_round", round["id"])
    for candidate in raw["candidates"]:
        assert candidate["measurement"]["integrated_lufs"] + candidate["gain_db"] == pytest.approx(round["effective_target_lufs"])
        assert candidate["measurement"]["true_peak_dbtp"] + candidate["gain_db"] <= -1
        artifact = service.get_artifact(candidate["playback_artifact_id"])["artifact"]
        assert "asset_id" not in artifact["metadata"]
    assert service.get_project_audio(pid) == before
    assert service.store.list("job") == []


def test_feedback_conflict_reveal_history_direction_and_archive(listening):
    service, pid, _, args = listening
    round = execute(service, "create_audition", args)
    cid = round["candidates"][0]["id"]
    common = {"project_id": pid, "audition_id": round["id"]}
    feedback = {**common, "expected_revision_id": round["revision_id"], "candidate_id": cid, "reviewer": "User", "feedback": {"liked": "Texture", "scores": {"reading": 5}}}
    rated = execute(service, "save_audition_feedback", feedback)
    assert not rated["feedback"][0]["after_reveal"]
    with pytest.raises(ChromaError, match="草稿"):
        execute(service, "save_audition_feedback", feedback)
    assert len(service.store.list("audition_revision")) == 2
    revealed = execute(service, "reveal_audition", {**common, "expected_revision_id": rated["revision_id"]})
    assert revealed["candidates"][0]["asset_id"]
    rerated = execute(service, "save_audition_feedback", {**feedback, "expected_revision_id": revealed["revision_id"]})
    assert rerated["feedback"][0]["after_reveal"]
    direction = {"outcome": "single", "references": [{"candidate_id": cid, "aspects": "Texture"}], "instructions": "Reduce drums", "next_step": "full_score"}
    decided = execute(service, "save_audition_direction", {**common, "expected_revision_id": rerated["revision_id"], "direction": direction, "confirm": True})
    assert decided["direction_confirmed"] and decided["direction_revision_id"] == decided["revision_id"]
    # A reference may defer entirely to the reviewer's saved feedback.
    deferred = execute(service, "save_audition_direction", {**common, "expected_revision_id": decided["revision_id"], "direction": {**direction, "references": [{"candidate_id": cid}]}, "confirm": True})
    assert [(r["candidate_id"], r["aspects"]) for r in deferred["direction"]["references"]] == [(cid, "")]
    decided = deferred
    archived = execute(service, "archive_audition", {**common, "expected_revision_id": decided["revision_id"]})
    assert service.list_auditions(pid)["total"] == 0
    restored = execute(service, "archive_audition", {**common, "expected_revision_id": archived["revision_id"], "archived": False})
    assert restored["status"] == "decided"
    assert service.get_audition(pid, round["id"], round["revision_id"])["feedback"] == []
    assert service.get_project_audio(pid)["revision_id"] == "0"


def test_baseline_frozen_and_application_explicit(listening):
    service, pid, assets, args = listening
    timeline = service.set_project_audio(pid, "0", AudioTimeline(clips=[AudioClip(asset_id=assets[0]["id"], gain_db=-12)]))
    round = execute(service, "create_audition", {**args, "blind": False})
    baseline_id = round["baseline"]["playback_artifact_id"]
    original = service.store.file(service.get_artifact(baseline_id)["artifact"]["relative_path"]).read_bytes()
    service.set_project_audio(pid, timeline["revision_id"], AudioTimeline())
    assert service.store.file(service.get_artifact(baseline_id)["artifact"]["relative_path"]).read_bytes() == original
    candidate = round["candidates"][0]
    now = service.get_project_audio(pid)
    applied = service.set_project_audio(pid, now["revision_id"], AudioTimeline(clips=[AudioClip(asset_id=candidate["asset_id"], audition_id=round["id"], candidate_id=candidate["id"], duration_seconds=1)]))
    assert applied["timeline"]["clips"][0]["candidate_id"] == candidate["id"]
    cloned = service.clone_project(pid, "Cloned listening project")["project"]["id"]
    cloned_clip = service.get_project_audio(cloned)["timeline"]["clips"][0]
    assert cloned_clip["audition_id"] is None and cloned_clip["candidate_id"] is None
    assert service.get_audition(pid, round["id"])["applications"][0]["currently_used"]
    usage = service.get_project_audio(pid, include_usage=True)["assets"]
    assert next(a for a in usage if a["id"] == candidate["asset_id"])["usage"] == {"auditions": 1, "timeline_clips": 1}
    with pytest.raises(ChromaError):
        service.set_project_audio(pid, applied["revision_id"], AudioTimeline(clips=[AudioClip(asset_id=candidate["asset_id"], audition_id=round["id"], candidate_id="missing")]))
    service.set_project_audio(pid, applied["revision_id"], AudioTimeline())
    assert not service.get_audition(pid, round["id"])["applications"][0]["currently_used"]


def test_invalid_sources_ranges_silence_and_parent(listening):
    service, pid, assets, args = listening
    other = service.create_project("Other")["project"]["id"]
    with pytest.raises(ChromaError):
        execute(service, "create_audition", {**args, "project_id": other})
    with pytest.raises(ChromaError):
        execute(service, "create_audition", {**args, "duration_seconds": 3})
    assert not service.store.list("audition_round") and not service.store.list("audition_revision")
    silent = service.create_audio(pid, "Silence", AudioSynthesis(duration_seconds=1, events=[]))["asset"]
    round = execute(service, "create_audition", {**args, "candidates": [{"asset_id": silent["id"]}]})
    assert not round["candidates"][0]["normalization_available"]
    assert round["candidates"][0]["gain_db"] == 0
    assert len(list(service.store.root.glob("auditions/*/*.wav"))) == 1
    decided = execute(service, "save_audition_direction", {"project_id": pid, "audition_id": round["id"], "expected_revision_id": round["revision_id"], "direction": {"instructions": "Try another texture"}, "confirm": True})
    child = execute(service, "create_audition", {**args, "idempotency_key": "child", "parent_audition_id": round["id"], "parent_direction_revision_id": decided["revision_id"]})
    execute(service, "save_audition_direction", {"project_id": pid, "audition_id": round["id"], "expected_revision_id": decided["revision_id"], "direction": {"instructions": "Changed direction"}, "confirm": True})
    assert service.get_audition(pid, child["id"])["parent_direction_changed"]


def test_three_transports_share_blind_projection_and_schemas(listening, monkeypatch):
    service, pid, _, args = listening
    web = create_app(service.store.root)
    with TestClient(web, base_url="http://127.0.0.1:8765") as http:
        token = http.get("/api/session").json()["token"]
        headers = {"Authorization": "Bearer " + token}
        response = http.post("/api/operations/create_audition", json=args, headers=headers)
        assert response.status_code == 200, response.text
        round = response.json()
        read = {"project_id": pid, "audition_id": round["id"]}
        expected = http.post("/api/operations/get_audition", json=read, headers=headers).json()
        monkeypatch.setattr("chromascript.mcp_server.endpoint", lambda _: {"url": "http://127.0.0.1:8765", "token": token})
        client_type = httpx.AsyncClient
        monkeypatch.setattr("chromascript.mcp_server.httpx.AsyncClient", lambda **kw: client_type(transport=httpx.ASGITransport(app=web), **kw))
        async def check():
            async with Client(create_mcp(service.store.root)) as client:
                tools = (await client.list_tools()).tools
                for name in ("create_audition", "get_audition", "list_auditions", "save_audition_feedback", "save_audition_direction", "reveal_audition", "archive_audition"):
                    tool = next(t for t in tools if t.name == "chromascript." + name)
                    assert tool.input_schema["$defs"][REGISTRY[name].input_model.__name__]["additionalProperties"] is False
                reply = await client.call_tool("chromascript.get_audition", {"request": read})
                assert reply.structured_content == expected
        asyncio.run(check())
        monkeypatch.setattr("chromascript.cli.call", lambda root, op, arguments: execute(service, op, arguments))
        cli = CliRunner().invoke(app, ["call", "get_audition", "--arguments", json.dumps(read)])
        assert cli.exit_code == 0 and json.loads(cli.stdout) == expected
        assert http.post("/api/operations/create_audition", json={**args, "bad": True}, headers=headers).status_code == 422


def test_frozen_picture_revision_and_rational_bounds(listening):
    service, pid, _, args = listening
    motion = service.create_motion_scene(pid, engine="remotion-sdr-v1")
    context = {"work_kind": "motion", "scene_id": motion["record"]["id"],
               "revision_id": motion["revision_id"], "fps": motion["scene"]["fps"],
               "start_frame": 15, "frames": 30}
    round = execute(service, "create_audition", {**args, "context": context})
    assert round["context"]["revision_id"] == motion["revision_id"]
    with pytest.raises(ChromaError):
        execute(service, "create_audition", {**args, "idempotency_key": "bad-rate", "context": {**context, "fps": {"numerator": 24, "denominator": 1}}})
    with pytest.raises(ChromaError):
        execute(service, "create_audition", {**args, "idempotency_key": "bad-end", "context": {**context, "start_frame": motion["scene"]["frames"]}})
    with pytest.raises(ChromaError):
        execute(service, "create_audition", {**args, "blind": "true"})


def test_common_target_preserves_peak_headroom_without_limiting(listening):
    service, pid, assets, args = listening
    transient = service.create_audio(pid, "High crest", AudioSynthesis(duration_seconds=2,
        events=[AudioEvent(start_seconds=0.3, duration_seconds=0.02, frequency_hz=1000, gain_db=-0.5, fade_seconds=0)]))["asset"]
    round = execute(service, "create_audition", {**args, "blind": False,
        "loudness_target_lufs": -10,
        "candidates": [{"asset_id": transient["id"]}, {"asset_id": assets[1]["id"]}]})
    assert round["effective_target_lufs"] < -10
    for candidate in round["candidates"]:
        measurement = candidate["measurement"]
        assert measurement["true_peak_dbtp"] + candidate["gain_db"] <= -1
        assert measurement["integrated_lufs"] + candidate["gain_db"] == pytest.approx(round["effective_target_lufs"])
