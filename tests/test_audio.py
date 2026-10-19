"""Actual PCM, muxed streams, revision conflicts and transport parity for audio."""

import base64
import wave
from fractions import Fraction

import numpy as np
import pytest
from typer.testing import CliRunner

from chromascript.audio import (
    AudioClip,
    AudioEvent,
    AudioSynthesis,
    AudioTimeline,
    freeze_audio,
    mix_audio,
    samples,
)
from chromascript.cli import app
from chromascript.encoding import check_probe, encode
from chromascript.errors import ChromaError
from chromascript.models import Rational, RenderSettings
from chromascript.operations import execute
from chromascript.presets import builtins
from chromascript.service import Service
from chromascript.worker import run


@pytest.fixture
def audio_project(tmp_path):
    service = Service(tmp_path / "workspace")
    project = service.create_project("Audio")["project"]["id"]
    result = execute(
        service,
        "create_audio",
        {
            "project_id": project,
            "name": "Beat",
            "spec": {
                "duration_seconds": 1,
                "events": [
                    {
                        "waveform": "sine",
                        "frequency_hz": 1000,
                        "duration_seconds": 1,
                        "gain_db": -6,
                        "pan": -1,
                        "fade_seconds": 0,
                    }
                ],
            },
        },
    )
    return service, project, result["asset"]


def pcm(path):
    with wave.open(str(path), "rb") as reader:
        return (
            np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i4").reshape(-1, 2)
            / 2**31
        )


def test_create_import_and_cli(audio_project, monkeypatch):
    service, project, asset = audio_project
    path = service.store.file(asset["relative_path"])
    data = pcm(path)
    assert data.shape == (48000, 2)
    assert np.max(np.abs(data[:, 1])) == 0
    assert np.max(np.abs(data[:, 0])) == pytest.approx(10 ** (-6 / 20))
    assert np.argmax(np.abs(np.fft.rfft(data[:, 0]))) == 1000
    assert asset["metadata"]["probe"]["streams"][0]["codec_name"] == "pcm_s32le"
    imported = service.import_audio(project, "imported", source_path=str(path))["asset"]
    np.testing.assert_array_equal(pcm(service.store.file(imported["relative_path"])), data)
    encoded = base64.b64encode(path.read_bytes()).decode()
    assert service.import_audio(project, "bytes", data_base64=encoded)["asset"]["samples"] == 48000
    monkeypatch.setattr("chromascript.cli.call", lambda root, op, args: execute(service, op, args))
    result = CliRunner().invoke(
        app, ["call", "get_project_audio", "--arguments", '{"project_id":"' + project + '"}']
    )
    assert result.exit_code == 0 and "Beat" in result.stdout
    assert service.get_artifact(asset["id"])["artifact"]["mime_type"] == "audio/wav"


def test_validation_conflicts_and_frozen_timeline(audio_project, tmp_path):
    service, project, asset = audio_project
    timeline = AudioTimeline(
        clips=[
            AudioClip(
                asset_id=asset["id"],
                start_seconds=0.2,
                source_start_seconds=0.1,
                duration_seconds=0.5,
            )
        ]
    )
    saved = service.set_project_audio(project, "0", timeline)
    frozen = freeze_audio(service.store, project, RenderSettings())
    service.set_project_audio(project, saved["revision_id"], AudioTimeline())
    with pytest.raises(ChromaError, match="latest audio"):
        service.set_project_audio(project, saved["revision_id"], timeline)
    settings = RenderSettings(frames=30, fps=Rational(numerator=30000, denominator=1001))
    target = tmp_path / "mix.wav"
    report = mix_audio(service.store, frozen, settings, target)
    assert report["samples"] == 48048
    data = pcm(target)
    assert np.count_nonzero(data[:9600]) == 0
    assert np.count_nonzero(data[33600:]) == 0
    np.testing.assert_array_equal(
        data[9600:33600], pcm(service.store.file(asset["relative_path"]))[4800:28800]
    )
    # start_frame is a timeline crop, not a delay added before the audio.
    mix_audio(service.store, frozen, settings.model_copy(update={"start_frame": 9}), target)
    assert np.count_nonzero(pcm(target)[:100]) > 0
    service.store.file(asset["relative_path"]).write_bytes(b"changed")
    with pytest.raises(ChromaError, match="missing or changed"):
        mix_audio(service.store, frozen, settings, target)


def test_invalid_inputs(audio_project, tmp_path):
    service, project, asset = audio_project
    for args in [
        {},
        {"source_path": "relative.wav"},
        {"data_base64": "!bad"},
        {"source_path": "/missing", "data_base64": "AAAA"},
    ]:
        with pytest.raises(ChromaError):
            service.import_audio(project, "bad", **args)
    bad = tmp_path / "bad.wav"
    bad.write_text("#EXTM3U\nhttp://127.0.0.1:9/private\n")
    with pytest.raises(ChromaError, match="decode"):
        service.import_audio(project, "playlist", source_path=str(bad))
    other = service.create_project("other")["project"]["id"]
    with pytest.raises(ChromaError, match="different project"):
        service.set_project_audio(
            other, "0", AudioTimeline(clips=[AudioClip(asset_id=asset["id"])])
        )
    with pytest.raises(ChromaError, match="outside"):
        service.set_project_audio(
            project, "0", AudioTimeline(clips=[AudioClip(asset_id=asset["id"], duration_seconds=2)])
        )
    for spec in [
        {"duration_seconds": 601},
        {"duration_seconds": float("nan")},
        {"duration_seconds": 1, "events": [{"frequency_hz": 30000}]},
        {"duration_seconds": 1, "events": [{"start_seconds": 0.1, "duration_seconds": 1}]},
        {"duration_seconds": 1, "unexpected": 3},
    ]:
        with pytest.raises(ChromaError):
            execute(
                service, "create_audio", {"project_id": project, "name": "invalid", "spec": spec}
            )
    service.archive_project(project)
    with pytest.raises(ChromaError, match="Restore"):
        service.create_audio(project, "archived", AudioSynthesis(duration_seconds=1))


@pytest.mark.parametrize("preset_id", ["h264-srgb-preview", "h264-mov", "ffv1-rgb16"])
def test_mux_audio_actual_streams(audio_project, tmp_path, preset_id):
    service, project, asset = audio_project
    preset = builtins().get(preset_id)
    assert preset is not None
    from chromascript.models import ColorPipeline

    preset = preset.model_copy(update={"color": ColorPipeline(output="sRGB")})
    service.set_project_audio(project, "0", AudioTimeline(clips=[AudioClip(asset_id=asset["id"])]))
    settings = RenderSettings(width=32, height=32, frames=12, fps=Rational(numerator=24))
    wav = tmp_path / "mix.wav"
    mix_audio(service.store, freeze_audio(service.store, project, RenderSettings()), settings, wav)
    frame = np.ones((32, 32, 4), dtype=np.float32)
    info = encode(
        preset,
        settings,
        tmp_path / ("video." + preset.container),
        (frame for _ in range(settings.frames)),
        lambda n: None,
        audio_path=wav,
    )
    assert check_probe(info, preset, settings)["passed"]
    assert info["audio_verification"]["passed"]


@pytest.mark.gpu
def test_sequence_delivery_freezes_and_exports_audio(audio_project):
    service, project, asset = audio_project
    service.set_project_audio(project, "0", AudioTimeline(clips=[AudioClip(asset_id=asset["id"])]))
    scene = service.create_scene(project)["record"]["id"]
    # This test runs the real worker, so submission must freeze the real adapter
    # identity too; a fabricated submission backend would correctly be rejected.
    job = service.submit_delivery(
        scene, RenderSettings(width=16, height=16, frames=2, samples=1), ["exr-16-zip"], "audio-job"
    )["job"]
    snapshot = service.store.get("snapshot", job["id"])
    latest = service.get_project_audio(project)
    service.set_project_audio(project, latest["revision_id"], AudioTimeline())
    assert snapshot["audio"]["assets"][asset["id"]]["sha256"] == asset["sha256"]
    run(service.store.root, job["id"])
    done = service.get_job(job["id"])
    assert done["job"]["status"] == "completed", done["job"]["error"]
    from pathlib import Path

    assert (Path(job["export_path"]) / "audio.wav").is_file()
    assert service.validate_delivery_output(job["id"])["audio"]["samples"] == samples(
        Fraction(2, 60)
    )


def test_synthesis_determinism_and_clipping(audio_project):
    service, project, _ = audio_project
    spec = AudioSynthesis(
        duration_seconds=1.2, events=[AudioEvent(waveform="noise", duration_seconds=1.2)] * 5
    )
    one = service.create_audio(project, "noise1", spec)["asset"]
    two = service.create_audio(project, "noise2", spec)["asset"]
    assert one["sha256"] == two["sha256"]


def test_mcp_audio_calls_use_shared_daemon(tmp_path, monkeypatch):
    import asyncio

    import httpx
    from fastapi.testclient import TestClient
    from mcp import Client

    from chromascript.api import create_app
    from chromascript.mcp_server import create_mcp

    web = create_app(tmp_path)
    with TestClient(web) as http:
        session = http.get("/api/session").json()
        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            "chromascript.mcp_server.endpoint",
            lambda root: {"url": "http://testserver", "token": session["token"]},
        )
        monkeypatch.setattr(
            "chromascript.mcp_server.httpx.AsyncClient",
            lambda **kw: real_client(transport=httpx.ASGITransport(app=web), **kw),
        )

        async def scenario():
            async with Client(create_mcp(tmp_path)) as mcp:
                project = await mcp.call_tool(
                    "chromascript.create_project", {"request": {"name": "MCP audio"}}
                )
                pid = project.structured_content["project"]["id"]
                created = await mcp.call_tool(
                    "chromascript.create_audio",
                    {
                        "request": {
                            "project_id": pid,
                            "name": "MCP beat",
                            "spec": {
                                "duration_seconds": 0.1,
                                "events": [{"duration_seconds": 0.1}],
                            },
                        }
                    },
                )
                assert not created.is_error
                asset = created.structured_content["asset"]
                saved = await mcp.call_tool(
                    "chromascript.set_project_audio",
                    {
                        "request": {
                            "project_id": pid,
                            "expected_revision_id": "0",
                            "timeline": {"clips": [{"asset_id": asset["id"]}]},
                        }
                    },
                )
                assert not saved.is_error
                assert (
                    web.state.service.get_project_audio(pid)["revision_id"]
                    == saved.structured_content["revision_id"]
                )
                wav = http.get("/api/artifacts/" + asset["id"], headers={"Range": "bytes=0-43"})
                assert wav.status_code == 206 and wav.content[:4] == b"RIFF"
                guidelines = await mcp.call_tool(
                    "chromascript.get_audio_guidelines", {"request": {"project_id": pid}}
                )
                assert not guidelines.is_error
                assert guidelines.structured_content["sample_rate"] == 48000
                assert guidelines.structured_content["managed_directory"] == str(tmp_path / "audio")
                doc = await mcp.read_resource("chromascript://docs/audio")
                assert "create_audio" in doc.contents[0].text

        asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("engine,video", [("three-float-v1", "none"), ("remotion-sdr-v1", "srgb")])
def test_motion_audio_bundles(audio_project, engine, video):
    from pathlib import Path

    from chromascript.motion import MotionOutput, blank_motion

    service, project, asset = audio_project
    service.set_project_audio(project, "0", AudioTimeline(clips=[AudioClip(asset_id=asset["id"])]))
    spec = blank_motion(engine).model_copy(update={"width": 32, "height": 32, "frames": 3})
    saved = service.create_motion_scene(project, spec=spec)
    output = MotionOutput(
        master_bit_depth=32 if engine == "three-float-v1" else None, video=video, render_scale=1
    )
    job = service.submit_motion_render(
        saved["record"]["id"], saved["revision_id"], engine, output=output
    )["job"]
    run(service.store.root, job["id"])
    result = service.get_job(job["id"])
    assert result["job"]["status"] == "completed", result["job"]["error"]
    assert result["job"]["export_completed"]
    assert (Path(job["export_path"]) / "audio.wav").is_file()
    audio = next(a for a in result["artifacts"] if a["kind"] == "audio")
    assert Path(service.get_artifact(audio["id"])["local_path"]).is_file()
    report = service.validate_delivery_output(job["id"])
    assert report["audio"]["samples"] == 4800
    if video != "none":
        assert report["probe"]["audio_verification"]["decoded_peak"] > 0.1


def test_import_records_source_precision_and_resampling(audio_project, tmp_path):
    service, project, _ = audio_project
    original = tmp_path / "mono44100.wav"
    with wave.open(str(original), "wb") as writer:
        writer.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        writer.writeframes(
            (np.sin(np.arange(4410) * 2 * np.pi * 1000 / 44100) * 16384).astype("<i2").tobytes()
        )
    asset = service.import_audio(project, "mono input", source_path=str(original))["asset"]
    source = asset["metadata"]["source_probe"]["streams"][0]
    assert (source["sample_rate"], source["channels"], source["codec_name"]) == (
        "44100",
        1,
        "pcm_s16le",
    )
    assert (asset["sample_rate"], asset["channels"], asset["samples"]) == (48000, 2, 4800)
    assert asset["metadata"]["probe"]["streams"][0]["codec_name"] == "pcm_s32le"


def test_saturation_and_audio_runtime_recovery(audio_project, tmp_path, monkeypatch):
    service, project, asset = audio_project
    saturated = service.create_audio(
        project,
        "clipped",
        AudioSynthesis(
            duration_seconds=0.1,
            events=[AudioEvent(duration_seconds=0.1, gain_db=0, pan=-1, fade_seconds=0)] * 4,
        ),
    )["asset"]
    assert saturated["metadata"]["clipped_samples"] > 0
    data = pcm(service.store.file(saturated["relative_path"]))
    assert data.min() == -1 and data.max() == 1 - 2**-31
    service.set_project_audio(project, "0", AudioTimeline(clips=[AudioClip(asset_id=asset["id"])]))
    settings = RenderSettings()
    snapshot = freeze_audio(service.store, project, settings)
    monkeypatch.setattr("chromascript.audio.audio_identity", lambda: "changed")
    with pytest.raises(ChromaError, match="implementation changed"):
        mix_audio(service.store, snapshot, settings, tmp_path / "refuse.wav")
    assert not (tmp_path / "refuse.wav").exists()


@pytest.mark.parametrize(
    "extension,codec",
    [
        ("mp3", "libmp3lame"),
        ("flac", "flac"),
        ("aiff", "pcm_s16be"),
        ("ogg", "libvorbis"),
        ("m4a", "aac"),
    ],
)
def test_import_actual_common_audio_formats(audio_project, tmp_path, extension, codec):
    import subprocess

    service, project, asset = audio_project
    source = tmp_path / ("input." + extension)
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(service.store.file(asset["relative_path"])),
            "-c:a",
            codec,
            str(source),
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    imported = service.import_audio(project, extension, source_path=str(source))["asset"]
    assert abs(imported["duration_seconds"] - 1) < 0.05
    assert imported["metadata"]["source_probe"]["streams"][0]["codec_type"] == "audio"
    assert np.max(np.abs(pcm(service.store.file(imported["relative_path"])))) > 0.1


def test_low_video_fps_does_not_certify_truncated_audio(audio_project, tmp_path):
    service, project, _ = audio_project
    asset = service.create_audio(project, "too short", AudioSynthesis(duration_seconds=0.1))[
        "asset"
    ]
    settings = RenderSettings(width=32, height=32, frames=1, fps=Rational(numerator=1))
    frame = np.ones((32, 32, 4), dtype=np.float32)
    with pytest.raises(ChromaError, match="audio properties"):
        encode(
            builtins()["h264-srgb-preview"],
            settings,
            tmp_path / "truncated.mp4",
            [frame],
            lambda n: None,
            audio_path=service.store.file(asset["relative_path"]),
        )


def test_project_audio_guidelines_paths_and_transport(audio_project, monkeypatch):
    service, project, asset = audio_project
    result = execute(service, "get_audio_guidelines", {"project_id": project})
    # Discovery is read-only and identifies the actual managed file convention.
    assert result["managed_directory"] == str(service.store.root / "audio")
    assert result["source_directory"] == str(service.store.root / "audio-sources" / project)
    assert not (service.store.root / "audio-sources").exists()
    assert service.get_artifact(asset["id"])["local_path"] == str(
        service.store.root / "audio" / f"{asset['id']}.wav"
    )
    assert "1.001" in result["documentation"]
    assert "COMPLETE clips array" in result["documentation"]
    assert "Web live WGSL and animation previews" in result["documentation"]
    monkeypatch.setattr("chromascript.cli.call", lambda root, op, args: execute(service, op, args))
    reply = CliRunner().invoke(
        app, ["call", "get_audio_guidelines", "--arguments", '{"project_id":"' + project + '"}']
    )
    assert reply.exit_code == 0 and "source_directory" in reply.stdout
    with pytest.raises(ChromaError, match="does not exist"):
        execute(service, "get_audio_guidelines", {"project_id": "missing"})
