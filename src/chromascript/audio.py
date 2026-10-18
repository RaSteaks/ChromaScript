"""Offline, immutable PCM assets and sample-accurate project audio timelines."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import wave
from contextlib import ExitStack
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import Field, model_validator

from .encoding import probe
from .errors import ChromaError
from .images import file_hash
from .models import Artifact, Model, RenderSettings
from .operations import operation
from .store import Store, canonical, identifier, now

RATE = 48_000
CHANNELS = 2
MAX_SECONDS = 600
MAX_INPUT_BYTES = 128 * 1024**2
BLOCK = 48_000


def samples(seconds: float | Fraction) -> int:
    # Convert decimal seconds or rational frame time once, to the nearest sample
    # (ties to even). Never accumulate rounded per-frame durations: that drifts.
    return round(Fraction(str(seconds)) * RATE)


class AudioEvent(Model):
    waveform: Literal["sine", "triangle", "noise"] = "sine"
    start_seconds: float = Field(default=0, ge=0, le=MAX_SECONDS)
    duration_seconds: float = Field(default=1, ge=0.001, le=MAX_SECONDS)
    frequency_hz: float = Field(default=440, ge=20, le=20_000)
    gain_db: float = Field(default=-18, ge=-96, le=0)
    pan: float = Field(default=0, ge=-1, le=1)
    fade_seconds: float = Field(default=0.005, ge=0, le=10)


class AudioSynthesis(Model):
    duration_seconds: float = Field(ge=0.001, le=MAX_SECONDS)
    events: list[AudioEvent] = Field(default_factory=list, max_length=256)
    seed: int = Field(default=42, ge=0, le=4_294_967_295, strict=True)

    @model_validator(mode="after")
    def event_bounds(self) -> AudioSynthesis:
        if any(
            samples(e.start_seconds) + samples(e.duration_seconds) > samples(self.duration_seconds)
            for e in self.events
        ):
            raise ValueError("Audio events must fit inside duration_seconds")
        return self


class MusicNote(Model):
    # Duration is key-down time. SoundFont release tails need room before the score ends.
    pitch: int = Field(ge=0, le=127, strict=True)
    start_seconds: float = Field(default=0, ge=0, le=MAX_SECONDS)
    duration_seconds: float = Field(ge=0.001, le=MAX_SECONDS)
    velocity: int = Field(default=72, ge=1, le=127, strict=True)

    def bounds(self) -> tuple[int, int]:
        # Round absolute boundaries once, including rational addition of decimal inputs.
        return samples(self.start_seconds), samples(
            Fraction(str(self.start_seconds)) + Fraction(str(self.duration_seconds))
        )


class MusicTrack(Model):
    name: str = Field(min_length=1, max_length=120, pattern=r"\S")
    bank: int = Field(default=0, ge=0, le=16383, strict=True)
    program: int = Field(ge=0, le=127, strict=True)
    volume: int = Field(default=100, ge=0, le=127, strict=True)
    pan: float = Field(default=0, ge=-1, le=1)
    notes: list[MusicNote] = Field(min_length=1, max_length=4096)


class MusicHit(Model):
    label: str = Field(default="", max_length=240)
    time_seconds: float = Field(ge=0, le=MAX_SECONDS)
    tolerance_seconds: float = Field(default=0.05, ge=0, le=1)
    track_name: str | None = Field(default=None, min_length=1, max_length=120)


class MusicReadingWindow(Model):
    label: str = Field(default="", max_length=240)
    start_seconds: float = Field(ge=0, le=MAX_SECONDS)
    end_seconds: float = Field(gt=0, le=MAX_SECONDS)
    max_velocity: int = Field(default=64, ge=1, le=127, strict=True)
    allow_note_onsets: bool = True


class MusicConstraints(Model):
    theme: str = Field(default="", max_length=4000)
    # Instrumentation references unique track names, never assumed GM instrument numbers.
    instrumentation: list[str] = Field(default_factory=list, max_length=15)
    hit_points: list[MusicHit] = Field(default_factory=list, max_length=256)
    reading_windows: list[MusicReadingWindow] = Field(default_factory=list, max_length=128)


class MusicScore(Model):
    duration_seconds: float = Field(ge=0.001, le=MAX_SECONDS)
    tracks: list[MusicTrack] = Field(min_length=1, max_length=15)
    gain_db: float = Field(default=0, ge=-96, le=12)
    fade_out_seconds: float = Field(default=0.02, ge=0, le=10)
    constraints: MusicConstraints = Field(default_factory=MusicConstraints)

    def hit_report(self) -> list[dict[str, Any]]:
        report = []
        for hit in self.constraints.hit_points:
            candidates = [
                (abs(note.bounds()[0] - samples(hit.time_seconds)), track.name, note.bounds()[0])
                for track in self.tracks
                if track.volume > 0 and hit.track_name in (None, track.name)
                for note in track.notes
            ]
            nearest = min(candidates) if candidates else None
            report.append({
                "label": hit.label,
                "target_seconds": hit.time_seconds,
                "track_name": nearest[1] if nearest else None,
                "note_on_seconds": nearest[2] / RATE if nearest else None,
                "error_seconds": nearest[0] / RATE if nearest else None,
                "matched": nearest is not None and nearest[0] <= samples(hit.tolerance_seconds),
            })
        return report

    @model_validator(mode="after")
    def score_bounds(self) -> MusicScore:
        total = samples(self.duration_seconds)
        names = [track.name for track in self.tracks]
        if len(set(names)) != len(names):
            raise ValueError("Music track names must be unique")
        if sum(len(track.notes) for track in self.tracks) > 16384:
            raise ValueError("A score supports at most 16384 notes")
        if self.fade_out_seconds > self.duration_seconds:
            raise ValueError("fade_out_seconds must fit inside duration_seconds")
        if not set(self.constraints.instrumentation) <= set(names):
            raise ValueError("Required instrumentation must reference existing track names")
        for window in self.constraints.reading_windows:
            if not 0 <= samples(window.start_seconds) < samples(window.end_seconds) <= total:
                raise ValueError("Reading windows must fit inside duration_seconds")
        for track in self.tracks:
            ends: dict[int, int] = {}
            for note in sorted(track.notes, key=lambda n: n.start_seconds):
                start, end = note.bounds()
                if end > total:
                    raise ValueError("Music notes must fit inside duration_seconds")
                # MIDI note-off cannot independently release overlapping unisons on one channel.
                if start < ends.get(note.pitch, -1):
                    raise ValueError("Same-pitch notes must not overlap within a track")
                ends[note.pitch] = end
                for window in self.constraints.reading_windows:
                    left, right = samples(window.start_seconds), samples(window.end_seconds)
                    if start < right and end > left and note.velocity > window.max_velocity:
                        raise ValueError("Note exceeds reading window max_velocity")
                    if not window.allow_note_onsets and left <= start < right:
                        raise ValueError("Note onset falls inside a protected reading window")
        if any(samples(hit.time_seconds) >= total for hit in self.constraints.hit_points):
            raise ValueError("Hit points must fall before the score ends")
        if not all(hit["matched"] for hit in self.hit_report()):
            raise ValueError("Every hit point needs a note onset within its tolerance on the requested track")
        return self


class AudioClip(Model):
    asset_id: str = Field(min_length=1, max_length=64)
    start_seconds: float = Field(default=0, ge=0, le=86_400)
    source_start_seconds: float = Field(default=0, ge=0, le=MAX_SECONDS)
    duration_seconds: float | None = Field(default=None, ge=0.001, le=MAX_SECONDS)
    gain_db: float = Field(default=0, ge=-96, le=12)


class AudioTimeline(Model):
    clips: list[AudioClip] = Field(default_factory=list, max_length=32)


def pcm_writer(path: Path) -> wave.Wave_write:
    writer = wave.open(str(path), "wb")
    writer.setparams((CHANNELS, 4, RATE, 0, "NONE", "not compressed"))
    return writer


def pcm_bytes(block: np.ndarray) -> bytes:
    # Explicit saturation avoids integer wraparound. Clipping is reported, never
    # disguised as loudness normalization; this is 32-bit integer PCM, not float.
    return np.rint(np.clip(block, -1, 1 - 2**-31) * 2**31).astype("<i4").tobytes()


def synthesize(path: Path, spec: AudioSynthesis) -> dict[str, Any]:
    rngs = [np.random.default_rng(spec.seed + i) for i in range(len(spec.events))]
    clipped = 0
    total = samples(spec.duration_seconds)
    with pcm_writer(path) as writer:
        for start in range(0, total, BLOCK):
            block = np.zeros((min(BLOCK, total - start), CHANNELS), dtype=np.float64)
            for event, rng in zip(spec.events, rngs, strict=True):
                offset, length = samples(event.start_seconds), samples(event.duration_seconds)
                left, right = max(start, offset), min(start + len(block), offset + length)
                if left >= right:
                    continue
                t = np.arange(left - offset, right - offset, dtype=np.float64)
                phase = t * event.frequency_hz / RATE
                if event.waveform == "noise":
                    signal = rng.uniform(-1, 1, len(t))
                elif event.waveform == "triangle":
                    signal = 2 / math.pi * np.arcsin(np.sin(2 * math.pi * phase))
                else:
                    signal = np.sin(2 * math.pi * phase)
                fade = min(samples(event.fade_seconds), length // 2)
                if fade:
                    signal *= np.minimum(1, np.minimum(t / fade, (length - 1 - t) / fade))
                # Equal-power pan; stereo duplication would add 3 dB at the centre.
                pan = (event.pan + 1) * math.pi / 4
                signal *= 10 ** (event.gain_db / 20)
                block[left - start : right - start] += signal[:, None] * [
                    math.cos(pan),
                    math.sin(pan),
                ]
            clipped += int(np.count_nonzero((block < -1) | (block >= 1)))
            writer.writeframesraw(pcm_bytes(block))
    return {"clipped_samples": clipped, "synthesis": spec.model_dump()}


def read_timeline(store: Store, project_id: str) -> dict[str, Any]:
    try:
        return store.get("audio_timeline", project_id)
    except ChromaError as exc:
        if exc.code != "not_found":
            raise
        return {
            "project_id": project_id,
            "revision_id": "0",
            "timeline": AudioTimeline().model_dump(),
        }


def freeze_audio(store: Store, project_id: str, settings: RenderSettings) -> dict[str, Any] | None:
    timeline = read_timeline(store, project_id)
    if not timeline["timeline"]["clips"]:
        return None
    audio_sample_window(settings)
    assets = {}
    for clip in timeline["timeline"]["clips"]:
        asset = store.get("audio_asset", clip["asset_id"])
        if asset["project_id"] != project_id:
            raise ChromaError("audio_project", "Audio asset belongs to a different project")
        assets[asset["id"]] = asset
    return {**timeline, "assets": assets, "implementation_sha256": audio_identity()}


def audio_identity() -> str:
    # Audio algorithm changes must not alter an interrupted delivery on resume.
    return hashlib.sha256(
        Path(__file__).read_bytes() + Path(__file__).with_name("encoding.py").read_bytes()
    ).hexdigest()


def audio_sample_window(settings: RenderSettings) -> tuple[int, int]:
    total = samples(Fraction(settings.frames * settings.fps.denominator, settings.fps.numerator))
    origin = samples(
        Fraction(settings.start_frame * settings.fps.denominator, settings.fps.numerator)
    )
    # WAV has a 32-bit length field. Fail before allocation rather than emit a
    # corrupt RIFF file for extremely long custom-rate animations.
    if not 1 <= total < (2**32 - 128) // 8:
        raise ChromaError("audio_duration", "Audio delivery must fit a PCM WAV (under 4 GiB)")
    return total, origin


def mix_audio(
    store: Store, frozen: dict[str, Any] | None, settings: RenderSettings, destination: Path
) -> dict[str, Any] | None:
    if frozen is None:
        return None
    if frozen.get("implementation_sha256") != audio_identity():
        raise ChromaError("audio_runtime_changed", "Audio implementation changed; submit a new job")
    total, origin = audio_sample_window(settings)
    if shutil.disk_usage(destination.parent).free < total * 8 + 1024**2:
        raise ChromaError("disk_space", "Insufficient space for the audio mix")
    timeline = AudioTimeline.model_validate(frozen["timeline"])
    partial = destination.with_suffix(".partial.wav")
    clipped = 0
    try:
        with ExitStack() as stack:
            readers = {}
            for aid, asset in frozen["assets"].items():
                path = store.file(asset["relative_path"])
                if not path.is_file() or file_hash(path) != asset["sha256"]:
                    raise ChromaError(
                        "audio_asset_changed", "Frozen audio is missing or changed", asset_id=aid
                    )
                readers[aid] = stack.enter_context(wave.open(str(path), "rb"))
            writer = stack.enter_context(pcm_writer(partial))
            for pos in range(0, total, BLOCK):
                block = np.zeros((min(BLOCK, total - pos), CHANNELS), dtype=np.float64)
                for clip in timeline.clips:
                    reader = readers[clip.asset_id]
                    source = samples(clip.source_start_seconds)
                    length = (
                        samples(clip.duration_seconds)
                        if clip.duration_seconds is not None
                        else reader.getnframes() - source
                    )
                    offset = samples(clip.start_seconds) - origin
                    left, right = max(pos, offset), min(pos + len(block), offset + length)
                    if left >= right:
                        continue
                    reader.setpos(source + left - offset)
                    data = np.frombuffer(reader.readframes(right - left), dtype="<i4").reshape(
                        -1, CHANNELS
                    )
                    block[left - pos : right - pos] += (
                        data.astype(np.float64) / 2**31 * 10 ** (clip.gain_db / 20)
                    )
                clipped += int(np.count_nonzero((block < -1) | (block >= 1)))
                writer.writeframesraw(pcm_bytes(block))
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
    return {
        "sample_rate": RATE,
        "channels": CHANNELS,
        "samples": total,
        "duration_seconds": total / RATE,
        "clipped_samples": clipped,
        "timeline_revision_id": frozen["revision_id"],
        "sha256": file_hash(destination),
        "probe": probe(destination),
    }


def run_audio_process(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ChromaError("audio_timeout", "音频解码超时，请缩短素材后重试。") from exc
    except OSError as exc:
        raise ChromaError("audio_runtime", "无法启动音频解码器，请检查 FFmpeg 安装。") from exc


class AudioOperations:
    """The registry exposes these same operations through HTTP, MCP and CLI call."""

    store: Store

    def _audio_project(self, project_id: str, writable: bool = False) -> None:
        project = self.store.get("project", project_id)
        if writable and project["archived"]:
            raise ChromaError("project_archived", "Restore the project before editing audio")

    def _save_audio(
        self, project_id: str, name: str, path: Path, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        if not name.strip() or len(name) > 120:
            raise ChromaError("invalid_name", "Audio name needs 1..120 characters")
        info = probe(path)
        stream = next(s for s in info["streams"] if s["codec_type"] == "audio")
        with wave.open(str(path), "rb") as reader:
            count = reader.getnframes()
            if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (
                RATE,
                CHANNELS,
                4,
            ):
                raise ChromaError("audio_format", "Expected 48 kHz stereo 32-bit PCM")
        if not 1 <= count <= RATE * MAX_SECONDS or stream["codec_name"] != "pcm_s32le":
            raise ChromaError("audio_duration", "Audio must contain 0–600 seconds of decodable PCM")
        aid = identifier()
        target = self.store.file(f"audio/{aid}.wav")
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)
        asset: dict[str, Any] = {
            "id": aid,
            "project_id": project_id,
            "name": name,
            "relative_path": str(target.relative_to(self.store.root)),
            "created_at": now(),
            "sample_rate": RATE,
            "channels": CHANNELS,
            "samples": count,
            "duration_seconds": count / RATE,
            "sha256": file_hash(target),
            "metadata": {**metadata, "probe": info},
        }
        artifact = Artifact(
            id=aid,
            project_id=project_id,
            job_id="",
            name=name,
            kind="audio-source",
            relative_path=asset["relative_path"],
            mime_type="audio/wav",
            size_bytes=target.stat().st_size,
            created_at=asset["created_at"],
            metadata=asset,
        )
        # Asset discovery and artifact playback become visible atomically.
        try:
            with self.store.connect() as connection:
                connection.executemany(
                    "INSERT INTO records VALUES (?,?,?)",
                    [
                        ("audio_asset", aid, canonical(asset)),
                        ("artifact", aid, canonical(artifact.model_dump())),
                    ],
                )
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return {"asset": asset, "artifact": artifact.model_dump()}

    @operation(
        "Import local audio (absolute source_path OR base64 data) as an immutable project asset. Decode to 48 kHz stereo PCM, at most 600 seconds / 128 MiB input. No URLs."
    )
    def import_audio(
        self,
        project_id: str,
        name: str,
        source_path: str | None = None,
        data_base64: str | None = None,
    ) -> dict[str, Any]:
        self._audio_project(project_id, True)
        if (source_path is None) == (data_base64 is None):
            raise ChromaError("audio_input", "Supply exactly one of source_path or data_base64")
        with tempfile.TemporaryDirectory(dir=self.store.root, prefix="audio-import-") as temp:
            source = Path(temp) / "input"
            if source_path is not None:
                original = Path(source_path).expanduser()
                if (
                    not original.is_absolute()
                    or not original.is_file()
                    or original.stat().st_size > MAX_INPUT_BYTES
                ):
                    raise ChromaError("audio_input", "Supply an absolute local file up to 128 MiB")
                shutil.copyfile(original, source)
            else:
                if len(data_base64 or "") > (MAX_INPUT_BYTES * 4 // 3 + 4):
                    raise ChromaError("audio_input", "Base64 input exceeds 128 MiB")
                try:
                    source.write_bytes(base64.b64decode(data_base64 or "", validate=True))
                except (ValueError, binascii.Error) as exc:
                    raise ChromaError("audio_input", "Invalid base64 audio") from exc
            if source.stat().st_size > MAX_INPUT_BYTES:
                raise ChromaError("audio_input", "Audio input exceeds 128 MiB")
            executable = shutil.which("ffmpeg")
            if not executable:
                raise ChromaError("ffmpeg_unavailable", "FFmpeg is required to import audio")
            output = Path(temp) / "decoded.wav"
            # Restrict demuxers/protocols: an imported playlist must never fetch
            # network assets or other local files. Formats are decoded, not trusted by suffix.
            formats = "wav,mp3,flac,aiff,ogg,mov,matroska,aac"
            result = run_audio_process(
                [
                    executable,
                    "-v",
                    "error",
                    "-nostdin",
                    "-protocol_whitelist",
                    "file",
                    "-format_whitelist",
                    formats,
                    "-i",
                    str(source),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-t",
                    str(MAX_SECONDS + 0.01),
                    "-ar",
                    str(RATE),
                    "-ac",
                    "2",
                    "-c:a",
                    "pcm_s32le",
                    "-map_metadata",
                    "-1",
                    str(output),
                ],
                timeout=120,
            )
            if result.returncode:
                raise ChromaError(
                    "audio_decode", "Unable to decode audio", diagnostic=result.stderr[-2000:]
                )
            # Inspect the original stream through the same restricted demuxers;
            # normalized PCM bit depth is never presented as source precision.
            source_probe = run_audio_process(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file",
                    "-format_whitelist",
                    formats,
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(source),
                ],
                timeout=30,
            )
            original_info = (
                json.loads(source_probe.stdout) if source_probe.returncode == 0 else None
            )
            return self._save_audio(
                project_id,
                name,
                output,
                {
                    "source": "import",
                    "source_sha256": file_hash(source),
                    "source_probe": original_info,
                    "normalization": "Resampled/downmixed to 48 kHz stereo signed 32-bit PCM; not original source precision",
                },
            )

    @operation(
        "Create basic tones/effects from sine/triangle/noise events. Prefer create_music with a SoundFont for instrumental scores. Empty events create silence. No speech/music model or network service."
    )
    def create_audio(self, project_id: str, name: str, spec: AudioSynthesis) -> dict[str, Any]:
        self._audio_project(project_id, True)
        with tempfile.TemporaryDirectory(dir=self.store.root, prefix="audio-create-") as temp:
            path = Path(temp) / "created.wav"
            metadata = synthesize(path, spec)
            return self._save_audio(project_id, name, path, {"source": "synthesis", **metadata})

    @operation(
        "Inspect an absolute local SF2 SoundFont: actual preset names, zero-based bank/program, size and SHA-256. Reads headers without requiring FluidSynth; does not certify sound quality or instrument identity."
    )
    def inspect_soundfont(self, soundfont_path: str) -> dict[str, Any]:
        from .music import inspect_soundfont

        return inspect_soundfont(soundfont_path, self.store.root)

    @operation(
        "Render a timed MusicScore offline with FluidSynth and a local SF2 SoundFont. Up to 15 tracks / 16384 notes; validates hit points, reading windows and instrumentation. Returns immutable 48 kHz stereo WAV and generation_report; attach separately with set_project_audio."
    )
    def create_music(
        self, project_id: str, name: str, soundfont_path: str, score: MusicScore
    ) -> dict[str, Any]:
        from .music import render_music

        self._audio_project(project_id, True)
        if not name.strip() or len(name) > 120:
            raise ChromaError("invalid_name", "Audio name needs 1..120 characters")
        with tempfile.TemporaryDirectory(dir=self.store.root, prefix="music-create-") as temp:
            path = Path(temp) / "created.wav"
            report = render_music(path, soundfont_path, score)
            result = self._save_audio(
                project_id, name, path, {"source": "fluidsynth", "generation_report": report}
            )
            return {**result, "generation_report": report}

    @operation(
        "Read project-specific local audio storage and production rules before creating sound. Returns absolute source/managed directories, format requirements and the attach-to-timeline workflow."
    )
    def get_audio_guidelines(self, project_id: str) -> dict[str, Any]:
        from .audio_review import review_capabilities
        from .documentation import DOCUMENTS
        from .music import music_environment

        self._audio_project(project_id)
        # Suggested authoring paths are not created by discovery. Only import/
        # synthesis owns immutable managed files and their database/artifact IDs.
        return {
            "project_id": project_id,
            "source_directory": str(self.store.root / "audio-sources" / project_id),
            "managed_directory": str(self.store.root / "audio"),
            "managed_filename": "<asset_id>.wav",
            "sample_rate": RATE,
            "channels": CHANNELS,
            "sample_format": "signed 32-bit little-endian integer PCM",
            "max_asset_seconds": MAX_SECONDS,
            "max_input_bytes": MAX_INPUT_BYTES,
            "music": music_environment(),
            # Discovery exposes the listening loop without starting model inference.
            "model_review": review_capabilities(self.store),
            "documentation": DOCUMENTS["audio"],
        }

    @operation(
        "List a project's immutable audio assets and revisioned timeline. Asset IDs also identify playable/exportable WAV artifacts."
    )
    def get_project_audio(self, project_id: str) -> dict[str, Any]:
        self._audio_project(project_id)
        return {
            **read_timeline(self.store, project_id),
            "assets": [a for a in self.store.list("audio_asset") if a["project_id"] == project_id],
        }

    @operation(
        "Replace the project audio timeline with revision checking (initial revision 0). Clips mix on the scene timeline in seconds; every new delivery freezes this timeline. Empty clips removes audio from future jobs."
    )
    def set_project_audio(
        self, project_id: str, expected_revision_id: str, timeline: AudioTimeline
    ) -> dict[str, Any]:
        self._audio_project(project_id, True)
        for clip in timeline.clips:
            asset = self.store.get("audio_asset", clip.asset_id)
            if asset["project_id"] != project_id:
                raise ChromaError("audio_project", "Audio asset belongs to a different project")
            source = samples(clip.source_start_seconds)
            length = (
                samples(clip.duration_seconds)
                if clip.duration_seconds is not None
                else asset["samples"] - source
            )
            if source >= asset["samples"] or source + length > asset["samples"] or length < 1:
                raise ChromaError("audio_trim", "Audio clip trim is outside the decoded source")
        # Check and write inside one SQLite transaction, including the absent
        # initial record. Other daemons cannot silently overwrite a user's draft.
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM records WHERE kind='audio_timeline' AND id=?", (project_id,)
            ).fetchone()
            current = json.loads(row[0])["revision_id"] if row else "0"
            if current != expected_revision_id:
                raise ChromaError(
                    "revision_conflict",
                    "Read the latest audio timeline; preserve your draft",
                    current_revision_id=current,
                )
            record = {
                "project_id": project_id,
                "revision_id": identifier(),
                "timeline": timeline.model_dump(),
            }
            connection.execute(
                "INSERT INTO records VALUES ('audio_timeline',?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
                (project_id, canonical(record)),
            )
        return record
