"""Regression evidence for cloud-audio privacy boundaries; never sends real data."""

import asyncio
import base64
import io
import json
import os
import stat

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from PIL import Image, PngImagePlugin
from test_cloud_music import cloud as cloud
from test_cloud_music import project as project

from chromascript.api import create_app
from chromascript.cloud_music import MusicGenerationRequest, read_references
from chromascript.cloud_privacy import private_directory_tree, read_private_env
from chromascript.errors import ChromaError
from chromascript.mcp_server import create_mcp
from chromascript.operations import execute


def test_nested_private_directory_rejects_intermediate_symlink(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    outside = tmp_path / 'unrelated'
    outside.mkdir(mode=0o755)
    original_mode = outside.stat().st_mode
    (root / 'audio').symlink_to(outside, target_is_directory=True)
    # Reject the intermediate link before creating or chmod-ing its target.
    with pytest.raises(ChromaError) as error:
        private_directory_tree(root, root / 'audio/generation/speech')
    assert error.value.code == 'cloud_storage'
    assert outside.stat().st_mode == original_mode
    assert list(outside.iterdir()) == []


def test_upload_discards_metadata_and_trailing_bytes(project, cloud):
    service, args = project
    stage = service.store.root / "audio-sources" / args["project_id"]
    stage.mkdir(parents=True)
    path = stage / "selected.png"
    marker = "PRIVATE_GPS_ADDRESS_AND_EMBEDDED_SECRET"
    tags = PngImagePlugin.PngInfo()
    tags.add_text("GPS", marker)
    tags.add_text("Comment", marker)
    Image.new("RGBA", (12, 8), (64, 32, 16, 128)).save(path, pnginfo=tags)
    with path.open("ab") as f:
        f.write(marker.encode())
    args["request"]["reference_images"] = [{"path": str(path)}]
    result = execute(service, "generate_music", args)
    payload = cloud["music_calls"][0]
    uri = payload["messages"][0]["content"][2]["image_url"]["url"]
    data = base64.b64decode(uri.split(",")[1])
    assert marker.encode() not in data
    with Image.open(io.BytesIO(data)) as clean:
        assert clean.getpixel((0, 0)) == (64, 32, 16, 128)
        assert set(clean.info) == {"icc_profile"}
    assert marker in path.read_bytes().decode("latin1")  # User's original stays intact.
    public = json.dumps(result)
    assert str(path) not in public and args["request"]["prompt"] not in public
    assert payload["provider"] == {"zdr": True, "data_collection": "deny"}


def test_jpeg_exif_orientation_is_baked_without_metadata(project):
    service, args = project
    stage = service.store.root / "audio-sources" / args["project_id"]
    stage.mkdir(parents=True)
    path = stage / "selected.jpg"
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = "PRIVATE_AUTHOR"
    Image.new("RGB", (16, 8), "red").save(path, exif=exif)
    request = MusicGenerationRequest(prompt="x", reference_images=[{"path": str(path)}])
    data, report = read_references(request, service.store, args["project_id"])[0]
    assert b"PRIVATE_AUTHOR" not in data and report["sanitized"]
    with Image.open(io.BytesIO(data)) as clean:
        assert clean.size == (8, 16) and not clean.getexif()


def test_reference_scope_blocks_home_other_project_and_symlinks(project, cloud, tmp_path):
    service, args = project
    other = tmp_path / "private-photo.png"
    Image.new("RGB", (8, 8)).save(other)
    for path in [other]:
        with pytest.raises(ChromaError) as error:
            execute(
                service,
                "generate_music",
                {**args, "request": {"prompt": "x", "reference_images": [{"path": str(path)}]}},
            )
        assert error.value.code == "music_reference_scope"
    stage = service.store.root / "audio-sources" / args["project_id"]
    stage.mkdir(parents=True)
    link = stage / "link.png"
    link.symlink_to(other)
    with pytest.raises(ChromaError) as error:
        execute(
            service,
            "generate_music",
            {**args, "request": {"prompt": "x", "reference_images": [{"path": str(link)}]}},
        )
    assert error.value.code == "music_reference_scope"
    assert cloud["music_calls"] == []


def test_registered_project_preview_remains_usable(project, tmp_path):
    service, args = project
    path = service.store.root / "preview.png"
    Image.new("RGB", (8, 8)).save(path)
    service.store.put(
        "artifact",
        "preview",
        {
            "id": "preview",
            "project_id": args["project_id"],
            "mime_type": "image/png",
            "relative_path": "preview.png",
        },
    )
    request = MusicGenerationRequest(prompt="x", reference_images=[{"path": str(path)}])
    assert read_references(request, service.store, args["project_id"])
    with pytest.raises(ChromaError):
        read_references(request, service.store, "another-project")


def test_private_provenance_permissions_and_minimal_public_records(project, cloud):
    service, args = project
    prompt = "PRIVATE_UNRELEASED_PROJECT_SCRIPT"
    args["request"]["prompt"] = prompt
    result = execute(service, "generate_music", args)
    directory = service.store.root / "music-sources" / result["request_id"]
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(directory.parent.stat().st_mode) == 0o700
    for path in directory.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert prompt in (directory / "generation.json").read_text()
    assert prompt not in json.dumps(result)
    for kind in ("music_request", "artifact", "audio_asset"):
        assert prompt not in json.dumps(service.store.list(kind))
    assert (
        stat.S_IMODE(service.store.file(result["asset"]["relative_path"]).stat().st_mode) == 0o600
    )
    assert "source_path" not in result["asset"]["metadata"]["music_generation"]
    assert "filename" not in json.dumps(result["asset"]["metadata"]["source_probe"])


def test_both_routes_privacy_policy_and_local_only_zdr_override(project, cloud, monkeypatch):
    service, args = project
    execute(
        service,
        "generate_speech",
        {
            "project_id": args["project_id"],
            "name": "voice",
            "idempotency_key": "voice",
            "request": {"text": "hello"},
        },
    )
    assert cloud["speech_calls"][0]["provider"] == {"data_collection": "deny", "zdr": True}
    monkeypatch.setenv("OPENROUTER_REQUIRE_ZDR", "false")
    execute(service, "generate_music", args)
    assert cloud["music_calls"][0]["provider"] == {"data_collection": "deny", "zdr": False}
    assert service.get_music_config()["privacy_policy"]["zdr"] is False
    monkeypatch.setenv("OPENROUTER_REQUIRE_ZDR", "invalid")
    assert service.get_music_config()["configuration_error"]
    assert service.get_speech_config()["configuration_error"]
    with pytest.raises(ChromaError):
        execute(service, "generate_music", {**args, "idempotency_key": "invalid-policy"})
    assert len(cloud["music_calls"]) == 1


def test_private_env_rejects_links_and_shared_files(tmp_path):
    path = tmp_path / ".env"
    path.write_text("OPENROUTER_API_KEY=private-value")
    path.chmod(0o644)
    with pytest.raises(ValueError):
        read_private_env(path)
    path.chmod(0o600)
    assert "private-value" in read_private_env(path)
    link = tmp_path / "linked.env"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_private_env(link)
    fifo = tmp_path / "pipe.env"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ValueError):
        read_private_env(fifo)


def test_credentials_in_generation_inputs_rejected_before_network(project, cloud):
    service, args = project
    for op, request in [
        ("generate_music", {"prompt": "shared-test-secret"}),
        ("generate_speech", {"text": "shared-test-secret"}),
    ]:
        with pytest.raises(ChromaError) as error:
            execute(service, op, {**args, "request": request})
        assert error.value.code == "cloud_secret_in_input"
        assert "shared-test-secret" not in str(error.value)
    assert cloud["music_calls"] == cloud["speech_calls"] == []


def test_validation_errors_never_echo_credential_values_http_cli_mcp(tmp_path, cloud, monkeypatch):
    web = create_app(tmp_path / "workspace")
    service = web.state.service
    pid = service.create_project("Privacy")["project"]["id"]
    secret = "sk-or-v1-PRIVATE_CREDENTIAL_DO_NOT_ECHO"
    args = {
        "project_id": pid,
        "name": "x",
        "idempotency_key": "x",
        "request": {"text": "x", "api_key": secret},
    }
    with pytest.raises(ChromaError) as error:
        execute(service, "generate_speech", args)
    assert secret not in json.dumps(error.value.as_dict())
    with TestClient(web, base_url="http://127.0.0.1:8765") as http:
        token = http.get("/api/session").json()["token"]
        headers = {"Authorization": "Bearer " + token}
        response = http.post("/api/operations/generate_speech", json=args, headers=headers)
        assert response.status_code == 422 and secret not in response.text
        response = http.post(
            "/api/operations/generate_music",
            content='{"bad":"' + secret,
            headers={**headers, "Content-Type": "application/json"},
        )
        assert response.status_code == 422 and secret not in response.text
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
                result = await client.call_tool("chromascript.generate_speech", {"request": args})
                assert result.is_error and secret not in str(result)

        asyncio.run(check())
    assert not cloud["music_calls"] and not cloud["speech_calls"]


def test_configuration_never_reflects_misplaced_key(project, monkeypatch):
    service, _ = project
    monkeypatch.setenv("OPENROUTER_TTS_MODEL", "shared-test-secret")
    monkeypatch.setenv("OPENROUTER_TTS_VOICE", "shared-test-secret")
    monkeypatch.setenv("OPENROUTER_MUSIC_MODEL", "shared-test-secret")
    assert "shared-test-secret" not in json.dumps(service.get_speech_config())
    assert "shared-test-secret" not in json.dumps(service.get_music_config())


def test_private_storage_rejects_redirected_directory_without_touching_files(
    project, cloud, tmp_path
):
    service, args = project
    outside = tmp_path / "shared-output"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("untouched")
    marker.chmod(0o644)
    (service.store.root / "music-sources").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ChromaError):
        execute(service, "generate_music", args)
    assert marker.read_text() == "untouched"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o644
    assert not cloud["music_calls"]


def test_secret_name_and_tool_policy_override_fail_before_network(project, cloud):
    service, args = project
    for changed in [
        {**args, "name": "shared-test-secret"},
        {**args, "request": {"prompt": "x", "provider": {"zdr": False}}},
    ]:
        with pytest.raises(ChromaError) as error:
            execute(service, "generate_music", changed)
        assert "shared-test-secret" not in json.dumps(error.value.as_dict())
    assert not cloud["music_calls"]
