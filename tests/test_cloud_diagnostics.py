"""Failed completions retain private evidence without leaking it into tool output."""

import json
import stat
from pathlib import Path

import pytest
from test_cloud_music import cloud as cloud
from test_cloud_music import event, music_stream
from test_cloud_music import project as project
from test_media_analysis import provider as provider

from chromascript.cloud_diagnostics import safe_usage
from chromascript.errors import ChromaError
from chromascript.operations import execute
from chromascript.service import Service


@pytest.mark.parametrize(
    "reason,code", [("length", "analysis_truncated"), ("content_filter", "analysis_refused")]
)
def test_analysis_failure_retains_response_usage_and_retry_summary(project, provider, reason, code):
    service, music = project
    private_text = '{"unfinished_score": "PRIVATE_PARTIAL"'
    body = {
        "id": "gen-failed-123",
        "provider": "Google Vertex",
        "choices": [{"finish_reason": reason, "message": {"content": private_text}}],
        "usage": {
            "prompt_tokens": 101,
            "completion_tokens": 6231,
            "completion_tokens_details": {"reasoning_tokens": 5631},
            "total_tokens": 6332,
            "cost": 0.01,
        },
        "debug": {"authorization": "Bearer shared-test-secret", "api_key": "different-secret"},
    }
    provider["raw"] = json.dumps(body).encode()
    args = {
        "project_id": music["project_id"],
        "idempotency_key": "failed-score",
        "request": {"prompt": "Write a score"},
    }
    with pytest.raises(ChromaError) as error:
        execute(service, "analyze_media", args)
    assert error.value.code == code
    details = error.value.details
    assert details["finish_reason"] == reason and details["generation_id"] == "gen-failed-123"
    assert details["usage"]["reasoning_tokens"] == 5631
    assert details["usage"]["output_tokens"] == 600
    assert details["usage"]["cost"] == 0.01
    path = Path(details["diagnostics_path"])
    saved = json.loads(path.read_text())
    assert saved["response"]["choices"][0]["message"]["content"] == private_text
    assert saved["http_status"] == 200 and saved["provider_name"] == "Google Vertex"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for secret in ("shared-test-secret", "different-secret"):
        assert secret not in path.read_text()
    assert "PRIVATE_PARTIAL" not in json.dumps(error.value.as_dict())
    with pytest.raises(ChromaError) as retry:
        execute(Service(service.store.root), "analyze_media", args)
    assert retry.value.code == "analysis_not_retried"
    assert retry.value.details["usage"] == details["usage"]
    assert len(provider["calls"]) == 1


def test_analysis_http_errors_keep_private_body(project, provider):
    service, music = project
    provider.update(
        status=403,
        raw=json.dumps(
            {
                "error": {
                    "code": 403,
                    "message": "Policy rejected PRIVATE_PROMPT shared-test-secret",
                    "metadata": {"provider_name": "Google", "generation_id": "gen-http-123"},
                }
            }
        ).encode(),
    )
    with pytest.raises(ChromaError) as error:
        execute(
            service,
            "analyze_media",
            {
                "project_id": music["project_id"],
                "idempotency_key": "http",
                "request": {"prompt": "test"},
            },
        )
    saved = json.loads(Path(error.value.details["diagnostics_path"]).read_text())
    assert saved["http_status"] == 403 and saved["generation_id"] == "gen-http-123"
    assert "PRIVATE_PROMPT" in saved["response"]["error"]["message"]
    assert "shared-test-secret" not in json.dumps(saved)
    assert "PRIVATE_PROMPT" not in json.dumps(error.value.as_dict())


@pytest.mark.parametrize("http_error", [False, True])
def test_music_errors_preserve_event_provider_id_and_usage(project, cloud, http_error):
    service, args = project
    upstream = {
        "id": "gen-lyria-123",
        "provider": "Google AI Studio",
        "usage": {"cost": 0.08},
        "error": {
            "code": 400,
            "message": "PRIVATE_ARTIST is not allowed",
            "metadata": {
                "provider_name": "Google AI Studio",
                "raw": "Authorization: Bearer shared-test-secret",
            },
        },
    }
    cloud["body"] = json.dumps(upstream).encode() if http_error else event(upstream)
    if http_error:
        cloud["status"] = 400
    with pytest.raises(ChromaError) as error:
        execute(service, "generate_music", args)
    details = error.value.details
    saved = json.loads(Path(details["diagnostics_path"]).read_text())
    assert saved["generation_id"] == "gen-lyria-123"
    assert saved["provider_name"] == "Google AI Studio"
    assert "PRIVATE_ARTIST" in json.dumps(saved)
    assert "shared-test-secret" not in json.dumps(saved)
    assert "PRIVATE_ARTIST" not in json.dumps(details)
    assert details["usage"]["cost"] == 0.08
    with pytest.raises(ChromaError) as retry:
        execute(service, "generate_music", args)
    assert retry.value.details["generation_id"] == "gen-lyria-123"
    assert len(cloud["music_calls"]) == 1


def test_reasoning_budget_passthrough_and_upper_bound(project, provider):
    service, music = project
    base = {
        "project_id": music["project_id"],
        "idempotency_key": "reasoning",
        "request": {
            "prompt": "Write score",
            "max_tokens": 65536,
            "reasoning": {"effort": "low", "exclude": True},
        },
    }
    execute(service, "analyze_media", base)
    assert provider["calls"][0]["max_tokens"] == 65536
    assert provider["calls"][0]["reasoning"] == {"effort": "low", "exclude": True}
    assert provider["calls"][0]["provider"]["zdr"] is True
    for request in [
        {"prompt": "x", "max_tokens": 65537},
        {"prompt": "x", "reasoning": {"effort": "low", "max_tokens": 100}},
        {"prompt": "x", "max_tokens": 2048, "reasoning": {"max_tokens": 2048}},
        {"prompt": "x", "reasoning": {"effort": "low", "provider": {"zdr": False}}},
    ]:
        with pytest.raises(ChromaError):
            execute(
                service, "analyze_media", {**base, "idempotency_key": "bad", "request": request}
            )
    execute(
        service,
        "analyze_media",
        {
            **base,
            "idempotency_key": "budget",
            "request": {"prompt": "x", "max_tokens": 16384, "reasoning": {"max_tokens": 2048}},
        },
    )
    assert provider["calls"][1]["reasoning"] == {"max_tokens": 2048}
    assert len(provider["calls"]) == 2


def test_usage_never_invents_reasoning_or_zero_cost():
    assert safe_usage({"completion_tokens": 9}) == {"completion_tokens": 9}
    assert safe_usage(None) == {}
    assert (
        safe_usage({"completion_tokens": 3, "completion_tokens_details": {"reasoning_tokens": 5}})[
            "output_tokens"
        ]
        is None
    )


def test_music_truncated_stream_retains_received_metadata(project, cloud):
    service, args = project
    cloud["body"] = music_stream(cloud["mp3"], finish="length")
    with pytest.raises(ChromaError) as error:
        execute(service, "generate_music", args)
    saved = json.loads(Path(error.value.details["diagnostics_path"]).read_text())
    assert saved["finish_reason"] == "length" and saved["generation_id"] == "music-generation-1"
    assert saved["received_audio_bytes"] == len(cloud["mp3"])


def test_music_failure_reads_trailing_usage(project, cloud):
    service, args = project
    cloud["body"] = (
        event({"id": "gen-error-trailer", "error": {"message": "provider failed"}})
        + event(
            {
                "choices": [],
                "usage": {
                    "completion_tokens": 100,
                    "completion_tokens_details": {"reasoning_tokens": 90},
                    "cost": 0.08,
                },
            }
        )
        + event("[DONE]")
    )
    with pytest.raises(ChromaError) as error:
        execute(service, "generate_music", args)
    assert error.value.code == "music_provider"
    assert error.value.details["usage"]["output_tokens"] == 10
    assert error.value.details["usage"]["cost"] == 0.08
    assert (
        json.loads(Path(error.value.details["diagnostics_path"]).read_text())["complete"] is False
    )
