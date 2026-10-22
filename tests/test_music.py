"""Real offline SoundFont rendering, score constraints and shared transport contracts."""

import asyncio
import copy
import hashlib
import json
import shutil
import struct
import subprocess
import wave
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from pydantic import ValidationError
from typer.testing import CliRunner

from chromascript.api import create_app
from chromascript.audio import AudioClip, AudioTimeline, MusicScore, freeze_audio, mix_audio
from chromascript.cli import app
from chromascript.errors import ChromaError
from chromascript.mcp_server import create_mcp
from chromascript.models import Rational, RenderSettings
from chromascript.music import finish_pcm, music_environment, score_midi
from chromascript.operations import catalogue, execute
from chromascript.service import Service


def make_soundfont(path):
    """Generate a tiny original looping sine sample; no licensed instrument downloads."""

    def chunk(tag, payload):
        return tag + struct.pack("<I", len(payload)) + payload + b"\0" * (len(payload) % 2)

    def name(value):
        return value.encode().ljust(20, b"\0")

    # Four named test presets plus a nonzero bank exercise actual preset routing.
    # They intentionally share a synthetic sample, not claimed acoustic instruments.
    presets = [
        ("Test Piano", 0, 0),
        ("Test Violin", 0, 40),
        ("Test Viola", 0, 41),
        ("Test Cello", 0, 42),
        ("Test Bank", 129, 7),
    ]
    count = 4800
    pcm = (np.sin(np.arange(count) * 2 * np.pi * 440 / 48000) * 20000).astype("<i2")
    info = chunk(
        b"LIST",
        b"INFO"
        + chunk(b"ifil", struct.pack("<HH", 2, 1))
        + chunk(b"isng", b"EMU8000\0")
        + chunk(b"INAM", b"ChromaScript test\0"),
    )
    data = chunk(b"LIST", b"sdta" + chunk(b"smpl", pcm.tobytes() + bytes(92)))
    phdr = b"".join(
        struct.pack("<20sHHHIII", name(n), p, b, i, 0, 0, 0) for i, (n, b, p) in enumerate(presets)
    )
    phdr += struct.pack("<20sHHHIII", name("EOP"), 0, 0, len(presets), 0, 0, 0)
    pbag = b"".join(struct.pack("<HH", i, 0) for i in range(len(presets) + 1))
    pgen = struct.pack("<HH", 41, 0) * len(presets) + bytes(4)
    inst = struct.pack("<20sH20sH", name("Synthetic test"), 0, name("EOI"), 1)
    ibag = struct.pack("<HHHH", 0, 0, 3, 0)
    # Loop until note-off, then a short natural release to verify tail retention.
    igen = struct.pack("<HhHHHH", 38, -2400, 54, 1, 53, 0) + bytes(4)
    shdr = struct.pack("<20sIIIIIBbHH", name("sine"), 0, count, 0, count, 48000, 69, 0, 0, 1)
    shdr += struct.pack(
        "<20sIIIIIBbHH",
        name("EOS"),
        count + 46,
        count + 46,
        count + 46,
        count + 46,
        48000,
        0,
        0,
        0,
        1,
    )
    pdta = chunk(
        b"LIST",
        b"pdta"
        + b"".join(
            chunk(tag, value)
            for tag, value in [
                (b"phdr", phdr),
                (b"pbag", pbag),
                (b"pmod", bytes(10)),
                (b"pgen", pgen),
                (b"inst", inst),
                (b"ibag", ibag),
                (b"imod", bytes(10)),
                (b"igen", igen),
                (b"shdr", shdr),
            ]
        ),
    )
    path.write_bytes(chunk(b"RIFF", b"sfbk" + info + data + pdta))
    return path


@pytest.fixture
def font(tmp_path):
    return make_soundfont(tmp_path / "test.sf2")


@pytest.fixture
def project(tmp_path):
    service = Service(tmp_path / "workspace")
    return service, service.create_project("Music")["project"]["id"]


def score_data():
    return {
        "duration_seconds": 1.25,
        "fade_out_seconds": 0.1,
        "tracks": [
            {
                "name": "piano",
                "program": 0,
                "pan": -1,
                "notes": [
                    {"pitch": 69, "start_seconds": 0.1, "duration_seconds": 0.5, "velocity": 60},
                ],
            }
        ],
        "constraints": {
            "theme": "Quiet explanation",
            "instrumentation": ["piano"],
            "hit_points": [{"time_seconds": 0.1, "tolerance_seconds": 0, "track_name": "piano"}],
            "reading_windows": [
                {
                    "start_seconds": 0.2,
                    "end_seconds": 0.5,
                    "max_velocity": 64,
                    "allow_note_onsets": False,
                }
            ],
        },
    }


def expressive_score_data():
    # Use the same score across transports so new nested fields cannot disappear
    # at the MCP/HTTP/CLI boundary while local rendering still happens to work.
    data = score_data()
    data.update(reverb={"level": 0.3}, chorus={"nr": 2, "speed": 0.5})
    data["tracks"][0].update(
        reverb_send=70, chorus_send=40,
        sustain_pedal=[{"down_seconds": 0.1, "up_seconds": 0.95}],
    )
    return data


def pcm(path):
    with wave.open(str(path), "rb") as reader:
        assert (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) == (
            48000,
            2,
            4,
        )
        return (
            np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i4").reshape(-1, 2)
            / 2**31
        )


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(extra=True),
        lambda d: d.update(duration_seconds=float("nan")),
        lambda d: d.update(fade_out_seconds=2),
        lambda d: d["tracks"][0].update(program=128),
        lambda d: d["tracks"][0].update(volume="100"),
        lambda d: d["tracks"][0]["notes"][0].update(pitch=True),
        lambda d: d["tracks"][0]["notes"][0].update(velocity=0),
        lambda d: d["tracks"][0]["notes"][0].update(duration_seconds=2),
        lambda d: d["tracks"][0]["notes"].append(copy.deepcopy(d["tracks"][0]["notes"][0])),
        lambda d: d["tracks"].append(copy.deepcopy(d["tracks"][0])),
        lambda d: d["constraints"].update(instrumentation=["cello"]),
        lambda d: d["constraints"]["hit_points"][0].update(time_seconds=1.25),
        lambda d: d["constraints"]["hit_points"][0].update(time_seconds=0.8),
        lambda d: d["constraints"]["hit_points"][0].update(track_name="missing"),
        lambda d: d["tracks"][0].update(volume=0),
        lambda d: d["constraints"]["reading_windows"][0].update(end_seconds=2),
        lambda d: d["constraints"]["reading_windows"][0].update(end_seconds=0.1),
        lambda d: d["constraints"]["reading_windows"][0].update(max_velocity=30),
        lambda d: d["constraints"]["reading_windows"][0].update(start_seconds=0.1),
    ],
)
def test_invalid_scores(change):
    data = score_data()
    change(data)
    with pytest.raises(ValidationError):
        MusicScore.model_validate(data)


def test_limits_and_adjacent_notes():
    data = score_data()
    data["constraints"] = {}
    data["tracks"][0]["notes"].append({"pitch": 69, "start_seconds": 0.6, "duration_seconds": 0.3})
    assert MusicScore.model_validate(data).tracks[0].notes[0].bounds() == (4800, 28800)
    data["tracks"] *= 16
    with pytest.raises(ValidationError):
        MusicScore.model_validate(data)
    data = score_data()
    data["tracks"][0]["notes"] *= 4097
    with pytest.raises(ValidationError):
        MusicScore.model_validate(data)


def test_inspect_headers_without_fluidsynth(project, font, monkeypatch, tmp_path):
    service, _ = project
    monkeypatch.setattr("chromascript.music.shutil.which", lambda name: None)
    info = execute(service, "inspect_soundfont", {"soundfont_path": str(font)})
    assert info["sha256"] == hashlib.sha256(font.read_bytes()).hexdigest()
    assert info["presets"][-1] == {"name": "Test Bank", "bank": 129, "program": 7}
    assert len(info["presets"]) == 5
    assert not music_environment()["available"]
    assert not list(service.store.root.glob("soundfont-inspect-*"))
    for content in [b"not a font", font.read_bytes()[:-1], b"RIFF\xff\xff\xff\xffsfbk"]:
        bad = tmp_path / "bad.sf2"
        bad.write_bytes(content)
        with pytest.raises(ChromaError):
            service.inspect_soundfont(str(bad))
    with pytest.raises(ChromaError):
        service.inspect_soundfont("relative.sf2")


def test_unavailable_and_failed_render_are_atomic(project, font, monkeypatch):
    service, pid = project
    monkeypatch.setattr(
        "chromascript.music.music_environment",
        lambda: {
            "available": False,
            "unavailable_reason": "not installed",
        },
    )
    with pytest.raises(ChromaError, match="not installed"):
        service.create_music(pid, "music", str(font), MusicScore.model_validate(score_data()))
    assert service.store.list("audio_asset") == []
    assert service.store.list("artifact") == []
    assert not list(service.store.root.glob("music-create-*"))


def test_pcm_peak_clipping_gain_and_fade(tmp_path):
    data = score_data()
    data.update(duration_seconds=1.25, gain_db=6, fade_out_seconds=0.25)
    raw = tmp_path / "float.raw"
    np.full((60000, 2), 0.75, dtype="<f4").tofile(raw)
    output = tmp_path / "output.wav"
    report = finish_pcm(raw, output, MusicScore.model_validate(data))
    actual = pcm(output)
    assert report["clipping"] and report["clipped_samples"] > 96000
    assert report["peak_before_saturation"] == pytest.approx(0.75 * 10**0.3)
    assert report["peak"] == np.max(actual)
    assert actual[-1].tolist() == [0, 0]
    assert np.isfinite(actual).all()
    raw.write_bytes(bytes(8))
    with pytest.raises(ChromaError, match="sample count"):
        finish_pcm(raw, output, MusicScore.model_validate(data))


@pytest.mark.skipif(not shutil.which("fluidsynth"), reason="FluidSynth is optional")
def test_real_four_part_render_report_and_existing_timeline(project, font):
    service, pid = project
    data = score_data()
    for name, program, pitch in [("violin", 40, 72), ("viola", 41, 67), ("cello", 42, 48)]:
        data["tracks"].append(
            {
                "name": name,
                "program": program,
                "pan": 1,
                "notes": [
                    {"pitch": pitch, "start_seconds": 0.7, "duration_seconds": 0.2, "velocity": 70},
                ],
            }
        )
    data["constraints"]["instrumentation"] = ["piano", "violin", "viola", "cello"]
    result = execute(
        service,
        "create_music",
        {
            "project_id": pid,
            "name": "Four parts",
            "soundfont_path": str(font),
            "score": data,
        },
    )
    asset, report = result["asset"], result["generation_report"]
    actual = pcm(service.store.file(asset["relative_path"]))
    assert actual.shape == (60000, 2)
    assert not np.any(actual[:4600])
    assert np.max(np.abs(actual[5500:20000, 0])) > 0.001
    assert np.max(np.abs(actual[5500:20000, 1])) < 0.0001
    assert np.max(np.abs(actual[35000:40000, 0])) > 0  # SoundFont release tail retained.
    assert np.max(np.abs(actual[36000:, 1])) > 0.001
    assert report["peak"] == float(np.max(np.abs(actual)))
    assert report["sha256"] == asset["sha256"]
    assert report["loudness"]["integrated_lufs"] is not None
    assert report["loudness"]["true_peak_dbtp"] is not None
    assert report["loudness"] == asset["metadata"]["loudness"]
    assert report["soundfont"]["sha256"] == hashlib.sha256(font.read_bytes()).hexdigest()
    assert report["constraint_checks"]["hit_points"][0]["matched"]
    assert report["renderer"]["probe"]["streams"][0]["codec_name"] == "pcm_f32le"
    assert service.store.get("audio_asset", asset["id"])["metadata"]["generation_report"] == report
    again = service.create_music(pid, "Repeat", str(font), MusicScore.model_validate(data))
    assert again["asset"]["sha256"] == asset["sha256"]
    assert service.get_project_audio(pid)["timeline"]["clips"] == []
    service.set_project_audio(pid, "0", AudioTimeline(clips=[AudioClip(asset_id=asset["id"])]))
    settings = RenderSettings(frames=30, fps=Rational(numerator=24))
    frozen = freeze_audio(service.store, pid, settings)
    mix = service.store.root / "mix.wav"
    mix_audio(service.store, frozen, settings, mix)
    np.testing.assert_array_equal(pcm(mix), actual)
    assert service.get_artifact(asset["id"])["local_path"].endswith(".wav")
    assert not list(service.store.root.glob("music-create-*"))


@pytest.mark.skipif(not shutil.which("fluidsynth"), reason="FluidSynth is optional")
def test_nonzero_bank_and_missing_preset(project, font):
    service, pid = project
    data = score_data()
    data["tracks"][0].update(bank=129, program=7)
    result = service.create_music(pid, "Bank", str(font), MusicScore.model_validate(data))
    assert result["generation_report"]["peak"] > 0.001
    data["tracks"][0]["program"] = 8
    with pytest.raises(ChromaError, match="does not exist"):
        service.create_music(pid, "Bad bank", str(font), MusicScore.model_validate(data))
    assert len(service.store.list("audio_asset")) == 1


def test_midi_channel_reservation_and_releases():
    data = score_data()
    data["constraints"] = {}
    data["tracks"] = [{**copy.deepcopy(data["tracks"][0]), "name": str(i)} for i in range(15)]
    encoded = score_midi(MusicScore.model_validate(data))
    assert struct.unpack(">H", encoded[12:14])[0] == 24000
    assert bytes([0x99, 69, 60]) not in encoded
    assert bytes([0x9F, 69, 60]) in encoded


def test_timeout_and_discovery_failure(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("fluidsynth", 5)

    monkeypatch.setattr("chromascript.music.shutil.which", lambda _: "/fluidsynth")
    monkeypatch.setattr("chromascript.music.subprocess.run", timeout)
    assert music_environment()["available"] is False


@pytest.mark.skipif(not shutil.which("fluidsynth"), reason="FluidSynth is optional")
def test_cli_mcp_http_parity(tmp_path, font, monkeypatch):
    web = create_app(tmp_path / "workspace")
    service = web.state.service
    with TestClient(web, base_url="http://127.0.0.1:8765") as http:
        token = http.get("/api/session").json()["token"]
        headers = {"Authorization": "Bearer " + token}
        pid = service.create_project("Transports")["project"]["id"]
        monkeypatch.setattr(
            "chromascript.mcp_server.endpoint",
            lambda _: {"url": "http://127.0.0.1:8765", "token": token},
        )
        original_client = httpx.AsyncClient
        monkeypatch.setattr(
            "chromascript.mcp_server.httpx.AsyncClient",
            lambda **kw: original_client(transport=httpx.ASGITransport(app=web), **kw),
        )

        async def check():
            async with Client(create_mcp(service.store.root)) as client:
                font_reply = await client.call_tool(
                    "chromascript.inspect_soundfont", {"request": {"soundfont_path": str(font)}}
                )
                assert not font_reply.is_error
                reply = await client.call_tool(
                    "chromascript.create_music",
                    {
                        "request": {
                            "project_id": pid,
                            "name": "MCP Music",
                            "soundfont_path": str(font),
                            "score": expressive_score_data(),
                        }
                    },
                )
                assert not reply.is_error
                aid = reply.structured_content["asset"]["id"]
                assert service.store.get("audio_asset", aid)["name"] == "MCP Music"
                report = reply.structured_content["generation_report"]
                assert report["score"]["reverb"]["level"] == 0.3
                assert report["score"]["tracks"][0]["sustain_pedal"][0]["up_seconds"] == 0.95
                assert report["loudness"]["true_peak_dbtp"] is not None
                docs = await client.read_resource("chromascript://docs/audio")
                assert "reading_windows" in docs.contents[0].text

        asyncio.run(check())
        invalid = http.post(
            "/api/operations/create_music",
            headers=headers,
            json={
                "project_id": pid,
                "name": "invalid",
                "soundfont_path": str(font),
                "score": {**score_data(), "unexpected": True},
            },
        )
        assert invalid.status_code == 422
        monkeypatch.setattr(
            "chromascript.cli.call", lambda root, op, args: execute(service, op, args)
        )
        for operation, arguments in [
            ("inspect_soundfont", {"soundfont_path": str(font)}),
            (
                "create_music",
                {
                    "project_id": pid,
                    "name": "CLI Music",
                    "soundfont_path": str(font),
                    "score": expressive_score_data(),
                },
            ),
        ]:
            result = CliRunner().invoke(
                app, ["call", operation, "--arguments", json.dumps(arguments)]
            )
            assert result.exit_code == 0, result.output
            if operation == "create_music":
                report = json.loads(result.output)["generation_report"]
                assert report["score"]["chorus"]["nr"] == 2
                assert report["loudness"]["integrated_lufs"] is not None
        service._renderer = SimpleNamespace(info={})
        assert (
            service.get_capabilities()["audio"]["music"]
            == service.get_audio_guidelines(pid)["music"]
        )


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(reverb={"room_size": 1.01}),
        lambda d: d.update(reverb={"damp": -0.01}),
        lambda d: d.update(reverb={"width": 101}),
        lambda d: d.update(reverb={"level": float("nan")}),
        lambda d: d.update(reverb={"unknown": 1}),
        lambda d: d.update(chorus={"nr": True}),
        lambda d: d.update(chorus={"nr": "3"}),
        lambda d: d.update(chorus={"nr": 100}),
        lambda d: d.update(chorus={"speed": 0.09}),
        lambda d: d.update(chorus={"depth": 257}),
        lambda d: d.update(chorus={"level": 11}),
        lambda d: d["tracks"][0].update(reverb_send=128),
        lambda d: d["tracks"][0].update(chorus_send=True),
        lambda d: d["tracks"][0].update(sustain_pedal=[{"down_seconds": 0.2, "up_seconds": 0.1}]),
        lambda d: d["tracks"][0].update(sustain_pedal=[{"down_seconds": 0.1, "up_seconds": 0.100001}]),
        lambda d: d["tracks"][0].update(sustain_pedal=[{"down_seconds": 0, "up_seconds": 1.25}]),
        lambda d: d["tracks"][0].update(sustain_pedal=[{"down_seconds": 0, "up_seconds": 1.2}]),
        lambda d: d["tracks"][0].update(sustain_pedal=[
            {"down_seconds": 0.2, "up_seconds": 0.7}, {"down_seconds": 0, "up_seconds": 0.3},
        ]),
    ],
)
def test_invalid_effects_and_pedals(change):
    data = score_data()
    change(data)
    with pytest.raises(ValidationError):
        MusicScore.model_validate(data)


def midi_events(encoded):
    # Decode actual MIDI delta times/status bytes to assert simultaneous-event
    # semantics, including multi-byte deltas, rather than searching raw substrings.
    pos, tick, events = 22, 0, []

    def variable():
        nonlocal pos
        value = 0
        while True:
            byte = encoded[pos]
            pos += 1
            value = (value << 7) | (byte & 127)
            if byte < 128:
                return value

    while pos < len(encoded):
        tick += variable()
        status = encoded[pos]
        pos += 1
        if status == 255:
            pos += 1
            length = variable()
            pos += length
        else:
            length = 1 if status & 0xF0 == 0xC0 else 2
            events.append((tick, bytes([status]) + encoded[pos:pos + length]))
            pos += length
    return events


def test_pedal_same_tick_order_adjacent_unisons_and_channels():
    data = score_data()
    data["constraints"] = {}
    track = data["tracks"][0]
    track.update(reverb_send=0, chorus_send=127, sustain_pedal=[
        {"down_seconds": 0.6, "up_seconds": 1.15},
        {"down_seconds": 0.1, "up_seconds": 0.6},
    ])
    track["notes"].append({"pitch": 69, "start_seconds": 0.6, "duration_seconds": 0.3})
    data["tracks"] = [{**copy.deepcopy(track), "name": str(i)} for i in range(15)]
    events = midi_events(score_midi(MusicScore.model_validate(data)))
    for channel in [*range(9), *range(10, 16)]:
        assert (0, bytes([0xB0 | channel, 91, 0])) in events
        assert (0, bytes([0xB0 | channel, 93, 127])) in events
        same_tick = [event for tick, event in events if tick == 28800 and event[0] & 15 == channel]
        assert same_tick == [
            bytes([0x80 | channel, 69, 0]), bytes([0xB0 | channel, 64, 0]),
            bytes([0xB0 | channel, 64, 127]), bytes([0x90 | channel, 69, 72]),
        ]
    assert not any(event[0] & 15 == 9 for _, event in events)
    # Key-down overlap remains forbidden even when the whole phrase is pedalled.
    data["tracks"][0]["notes"][1]["start_seconds"] = 0.59
    with pytest.raises(ValidationError, match="Same-pitch"):
        MusicScore.model_validate(data)


@pytest.mark.skipif(not shutil.which("fluidsynth"), reason="FluidSynth is optional")
@pytest.mark.parametrize("feature", ["reverb", "chorus", "sustain_pedal"])
def test_real_effect_and_pedal_audio(project, font, feature):
    service, pid = project
    data = score_data()
    data["tracks"][0].update(reverb_send=100, chorus_send=100)
    dry = service.create_music(pid, "dry", str(font), MusicScore.model_validate(data))
    if feature == "sustain_pedal":
        data["tracks"][0][feature] = [{"down_seconds": 0.1, "up_seconds": 0.95}]
    else:
        data[feature] = {}
    wet = service.create_music(pid, feature, str(font), MusicScore.model_validate(data))
    dry_pcm = pcm(service.store.file(dry["asset"]["relative_path"]))
    wet_pcm = pcm(service.store.file(wet["asset"]["relative_path"]))
    assert wet_pcm.shape == dry_pcm.shape == (60000, 2)
    assert np.max(np.abs(wet_pcm - dry_pcm)) > 0.0001
    assert np.isfinite(wet_pcm).all() and not np.any(wet_pcm[-1])
    if feature == "sustain_pedal":
        # The native synth holds the sample after note-off until the later CC64 up.
        assert np.mean(wet_pcm[40000:44000] ** 2) > 20 * np.mean(dry_pcm[40000:44000] ** 2)
    else:
        options = wet["generation_report"]["renderer"]["options"]
        flag = "-R" if feature == "reverb" else "-C"
        assert options[options.index(flag) + 1] == "1"
        assert any(option.startswith(f"synth.{feature}.") for option in options)
        data["tracks"][0][f"{feature}_send"] = 0
        zero = service.create_music(pid, "zero send", str(font), MusicScore.model_validate(data))
        np.testing.assert_array_equal(pcm(service.store.file(zero["asset"]["relative_path"])), dry_pcm)


def test_controls_in_shared_schema_and_example():
    from pathlib import Path

    operation = next(op for op in catalogue() if op["name"] == "chromascript.create_music")
    schema = operation["inputSchema"]
    assert {"reverb", "chorus"} <= schema["$defs"]["MusicScore"]["properties"].keys()
    assert {"sustain_pedal", "reverb_send", "chorus_send"} <= schema["$defs"]["MusicTrack"]["properties"].keys()
    example = json.loads((Path(__file__).parents[1] / "examples/music-score.json").read_text())
    MusicScore.model_validate(example["score"])
