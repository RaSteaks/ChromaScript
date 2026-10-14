"""Transient native MCP previews for SDR compositions, without a false float-master claim."""

import base64
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .errors import ChromaError
from .motion import FRONTEND, SDR_RUNTIME, MotionSpec, browser_executable
from .store import canonical


def render_sdr_frame(spec: MotionSpec, frame: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="chromascript-sdr-preview-") as temporary:
        directory = Path(temporary)
        try:
            process = subprocess.run(
                ["node", str(SDR_RUNTIME), "frames"],
                input=canonical(
                    {
                        "spec": spec.model_dump(),
                        "directory": str(directory),
                        "frames": str(directory / "frames"),
                        "browser": browser_executable(),
                        "frame": frame,
                        "scale": min(1, 480 / max(spec.width, spec.height)),
                    }
                ),
                cwd=FRONTEND,
                capture_output=True,
                text=True,
                timeout=50,
            )
        except subprocess.TimeoutExpired as exc:
            raise ChromaError("motion_timeout", "SDR 单帧预览超时，请简化代码后重试。") from exc
        if process.returncode:
            raise ChromaError("motion_render", process.stderr[-6000:])
        frames = list((directory / "frames").glob("*.png"))
        if len(frames) != 1:
            raise ChromaError("motion_frame", "SDR 单帧预览输出不完整。")
        from PIL import Image

        with Image.open(frames[0]) as image:
            width, height = image.size
        return {
            "frame": frame,
            "transient": True,
            "color_space": "sRGB",
            "scene_linear_master": False,
            "source_precision": "8-bit browser screenshot",
            "images": [
                {
                    "data": base64.b64encode(frames[0].read_bytes()).decode(),
                    "mime_type": "image/png",
                    "width": width,
                    "height": height,
                    "bit_depth": 8,
                    "color_space": "sRGB",
                }
            ],
        }
