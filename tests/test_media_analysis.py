"""Real audio upload preparation with mocked OpenRouter text inference and safe policy scopes."""

import asyncio
import base64
import io
import json
import stat
import wave

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from test_cloud_music import cloud as cloud
from test_cloud_music import project as project
from typer.testing import CliRunner

from chromascript.api import create_app
from chromascript.audio import AudioEvent, AudioSynthesis
from chromascript.cli import app
from chromascript.cloud_privacy import provider_policy
from chromascript.errors import ChromaError
from chromascript.mcp_server import create_mcp
from chromascript.operations import execute
from chromascript.service import Service


@pytest.fixture
def provider(cloud, monkeypatch):
    state = {
        "calls": [],
        "status": 200,
        "content": "听到持续音调。没有画面，不能判断视觉同步。",
        "finish": "stop",
    }

    def handler(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer shared-test-secret"
        state["calls"].append(json.loads(request.content))
        if state.get("timeout"):
            raise httpx.ReadTimeout("shared-test-secret", request=request)
        if "raw" in state:
            return httpx.Response(
                state["status"], content=state["raw"], headers={"content-type": "application/json"}
            )
        return httpx.Response(
            state["status"],
            json={
                "id": "analysis-generation-1",
                "model": "google/gemini-3.8-flash",
                "choices": [
                    {"message": {"content": state["content"]}, "finish_reason": state["finish"]}
                ],
                "usage": {"prompt_tokens": 128, "completion_tokens": 32},
            },
        )

    monkeypatch.setattr(
        "chromascript.media_analysis.analysis_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
    )
    return state


def arguments(project):
    service, music_args = project
    asset = service.create_audio(
        music_args["project_id"],
        "tone",
        AudioSynthesis(duration_seconds=2, events=[AudioEvent(duration_seconds=2)]),
    )["asset"]
    return {
        "project_id": music_args["project_id"],
        "idempotency_key": "analysis-v1",
        "request": {
            "audio_asset_id": asset["id"],
            "prompt": "描述实际听到的音频，给出配器改进建议。",
            "start_seconds": 0.2,
            "duration_seconds": 0.5,
        },
    }


def test_actual_audio_payload_analysis_artifact_and_replay(project, provider):
    service, _ = project
    args = arguments(project)
    result = execute(service, "analyze_media", args)
    payload = provider["calls"][0]
    assert payload["model"] == "google/gemini-3.8-flash"
    assert payload["provider"] == {"data_collection": "deny", "zdr": True}
    assert payload["stream"] is False and payload["max_tokens"] == 2048
    assert "No images or video" in payload["messages"][0]["content"]
    audio = payload["messages"][1]["content"][1]["input_audio"]
    assert audio["format"] == "wav"
    with wave.open(io.BytesIO(base64.b64decode(audio["data"]))) as reader:
        assert (
            reader.getframerate(),
            reader.getnchannels(),
            reader.getsampwidth(),
            reader.getnframes(),
        ) == (24000, 1, 2, 12000)
    assert result["audio"]["start_sample"] == 9600
    assert result["analysis"] == provider["content"]
    path = service.store.file(result["artifact"]["relative_path"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert "shared-test-secret" not in path.read_text()
    assert "data" not in json.loads(path.with_name("request.json").read_text())
    for kind in ("analysis_request", "artifact"):
        assert args["request"]["prompt"] not in json.dumps(service.store.list(kind))
        assert result["analysis"] not in json.dumps(service.store.list(kind))
    assert execute(Service(service.store.root), "analyze_media", args) == result
    assert len(provider["calls"]) == 1
    assert service.get_artifact(result["artifact"]["id"])["text"] == path.read_text()
    assert service.get_project_audio(args["project_id"])["timeline"]["clips"] == []
    changed = {**args, "request": {**args["request"], "prompt": "new"}}
    with pytest.raises(ChromaError) as error:
        execute(service, "analyze_media", changed)
    assert error.value.code == "idempotency_conflict"
    path.write_text("changed")
    with pytest.raises(ChromaError) as error:
        execute(service, "analyze_media", args)
    assert error.value.code == "analysis_report_changed" and len(provider["calls"]) == 1


def test_text_only_and_independent_music_privacy(project, provider, monkeypatch):
    service, music = project
    monkeypatch.setenv("OPENROUTER_REQUIRE_ZDR", "true")
    monkeypatch.setenv("OPENROUTER_MUSIC_REQUIRE_ZDR", "false")
    assert service.get_music_config()["privacy_policy"]["zdr"] is False
    assert service.get_speech_config()["privacy_policy"]["zdr"] is True
    assert service.get_analysis_config()["privacy_policy"]["zdr"] is True
    # Even an old global opt-out never disables analysis ZDR.
    assert provider_policy({"OPENROUTER_REQUIRE_ZDR": "false"}, scope="analysis")["zdr"] is True
    service.speech_lock.acquire()
    service.music_generation_lock.acquire()
    try:
        result = execute(
            service,
            "analyze_media",
            {
                "project_id": music["project_id"],
                "idempotency_key": "text",
                "request": {"prompt": "为钢琴和弦乐写配乐提示词"},
            },
        )
    finally:
        service.speech_lock.release()
        service.music_generation_lock.release()
    assert result["audio"] is None
    assert len(provider["calls"][0]["messages"][1]["content"]) == 1


@pytest.mark.parametrize(
    "kind", ["http", "redirect", "timeout", "bad_json", "empty", "truncated", "bad_type", "large"]
)
def test_failure_never_retries_or_publishes(project, provider, monkeypatch, kind):
    service, _ = project
    args = arguments(project)
    if kind == "http":
        provider.update(status=401, raw=b"shared-test-secret")
    elif kind == "redirect":
        provider.update(status=302)
    elif kind == "timeout":
        provider["timeout"] = True
    elif kind == "bad_json":
        provider["raw"] = b"shared-test-secret"
    elif kind == "empty":
        provider["content"] = ""
    elif kind == "truncated":
        provider["finish"] = "length"
    elif kind == "bad_type":
        provider["content"] = {"secret": "shared-test-secret"}
    else:
        monkeypatch.setattr("chromascript.media_analysis.MAX_RESPONSE_BYTES", 100)
    with pytest.raises(ChromaError) as error:
        execute(service, "analyze_media", args)
    assert "shared-test-secret" not in json.dumps(error.value.as_dict())
    assert not [a for a in service.store.list("artifact") if a["kind"] == "media-analysis"]
    with pytest.raises(ChromaError) as error:
        execute(Service(service.store.root), "analyze_media", args)
    assert error.value.code == "analysis_not_retried" and len(provider["calls"]) == 1


def test_invalid_audio_scope_ranges_and_changed_source_never_upload(project, provider):
    service, _ = project
    args = arguments(project)
    foreign = service.create_project("Other")["project"]["id"]
    bad = [
        {**args, "project_id": foreign},
        {**args, "request": {**args["request"], "duration_seconds": 3}},
        {**args, "request": {"prompt": "x", "start_seconds": 1}},
        {**args, "request": {"prompt": "x", "source_path": "/private/photo"}},
        {**args, "request": {"prompt": "x", "provider": {"zdr": False}}},
        {**args, "request": {"prompt": "shared-test-secret"}},
    ]
    for case in bad:
        with pytest.raises(ChromaError):
            execute(service, "analyze_media", case)
    asset = service.store.get("audio_asset", args["request"]["audio_asset_id"])
    service.store.file(asset["relative_path"]).write_bytes(b"changed")
    with pytest.raises(ChromaError):
        execute(service, "analyze_media", args)
    assert provider["calls"] == []


def test_http_cli_mcp_parity_and_validation_privacy(tmp_path, provider, monkeypatch):
    web = create_app(tmp_path / "workspace")
    service = web.state.service
    pid = service.create_project("Analysis transports")["project"]["id"]
    args = {
        "project_id": pid,
        "idempotency_key": "text",
        "request": {"prompt": "Write a music brief"},
    }
    with TestClient(web, base_url="http://127.0.0.1:8765") as http:
        token = http.get("/api/session").json()["token"]
        headers = {"Authorization": "Bearer " + token}
        response = http.post("/api/operations/analyze_media", json=args, headers=headers)
        assert response.status_code == 200, response.text
        result = response.json()
        bad = {**args, "request": {"prompt": "x", "api_key": "PRIVATE-KEY"}}
        invalid = http.post("/api/operations/analyze_media", json=bad, headers=headers)
        assert invalid.status_code == 422 and "PRIVATE-KEY" not in invalid.text
        monkeypatch.setattr(
            "chromascript.mcp_server.endpoint",
            lambda _: {"url": "http://127.0.0.1:8765", "token": token},
        )
        client_type = httpx.AsyncClient
        monkeypatch.setattr(
            "chromascript.mcp_server.httpx.AsyncClient",
            lambda **kw: client_type(transport=httpx.ASGITransport(app=web), **kw),
        )

        async def check():
            async with Client(create_mcp(service.store.root)) as client:
                config = await client.call_tool("chromascript.get_analysis_config", {"request": {}})
                assert not config.is_error
                actual = await client.call_tool("chromascript.analyze_media", {"request": args})
                assert actual.structured_content == result
                invalid = await client.call_tool("chromascript.analyze_media", {"request": bad})
                assert invalid.is_error and "PRIVATE-KEY" not in str(invalid)

        asyncio.run(check())
        monkeypatch.setattr(
            "chromascript.cli.call", lambda root, op, arguments: execute(service, op, arguments)
        )
        cli = CliRunner().invoke(app, ["call", "analyze_media", "--arguments", json.dumps(args)])
        assert cli.exit_code == 0 and json.loads(cli.stdout) == result
    assert len(provider["calls"]) == 1
