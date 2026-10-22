"""Audio bytes, local-only routing, review provenance and MCP/CLI parity."""

import asyncio
import base64
import copy
import io
import json
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from pydantic import ValidationError
from typer.testing import CliRunner

from chromascript.api import create_app
from chromascript.audio import AudioEvent, AudioSynthesis
from chromascript.audio_review import AudioModelConfig, AudioReviewRequest
from chromascript.cli import app
from chromascript.errors import ChromaError
from chromascript.mcp_server import create_mcp
from chromascript.operations import execute
from chromascript.service import Service


def feedback():
    return {
        "summary": "听到稳定的合成音。",
        "strengths": ["声音平稳"],
        "issues": [
            {
                "category": "composition",
                "severity": "info",
                "start_seconds": 0.1,
                "end_seconds": 0.4,
                "observation": "旋律变化较少",
                "suggestion": "尝试在镜头切换处加入短动机",
            }
        ],
        "suggested_changes": ["降低阅读窗口的起音密度"],
        "uncertainty": "这是单声道片段，不能评价原始声像或画面同步。",
    }


@pytest.fixture
def local_model(monkeypatch):
    state = {
        "audio": True,
        "content": json.dumps(feedback(), ensure_ascii=False),
        "finish": "stop",
        "model": "local-qwen",
        "posts": [],
        "requests": [],
        "http_status": 200,
    }

    def handler(request):
        state["requests"].append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "local-qwen",
                            "architecture": {
                                "input_modalities": ["text", "audio"]
                                if state["audio"]
                                else ["text"],
                            },
                        }
                    ]
                },
            )
        if request.url.path == "/props":
            return httpx.Response(
                200, json={"build_info": "test-build", "modalities": {"audio": state["audio"]}}
            )
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        state["posts"].append(payload)
        if state.get("timeout"):
            raise httpx.ReadTimeout("test", request=request)
        return httpx.Response(
            state["http_status"],
            json={
                "model": state["model"],
                "choices": [
                    {"finish_reason": state["finish"], "message": {"content": state["content"]}}
                ],
                "usage": {"completion_tokens": 50, "prompt_tokens": 100, "total_tokens": 150},
            },
        )

    def client(config, timeout):
        return httpx.Client(
            base_url=config.base_url,
            transport=httpx.MockTransport(handler),
            trust_env=False,
            follow_redirects=False,
        )

    monkeypatch.setattr("chromascript.audio_review.model_client", client)
    return state


@pytest.fixture
def audio_project(tmp_path):
    service = Service(tmp_path / "workspace")
    pid = service.create_project("Model review")["project"]["id"]
    asset = service.create_audio(
        pid,
        "test tone",
        AudioSynthesis(
            duration_seconds=1,
            events=[AudioEvent(duration_seconds=1, pan=-1, fade_seconds=0, gain_db=-6)],
        ),
    )["asset"]
    return service, pid, asset


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8080",
        "http://example.com",
        "http://192.168.0.1",
        "http://0.0.0.0",
        "http://localhost.evil.test",
        "http://user:secret@127.0.0.1:8080",
        "http://127.0.0.1:8080/proxy",
        "http://127.0.0.1:8080?url=elsewhere",
        "http://127.0.0.1:8080#fragment",
        "http://[::ffff:192.168.0.1]:8080",
        "file:///tmp/model",
        "http://127.0.0.1:99999",
    ],
)
def test_local_only_config(url):
    with pytest.raises(ValidationError):
        AudioModelConfig(base_url=url)


def test_url_normalization_and_schema():
    assert (
        AudioModelConfig(base_url="http://localhost:8081/v1/").base_url == "http://127.0.0.1:8081"
    )
    assert AudioModelConfig(base_url="http://[::1]:8080").base_url == "http://[::1]:8080"
    with pytest.raises(ValidationError):
        AudioModelConfig(max_tokens="1000")
    with pytest.raises(ValidationError):
        AudioReviewRequest.model_validate({"brief": "hi", "api_key": "secret"})


def test_discovery_and_config_do_not_infer(audio_project, local_model):
    service, pid, _ = audio_project
    service._renderer = SimpleNamespace(info={})
    assert (
        service.get_audio_guidelines(pid)["model_review"]
        == service.get_capabilities()["audio"]["model_review"]
    )
    assert local_model["requests"] == []
    status = service.get_audio_model()
    assert status["available"] and status["audio_input_evidence"] == "server metadata"
    saved = execute(
        service, "configure_audio_model", {"config": {"base_url": "http://localhost:8080/v1"}}
    )
    assert saved["config"]["model"] == "local-qwen"
    assert service.store.get("audio_model_config", "default")["model"] == "local-qwen"
    assert local_model["posts"] == []
    local_model["audio"] = False
    assert not service.get_audio_model()["available"]
    with pytest.raises(ChromaError, match="not saved"):
        service.configure_audio_model(AudioModelConfig())
    assert service.store.get("audio_model_config", "default")["model"] == "local-qwen"


def test_actual_clip_bytes_feedback_and_report(audio_project, local_model):
    import wave

    service, pid, asset = audio_project
    original = service.store.file(asset["relative_path"]).read_bytes()
    result = execute(
        service,
        "review_audio",
        {
            "project_id": pid,
            "asset_id": asset["id"],
            "review": {
                "start_seconds": 0.25,
                "duration_seconds": 0.5,
                "brief": "温和的公式讲解",
                "focus": "旋律发展",
            },
        },
    )
    report = result["report"]
    assert report["feedback_status"] == "complete"
    assert report["feedback"]["summary"] == feedback()["summary"]
    assert report["objective"]["source_sha256"] == asset["sha256"]
    assert report["objective"]["start_sample"] == 12000
    assert report["objective"]["samples"] == 24000
    assert report["objective"]["sample_peak"] == pytest.approx(10 ** (-6 / 20))
    assert report["objective"]["rms_per_channel"][1] == 0
    # This is the selected stereo source clip, independently of model/downmix output.
    assert report["objective"]["loudness"]["true_peak_dbtp"] == pytest.approx(-6, abs=0.1)
    assert report["objective"]["loudness"]["integrated_lufs"] == pytest.approx(-9.7, abs=0.2)
    payload = local_model["posts"][0]
    assert payload["stream"] is False and "tools" not in payload
    part = payload["messages"][1]["content"][0]
    assert part["type"] == "input_audio"
    # Confirm the actual network payload is a decoded audio crop, not a transcript,
    # score-only prompt, URL or a reference to a local path the model cannot read.
    with wave.open(io.BytesIO(base64.b64decode(part["input_audio"]["data"])), "rb") as reader:
        assert (
            reader.getframerate(),
            reader.getnchannels(),
            reader.getsampwidth(),
            reader.getnframes(),
        ) == (16000, 1, 2, 8000)
        assert np.max(np.abs(np.frombuffer(reader.readframes(8000), dtype="<i2"))) > 1000
    assert service.store.file(asset["relative_path"]).read_bytes() == original
    assert service.get_project_audio(pid)["timeline"]["clips"] == []
    saved = service.get_artifact(result["artifact"]["id"])
    assert json.loads(saved["text"])["feedback"] == report["feedback"]
    assert result["artifact"]["kind"] == "audio-review"
    assert not list(service.store.root.glob("audio-review-*"))


@pytest.mark.parametrize(
    "settings",
    [
        {"content": "not json"},
        {"finish": "length"},
        {"model": "silently-switched"},
        {
            "content": json.dumps(
                {**feedback(), "issues": [{**feedback()["issues"][0], "end_seconds": 2}]}
            )
        },
    ],
)
def test_invalid_feedback_is_never_certified(audio_project, local_model, settings):
    service, pid, asset = audio_project
    local_model.update(settings)
    report = service.review_audio(pid, asset["id"])["report"]
    assert report["feedback_status"] == "invalid" and report["feedback"] is None
    assert report["raw_feedback"] == local_model["content"]
    assert report["validation_error"]


def test_bounds_and_integrity_before_sending(audio_project, local_model):
    service, pid, asset = audio_project
    for request in [
        AudioReviewRequest(start_seconds=1),
        AudioReviewRequest(start_seconds=0.8, duration_seconds=0.5),
    ]:
        with pytest.raises(ChromaError, match="in-bounds"):
            service.review_audio(pid, asset["id"], request)
    long = service.create_audio(pid, "long", AudioSynthesis(duration_seconds=31))["asset"]
    with pytest.raises(ChromaError, match="explicit duration"):
        service.review_audio(pid, long["id"])
    other = service.create_project("Other")["project"]["id"]
    with pytest.raises(ChromaError, match="different project"):
        service.review_audio(other, asset["id"])
    service.store.file(asset["relative_path"]).write_bytes(b"changed")
    with pytest.raises(ChromaError, match="checksum"):
        service.review_audio(pid, asset["id"])
    assert local_model["requests"] == []


@pytest.mark.parametrize(
    "settings", [{"timeout": True}, {"http_status": 400}, {"http_status": 307}, {"content": ""}]
)
def test_request_failure_releases_lock_without_report(audio_project, local_model, settings):
    service, pid, asset = audio_project
    local_model.update(settings)
    before = len(service.store.list("artifact"))
    with pytest.raises(ChromaError):
        service.review_audio(pid, asset["id"])
    assert len(service.store.list("artifact")) == before
    assert not service.audio_review_lock.locked()
    assert not list(service.store.root.glob("audio-review-*"))


def test_busy_review_sends_no_audio(audio_project, local_model):
    service, pid, asset = audio_project
    with service.audio_review_lock:
        with pytest.raises(ChromaError, match="running"):
            execute(service, "review_audio", {"project_id": pid, "asset_id": asset["id"]})
        assert service.get_project_audio(pid)["assets"]
    assert local_model["requests"] == []


def test_http_mcp_cli_feedback_parity(tmp_path, local_model, monkeypatch):
    web = create_app(tmp_path / "workspace")
    service = web.state.service
    pid = service.create_project("Review transports")["project"]["id"]
    asset = service.create_audio(pid, "tone", AudioSynthesis(duration_seconds=1))["asset"]
    arguments = {"project_id": pid, "asset_id": asset["id"]}
    with TestClient(web, base_url="http://127.0.0.1:8765") as http:
        token = http.get("/api/session").json()["token"]
        headers = {"Authorization": "Bearer " + token}
        response = http.post("/api/operations/review_audio", json=arguments, headers=headers)
        assert response.status_code == 200
        assert response.json()["report"]["feedback_status"] == "complete"
        invalid = http.post(
            "/api/operations/review_audio",
            json={**arguments, "review": {"duration_seconds": 31}},
            headers=headers,
        )
        assert invalid.status_code == 422
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
                for name, args in [
                    ("get_audio_model", {}),
                    ("configure_audio_model", {"config": {}}),
                    ("review_audio", arguments),
                ]:
                    result = await client.call_tool("chromascript." + name, {"request": args})
                    assert not result.is_error
                assert result.structured_content["report"]["feedback_status"] == "complete"

        asyncio.run(check())
        monkeypatch.setattr(
            "chromascript.cli.call", lambda root, name, args: execute(service, name, args)
        )
        result = CliRunner().invoke(
            app, ["call", "review_audio", "--arguments", json.dumps(arguments)]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["report"]["feedback_status"] == "complete"


def test_creation_intent_is_labeled_not_heard_evidence(audio_project, local_model):
    service, pid, asset = audio_project
    enriched = copy.deepcopy(asset)
    enriched["metadata"]["generation_report"] = {
        "score": {
            "constraints": {
                "theme": "弦乐主题",
                "instrumentation": ["violin"],
                "hit_points": [
                    {"label": "cut", "time_seconds": 0.3},
                    {"label": "outside", "time_seconds": 3},
                ],
                "reading_windows": [{"start_seconds": 0.2, "end_seconds": 0.4}],
            }
        }
    }
    service.store.put("audio_asset", asset["id"], enriched)
    report = service.review_audio(pid, asset["id"])["report"]
    intent = report["context"]["composition_intent"]
    assert intent["instrumentation"] == ["violin"]
    assert len(intent["hit_points"]) == 1
    assert "不是听觉事实" in local_model["posts"][0]["messages"][0]["content"]
