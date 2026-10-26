"""Measured hardware encoder selection, independent of browser/GPU source semantics."""

from __future__ import annotations

import hashlib
import platform
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from .encoding import check_probe, encode
from .errors import ChromaError
from .models import DeliveryPreset, MotionRate, MotionRenderSettings
from .video_transport import VideoFrame, VideoPixelFormat


def hardware_probe(preset: DeliveryPreset, settings: MotionRenderSettings, speed: str) -> dict[str, Any]:
    executable = shutil.which("ffmpeg")
    if not executable or platform.system() != "Darwin":
        return {"available": False, "reason": "本机不支持 Apple VideoToolbox 硬件编码。"}
    stat = Path(executable).stat()
    identity = hashlib.sha256(repr((executable, stat.st_size, stat.st_mtime_ns, platform.platform())).encode()).hexdigest()
    try:
        return _probe(identity, preset.model_dump_json(), settings.width, settings.height,
                      settings.fps.numerator, settings.fps.denominator, speed)
    except (ChromaError, OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "identity": identity, "reason": str(exc)}


# Exceptions leave the LRU unchanged. A transient busy/IO failure must be probed
# again for a new request; only verified successful combinations are reusable.
@lru_cache(maxsize=64)
def _probe(identity: str, preset_json: str, width: int, height: int, numerator: int,
           denominator: int, speed: str) -> dict[str, Any]:
    preset = DeliveryPreset.model_validate_json(preset_json)
    settings = MotionRenderSettings(width=width, height=height, frames=3,
                                   fps=MotionRate(numerator=numerator, denominator=denominator))
    ten = preset.bit_depth == 10
    pixel_format: VideoPixelFormat = "p010le" if ten else "nv12"
    # The probe exercises the actual raster/rational rate and native tags. HLG
    # includes legal super-white Y codes to reject a clipping hardware path.
    values = np.full(width * height * 3 // 2, 512 if ten else 128, dtype="<u2" if ten else "u1")
    maximum = 1000 if preset.color.output == "HLG" else 940 if ten else 235
    values[:width * height] = np.tile(np.linspace(64 if ten else 16, maximum, width).astype(values.dtype), height)
    if ten:
        values <<= 6
    frame = VideoFrame(values.tobytes(), pixel_format)
    with tempfile.TemporaryDirectory(prefix="chromascript-hardware-probe-") as temporary:
        path = Path(temporary) / "probe.mp4"
        info = encode(preset, settings, path, [frame] * 3, lambda _: None, video_input=pixel_format, speed=speed)
        checked = check_probe(info, preset, settings)
        if not checked["passed"]:
            raise ChromaError("video_hardware", "; ".join(checked["issues"]))
        if preset.color.output == "HLG":
            decoded = subprocess.run([
                "ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1",
                "-pix_fmt", "yuv420p10le", "-f", "rawvideo", "pipe:1",
            ], capture_output=True, check=True, timeout=30).stdout
            if np.frombuffer(decoded, "<u2")[:width * height].max() <= 980:
                raise ChromaError("hlg_storage_range", "硬件编码裁切了 HLG 超白。")
        # Cache verified properties only, not durations/file hashes or performance
        # measurements. Submission freezes this evidence once for each request.
        return {"available": True, "identity": identity, "encoder": preset.codec,
                "width": width, "height": height, "fps": f"{numerator}/{denominator}",
                "pixel_format": checked["video"]["pix_fmt"], "profile": checked["video"]["profile"],
                "color": info["container_color"], "strict_decode_verified": True,
                "native_bitstream_color_verified": True, "hardware_required": True}
