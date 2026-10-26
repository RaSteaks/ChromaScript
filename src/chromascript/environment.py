"""Frozen runtime identity prevents stale cache reuse after numerical code changes."""

import hashlib
import importlib.metadata
import platform
import subprocess
from pathlib import Path

PACKAGES = ("numpy", "wgpu", "opencolorio", "OpenImageIO", "mcp", "fastapi", "pydantic")


def runtime_identity() -> dict:
    versions = {name: importlib.metadata.version(name) for name in PACKAGES}
    digest = hashlib.sha256()
    # Freeze both master generation and delivery processing: chroma sampling,
    # alpha flattening and encoder changes must not resume under an old identity.
    # UI and transport-only edits still leave numerical jobs unchanged.
    for name in ("gpu.py", "color.py", "images.py", "models.py", "encoding.py", "worker.py", "frame_manifest.py"):
        digest.update(name.encode())
        digest.update(Path(__file__).with_name(name).read_bytes())
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": versions,
        "ffmpeg": subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, check=True
        ).stdout.splitlines()[0],
        "renderer_sha256": digest.hexdigest(),
    }
