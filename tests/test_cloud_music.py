"""Lyria SSE, explicit image uploads, paid retry safety and simultaneous speech/mix."""

import asyncio
import base64
import json
import subprocess
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from PIL import Image
from typer.testing import CliRunner

from chromascript.api import create_app
from chromascript.audio import AudioClip, AudioTimeline, freeze_audio, mix_audio
from chromascript.cli import app
from chromascript.errors import ChromaError
from chromascript.mcp_server import create_mcp
from chromascript.models import RenderSettings
from chromascript.operations import execute
from chromascript.service import Service
from chromascript.speech import ENV_KEYS


def event(value):
    text = value if isinstance(value, str) else json.dumps(value)
    return ("data: " + text + "\r\n\r\n").encode()


def music_stream(audio, finish="stop", done=True):
    # Each base64 chunk is independent; transport packets may split anywhere.
    result = b": keepalive\r\n\r\n"
    result += event(
        {
            "id": "music-generation-1",
            "model": "google/lyria-3-pro-preview",
            "choices": [{"index": 0, "delta": {"content": "Instrumental introduction"}}],
        }
    )
    middle = len(audio) // 2
    for piece in (audio[:middle], audio[middle:]):
        result += event(
            {
                "choices": [
                    {"index": 0, "delta": {"audio": {"data": base64.b64encode(piece).decode()}}}
                ]
            }
        )
    result += event({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    result += event({"choices": [], "usage": {"total_tokens": 123}})
    return result + (event("[DONE]") if done else b"")


class Fragmented(httpx.SyncByteStream):
    def __init__(self, data):
        self.data = data

    def __iter__(self):
        for start in range(0, len(self.data), 113):
            yield self.data[start : start + 113]


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Both independent adapters read the same local dotenv credential.
    (tmp_path / ".env").write_text(
        "OPENROUTER_API_KEY=shared-test-secret\nOPENROUTER_TTS_MODEL=google/gemini-3.8-flash-tts\nOPENROUTER_MUSIC_MODEL=google/lyria-3-pro-preview\n"
    )
    (tmp_path / ".env").chmod(0o600)
    pcm = (np.sin(np.arange(48000) * 2 * np.pi * 440 / 48000) * 8000).astype("<i2").tobytes()
    source = tmp_path / "music.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
        wav.writeframes(np.repeat(np.frombuffer(pcm, dtype="<i2"), 2).tobytes())
    mp3 = tmp_path / "music.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), str(mp3)], check=True)
    speech = (np.sin(np.arange(24000) * 2 * np.pi * 220 / 24000) * 4000).astype("<i2").tobytes()
    state = {
        "mp3": mp3.read_bytes(),
        "music_calls": [],
        "speech_calls": [],
        "status": 200,
        "mime": "text/event-stream",
    }

    def handler(request):
        assert request.headers["authorization"] == "Bearer shared-test-secret"
        payload = json.loads(request.content)
        if request.url.path.endswith("/audio/speech"):
            state["speech_calls"].append(payload)
            if "barrier" in state:
                state["barrier"].wait(timeout=5)
            return httpx.Response(
                200, content=speech, headers={"content-type": "audio/pcm;rate=24000;channels=1"}
            )
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        state["music_calls"].append(payload)
        if "barrier" in state:
            state["barrier"].wait(timeout=5)
        if state.get("timeout"):
            raise httpx.ReadTimeout("shared-test-secret", request=request)
        body = state.get("body", music_stream(state["mp3"]))
        return httpx.Response(
            state["status"], stream=Fragmented(body), headers={"content-type": state["mime"]}
        )

    def client():
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    monkeypatch.setattr("chromascript.cloud_music.music_client", client)
    monkeypatch.setattr("chromascript.speech.speech_client", client)
    return state


@pytest.fixture
def project(tmp_path, cloud):
    service = Service(tmp_path / "workspace")
    pid = service.create_project("Lyria + narration")["project"]["id"]
    args = {
        "project_id": pid,
        "name": "配乐 v1",
        "idempotency_key": "shared-key",
        "request": {
            "prompt": "Warm piano score for a sunrise scene",
            "target_duration_seconds": 30,
        },
    }
    return service, args


@pytest.mark.parametrize('kind', ['speech', 'music'])
@pytest.mark.parametrize('new_project', [False, True])
def test_first_generation_in_portable_project(project, cloud, kind, new_project):
    from chromascript.project_storage import migrate

    service, args = project
    migrate(service.store)
    if new_project:
        args['project_id'] = service.create_project('After migration')['project']['id']
    if kind == 'speech':
        args['request'] = {'text': 'Private narration'}
    pid = args['project_id']
    folder = service.store.project_directory(pid)
    assert not (folder / '音频/生成记录').exists()
    # Real decode/publication with a mocked provider exercises the complete new
    # directory hierarchy and durable retry across a service restart.
    result = execute(service, 'generate_' + kind, args)
    source = service.store.project_file(pid, kind + '-sources/' + result['request_id'])
    assert source.is_relative_to(folder)
    assert (source / 'generation.json').is_file()
    assert source.stat().st_mode & 0o777 == 0o700
    assert source.parent.stat().st_mode & 0o777 == 0o700
    assert source.parent.parent.stat().st_mode & 0o777 == 0o700
    assert service.store.file(result['asset']['relative_path']).is_file()
    assert execute(Service(service.store.root), 'generate_' + kind, args) == result
    assert len(cloud[kind + '_calls']) == 1


def test_storage_preflight_failure_does_not_consume_generation_key(project, cloud, monkeypatch):
    from chromascript.project_storage import migrate

    service, args = project
    migrate(service.store)
    folder = service.store.project_directory(args['project_id'])
    original = Path.mkdir

    def fail_generation_parent(path, *positional, **keywords):
        if path == folder / '音频/生成记录':
            raise OSError('simulated disk failure')
        return original(path, *positional, **keywords)

    with monkeypatch.context() as context:
        context.setattr('pathlib.Path.mkdir', fail_generation_parent)
        with pytest.raises(ChromaError) as error:
            execute(service, 'generate_music', args)
        assert error.value.code == 'cloud_storage'
    assert service.store.list('music_request') == []
    assert cloud['music_calls'] == []
    # Fixing a purely local preflight failure permits the original key once.
    assert execute(service, 'generate_music', args)['asset']
    assert len(cloud['music_calls']) == 1


def test_images_request_provenance_and_idempotency(project, cloud, tmp_path):
    service, args = project
    stage = service.store.root / "audio-sources" / args["project_id"]
    stage.mkdir(parents=True)
    image = stage / "preview.png"
    Image.new("RGB", (64, 32), "orange").save(image)
    args["request"]["reference_images"] = [
        {"path": str(image), "time_seconds": 12, "description": "日出"}
    ]
    result = execute(service, "generate_music", args)
    payload = cloud["music_calls"][0]
    assert payload["audio"] == {"format": "mp3"} and payload["stream"] is True
    assert payload["modalities"] == ["text", "audio"]
    content = payload["messages"][0]["content"]
    assert "Instrumental only" in content[0]["text"] and "30 seconds" in content[0]["text"]
    assert "12 seconds" in content[1]["text"]
    uploaded = base64.b64decode(content[2]["image_url"]["url"].split(",")[1])
    import io

    with Image.open(io.BytesIO(uploaded)) as clean, Image.open(image) as original:
        assert clean.convert("RGB").tobytes() == original.tobytes()
    asset = result["asset"]
    assert asset["sample_rate"] == 48000 and asset["channels"] == 2
    assert asset["metadata"]["source_probe"]["streams"][0]["codec_name"] == "mp3"
    report = asset["metadata"]["music_generation"]
    assert report["usage"]["total_tokens"] == 123 and report["id"] == "music-generation-1"
    assert "generated_text" not in report
    private_report = json.loads(
        (
            service.store.root / "music-sources" / result["request_id"] / "generation.json"
        ).read_text()
    )
    assert private_report["generated_text"] == "Instrumental introduction"
    assert report["timing_verified"] is False
    assert "shared-test-secret" not in json.dumps(result) and "data:image" not in json.dumps(result)
    from pathlib import Path

    assert Path(private_report["reference_images"][0]["snapshot_path"]).read_bytes() == uploaded
    assert service.get_project_audio(args["project_id"])["timeline"]["clips"] == []
    assert execute(Service(service.store.root), "generate_music", args) == result
    assert len(cloud["music_calls"]) == 1
    Image.new("RGB", (64, 32), "blue").save(image)
    with pytest.raises(ChromaError) as error:
        execute(service, "generate_music", args)
    assert error.value.code == "idempotency_conflict" and len(cloud["music_calls"]) == 1


def test_concurrent_speech_music_same_key_and_timeline_mix(project, cloud, tmp_path):
    service, args = project
    cloud["barrier"] = threading.Barrier(2)
    speech_args = {
        "project_id": args["project_id"],
        "name": "Narration",
        "idempotency_key": args["idempotency_key"],
        "request": {"text": "The sun rises."},
    }
    # A barrier inside both HTTP handlers proves the two inferences overlap.
    with ThreadPoolExecutor(max_workers=2) as pool:
        music = pool.submit(execute, service, "generate_music", args)
        speech = pool.submit(execute, service, "generate_speech", speech_args)
        music, speech = music.result(timeout=10), speech.result(timeout=10)
    assert len(cloud["music_calls"]) == len(cloud["speech_calls"]) == 1
    assert music["asset"]["id"] != speech["asset"]["id"]
    timeline = AudioTimeline(
        clips=[
            AudioClip(asset_id=music["asset"]["id"], gain_db=-12),
            AudioClip(asset_id=speech["asset"]["id"], gain_db=-3),
        ]
    )
    service.set_project_audio(args["project_id"], "0", timeline)
    settings = RenderSettings(frames=60)
    frozen = freeze_audio(service.store, args["project_id"], settings)
    output = tmp_path / "mix.wav"
    result = mix_audio(service.store, frozen, settings, output)
    assert result["samples"] == 48000
    with wave.open(str(output)) as reader:
        data = np.frombuffer(reader.readframes(48000), dtype="<i4").reshape(-1, 2)[:, 0] / 2**31
    spectrum = abs(np.fft.rfft(data))
    assert spectrum[220] > 100 and spectrum[440] > 100
    assert service.get_speech_config()["model"] == "google/gemini-3.8-flash-tts"
    assert service.get_music_config()["model"] == "google/lyria-3-pro-preview"


@pytest.mark.parametrize(
    "case",
    [
        "http",
        "redirect",
        "timeout",
        "mime",
        "error",
        "no_done",
        "no_finish",
        "length",
        "empty",
        "bad_base64",
        "bad_json",
        "partial_event",
    ],
)
def test_no_partial_publication_or_automatic_charge_retry(project, cloud, case):
    service, args = project
    if case == "http":
        cloud.update(status=429, body=b"shared-test-secret")
    elif case == "redirect":
        cloud.update(status=302)
    elif case == "timeout":
        cloud["timeout"] = True
    elif case == "mime":
        cloud["mime"] = "application/json"
    elif case == "error":
        cloud["body"] = event({"error": {"message": "shared-test-secret"}})
    elif case == "no_done":
        cloud["body"] = music_stream(cloud["mp3"], done=False)
    elif case == "no_finish":
        cloud["body"] = music_stream(cloud["mp3"], finish=None)
    elif case == "length":
        cloud["body"] = music_stream(cloud["mp3"], finish="length")
    elif case == "empty":
        cloud["body"] = music_stream(b"")
    elif case == "bad_base64":
        cloud["body"] = event({"choices": [{"delta": {"audio": {"data": "???"}}}]})
    elif case == "bad_json":
        cloud["body"] = event("not-json-shared-test-secret")
    else:
        cloud["body"] = b'data: {"choices": '
    with pytest.raises(ChromaError) as error:
        execute(service, "generate_music", args)
    assert "shared-test-secret" not in json.dumps(error.value.as_dict())
    assert service.store.list("audio_asset") == []
    with pytest.raises(ChromaError) as error:
        execute(Service(service.store.root), "generate_music", args)
    assert error.value.code == "music_not_retried" and len(cloud["music_calls"]) == 1


def test_preflight_limits_config_and_reference_errors(project, cloud, tmp_path, monkeypatch):
    service, args = project
    for request in [
        {"prompt": " "},
        {"prompt": 1},
        {"prompt": "x", "instrumental": "yes"},
        {"prompt": "x", "api_key": "secret"},
        {"prompt": "x", "target_duration_seconds": 0},
        {"prompt": "x", "model": "google/gemini-3.8-flash-tts"},
        {"prompt": "x", "reference_images": [{"path": "https://example.com/a.png"}]},
    ]:
        with pytest.raises(ChromaError):
            execute(service, "generate_music", {**args, "request": request})
    fake = tmp_path / "fake.png"
    fake.write_text("not an image")
    with pytest.raises(ChromaError):
        execute(
            service,
            "generate_music",
            {**args, "request": {"prompt": "x", "reference_images": [{"path": str(fake)}]}},
        )
    assert not cloud["music_calls"] and not service.store.list("music_request")
    monkeypatch.setenv("OPENROUTER_MUSIC_MODEL", "google/lyria-3-clip-preview")
    assert service.get_music_config()["model"] == "google/lyria-3-clip-preview"
    assert service.get_speech_config()["model"] == "google/gemini-3.8-flash-tts"


def test_stream_limits_and_preserved_paid_source(project, cloud, monkeypatch):
    service, args = project
    monkeypatch.setattr("chromascript.cloud_music.MAX_EVENT_BYTES", 100)
    with pytest.raises(ChromaError) as error:
        execute(service, "generate_music", args)
    assert error.value.code == "music_limit"
    monkeypatch.setattr("chromascript.cloud_music.MAX_EVENT_BYTES", 24 * 1024**2)

    def fail(*a, **kw):
        raise ChromaError("audio_decode", "test failure")

    monkeypatch.setattr(service, "_import_audio", fail)
    with pytest.raises(ChromaError):
        execute(service, "generate_music", {**args, "idempotency_key": "decode-fail"})
    sources = list((service.store.root / "music-sources").glob("*/source.mp3"))
    assert len(sources) == 1
    assert sources[0].read_bytes() == cloud["mp3"]
    assert sources[0].with_name("generation.json").exists()


def test_http_mcp_cli_music_parity(tmp_path, cloud, monkeypatch):
    web = create_app(tmp_path / "workspace")
    service = web.state.service
    pid = service.create_project("Music transports")["project"]["id"]
    args = {
        "project_id": pid,
        "name": "Score",
        "request": {"prompt": "Warm piano"},
        "idempotency_key": "transport",
    }
    with TestClient(web, base_url="http://127.0.0.1:8765") as http:
        token = http.get("/api/session").json()["token"]
        response = http.post(
            "/api/operations/generate_music",
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
                config = await client.call_tool("chromascript.get_music_config", {"request": {}})
                assert not config.is_error and "shared-test-secret" not in str(config)
                result = await client.call_tool("chromascript.generate_music", {"request": args})
                assert not result.is_error and result.structured_content["asset"]["id"] == aid

        asyncio.run(check())
        monkeypatch.setattr(
            "chromascript.cli.call", lambda root, op, arguments: execute(service, op, arguments)
        )
        result = CliRunner().invoke(
            app, ["call", "generate_music", "--arguments", json.dumps(args)]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["asset"]["id"] == aid
    assert len(cloud["music_calls"]) == 1
