"""Lyria music from explicit visual references; separate from local score rendering."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any, Callable

import httpx
from PIL import Image, ImageCms, ImageOps
from pydantic import Field, field_validator

from .artifacts import artifact_path
from .audio import MAX_INPUT_BYTES
from .cloud_diagnostics import ProviderDiagnostics
from .cloud_privacy import assert_no_secret, provider_policy, public_setting
from .errors import ChromaError
from .images import file_hash, srgb_profile
from .operations import Input, operation
from .speech import SpeechOperations, SpeechRequest, configuration
from .store import Store, canonical, now

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/lyria-3-pro-preview"
MAX_IMAGE_BYTES = 8 * 1024**2
MAX_REFERENCE_BYTES = 16 * 1024**2
MAX_EVENT_BYTES = 24 * 1024**2
MAX_WIRE_BYTES = 192 * 1024**2
MAX_TEXT = 65536


class MusicReference(Input):
    path: str = Field(min_length=1, max_length=4096, strict=True)
    time_seconds: float | None = Field(default=None, ge=0, le=86400, strict=True)
    description: str = Field(default="", max_length=1000, strict=True)


class MusicGenerationRequest(Input):
    prompt: str = Field(min_length=1, max_length=12000, strict=True)
    model: str | None = Field(default=None, max_length=200, strict=True)
    instrumental: bool = Field(default=True, strict=True)
    target_duration_seconds: float | None = Field(default=None, ge=1, le=600, strict=True)
    reference_images: list[MusicReference] = Field(default_factory=list, max_length=4)

    @field_validator("prompt")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Music prompt must not be blank")
        return value

    @field_validator("model")
    @classmethod
    def model_id(cls, value: str | None) -> str | None:
        return SpeechRequest.model_id(value)


def music_configuration(store: Store) -> dict[str, Any]:
    try:
        values = configuration(store)
        privacy = provider_policy(values, scope="music")
    except ChromaError:
        return {
            "provider": "OpenRouter",
            "configuration_error": "music_config",
            "api_key_configured": False,
            "cloud": True,
        }
    return {
        "provider": "OpenRouter",
        "endpoint": ENDPOINT,
        "api_key_configured": bool(values.get("OPENROUTER_API_KEY", "").strip()),
        "model": public_setting(values.get("OPENROUTER_MUSIC_MODEL") or DEFAULT_MODEL, values),
        "cloud": True,
        "env_files": [str(store.root.parent / ".env"), str(store.root / ".env")],
        "precedence": "process environment > workspace/.env > workspace parent/.env",
        "generation_may_incur_cost": True,
        "privacy_policy": privacy,
        "generate_operation": "chromascript.generate_music",
        "reference_images": "0–4 project-scoped preview images; metadata stripped before cloud upload; 8 MiB each, 16 MiB total",
        "format_requested": "mp3; actual encoding/rate/channels measured after decoding",
        "timing": "Duration and image timestamps are prompt guidance, not synchronization guarantees",
        "concurrency": "One music and one speech request may run concurrently per daemon",
        "note": "Configuration only; no network call, model availability or balance validation",
    }


def read_references(
    request: MusicGenerationRequest, store: Store, project_id: str
) -> list[tuple[bytes, dict[str, Any]]]:
    result = []
    total = 0
    for reference in request.reference_images:
        path = Path(reference.path).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ChromaError(
                "music_reference", "Use absolute local paths to selected display preview images"
            )
        # A caller may upload this project's staged references or its registered
        # preview artifacts, not arbitrary photos from another project/home folder.
        resolved = path.resolve()
        staging = store.project_file(project_id, "audio-sources/" + project_id)
        allowed = resolved.is_relative_to(staging) and staging.resolve() == staging
        if not allowed:
            allowed = any(
                record["project_id"] == project_id
                and record.get("mime_type") in {"image/png", "image/jpeg", "image/webp"}
                and artifact_path(store, record).resolve() == resolved
                for record in store.list("artifact")
            )
        if not allowed:
            raise ChromaError(
                "music_reference_scope",
                "Stage selected images in this project's audio-sources directory or use its registered preview artifacts",
            )
        try:
            # Bound the read itself as well as decoded pixels; file size can change.
            with path.open("rb") as source:
                data = source.read(MAX_IMAGE_BYTES + 1)
            total += len(data)
            if len(data) > MAX_IMAGE_BYTES or total > MAX_REFERENCE_BYTES:
                raise ChromaError(
                    "music_reference", "Reference images exceed 8 MiB each / 16 MiB total"
                )
            with Image.open(io.BytesIO(data)) as image:
                mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(
                    image.format or ""
                )
                if (
                    not mime
                    or image.width * image.height > 16_777_216
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise ChromaError(
                        "music_reference", "Use still PNG/JPEG/WebP images up to 16 megapixels"
                    )
                dimensions = [image.width, image.height]
                image.load()
                # Bake orientation, then discard EXIF/GPS/XMP/comments/trailing
                # bytes. A fresh pixel image avoids Pillow copying source info.
                oriented = ImageOps.exif_transpose(image)
                pixels = oriented.convert("RGBA")
                rgb = pixels.convert("RGB")
                profile = image.info.get("icc_profile")
                if profile:
                    converted = ImageCms.profileToProfile(
                        rgb,
                        ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                        ImageCms.createProfile("sRGB"),
                        outputMode="RGB",
                    )
                    if converted is None:
                        raise ValueError("Unable to convert display profile")
                    rgb = converted
                # Alpha is copied independently and never enters color conversion.
                rgb.putalpha(pixels.getchannel("A"))
                clean = Image.frombytes("RGBA", rgb.size, rgb.tobytes())
                encoded = io.BytesIO()
                clean.save(encoded, format="PNG", icc_profile=srgb_profile())
                upload = encoded.getvalue()
                dimensions = list(clean.size)
                if len(upload) > MAX_IMAGE_BYTES:
                    raise ChromaError("music_reference", "Sanitized reference exceeds 8 MiB")
        except (OSError, ValueError, Image.DecompressionBombError, ImageCms.PyCMSError):
            raise ChromaError("music_reference", "Unable to decode a reference image") from None
        # Only sanitized display pixels leave the machine. Untagged input is
        # treated as a display-sRGB reference, never as a colorimetry measurement.
        total += max(0, len(upload) - len(data))
        if total > MAX_REFERENCE_BYTES:
            raise ChromaError("music_reference", "Sanitized references exceed 16 MiB")
        result.append(
            (
                upload,
                {
                    **reference.model_dump(),
                    "mime_type": "image/png",
                    "sanitized": True,
                    "upload_sha256": hashlib.sha256(upload).hexdigest(),
                    "dimensions": dimensions,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                },
            )
        )
    return result


def music_client() -> httpx.Client:
    # Fixed HTTPS destination; do not forward the shared secret to redirect targets.
    return httpx.Client(
        timeout=httpx.Timeout(300, connect=10), trust_env=False, follow_redirects=False
    )


def sse_events(response: httpx.Response) -> Iterator[str]:
    """Bound both wire data and unbroken SSE lines before JSON/base64 allocation."""
    buffer = b""
    data: list[bytes] = []
    event_size = total = 0
    started = time.monotonic()
    for chunk in response.iter_bytes(65536):
        total += len(chunk)
        if total > MAX_WIRE_BYTES or time.monotonic() - started > 300:
            raise ChromaError("music_limit", "Music response exceeded size or time limits")
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if line.startswith(b"data:"):
                part = line[5:].removeprefix(b" ")
                event_size += len(part)
                if event_size > MAX_EVENT_BYTES:
                    raise ChromaError("music_limit", "Music SSE event is too large")
                data.append(part)
            elif not line and data:
                yield b"\n".join(data).decode("utf-8")
                data, event_size = [], 0
        if len(buffer) + event_size > MAX_EVENT_BYTES:
            raise ChromaError("music_limit", "Music SSE line is too large")
    # Unfinished events are not accepted as complete audio on a broken connection.
    if buffer.strip() or data:
        raise ChromaError("music_incomplete", "Music stream ended inside an event")


def download_music(
    secret: str, payload: dict[str, Any], directory: Path, provenance: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    assert_no_secret(payload, secret)
    partial = directory / "source.partial.mp3"
    report: dict[str, Any] = {
        "provider": "OpenRouter",
        "model_requested": payload["model"],
        "created_at": now(),
        "privacy_policy": payload["provider"],
        **provenance,
    }
    diagnostic = ProviderDiagnostics(directory, secret)
    text = ""
    size = 0
    finished = done = False
    failure: tuple[str, str] | None = None
    try:
        with (
            music_client() as client,
            client.stream(
                "POST", ENDPOINT, json=payload, headers={"Authorization": "Bearer " + secret}
            ) as response,
        ):
            diagnostic.headers(response.status_code, response.headers)
            if response.status_code != 200:
                body = bytearray()
                for chunk in response.iter_bytes(65536):
                    if len(body) + len(chunk) > 262144:
                        diagnostic.data["body_truncated_by_client"] = True
                        break
                    body.extend(chunk)
                diagnostic.body(bytes(body))
                raise diagnostic.error(
                    "music_provider",
                    "OpenRouter rejected music generation; inspect private diagnostics",
                )
            if (
                response.headers.get("content-type", "").split(";", 1)[0].strip()
                != "text/event-stream"
            ):
                raise ChromaError("music_format", "Expected OpenRouter streaming audio response")
            with partial.open("xb") as output:
                for event in sse_events(response):
                    if event == "[DONE]":
                        done = True
                        break
                    try:
                        value = json.loads(event)
                    except ValueError:
                        diagnostic.data["malformed_event"] = event[:262144]
                        raise
                    diagnostic.capture(value)
                    if not isinstance(value, dict):
                        diagnostic.data["invalid_event"] = value
                        raise ChromaError("music_format", "Expected a JSON event object")
                    if "error" in value:
                        # Providers may send final usage AFTER an error/finish event.
                        # Drain the bounded stream before reporting semantic failure.
                        failure = ("music_provider", "Music provider reported a streaming error")
                        continue
                    for field in ("id", "model", "usage"):
                        if field in value:
                            report[field] = value[field]
                    for choice in value.get("choices", []):
                        if choice.get("index", 0) != 0:
                            raise ChromaError("music_format", "Unexpected multiple music outputs")
                        reason = choice.get("finish_reason")
                        if reason is not None:
                            if reason == "stop":
                                finished = True
                            elif failure is None:
                                code = (
                                    "music_truncated"
                                    if reason == "length"
                                    else "music_refused"
                                    if reason in {"content_filter", "refusal", "safety"}
                                    else "music_incomplete"
                                )
                                failure = (
                                    code,
                                    "Music ended without a complete result; inspect diagnostics",
                                )
                        delta = choice.get("delta") or {}
                        audio = delta.get("audio") or {}
                        if delta.get("refusal"):
                            diagnostic.data["refusal"] = delta["refusal"]
                            failure = ("music_refused", "Music request was refused")
                        for part in (delta.get("content"), audio.get("transcript")):
                            if part is not None:
                                if not isinstance(part, str) or len(text) + len(part) > MAX_TEXT:
                                    raise ChromaError(
                                        "music_limit", "Music text output exceeded its limit"
                                    )
                                text += part
                        encoded = audio.get("data")
                        if encoded:
                            decoded = base64.b64decode(encoded, validate=True)
                            size += len(decoded)
                            if size > MAX_INPUT_BYTES:
                                raise ChromaError("music_limit", "Music audio exceeded 128 MiB")
                            output.write(decoded)
            if failure is not None:
                raise diagnostic.error(*failure)
            if not done or not finished or not size:
                raise ChromaError(
                    "music_incomplete", "Music stream has no complete audio; not published"
                )
        source = directory / "source.mp3"
        partial.replace(source)
        report.update(
            response_bytes=size,
            response_sha256=file_hash(source),
            generated_text=text,
            format_requested="mp3",
            timing_verified=False,
        )
        report["diagnostics"] = diagnostic.summary()
        return source, report
    except ChromaError as exc:
        raise ChromaError(
            exc.code, exc.message, **{**exc.details, **diagnostic.summary()}
        ) from None
    except httpx.HTTPError:
        raise diagnostic.error(
            "music_network",
            "Music transport failed; upstream charge may have occurred; no automatic retry",
        ) from None
    except (ValueError, TypeError, AttributeError, binascii.Error):
        raise diagnostic.error("music_format", "Invalid music stream or encoded audio") from None
    finally:
        diagnostic.data.update(
            received_audio_bytes=size,
            partial_text=text,
            complete=done and finished and size > 0 and failure is None,
        )
        diagnostic.save()
        # Incomplete bytes remain clearly labelled, never published as a playable
        # asset. Recovery context has no key or image data URI.
        if partial.exists():
            (directory / "incomplete.json").write_text(
                canonical({"complete": False, "received_audio_bytes": size}), encoding="utf-8"
            )


class CloudMusicOperations(SpeechOperations):
    music_generation_lock: threading.Lock

    @operation(
        "Read local OpenRouter music configuration and key presence. No network or charge. Music and TTS have independent models and share one key.",
        serialized=False,
    )
    def get_music_config(self) -> dict[str, Any]:
        return music_configuration(self.store)

    @operation(
        "Generate CLOUD Lyria music from a prompt and optional explicit local keyframe images (uploaded to OpenRouter, billable). Independent of generate_speech; both may run concurrently. Requires an idempotency_key; never auto-retry failed/pending keys. Returns an immutable audio asset. Time/duration requests are guidance, not guaranteed sync. Attach separately using set_project_audio.",
        serialized=False,
    )
    def generate_music(
        self,
        project_id: str,
        name: Annotated[str, Field(min_length=1, max_length=120, strict=True)],
        request: MusicGenerationRequest,
        idempotency_key: Annotated[
            str, Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$", strict=True)
        ],
    ) -> dict[str, Any]:
        if not self.music_generation_lock.acquire(blocking=False):
            raise ChromaError(
                "music_busy", "Another music request is running; retry the same key later"
            )
        try:
            self._audio_project(project_id, True)
            references = read_references(request, self.store, project_id)
            # Include actual reference hashes so replacing an image at the same
            # path cannot silently reuse the earlier generated music.
            inputs = {
                **request.model_dump(),
                "reference_fingerprints": [info["sha256"] for _, info in references],
            }

            def prepare(
                values: dict[str, str], secret: str
            ) -> Callable[[Path], tuple[Path, dict[str, Any]]]:
                model = request.model or values.get("OPENROUTER_MUSIC_MODEL") or DEFAULT_MODEL
                try:
                    MusicGenerationRequest(prompt=request.prompt, model=model)
                except ValueError:
                    raise ChromaError("music_config", "Invalid configured music model ID") from None
                # This adapter is for Lyria chat-audio, never route a music prompt
                # to the independently configured speech endpoint/model.
                if not model.startswith("google/lyria-"):
                    raise ChromaError(
                        "music_config", "This music adapter requires a google/lyria- model"
                    )
                prompt = request.prompt
                if request.instrumental:
                    prompt += "\nInstrumental only. No vocals, singing, speech or lyrics."
                if request.target_duration_seconds is not None:
                    prompt += f"\nRequested duration: approximately {request.target_duration_seconds:g} seconds."
                content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
                for index, (data, info) in enumerate(references):
                    label = f"Visual reference {index + 1}"
                    if info["time_seconds"] is not None:
                        label += f" at {info['time_seconds']:g} seconds on the video timeline"
                    content.append({"type": "text", "text": label + ": " + info["description"]})
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:"
                                + info["mime_type"]
                                + ";base64,"
                                + base64.b64encode(data).decode("ascii")
                            },
                        }
                    )
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": content}],
                    "modalities": ["text", "audio"],
                    "audio": {"format": "mp3"},
                    "stream": True,
                    "provider": provider_policy(values, scope="music"),
                }

                assert_no_secret(payload, secret)

                def generate(directory: Path) -> tuple[Path, dict[str, Any]]:
                    image_reports = []
                    for index, (data, info) in enumerate(references):
                        suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[
                            info["mime_type"]
                        ]
                        snapshot = directory / f"reference-{index + 1}{suffix}"
                        snapshot.write_bytes(data)
                        snapshot.chmod(0o600)
                        image_reports.append({**info, "snapshot_path": str(snapshot)})
                    provenance = {
                        "request": request.model_dump(),
                        "prompt_sent": prompt,
                        "reference_images": image_reports,
                    }
                    (directory / "request.json").write_text(canonical(provenance), encoding="utf-8")
                    return download_music(secret, payload, directory, provenance)

                return generate

            return self._generate_cloud_audio(
                project_id, name, inputs, idempotency_key, "music", prepare
            )
        finally:
            self.music_generation_lock.release()
