"""OpenAI's event boundary is isolated; PCM import and durable recovery are real."""

import asyncio
import base64
import hashlib
import json
import stat
import wave

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from test_speech import provider as provider
from typer.testing import CliRunner
from websocket import (
    WebSocketBadStatusException,
    WebSocketConnectionClosedException,
    WebSocketTimeoutException,
)

from chromascript import openai_speech
from chromascript.api import create_app
from chromascript.cli import app
from chromascript.cloud_music import MusicGenerationRequest
from chromascript.errors import ChromaError
from chromascript.mcp_server import create_mcp
from chromascript.operations import catalogue, execute
from chromascript.project_storage import migrate
from chromascript.service import Service
from chromascript.speech import ENV_KEYS
from chromascript.store import canonical

SECRET = "sk-proj-local-fixture-secret-never-uploaded"
SESSION = {
    "model": "gpt-realtime-2.1-mini",
    "audio": {"output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "marin"}},
}


@pytest.fixture
def realtime(monkeypatch):
    for name in ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    state = {
        "connections": [],
        "sent": [],
        # An odd first delta deliberately splits a 16-bit PCM sample.
        "events": [
            {"type": "session.created", "session": SESSION},
            {"type": "session.updated", "session": SESSION},
            {
                "type": "response.created",
                "response": {"id": "resp_fixture", "status": "in_progress"},
            },
            {
                "type": "response.output_audio.delta",
                "response_id": "resp_fixture",
                "delta": base64.b64encode(b"\x01").decode(),
            },
            {
                "type": "response.output_audio.delta",
                "response_id": "resp_fixture",
                "delta": base64.b64encode(b"\x00\x01\x00" + b"\x01\x00" * 2398).decode(),
            },
            {"type": "response.output_audio.done", "response_id": "resp_fixture"},
            {
                "type": "response.done",
                "response": {
                    "id": "resp_fixture",
                    "status": "completed",
                    "usage": {"input_tokens": 20, "output_tokens": 30},
                },
            },
        ],
        "closed": False,
    }

    class Connection:
        def __init__(self):
            self.events = iter(state["events"])

        def recv(self):
            item = next(self.events, "")
            if isinstance(item, BaseException):
                raise item
            return json.dumps(item) if isinstance(item, dict) else item

        def send(self, message):
            state["sent"].append(json.loads(message))

        def settimeout(self, remaining):
            assert 0 < remaining <= 180

        def shutdown(self):
            state["closed"] = True

    def connect(key, model):
        state["connections"].append((key, model))
        return Connection()

    monkeypatch.setattr(openai_speech, "realtime_connection", connect)
    return state


@pytest.fixture
def project(tmp_path, realtime):
    service = Service(tmp_path / "workspace")
    pid = service.create_project("OpenAI speech")["project"]["id"]
    return service, {
        "project_id": pid,
        "name": "原文配音",
        "idempotency_key": "openai-intro-v1",
        "request": {"provider": "openai", "text": "故事开始。", "instructions": "自然普通话"},
    }


def test_provider_configuration_is_offline_and_independent(
    project, realtime, monkeypatch, tmp_path
):
    service, _ = project
    config = execute(service, "get_speech_config", {"provider": "openai"})
    assert config["provider"] == "OpenAI" and config["api_key_configured"]
    assert config["model"] == "gpt-realtime-2.1-mini" and config["voice"] == "marin"
    assert config["supported_formats"] == ["pcm"] and len(config["supported_voices"]) == 10
    assert config["privacy_policy"]["zdr_verified"] is False
    assert "zdr" not in config["privacy_policy"] and SECRET not in json.dumps(config)
    assert not service.get_speech_config()["api_key_configured"]
    monkeypatch.setenv("OPENROUTER_REQUIRE_ZDR", "invalid")
    assert service.get_speech_config("openai")["api_key_configured"]
    assert service.get_speech_config()["configuration_error"] == "cloud_privacy_config"
    assert realtime["connections"] == []
    monkeypatch.delenv("OPENAI_API_KEY")
    parent = tmp_path / ".env"
    parent.write_text("OPENAI_API_KEY=parent-fixture\nOPENAI_TTS_VOICE=cedar\n")
    parent.chmod(0o600)
    child = service.store.root / ".env"
    child.write_text("OPENAI_TTS_VOICE=coral\n")
    child.chmod(0o600)
    assert service.get_speech_config("openai")["voice"] == "coral"
    monkeypatch.setenv("OPENAI_TTS_VOICE", "ash")
    assert service.get_speech_config("openai")["voice"] == "ash"
    child.chmod(0o644)
    assert service.get_speech_config("openai")["configuration_error"] == "speech_config"


@pytest.mark.parametrize("portable", [False, True])
def test_pcm_generation_provenance_and_durable_replay(project, realtime, monkeypatch, portable):
    service, args = project
    if portable:
        migrate(service.store)
    result = execute(service, "generate_speech", args)
    asset = result["asset"]
    assert (asset["sample_rate"], asset["channels"], asset["samples"]) == (48000, 2, 4800)
    assert asset["metadata"]["source"] == "openai_tts"
    report = asset["metadata"]["speech_generation"]
    assert report["ai_generated"] and report["generation_id"] == "resp_fixture"
    assert report["source_encoding"] == {
        "sample_rate": 24000,
        "channels": 1,
        "sample_format": "s16le",
        "samples": 2400,
    }
    assert SECRET not in json.dumps(result) and args["request"]["text"] not in json.dumps(result)
    directory = service.store.project_file(
        args["project_id"], "speech-sources/" + result["request_id"]
    )
    with wave.open(str(directory / "source.wav")) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (
            1,
            2,
            24000,
            2400,
        )
    private = (directory / "generation.json").read_text()
    assert args["request"]["text"] in private and SECRET not in private
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in directory.iterdir())
    assert service.get_project_audio(args["project_id"])["timeline"]["clips"] == []
    update, response = realtime["sent"]
    assert update["session"]["audio"]["input"]["turn_detection"] is None
    assert update["session"]["tools"] == []
    assert response["type"] == "response.create" and response["response"]["conversation"] == "none"
    assert response["response"]["input"][0]["content"][0]["text"] == args["request"]["text"]
    assert args["request"]["instructions"] in response["response"]["instructions"]
    assert realtime["closed"] and len(realtime["connections"]) == 1
    monkeypatch.delenv("OPENAI_API_KEY")
    assert execute(Service(service.store.root), "generate_speech", args) == result
    assert len(realtime["connections"]) == 1
    assert (
        service.get_speech_request(args["project_id"], args["idempotency_key"])["asset"]["id"]
        == asset["id"]
    )
    with pytest.raises(ChromaError, match="new key") as error:
        execute(service, "generate_speech", {**args, "request": {"text": "故事开始。"}})
    assert error.value.code == "idempotency_conflict"


@pytest.mark.parametrize(
    "case,code",
    [
        ("rejected", "speech_provider"),
        ("incomplete", "speech_provider"),
        ("refusal", "speech_provider"),
        ("timeout", "speech_network"),
        ("closed", "speech_network"),
        ("malformed", "speech_format"),
        ("base64", "speech_format"),
        ("odd", "speech_format"),
        ("empty", "speech_format"),
        ("missing_done", "speech_format"),
        ("wrong_response", "speech_format"),
        ("wrong_format", "speech_format"),
        ("wire_limit", "speech_limit"),
        ("byte_limit", "speech_limit"),
        ("duration_limit", "speech_limit"),
        ("deadline", "speech_limit"),
    ],
)
def test_failure_preserves_partial_audio_and_never_rebills(
    project, realtime, monkeypatch, case, code
):
    service, args = project
    events = realtime["events"]
    if case == "rejected":
        events[-1] = {"type": "error", "error": {"message": SECRET}}
    elif case == "incomplete":
        events[-1]["response"]["status"] = "incomplete"
    elif case == "refusal":
        events[-1]["response"]["output"] = [{"content": [{"type": "refusal"}]}]
    elif case in {"timeout", "closed", "malformed"}:
        events[-1] = {
            "timeout": WebSocketTimeoutException(SECRET),
            "closed": WebSocketConnectionClosedException(SECRET),
            "malformed": '{"type":',
        }[case]
    elif case == "base64":
        events[4]["delta"] = "not-base64!"
    elif case == "odd":
        events[4]["delta"] = ""
    elif case == "empty":
        events[3]["delta"] = events[4]["delta"] = ""
    elif case == "missing_done":
        del events[5]
    elif case == "wrong_response":
        events[4]["response_id"] = "resp_other"
    elif case == "wrong_format":
        events[1] = {
            "type": "session.updated",
            "session": {
                **SESSION,
                "audio": {"output": {"format": {"type": "audio/pcmu"}, "voice": "marin"}},
            },
        }
    elif case == "wire_limit":
        monkeypatch.setattr(openai_speech, "MAX_EVENT_BYTES", 100)
    elif case == "byte_limit":
        monkeypatch.setattr(openai_speech, "MAX_INPUT_BYTES", 100)
    elif case == "duration_limit":
        monkeypatch.setattr(openai_speech, "MAX_SECONDS", 0.01)
    elif case == "deadline":
        monkeypatch.setattr(openai_speech, "TIMEOUT", 0)
    with pytest.raises(ChromaError) as error:
        execute(service, "generate_speech", args)
    assert error.value.code == code and SECRET not in str(error.value)
    assert service.store.list("audio_asset") == [] and realtime["closed"]
    row = service.store.list("speech_request")[0]
    assert row["status"] == "failed"
    directory = service.store.project_file(args["project_id"], "speech-sources/" + row["id"])
    assert (directory / "generation.json").is_file()
    if case == "rejected":
        assert (directory / "response.partial.pcm").stat().st_size == 4800
    with pytest.raises(ChromaError) as again:
        execute(Service(service.store.root), "generate_speech", args)
    assert again.value.code == "speech_not_retried" and len(realtime["connections"]) == 1


@pytest.mark.parametrize(
    "input_values",
    [
        {"provider": "unknown", "text": "x"},
        {"provider": "openai", "text": "x", "model": "google/gemini-test"},
        {"provider": "openai", "text": "x", "model": "gpt-4o-mini-tts"},
        {"provider": "openai", "text": "x", "voice": "Kore"},
        {"provider": "openai", "text": "x", "response_format": "mp3"},
        {"provider": "openai", "text": "x", "api_key": SECRET},
        {"provider": "openrouter", "text": "x", "model": "gpt-realtime-2.1-mini"},
    ],
)
def test_provider_validation_happens_before_reservation(project, realtime, input_values):
    service, args = project
    with pytest.raises(ChromaError) as error:
        execute(service, "generate_speech", {**args, "request": input_values})
    assert error.value.code == "invalid_arguments" and SECRET not in str(error.value)
    assert realtime["connections"] == [] and service.store.list("speech_request") == []


def test_missing_config_misplaced_credentials_and_music_validation(project, realtime, monkeypatch):
    service, args = project
    for field in ("name", "model", "text", "instructions"):
        changed = (
            {**args, "name": SECRET}
            if field == "name"
            else {**args, "request": {**args["request"], field: SECRET}}
        )
        with pytest.raises(ChromaError) as error:
            execute(service, "generate_speech", changed)
        assert SECRET not in str(error.value)
    monkeypatch.setenv("OPENROUTER_API_KEY", "other-local-credential")
    with pytest.raises(ChromaError) as error:
        execute(service, "generate_speech", {**args, "name": "other-local-credential"})
    assert error.value.code == "cloud_secret_in_input"
    monkeypatch.setenv("OPENAI_TTS_MODEL", SECRET)
    assert SECRET not in json.dumps(service.get_speech_config("openai"))
    with pytest.raises(ChromaError) as error:
        execute(service, "generate_speech", args)
    assert error.value.code == "speech_config"
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(ChromaError) as error:
        execute(service, "generate_speech", args)
    assert "OPENAI_API_KEY" in str(error.value)
    with pytest.raises(ValueError):
        MusicGenerationRequest(prompt="music", model="gpt-realtime-2.1-mini")
    assert realtime["connections"] == [] and service.store.list("speech_request") == []


def test_paid_source_survives_import_failure(project, realtime, monkeypatch):
    service, args = project
    monkeypatch.setattr(
        service, "_import_audio", lambda *a, **kw: (_ for _ in ()).throw(ValueError(SECRET))
    )
    with pytest.raises(ChromaError) as error:
        execute(service, "generate_speech", args)
    assert error.value.code == "speech_processing" and SECRET not in str(error.value)
    sources = list(service.store.root.glob("speech-sources/*/source.wav"))
    assert len(sources) == 1 and sources[0].with_name("generation.json").is_file()
    assert len(realtime["connections"]) == 1 and service.store.list("audio_asset") == []


def test_legacy_openrouter_fingerprint_and_explicit_retry(tmp_path, provider):
    service = Service(tmp_path / "workspace")
    pid = service.create_project("Legacy")["project"]["id"]
    args = {
        "project_id": pid,
        "name": "Legacy",
        "idempotency_key": "old-key",
        "request": {"text": "Hello"},
    }
    result = execute(service, "generate_speech", args)
    record = service.store.get("speech_request", result["request_id"])
    # The pre-provider production schema serialized these exact five fields.
    old_request = {
        "text": "Hello",
        "instructions": "",
        "model": None,
        "voice": None,
        "response_format": "pcm",
    }
    fingerprint = hashlib.sha256(
        canonical({"project_id": pid, "name": "Legacy", "request": old_request}).encode()
    ).hexdigest()
    assert record["fingerprint"] == fingerprint
    assert (
        execute(
            Service(service.store.root),
            "generate_speech",
            {**args, "request": {"text": "Hello", "provider": "openrouter"}},
        )
        == result
    )
    assert len(provider["calls"]) == 1


def test_http_mcp_cli_openai_parity(tmp_path, realtime, monkeypatch):
    web = create_app(tmp_path / "workspace")
    service = web.state.service
    pid = service.create_project("Transports")["project"]["id"]
    args = {
        "project_id": pid,
        "name": "Voice",
        "idempotency_key": "transport",
        "request": {"provider": "openai", "text": "Hello"},
    }
    schema = next(
        op["inputSchema"] for op in catalogue() if op["name"] == "chromascript.get_speech_config"
    )
    assert schema["properties"]["provider"]["enum"] == ["openrouter", "openai"]
    with TestClient(web, base_url="http://127.0.0.1:8765") as http:
        token = http.get("/api/session").json()["token"]
        reply = http.post(
            "/api/operations/generate_speech",
            headers={"Authorization": "Bearer " + token},
            json=args,
        )
        assert reply.status_code == 200, reply.text
        aid = reply.json()["asset"]["id"]
        bad_config = http.post(
            "/api/operations/get_speech_config",
            headers={"Authorization": "Bearer " + token},
            json={"provider": SECRET},
        )
        assert bad_config.status_code == 422 and SECRET not in bad_config.text
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
                config = await client.call_tool(
                    "chromascript.get_speech_config", {"request": {"provider": "openai"}}
                )
                assert not config.is_error and config.structured_content["provider"] == "OpenAI"
                status = await client.call_tool(
                    "chromascript.get_speech_request",
                    {"request": {"project_id": pid, "idempotency_key": "transport"}},
                )
                assert not status.is_error and status.structured_content["asset"]["id"] == aid
                response = await client.call_tool("chromascript.generate_speech", {"request": args})
                assert not response.is_error and response.structured_content["asset"]["id"] == aid

        asyncio.run(check())
        monkeypatch.setattr(
            "chromascript.cli.call", lambda root, op, arguments: execute(service, op, arguments)
        )
        reply = CliRunner().invoke(
            app, ["call", "generate_speech", "--arguments", json.dumps(args)]
        )
        assert reply.exit_code == 0 and json.loads(reply.stdout)["asset"]["id"] == aid
    assert len(realtime["connections"]) == 1


def test_handshake_error_is_sanitized(project, realtime, monkeypatch):
    service, args = project

    def reject(*_):
        raise WebSocketBadStatusException(SECRET, 401)

    monkeypatch.setattr(openai_speech, "realtime_connection", reject)
    with pytest.raises(ChromaError) as error:
        execute(service, "generate_speech", args)
    assert error.value.code == "speech_provider" and error.value.details == {"status": 401}
    assert SECRET not in str(error.value)


def test_status_reads_never_submit_or_expose_script(project, realtime):
    service, args = project
    status_args = {"project_id": args["project_id"], "idempotency_key": args["idempotency_key"]}
    assert execute(service, "get_speech_request", status_args)["status"] == "not_found"
    assert service.store.list("speech_request") == [] and realtime["connections"] == []
    result = execute(service, "generate_speech", args)
    assert execute(Service(service.store.root), "get_speech_request", status_args) == {
        "status": "complete",
        **result,
    }
    assert args["request"]["text"] not in json.dumps(
        execute(service, "get_speech_request", status_args)
    )
    row = service.store.get("speech_request", result["request_id"])
    for status in ("pending", "failed"):
        service.store.put("speech_request", row["id"], {**row, "status": status})
        assert execute(service, "get_speech_request", status_args)["status"] == status
    service.store.put("speech_request", row["id"], row)
    service.store.file(result["asset"]["relative_path"]).write_bytes(b"changed")
    with pytest.raises(ChromaError) as error:
        execute(service, "get_speech_request", status_args)
    assert error.value.code == "audio_asset_changed" and len(realtime["connections"]) == 1


def test_connection_uses_fixed_direct_tls_without_redirects(monkeypatch):
    calls = []

    class Transport:
        closed = False

        def close(self):
            self.closed = True

    transport = Transport()

    class Context:
        def wrap_socket(self, stream, server_hostname):
            assert stream is transport and server_hostname == "api.openai.com"
            return stream

    def direct(address, timeout):
        assert address == ("api.openai.com", 443) and timeout == 10
        return transport

    def upgrade(url, **options):
        calls.append((url, options))
        return transport

    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid:8080")
    monkeypatch.setattr(openai_speech.socket, "create_connection", direct)
    monkeypatch.setattr(openai_speech.ssl, "create_default_context", Context)
    monkeypatch.setattr(openai_speech, "create_connection", upgrade)
    assert openai_speech.realtime_connection(SECRET, "gpt-realtime-2.1-mini") is transport
    url, options = calls[0]
    assert url == "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1-mini"
    assert options["socket"] is transport and options["redirect_limit"] == 0
    assert options["header"] == {"Authorization": "Bearer " + SECRET}

    def reject(*args, **kwargs):
        raise WebSocketBadStatusException("refused", 401)

    monkeypatch.setattr(openai_speech, "create_connection", reject)
    with pytest.raises(WebSocketBadStatusException):
        openai_speech.realtime_connection(SECRET, "gpt-realtime-2.1-mini")
    assert transport.closed
