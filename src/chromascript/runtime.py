"""Local daemon discovery; stdio clients do not own render-worker lifetimes."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from .errors import ChromaError


def endpoint(root: Path) -> dict[str, Any]:
    try:
        info = json.loads((root / "server.json").read_text())
        if info["workspace"] != str(root.resolve()):
            raise ValueError("Workspace mismatch")
        return info
    except (OSError, ValueError, KeyError) as exc:
        raise ChromaError("daemon_offline", "Start chromascript serve for this workspace") from exc


def healthy(root: Path) -> bool:
    try:
        info = endpoint(root)
        result = httpx.get(info["url"] + "/health", timeout=0.8)
        return result.is_success and result.json().get("workspace") == str(root.resolve())
    except (ChromaError, httpx.HTTPError, ValueError):
        return False


def ensure_daemon(root: Path) -> dict[str, Any]:
    root = root.resolve()
    if healthy(root):
        return endpoint(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "daemon.log").open("ab") as log:
        # Match manual serve's fixed web address; never silently choose a random port.
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "chromascript.cli",
                "--workspace",
                str(root),
                "serve",
                "--port",
                "8765",
            ],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if healthy(root):
            return endpoint(root)
        time.sleep(0.2)
    raise ChromaError("daemon_start", "Daemon did not become ready; inspect workspace/daemon.log")


def call(
    root: Path, name: str, arguments: dict[str, Any], autostart: bool = True
) -> dict[str, Any]:
    info = ensure_daemon(root) if autostart else endpoint(root)
    response = httpx.post(
        info["url"] + "/api/operations/" + name.removeprefix("chromascript."),
        json=arguments,
        headers={"Authorization": "Bearer " + info["token"]},
        # Native selection is user-paced and writes nothing. Allow the same
        # dialog lifetime on CLI/MCP as on the browser transport.
        # User-selected local copies/ZIPs may outlast a request timeout; exclusive
        # destination creation prevents a retry from overwriting the first export.
        timeout=(
            httpx.Timeout(60, read=None)
            # Audition preparation, music, local reviews and cloud speech/music/analysis enforce their own processing timeouts.
            if name.removeprefix("chromascript.") in {"export_artifact", "export_project_folder", "migrate_workspace_storage", "create_audition", "create_audio", "import_audio", "create_music", "inspect_soundfont", "review_audio", "generate_speech", "generate_music", "analyze_media"}
            else 330 if name.removeprefix("chromascript.") == "choose_export_directory" else 60
        ),
    )
    data = response.json()
    if not response.is_success:
        error = data.get("error", {})
        raise ChromaError(
            error.get("code", "http_error"),
            error.get("message", response.text),
            **error.get("details", {}),
        )
    return data
