"""Bounded, private provider evidence captured before interpreting a completion."""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any

from .errors import ChromaError
from .store import canonical


def safe_usage(value: Any) -> dict[str, Any]:
    """Preserve reported counts; never interpret missing usage as zero cost."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {
        key: number
        for key, number in value.items()
        if key in {"prompt_tokens", "completion_tokens", "total_tokens", "cost"}
        and isinstance(number, (int, float))
        and not isinstance(number, bool)
        and math.isfinite(number)
        and number >= 0
    }
    details = value.get("completion_tokens_details")
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
    if isinstance(reasoning, int) and not isinstance(reasoning, bool) and reasoning >= 0:
        result["reasoning_tokens"] = reasoning
        total = result.get("completion_tokens")
        # OpenRouter counts reasoning inside completion_tokens; preserve ambiguity
        # instead of silently inventing an output-token count when usage is inconsistent.
        result["output_tokens"] = (
            total - reasoning if isinstance(total, int) and total >= reasoning else None
        )
    return result


def sanitize(value: Any, secret: str, depth: int = 0) -> Any:
    if depth > 40:
        return "[nesting limit]"
    if isinstance(value, str):
        value = value.replace(secret, "[redacted]") if secret else value
        value = re.sub(r"sk-or-v1-[A-Za-z0-9_-]+", "[redacted]", value)
        return re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [redacted]", value)
    if isinstance(value, dict):
        return {
            str(sanitize(k, secret, depth + 1)): (
                "[redacted]"
                if str(k).lower().replace("-", "_")
                in {"authorization", "api_key", "apikey", "access_token", "token"}
                else sanitize(v, secret, depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [sanitize(v, secret, depth + 1) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class ProviderDiagnostics:
    def __init__(self, directory: Path, secret: str):
        self.path = directory / "provider-response.json"
        self.secret = secret
        self.data: dict[str, Any] = {
            "http_status": None,
            "generation_id": None,
            "provider_name": None,
            "finish_reason": None,
            "usage": {},
        }

    def headers(self, status: int, headers: Any) -> None:
        # Never record request/response headers wholesale (cookies and credentials).
        self.data["http_status"] = status
        if headers.get("x-generation-id"):
            self.data["generation_id"] = headers["x-generation-id"]

    def capture(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        if value.get("id"):
            self.data["generation_id"] = value["id"]
        for key in ("model", "provider", "provider_name"):
            if isinstance(value.get(key), str):
                self.data["provider_name" if key == "provider" else key] = value[key]
        if isinstance(value.get("usage"), dict):
            self.data["raw_usage"] = value["usage"]
            self.data["usage"] = safe_usage(value["usage"])
        choices = value.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]
            if choice.get("finish_reason") is not None:
                self.data["finish_reason"] = choice["finish_reason"]
            if choice.get("native_finish_reason") is not None:
                self.data["native_finish_reason"] = choice["native_finish_reason"]
        if "error" in value:
            self.data["error_event"] = value
            error = value["error"]
            meta = error.get("metadata") if isinstance(error, dict) else None
            if isinstance(meta, dict):
                for key in ("provider_name", "generation_id"):
                    if meta.get(key):
                        self.data[key] = meta[key]

    def body(self, raw: bytes) -> None:
        # Caller enforces a response-size bound. Successful and failed JSON,
        # including partial text/usage, are saved before semantic validation.
        try:
            parsed = json.loads(raw)
        except (ValueError, RecursionError):
            self.data["raw_body"] = raw.decode("utf-8", errors="replace")
        else:
            self.data["response"] = parsed
            self.capture(parsed)

    def summary(self) -> dict[str, Any]:
        clean = sanitize(self.data, self.secret)
        result: dict[str, Any] = {
            "diagnostics_path": str(self.path),
            "http_status": clean["http_status"],
            "usage": clean.get("usage", {}),
        }
        for key in ("generation_id", "provider_name", "finish_reason", "native_finish_reason"):
            value = clean.get(key)
            if (
                isinstance(value, str)
                and len(value) <= 240
                and re.fullmatch(r"[A-Za-z0-9_.:/ -]+", value)
            ):
                result[key] = value
        return result

    def error(self, code: str, message: str) -> ChromaError:
        return ChromaError(code, message, **self.summary())

    def save(self) -> None:
        # Retained evidence is private even when this function is used outside
        # the normal 0700 request directory (for example in focused tests).
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(canonical(sanitize(self.data, self.secret)))
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
