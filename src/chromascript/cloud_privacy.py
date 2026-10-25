"""Privacy boundaries for explicit cloud audio: routing, storage and public provenance."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from .cloud_diagnostics import safe_usage
from .errors import ChromaError


def provider_policy(
    values: dict[str, str], *, scope: Literal["speech", "music", "analysis"] = "speech"
) -> dict[str, Any]:
    # Music alone may opt out locally. Analysis always requires ZDR, regardless
    # of the global compatibility setting; tool arguments cannot weaken policy.
    global_value = values.get("OPENROUTER_REQUIRE_ZDR", "true").strip().lower()
    music_value = values.get("OPENROUTER_MUSIC_REQUIRE_ZDR", global_value).strip().lower()
    if global_value not in {"true", "false"} or music_value not in {"true", "false"}:
        raise ChromaError("cloud_privacy_config", "OpenRouter ZDR settings must be true or false")
    value = music_value if scope == "music" else global_value
    return {"data_collection": "deny", "zdr": scope == "analysis" or value == "true"}


def assert_no_secret(payload: dict[str, Any], secret: str) -> None:
    # The credential belongs solely in the HTTPS Authorization header, never in
    # a transcript, model ID, style prompt or a provider-specific parameter.
    def contains(value: Any) -> bool:
        if isinstance(value, str):
            return secret in value or bool(re.search(r"sk-or-v1-[A-Za-z0-9_-]{16,}", value))
        if isinstance(value, dict):
            return any(contains(k) or contains(v) for k, v in value.items())
        return isinstance(value, list) and any(contains(v) for v in value)

    if contains(payload):
        raise ChromaError("cloud_secret_in_input", "Remove credentials from generation inputs")


def redact_secret(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[redacted]")
    if isinstance(value, dict):
        return {str(redact_secret(k, secret)): redact_secret(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_secret(v, secret) for v in value]
    return value


def private_directory(path: Path) -> None:
    # Parents are prepared separately. Reject links instead of chmod-ing another
    # location or placing retained prompts/images in a shared redirected directory.
    path.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise ChromaError("cloud_storage", "Cloud source directory must be owned by this user")
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def private_directory_tree(root: Path, path: Path) -> None:
    """Prepare managed parents one level at a time without following symlinks."""
    relative = path.relative_to(root)
    if '..' in relative.parts:
        raise ChromaError("cloud_storage", "Cloud source directory must stay inside its project")
    current = root
    try:
        # The project/workspace root is already trusted. Only descendants are
        # made private; do not chmod unrelated ancestors or resolve linked paths.
        for part in relative.parts:
            current = current / part
            private_directory(current)
    except OSError:
        raise ChromaError("cloud_storage", "Unable to prepare private cloud source directories") from None


def public_report(report: dict[str, Any]) -> dict[str, Any]:
    # No prompt, transcript, image/path lists or arbitrary provider objects cross
    # into artifact metadata / SQLite / MCP. Detailed provenance stays private.
    result: dict[str, Any] = {
        key: report[key]
        for key in (
            "provider",
            "model_requested",
            "created_at",
            "response_bytes",
            "response_sha256",
            "format_requested",
            "timing_verified",
            "privacy_policy",
        )
        if key in report
    }
    for key in ("generation_id", "id", "model"):
        value = report.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,240}", value):
            result[key] = value
    usage = safe_usage(report.get("usage"))
    if usage:
        result["usage"] = usage
    if isinstance(report.get("finish_reason"), str):
        result["finish_reason"] = report["finish_reason"]
    encoding = report.get("source_encoding")
    if isinstance(encoding, dict):
        result["source_encoding"] = {
            key: encoding[key]
            for key in ("sample_rate", "channels", "sample_format", "samples")
            if key in encoding
        }
    result["private_provenance_retained"] = True
    return result


def read_private_env(path: Path) -> str:
    # No symlinks/FIFOs, unbounded reads or broadly readable credential files.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Use an owner-only regular dotenv file")
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError("Oversized dotenv")
    return data.decode("utf-8")


def public_setting(value: str, values: dict[str, str]) -> str:
    # A key accidentally pasted into a model/voice field must not escape through
    # the otherwise read-only configuration discovery endpoint.
    secret = values.get("OPENROUTER_API_KEY", "").strip()
    if (secret and secret in value) or "sk-or-" in value:
        return "[redacted invalid configuration]"
    return value


def validation_fields(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    # Unknown property names can themselves contain secrets. Known model field
    # names remain useful, but extra-field locations are not reflected verbatim.
    return [
        {
            "loc": list(e["loc"][:-1]) + ["<unknown>"]
            if e["type"] == "extra_forbidden"
            else list(e["loc"]),
            "type": e["type"],
        }
        for e in errors
    ]
