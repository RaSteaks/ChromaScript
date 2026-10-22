"""Bounded local SoundFont inspection and deterministic score-to-PCM production."""

from __future__ import annotations

import hashlib
import math
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any, BinaryIO, Iterator

import numpy as np

from .audio import (
    BLOCK,
    CHANNELS,
    RATE,
    MusicScore,
    measure_loudness,
    pcm_bytes,
    pcm_writer,
    samples,
)
from .encoding import probe
from .errors import ChromaError
from .images import file_hash
from .store import canonical

MAX_SOUNDFONT_BYTES = 512 * 1024**2
RENDER_TIMEOUT = 300


def run_process(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ChromaError("music_timeout", "Music processing timed out; shorten the score") from exc
    except OSError as exc:
        raise ChromaError("music_runtime", "Unable to start the local music runtime") from exc
    if result.returncode:
        raise ChromaError(
            "music_process", "Local music processing failed", diagnostic=result.stderr[-2000:]
        )
    return result


def music_environment() -> dict[str, Any]:
    executable = shutil.which("fluidsynth")
    version, reason = None, None
    if executable:
        try:
            version = run_process([executable, "--version"], 5).stdout.strip()
            if not version:
                reason = "FluidSynth returned no version information"
        except ChromaError as exc:
            reason = str(exc)
    else:
        reason = "Install FluidSynth locally and make fluidsynth available on PATH"
    return {
        "available": executable is not None and version is not None and reason is None,
        "executable": executable,
        "version": version,
        "unavailable_reason": reason,
        "soundfont_formats": ["SF2"],
        "max_soundfont_bytes": MAX_SOUNDFONT_BYTES,
        "max_tracks": 15,
        "max_notes_per_track": 4096,
        "max_notes": 16384,
        "max_duration_seconds": 600,
        "render_timeout_seconds": RENDER_TIMEOUT,
        "inspect_operation": "chromascript.inspect_soundfont",
        "create_operation": "chromascript.create_music",
        "soundfont_required": "Absolute local file; no bundled/downloaded instruments",
        "constraints": ["theme", "instrumentation", "hit_points", "reading_windows"],
        "timing": "Score boundaries use 48000 ticks/second; FluidSynth playback has internal timer/block quantization. Hit checks validate score onsets, not acoustic attacks.",
        "analysis": "Sample peaks/saturation counts plus FFmpeg ebur128 integrated LUFS, LRA and true peak on the final WAV; no normalization",
        "effects": "Optional score.reverb/chorus objects enable global effects; null disables. Track reverb_send/chorus_send set CC91/CC93 (0..127); omitted sends retain native defaults.",
        "sustain_pedal": "Track sustain_pedal intervals emit CC64; non-overlapping and leave max(0.02, fade_out_seconds) seconds after pedal-up for release.",
    }


def snapshot_soundfont(source_path: str, destination: Path) -> dict[str, Any]:
    original = Path(source_path).expanduser()
    if not original.is_absolute() or not original.is_file():
        raise ChromaError("soundfont_input", "Supply an absolute local SoundFont file")
    if not 12 <= original.stat().st_size <= MAX_SOUNDFONT_BYTES:
        raise ChromaError("soundfont_input", "SoundFont must be between 12 bytes and 512 MiB")
    digest = hashlib.sha256()
    size = 0
    # Hash the exact private snapshot passed to FluidSynth, even if the source is
    # changed during production. Bound the streaming copy if the source grows.
    try:
        with original.open("rb") as src, destination.open("wb") as dst:
            while block := src.read(1024**2):
                size += len(block)
                if size > MAX_SOUNDFONT_BYTES:
                    raise ChromaError("soundfont_input", "SoundFont exceeds 512 MiB")
                digest.update(block)
                dst.write(block)
    except OSError as exc:
        raise ChromaError("soundfont_input", "Unable to snapshot the local SoundFont") from exc
    return {
        "source_path": str(original.resolve()),
        "sha256": digest.hexdigest(),
        "size_bytes": size,
    }


def chunks(reader: BinaryIO, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    position = start
    while position < end:
        reader.seek(position)
        header = reader.read(8)
        if len(header) != 8:
            raise ChromaError("soundfont_format", "Truncated RIFF chunk")
        tag, size = struct.unpack("<4sI", header)
        next_position = position + 8 + size + size % 2
        if next_position > end:
            raise ChromaError("soundfont_format", "RIFF chunk extends outside its container")
        yield tag, position + 8, size
        position = next_position


def soundfont_headers(path: Path) -> dict[str, Any]:
    with path.open("rb") as reader:
        header = reader.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"sfbk":
            raise ChromaError(
                "soundfont_format", "Expected a RIFF SoundFont, regardless of filename"
            )
        end = struct.unpack("<I", header[4:8])[0] + 8
        if end != path.stat().st_size:
            raise ChromaError("soundfont_format", "SoundFont RIFF size does not match the file")
        version = None
        presets: list[dict[str, Any]] = []
        for tag, offset, size in chunks(reader, 12, end):
            if tag != b"LIST" or size < 4:
                continue
            reader.seek(offset)
            group = reader.read(4)
            for subtag, suboffset, subsize in chunks(reader, offset + 4, offset + size):
                if group == b"INFO" and subtag == b"ifil":
                    reader.seek(suboffset)
                    if subsize != 4 or version is not None:
                        raise ChromaError("soundfont_format", "Invalid SoundFont version chunk")
                    version = struct.unpack("<HH", reader.read(4))
                if group == b"pdta" and subtag == b"phdr":
                    if presets or subsize < 76 or subsize % 38 or subsize > 38 * 16385:
                        raise ChromaError(
                            "soundfont_format", "Invalid or excessive SoundFont preset headers"
                        )
                    reader.seek(suboffset)
                    records = list(struct.iter_unpack("<20sHHHIII", reader.read(subsize)))
                    if records[-1][0].split(b"\0")[0] != b"EOP":
                        raise ChromaError("soundfont_format", "Missing SoundFont preset terminator")
                    for name, program, bank, *_ in records[:-1]:
                        if not 0 <= program <= 127 or not 0 <= bank <= 16383:
                            raise ChromaError(
                                "soundfont_format", "Preset bank/program is outside the MIDI range"
                            )
                        presets.append(
                            {
                                "name": name.split(b"\0")[0].decode("utf-8", errors="replace"),
                                "bank": bank,
                                "program": program,
                            }
                        )
        if version is None or version[0] != 2 or not presets:
            raise ChromaError(
                "soundfont_format", "Expected SF2 version 2 with instrument preset headers"
            )
        if len({(p["bank"], p["program"]) for p in presets}) != len(presets):
            raise ChromaError("soundfont_format", "Duplicate preset bank/program pairs")
        return {
            "format": "SF2",
            "version": f"{version[0]}.{version[1]}",
            "presets": presets,
            "inspection": "Preset headers only; names are supplied by the font, not certified instrument identities",
        }


def inspect_soundfont(source_path: str, root: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(dir=root, prefix="soundfont-inspect-") as temp:
        path = Path(temp) / "source.sf2"
        info = snapshot_soundfont(source_path, path)
        return {**info, **soundfont_headers(path)}


def variable_length(value: int) -> bytes:
    output = [value & 127]
    while value := value >> 7:
        output.insert(0, (value & 127) | 128)
    return bytes(output)


def score_midi(score: MusicScore) -> bytes:
    # At 500000 microseconds/quarter and PPQ=24000, one MIDI tick equals a
    # 48 kHz sample. FluidSynth's internal player may still quantize dispatch.
    events = [(0, -2, b"\xff\x51\x03\x07\xa1\x20")]
    for index, track in enumerate(score.tracks):
        channel = index if index < 9 else index + 1  # Channel 10 is reserved for drums.
        for controller, value in (
            (0, track.bank >> 7),
            (32, track.bank & 127),
            (7, track.volume),
            (10, round(64 + track.pan * (64 if track.pan < 0 else 63))),
            (11, 127),
        ):
            events.append((0, -1, bytes([0xB0 | channel, controller, value])))
        events.append((0, -1, bytes([0xC0 | channel, track.program])))
        for controller, send in ((91, track.reverb_send), (93, track.chorus_send)):
            if send is not None:
                events.append((0, -1, bytes([0xB0 | channel, controller, send])))
        for pedal in track.sustain_pedal:
            down, up = pedal.bounds()
            events.append((down, 2, bytes([0xB0 | channel, 64, 127])))
            events.append((up, 1, bytes([0xB0 | channel, 64, 0])))
        for note in track.notes:
            start, end = note.bounds()
            events.append((start, 3, bytes([0x90 | channel, note.pitch, note.velocity])))
            events.append((end, 0, bytes([0x80 | channel, note.pitch, 0])))
    events.append((samples(score.duration_seconds), 4, b"\xff\x2f\x00"))
    track_data, previous = bytearray(), 0
    # Stable sort preserves controller bank selection order; releases precede
    # attacks at the same tick so adjacent unisons are not accidentally cut off.
    # Note-off -> pedal-up -> pedal-down -> note-on also handles touching pedal
    # intervals without leaving CC64 off or releasing a newly attacked note.
    for tick, _, event in sorted(events, key=lambda e: (e[0], e[1])):
        track_data.extend(variable_length(tick - previous) + event)
        previous = tick
    return (
        b"MThd"
        + struct.pack(">IHHH", 6, 0, 1, 24000)
        + b"MTrk"
        + struct.pack(">I", len(track_data))
        + track_data
    )


def finish_pcm(raw: Path, path: Path, score: MusicScore) -> dict[str, Any]:
    total, fade = samples(score.duration_seconds), samples(score.fade_out_seconds)
    if raw.stat().st_size != total * CHANNELS * 4:
        raise ChromaError(
            "music_duration", "FluidSynth output did not decode to the requested sample count"
        )
    peak, output_peak, clipped = 0.0, 0.0, 0
    with raw.open("rb") as reader, pcm_writer(path) as writer:
        for pos in range(0, total, BLOCK):
            block = (
                np.frombuffer(reader.read(min(BLOCK, total - pos) * 8), dtype="<f4")
                .reshape(-1, CHANNELS)
                .astype(np.float64)
            )
            if not np.isfinite(block).all():
                raise ChromaError("music_samples", "FluidSynth produced non-finite samples")
            block *= 10 ** (score.gain_db / 20)
            if fade:
                # Global sample indices make the fade continuous across block boundaries.
                envelope = np.clip(
                    (total - 1 - np.arange(pos, pos + len(block))) / max(1, fade - 1), 0, 1
                )
                block *= envelope[:, None]
            peak = max(peak, float(np.max(np.abs(block))))
            clipped += int(np.count_nonzero((block < -1) | (block >= 1)))
            encoded = pcm_bytes(block)
            output_peak = max(
                output_peak,
                float(
                    np.max(np.abs(np.frombuffer(encoded, dtype="<i4").astype(np.float64) / 2**31))
                ),
            )
            writer.writeframesraw(encoded)
    return {
        "samples": total,
        "duration_seconds": total / RATE,
        "sample_rate": RATE,
        "channels": CHANNELS,
        "sample_format": "pcm_s32le",
        "peak_before_saturation": peak,
        "peak": output_peak,
        "peak_dbfs": 20 * math.log10(output_peak) if output_peak else None,
        "clipped_samples": clipped,
        "clipping": clipped > 0,
        "clipping_unit": "channel samples after output gain/fade, before PCM saturation",
        "sha256": file_hash(path),
        "loudness": measure_loudness(path),
    }


def render_music(path: Path, source_path: str, score: MusicScore) -> dict[str, Any]:
    runtime = music_environment()
    if not runtime["available"]:
        raise ChromaError("fluidsynth_unavailable", runtime["unavailable_reason"])
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ChromaError("ffmpeg_unavailable", "FFmpeg is required to verify music output")
    # Three PCM intermediates plus a bounded SoundFont snapshot and release-tail allowance.
    needed = samples(score.duration_seconds) * 24 + MAX_SOUNDFONT_BYTES + 64 * 1024**2
    if shutil.disk_usage(path.parent).free < needed:
        raise ChromaError("disk_space", "Insufficient space for music production")
    font = path.parent / "source.sf2"
    info = {**snapshot_soundfont(source_path, font), **soundfont_headers(font)}
    presets = {(p["bank"], p["program"]): p for p in info["presets"]}
    selected = []
    for track in score.tracks:
        preset = presets.get((track.bank, track.program))
        if preset is None:
            raise ChromaError(
                "soundfont_preset",
                "Requested bank/program does not exist",
                track=track.name,
                bank=track.bank,
                program=track.program,
            )
        selected.append({"track_name": track.name, **preset})
    midi, floating, raw = (path.parent / name for name in ("score.mid", "fluid.wav", "decoded.f32"))
    midi.write_bytes(score_midi(score))
    config = path.parent / "empty.cfg"
    config.write_text("")  # Never execute the user's/system's FluidSynth startup configuration.
    options = [
        "-ni",
        "-q",
        "-f",
        str(config),
        "-R",
        "1" if score.reverb is not None else "0",
        "-C",
        "1" if score.chorus is not None else "0",
        "-g",
        "0.2",
        "-r",
        str(RATE),
        "-z",
        "64",
        "-T",
        "wav",
        "-O",
        "float",
        "-E",
        "little",
        "-o",
        "player.timing-source=sample",
        "-o",
        "synth.midi-bank-select=mma",
        "-o",
        "synth.cpu-cores=1",
        "-o",
        "synth.polyphony=256",
        "-o",
        "synth.min-note-length=0",
    ]
    # Only public typed settings reach the native command; record every effective
    # value in renderer.options so changed synth defaults do not hide parameters.
    for effect, settings in (("reverb", score.reverb), ("chorus", score.chorus)):
        if settings is not None:
            for key, value in settings.model_dump(exclude={"schema_version"}).items():
                options.extend(["-o", f"synth.{effect}.{key.replace('_', '-')}={value}"])
    result = run_process(
        [runtime["executable"], *options, "-F", str(floating), str(font), str(midi)], RENDER_TIMEOUT
    )
    # Some native load/voice failures only appear on stderr despite exit status 0.
    if "error:" in result.stderr.lower() or not floating.is_file():
        raise ChromaError(
            "music_render",
            "FluidSynth could not render the score",
            diagnostic=result.stderr[-2000:],
        )
    decoded = probe(floating)
    streams = [s for s in decoded["streams"] if s["codec_type"] == "audio"]
    if len(streams) != 1 or any(
        [
            streams[0].get("codec_name") != "pcm_f32le",
            int(streams[0].get("sample_rate", 0)) != RATE,
            streams[0].get("channels") != CHANNELS,
        ]
    ):
        raise ChromaError("music_format", "FluidSynth must produce actual 48 kHz stereo float PCM")
    # Decode/crop by samples, preserving values above full scale for honest clipping
    # analysis. Do not quantize to integer before applying the requested output gain.
    run_process(
        [
            ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            "wav",
            "-i",
            str(floating),
            "-map",
            "0:a:0",
            "-af",
            f"atrim=end_sample={samples(score.duration_seconds)}",
            "-c:a",
            "pcm_f32le",
            "-f",
            "f32le",
            str(raw),
        ],
        60,
    )
    report = finish_pcm(raw, path, score)
    return {
        **report,
        "score": score.model_dump(),
        "score_sha256": hashlib.sha256(canonical(score.model_dump()).encode()).hexdigest(),
        "soundfont": info,
        "selected_presets": selected,
        "renderer": {
            "name": "FluidSynth",
            "version": runtime["version"],
            "executable_sha256": file_hash(Path(runtime["executable"])),
            "options": [o if o != str(config) else "<empty-config>" for o in options],
            "diagnostic": result.stderr[-2000:],
            "probe": decoded,
        },
        "implementation_sha256": hashlib.sha256(
            Path(__file__).read_bytes() + Path(__file__).with_name("audio.py").read_bytes()
        ).hexdigest(),
        "constraint_checks": {
            "hit_points": score.hit_report(),
            "reading_windows": "passed (key-down intervals only; pedal sustain, release and effect tails excluded)",
            "instrumentation": "passed (track names and actual preset existence)",
            "theme": "agent guidance; not semantically verified",
        },
        "timing": runtime["timing"],
        "release_policy": "Note durations are key-down; notes released with sustain pedal down remain held until pedal-up. Pedal-up reserves max(0.02, fade_out_seconds) seconds, but the SoundFont/effects can need more. Sustain, release and effect tails play only until score end, then crop/fade.",
        "reproducibility": "Retain the exact SoundFont and score. Bit identity across FluidSynth builds/platforms is not guaranteed.",
    }
