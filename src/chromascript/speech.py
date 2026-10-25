"""Explicit cloud TTS with local secrets, bounded downloads and durable retry guards."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import threading
import time
import wave
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

import httpx
from pydantic import Field, field_validator

from .audio import MAX_INPUT_BYTES, MAX_SECONDS, AudioOperations
from .cloud_privacy import (
    assert_no_secret,
    private_directory,
    private_directory_tree,
    provider_policy,
    public_report,
    public_setting,
    read_private_env,
    redact_secret,
)
from .errors import ChromaError
from .images import file_hash
from .operations import Input, operation
from .store import Store, canonical, now

ENDPOINT = "https://openrouter.ai/api/v1/audio/speech"
DEFAULT_MODEL = "google/gemini-3.8-flash-tts"
ENV_KEYS = {
    "OPENROUTER_API_KEY",
    "OPENROUTER_TTS_MODEL",
    "OPENROUTER_TTS_VOICE",
    "OPENROUTER_MUSIC_MODEL",
    "OPENROUTER_REQUIRE_ZDR",
    "OPENROUTER_MUSIC_REQUIRE_ZDR",
    "OPENROUTER_ANALYSIS_MODEL",
}


class SpeechRequest(Input):
    text: str = Field(min_length=1, max_length=12000, strict=True)
    instructions: str = Field(default="", max_length=4000, strict=True)
    model: str | None = Field(default=None, max_length=200, strict=True)
    voice: str | None = Field(default=None, min_length=1, max_length=200, strict=True)
    response_format: Literal["pcm", "mp3"] = "pcm"

    @field_validator("text", "voice")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Text and voice must not be blank")
        return value

    @field_validator("model")
    @classmethod
    def model_id(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]+/[A-Za-z0-9_./:-]+", value):
            raise ValueError("Use an exact OpenRouter provider/model ID")
        return value


def configuration(store: Store) -> dict[str, str]:
    # Resolve against the workspace, never the MCP client's unpredictable cwd.
    # Only allowlisted literal single-line keys are read; no sourcing, interpolation,
    # shell execution, or mutation of the daemon's environment occurs.
    values: dict[str, str] = {}
    for path in (store.root.parent / ".env", store.root / ".env"):
        if not path.exists() and not path.is_symlink():
            continue
        try:
            for line in read_private_env(path).splitlines():
                line = line.strip().removeprefix("export ")
                key, separator, raw = line.partition("=")
                key = key.strip()
                if not separator or key not in ENV_KEYS:
                    continue
                parts = shlex.split(raw, comments=True, posix=True)
                if len(parts) > 1:
                    raise ValueError("Quote whitespace")
                values[key] = parts[0] if parts else ""
        except (OSError, ValueError):
            # Never include dotenv contents or parser exception text in errors.
            raise ChromaError(
                "speech_config",
                "Unable to read OpenRouter .env; require an owner-only regular file (chmod 600) and literal KEY=value lines",
            ) from None
    values.update({key: os.environ[key] for key in ENV_KEYS if key in os.environ})
    return values


def public_configuration(store: Store) -> dict[str, Any]:
    try:
        values = configuration(store)
        privacy = provider_policy(values)
    except ChromaError as exc:
        # Optional cloud configuration must not break offline capability discovery.
        return {
            "provider": "OpenRouter",
            "configuration_error": exc.code,
            "api_key_configured": False,
            "cloud": True,
            "note": "Unable to read TTS .env; offline audio remains available",
        }
    return {
        "provider": "OpenRouter",
        "endpoint": ENDPOINT,
        "api_key_configured": bool(values.get("OPENROUTER_API_KEY", "").strip()),
        "model": public_setting(values.get("OPENROUTER_TTS_MODEL") or DEFAULT_MODEL, values),
        "voice": public_setting(values.get("OPENROUTER_TTS_VOICE") or "Kore", values),
        "env_files": [str(store.root.parent / ".env"), str(store.root / ".env")],
        "precedence": "process environment > workspace/.env > workspace parent/.env",
        "cloud": True,
        "generation_may_incur_cost": True,
        "generate_operation": "chromascript.generate_speech",
        "privacy_policy": privacy,
        "note": "Local configuration only; credentials and model availability have not been tested",
    }


def speech_client() -> httpx.Client:
    # The key is sent only to the fixed HTTPS endpoint, never redirects or env proxies.
    return httpx.Client(
        timeout=httpx.Timeout(180, connect=10), trust_env=False, follow_redirects=False
    )


def download_speech(
    key: str, payload: dict[str, Any], directory: Path
) -> tuple[Path, dict[str, Any]]:
    assert_no_secret(payload, key)
    raw = directory / "response.partial"
    started = time.monotonic()
    try:
        with (
            speech_client() as client,
            client.stream(
                "POST",
                ENDPOINT,
                json=payload,
                headers={"Authorization": "Bearer " + key},
            ) as response,
        ):
            if response.status_code != 200:
                # Provider error bodies may echo credentials or script content.
                raise ChromaError(
                    "speech_provider",
                    "OpenRouter rejected the TTS request; no automatic retry",
                    status=response.status_code,
                )
            content_type = response.headers.get("content-type", "")
            mime = content_type.split(";", 1)[0].strip().lower()
            expected = "audio/pcm" if payload["response_format"] == "pcm" else "audio/mpeg"
            if mime != expected:
                raise ChromaError(
                    "speech_format", "Provider returned an unexpected audio content type"
                )
            size = 0
            with raw.open("xb") as output:
                for chunk in response.iter_bytes(65536):
                    size += len(chunk)
                    if size > MAX_INPUT_BYTES or time.monotonic() - started > 180:
                        raise ChromaError(
                            "speech_limit", "TTS response exceeded 128 MiB or 180 seconds"
                        )
                    output.write(chunk)
            if not size:
                raise ChromaError("speech_format", "Provider returned empty audio")
            report: dict[str, Any] = {
                "provider": "OpenRouter",
                "model_requested": payload["model"],
                "request": payload,
                "generation_id": response.headers.get("x-generation-id"),
                "content_type": content_type,
                "response_bytes": size,
                "response_sha256": file_hash(raw),
                "created_at": now(),
                "privacy_policy": payload["provider"],
                "precision_note": "Source encoding is not model precision; managed PCM is resampled",
            }
            if payload["response_format"] == "pcm":
                params = {}
                for part in content_type.split(";")[1:]:
                    name, sep, value = part.strip().partition("=")
                    if sep:
                        params[name.lower()] = value.strip('"')
                # Gemini's documented default is s16le / 24 kHz / mono.
                # Other providers must declare their PCM rate/channel layout.
                gemini = payload["model"].startswith("google/gemini-")
                try:
                    rate = int(params.get("rate", "24000" if gemini else ""))
                    channels = int(params.get("channels", "1" if gemini else ""))
                except ValueError:
                    raise ChromaError(
                        "speech_format", "Missing or invalid PCM rate/channels"
                    ) from None
                if not 8000 <= rate <= 192000 or channels not in (1, 2) or size % (2 * channels):
                    raise ChromaError("speech_format", "Invalid PCM sample layout")
                if size // (2 * channels) > rate * MAX_SECONDS:
                    raise ChromaError("speech_limit", "Generated speech exceeds 600 seconds")
                source = directory / "source.wav"
                with wave.open(str(source), "wb") as wav:
                    wav.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
                    with raw.open("rb") as data:
                        while chunk := data.read(65536):
                            wav.writeframesraw(chunk)
                report["source_encoding"] = {
                    "sample_rate": rate,
                    "channels": channels,
                    "sample_format": "s16le",
                    "samples": size // (2 * channels),
                }
            else:
                source = directory / "source.mp3"
                raw.replace(source)
            raw.unlink(missing_ok=True)
            return source, report
    except httpx.HTTPError:
        raise ChromaError(
            "speech_network",
            "TTS transport failed; upstream charge may have occurred; no automatic retry",
        ) from None
    finally:
        raw.unlink(missing_ok=True)


class SpeechOperations(AudioOperations):
    speech_lock: threading.Lock
    operation_lock: Any

    @operation(
        "Read local OpenRouter TTS configuration and key presence, never the key. No network request or charge.",
        serialized=False,
    )
    def get_speech_config(self) -> dict[str, Any]:
        return public_configuration(self.store)

    @operation(
        "Generate CLOUD speech via OpenRouter (billable): sends text/style/voice, not video. Reads key from local .env. Requires a unique idempotency_key; reuse it for safe retries. Returns an immutable audio asset; attach separately with revision-checked set_project_audio. Failed/pending keys never automatically resubmit a paid request.",
        serialized=False,
    )
    def generate_speech(
        self,
        project_id: str,
        name: Annotated[str, Field(min_length=1, max_length=120, strict=True)],
        request: SpeechRequest,
        idempotency_key: Annotated[
            str, Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$", strict=True)
        ],
    ) -> dict[str, Any]:
        if not self.speech_lock.acquire(blocking=False):
            raise ChromaError(
                "speech_busy", "Another speech request is running; retry with the same key later"
            )
        try:
            return self._generate_speech(project_id, name, request, idempotency_key)
        finally:
            self.speech_lock.release()

    def _generate_speech(
        self, project_id: str, name: str, request: SpeechRequest, key: str
    ) -> dict[str, Any]:
        def prepare(
            values: dict[str, str], secret: str
        ) -> Callable[[Path], tuple[Path, dict[str, Any]]]:
            model = request.model or values.get("OPENROUTER_TTS_MODEL") or DEFAULT_MODEL
            voice = request.voice or values.get("OPENROUTER_TTS_VOICE") or "Kore"
            try:
                SpeechRequest(text=request.text, model=model, voice=voice)
            except ValueError:
                raise ChromaError(
                    "speech_config", "Invalid configured TTS model or voice"
                ) from None
            if model.startswith("google/gemini-") and request.response_format != "pcm":
                raise ChromaError("speech_format", "Gemini TTS requires response_format=pcm")
            payload = {
                "model": model,
                "input": request.text,
                "voice": voice,
                "instructions": request.instructions,
                "response_format": request.response_format,
                "provider": provider_policy(values),
            }
            assert_no_secret(payload, secret)
            return lambda directory: download_speech(secret, payload, directory)

        return self._generate_cloud_audio(
            project_id, name, request.model_dump(), key, "speech", prepare
        )

    def _generate_cloud_audio(
        self,
        project_id: str,
        name: str,
        inputs: dict[str, Any],
        key: str,
        kind: Literal["speech", "music"],
        prepare: Callable[[dict[str, str], str], Callable[[Path], tuple[Path, dict[str, Any]]]],
    ) -> dict[str, Any]:
        # Speech/music share recovery semantics but keep independent namespaces,
        # locks and source directories. Existing speech request IDs remain valid.
        record_kind = kind + "_request"
        with self.operation_lock:
            self._audio_project(project_id, True)
            if not name.strip():
                raise ChromaError("invalid_name", "Audio name must not be blank")
        fingerprint = hashlib.sha256(
            canonical({"project_id": project_id, "name": name, "request": inputs}).encode()
        ).hexdigest()
        rid = hashlib.sha256(f"{project_id}:{key}".encode()).hexdigest()
        directory = self.store.project_file(project_id, kind + "-sources/" + rid)
        # Reserve before any network I/O. A crash/timeout must never authorize
        # another charge, even through a different transport or a second daemon.
        with self.store.connect(project_id) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM records WHERE kind=? AND id=?", (record_kind, rid)
            ).fetchone() or self.store.legacy_record(record_kind, rid)
            if row:
                previous = json.loads(row[0])
                if previous["fingerprint"] != fingerprint:
                    raise ChromaError(
                        "idempotency_conflict", "Use a new key for changed audio inputs"
                    )
                if previous["status"] == "complete":
                    result = previous["result"]
                    asset = result["asset"]
                    path = self.store.file(asset["relative_path"])
                    if not path.is_file() or file_hash(path) != asset["sha256"]:
                        raise ChromaError(
                            "audio_asset_changed",
                            "Saved audio is missing or changed; no resubmission",
                        )
                    return dict(result)
                raise ChromaError(
                    kind + "_not_retried",
                    "Previous request is pending or failed; inspect its source before explicitly using a new billable key",
                    request_id=rid,
                    status=previous["status"],
                    source_directory=str(directory),
                    **previous.get("diagnostics", {}),
                )
            values = configuration(self.store)
            secret = values.get("OPENROUTER_API_KEY", "").strip()
            if not secret or any(c.isspace() for c in secret) or not secret.isascii():
                raise ChromaError(kind + "_config", "Set a valid OPENROUTER_API_KEY in local .env")
            # Names and reference descriptions also flow into durable metadata.
            assert_no_secret({"name": name, "inputs": inputs}, secret)
            if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
                raise ChromaError(
                    "ffmpeg_unavailable", "FFmpeg and ffprobe are required before paid generation"
                )
            generate = prepare(values, secret)
            # A portable project's generation path has several new parents.
            # Finish local storage preflight before reserving a non-retryable key.
            storage_root = self.store.project_directory(project_id) if self.store.partitioned else self.store.root
            private_directory_tree(storage_root, directory.parent)
            record = {
                "id": rid,
                "project_id": project_id,  # Request ownership survives failed/paid attempts.
                "status": "pending",
                "fingerprint": fingerprint,
                "created_at": now(),
            }
            connection.execute(
                "INSERT INTO records VALUES (?,?,?)", (record_kind, rid, canonical(record))
            )
        directory_owned = False
        try:
            if directory.exists() or directory.is_symlink():
                raise ChromaError("cloud_storage", "Cloud request directory already exists")
            private_directory(directory)
            directory_owned = True
            source, report = generate(directory)
            # Retain paid source/provenance if decoding fails. No cloud inference
            # holds the operation lock; only the final immutable publication does.
            report = redact_secret(report, secret)
            report["source_path"] = str(source)
            (directory / "generation.json").write_text(canonical(report), encoding="utf-8")
            with self.operation_lock:
                result = self._import_audio(
                    project_id,
                    name,
                    source_path=str(source),
                    metadata={
                        "source": "openrouter_tts" if kind == "speech" else "openrouter_music",
                        kind + "_generation": public_report(report),
                    },
                )
                result["request_id"] = rid
                self.store.put(record_kind, rid, {**record, "status": "complete", "result": result})
            return result
        except Exception as exc:
            code = exc.code if isinstance(exc, ChromaError) else kind + "_processing"
            self.store.put(record_kind, rid, {**record, "status": "failed", "error_code": code, "diagnostics": exc.details if isinstance(exc, ChromaError) and "diagnostics_path" in exc.details else {}})
            if isinstance(exc, ChromaError):
                raise
            raise ChromaError(
                kind + "_processing",
                "Audio processing failed; inspect retained sources before using a new key",
                request_id=rid,
                source_directory=str(directory),
            ) from None
        finally:
            # Source directories are already 0700; also restrict each new file.
            if directory_owned and directory.is_dir() and not directory.is_symlink():
                for retained in directory.iterdir():
                    if retained.is_file() and not retained.is_symlink():
                        retained.chmod(0o600)
