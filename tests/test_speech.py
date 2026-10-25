"""Cloud speech contract: no live charges, real PCM decoding and MCP/HTTP/CLI parity."""

import asyncio
import json
import wave

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner

from chromascript.api import create_app
from chromascript.cli import app
from chromascript.errors import ChromaError
from chromascript.mcp_server import create_mcp
from chromascript.operations import catalogue, execute
from chromascript.service import Service
from chromascript.speech import ENV_KEYS, configuration


@pytest.fixture
def provider(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-never-in-results")
    state = {
        "calls": [],
        "status": 200,
        "mime": "audio/pcm;rate=24000;channels=1",
        "body": b"\x01\x00" * 24000,
    }

    def handler(request):
        state["calls"].append(request)
        assert str(request.url) == "https://openrouter.ai/api/v1/audio/speech"
        assert request.headers["authorization"] == "Bearer secret-never-in-results"
        if state.get("timeout"):
            raise httpx.ReadTimeout("secret-never-in-results", request=request)
        return httpx.Response(
            state["status"],
            content=state["body"],
            headers={"content-type": state["mime"], "x-generation-id": "test-generation"},
        )

    monkeypatch.setattr(
        "chromascript.speech.speech_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
    )
    return state


@pytest.fixture
def project(tmp_path, provider):
    service = Service(tmp_path / "workspace")
    pid = service.create_project("Speech")["project"]["id"]
    args = {
        "project_id": pid,
        "name": "旁白 v1",
        "idempotency_key": "intro-v1",
        "request": {"text": "故事刚刚开始。", "instructions": "自然的普通话，温暖克制"},
    }
    return service, args


def test_dotenv_precedence_and_no_key_disclosure(tmp_path, monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    service = Service(tmp_path / "workspace")
    (tmp_path / ".env").write_text(
        'OPENROUTER_API_KEY="parent-secret" # comment\nOPENROUTER_TTS_MODEL=google/gemini-3.8-flash-tts\nUNRELATED="broken\n'
    )
    (tmp_path / ".env").chmod(0o600)
    (service.store.root / ".env").write_text("export OPENROUTER_TTS_VOICE='Puck'\n")
    (service.store.root / ".env").chmod(0o600)
    assert configuration(service.store)["OPENROUTER_API_KEY"] == "parent-secret"
    result = service.get_speech_config()
    assert result["api_key_configured"] and result["voice"] == "Puck"
    assert "parent-secret" not in json.dumps(result)
    monkeypatch.setenv("OPENROUTER_TTS_VOICE", "Kore")
    assert service.get_speech_config()["voice"] == "Kore"
    (service.store.root / ".env").write_text('OPENROUTER_API_KEY="secret-broken\n')
    with pytest.raises(ChromaError) as error:
        configuration(service.store)
    assert "secret-broken" not in str(error.value)
    assert service.get_speech_config()["configuration_error"] == "speech_config"


def test_generation_pcm_provenance_and_restart_retry(project, provider):
    service, args = project
    result = execute(service, "generate_speech", args)
    asset = result["asset"]
    assert asset["sample_rate"] == 48000 and asset["channels"] == 2
    assert asset["samples"] == 48000
    assert asset["metadata"]["probe"]["streams"][0]["codec_name"] == "pcm_s32le"
    report = asset["metadata"]["speech_generation"]
    assert report["source_encoding"]["sample_rate"] == 24000
    assert report["generation_id"] == "test-generation"
    with wave.open(
        str(service.store.root / "speech-sources" / result["request_id"] / "source.wav")
    ) as reader:
        assert (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) == (
            24000,
            1,
            2,
        )
    assert "secret-never-in-results" not in json.dumps(result)
    payload = json.loads(provider["calls"][0].content)
    assert payload["input"] == args["request"]["text"]
    assert payload["instructions"] == args["request"]["instructions"]
    assert service.get_project_audio(args["project_id"])["timeline"]["clips"] == []
    # Retry after restart must return the original asset without another charge.
    again = execute(Service(service.store.root), "generate_speech", args)
    assert again == result and len(provider["calls"]) == 1
    changed = {**args, "request": {"text": "不同台词"}}
    with pytest.raises(ChromaError) as error:
        execute(service, "generate_speech", changed)
    assert error.value.code == "idempotency_conflict"
    service.store.file(asset["relative_path"]).unlink()
    with pytest.raises(ChromaError) as error:
        execute(service, "generate_speech", args)
    assert error.value.code == "audio_asset_changed" and len(provider["calls"]) == 1


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"status": 401, "body": b"secret-never-in-results"}, "speech_provider"),
        ({"status": 302}, "speech_provider"),
        ({"timeout": True}, "speech_network"),
        ({"mime": "application/json"}, "speech_format"),
        ({"body": b""}, "speech_format"),
        ({"body": b"1"}, "speech_format"),
        ({"mime": "audio/pcm;rate=garbage;channels=1"}, "speech_format"),
    ],
)
def test_failure_is_sanitized_and_never_rebilled(project, provider, changes, code):
    service, args = project
    provider.update(changes)
    with pytest.raises(ChromaError) as error:
        execute(service, "generate_speech", args)
    assert error.value.code == code
    assert "secret-never-in-results" not in json.dumps(error.value.as_dict())
    with pytest.raises(ChromaError) as error:
        execute(Service(service.store.root), "generate_speech", args)
    assert error.value.code == "speech_not_retried"
    assert len(provider["calls"]) == 1
    assert service.store.list("audio_asset") == []


def test_preflight_schema_and_pending_guard(project, provider, monkeypatch):
    service, args = project
    for request in [
        {"text": " "},
        {"text": 3},
        {"text": "x", "api_key": "bad"},
        {"text": "x", "model": "no-provider"},
        {"text": "x", "response_format": "wav"},
    ]:
        with pytest.raises(ChromaError):
            execute(service, "generate_speech", {**args, "request": request})
    with pytest.raises(ChromaError):
        execute(
            service, "generate_speech", {**args, "request": {"text": "x", "response_format": "mp3"}}
        )
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(ChromaError):
        execute(service, "generate_speech", args)
    assert provider["calls"] == [] and service.store.list("speech_request") == []
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-never-in-results")
    result = execute(service, "generate_speech", args)
    record = service.store.get("speech_request", result["request_id"])
    service.store.put("speech_request", result["request_id"], {**record, "status": "pending"})
    with pytest.raises(ChromaError) as error:
        execute(service, "generate_speech", args)
    assert error.value.code == "speech_not_retried" and len(provider["calls"]) == 1


def test_pcm_limits_and_missing_layout(project, provider, monkeypatch):
    service, args = project
    monkeypatch.setattr("chromascript.speech.MAX_INPUT_BYTES", 100)
    with pytest.raises(ChromaError) as error:
        execute(service, "generate_speech", args)
    assert error.value.code == "speech_limit"
    monkeypatch.setattr("chromascript.speech.MAX_INPUT_BYTES", 128 * 1024**2)
    provider["mime"] = "audio/pcm"
    with pytest.raises(ChromaError) as error:
        execute(
            service,
            "generate_speech",
            {**args, "idempotency_key": "other", "request": {"text": "x", "model": "other/tts"}},
        )
    assert error.value.code == "speech_format"


def test_paid_source_survives_import_failure(project, provider, monkeypatch):
    service, args = project

    def fail(*a, **kw):
        raise ChromaError("audio_decode", "test decoder failure")

    monkeypatch.setattr(service, "_import_audio", fail)
    with pytest.raises(ChromaError):
        execute(service, "generate_speech", args)
    sources = list((service.store.root / "speech-sources").glob("*/source.wav"))
    assert len(sources) == 1 and sources[0].with_name("generation.json").is_file()
    assert service.store.list("audio_asset") == []
    assert service.store.list("speech_request")[0]["status"] == "failed"


def test_http_mcp_cli_parity(tmp_path, provider, monkeypatch):
    web = create_app(tmp_path / "workspace")
    service = web.state.service
    pid = service.create_project("TTS transports")["project"]["id"]
    args = {
        "project_id": pid,
        "name": "Voice",
        "request": {"text": "Hello"},
        "idempotency_key": "transport",
    }
    names = {op["name"] for op in catalogue()}
    assert {"chromascript.generate_speech", "chromascript.get_speech_config"} <= names
    with TestClient(web, base_url="http://127.0.0.1:8765") as http:
        token = http.get("/api/session").json()["token"]
        response = http.post(
            "/api/operations/generate_speech",
            json=args,
            headers={"Authorization": "Bearer " + token},
        )
        assert response.status_code == 200, response.text
        aid = response.json()["asset"]["id"]
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
                config = await client.call_tool("chromascript.get_speech_config", {"request": {}})
                assert not config.is_error and "secret-never-in-results" not in str(config)
                result = await client.call_tool("chromascript.generate_speech", {"request": args})
                assert not result.is_error
                assert result.structured_content["asset"]["id"] == aid

        asyncio.run(check())
        monkeypatch.setattr(
            "chromascript.cli.call", lambda root, op, arguments: execute(service, op, arguments)
        )
        result = CliRunner().invoke(
            app, ["call", "generate_speech", "--arguments", json.dumps(args)]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["asset"]["id"] == aid
        assert len(provider["calls"]) == 1


def test_busy_guard_does_not_block_project_edits(project, provider):
    service, args = project
    service.speech_lock.acquire()
    try:
        with pytest.raises(ChromaError) as error:
            execute(service, "generate_speech", args)
        assert error.value.code == "speech_busy"
        # The operation registry does not take the global edit lock for TTS.
        assert execute(service, "create_project", {"name": "Still editable"})["project"]
        assert provider["calls"] == []
    finally:
        service.speech_lock.release()


def test_mp3_provider_and_actual_decode(project, provider, tmp_path):
    import subprocess

    service, args = project
    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(provider["body"])
    mp3 = tmp_path / "source.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), str(mp3)], check=True)
    provider.update(mime="audio/mpeg", body=mp3.read_bytes())
    args["request"].update(model="minimax/speech-test", voice="test-voice", response_format="mp3")
    result = execute(service, "generate_speech", args)
    assert result["asset"]["metadata"]["source_probe"]["streams"][0]["codec_name"] == "mp3"
    assert result["asset"]["duration_seconds"] == pytest.approx(1, abs=0.05)
    assert json.loads(provider["calls"][0].content)["model"] == "minimax/speech-test"


def test_pcm_layout_headers_override_documented_defaults(project, provider):
    service, args = project
    provider.update(mime="audio/pcm;rate=48000;channels=2", body=b"\x01\x00" * 96000)
    result = execute(service, "generate_speech", args)
    assert result["asset"]["duration_seconds"] == 1
    encoding = result["asset"]["metadata"]["speech_generation"]["source_encoding"]
    assert encoding == {
        "sample_rate": 48000,
        "channels": 2,
        "sample_format": "s16le",
        "samples": 48000,
    }
