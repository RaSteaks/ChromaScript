"""Measured Apple ProRes 4444 capability, independent of decoder storage labels."""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from .models import ColorPipeline, DeliveryPreset

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def intermediate_preset() -> DeliveryPreset:
    return DeliveryPreset(
        id="motion-prores-4444-acescct", name="ProRes 4444 · ACEScct · 12-bit",
        family="调色中间片", container="mov", codec="prores_videotoolbox",
        profile="4", pixel_format="p416le", bit_depth=12, data_range="limited",
        color=ColorPipeline(output="ACEScct"), color_metadata_policy="declared-scene",
    )


def prores_identity() -> str:
    executable = shutil.which("ffmpeg")
    stat = Path(executable).stat() if executable else None
    # OS media frameworks are part of the encoder, not just the FFmpeg binary.
    return hashlib.sha256(repr((
        executable, stat.st_size if stat else None, stat.st_mtime_ns if stat else None,
        platform.platform(),
    )).encode()).hexdigest()


def prores_capability(root: Path) -> dict[str, Any]:
    """Check low two bits in all three components; retain bounded diagnostic logs."""
    try:
        identity = prores_identity()
    except OSError as exc:
        return {"available": False, "reason": f"无法检查本机编码器：{exc}"}
    key = str(root.resolve()) + identity
    with _lock:
        cached = _cache.get(key)
        if cached and (cached[1]["available"] or time.monotonic() - cached[0] < 10):
            return cached[1]
        evidence: dict[str, Any] = {
            "available": False, "identity": identity, "reason": "",
            "backend": "prores_videotoolbox", "hardware_verified": False,
            "transport": "p416le", "tested_precision_bits": 12,
            "tested_dimensions": [64, 32], "tested_frame_rate": "24/1",
            "profile": "4444", "alpha": False, "matrix": "bt709", "range": "limited",
        }
        if platform.system() != "Darwin" or not shutil.which("ffmpeg"):
            evidence["reason"] = "12-bit 4444 需要 macOS Apple VideoToolbox 与 FFmpeg。"
            return evidence
        directory = root / "capabilities" / ("prores4444-" + identity[:16])
        logs = bytearray()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=directory) as temporary:
                target = Path(temporary) / "probe.mov"
                width, height = 64, 32
                values = []
                payload = bytearray()
                for component in range(3):
                    for base in (512, 2048, 3500):
                        for offset in range(4):
                            codes = [2048, 2048, 2048]
                            codes[component] = base + offset
                            values.append(codes)
                            # P416 uses 16-bit words. Preserve all 12 meaningful bits.
                            payload.extend(np.full((height, width), codes[0] << 4, "<u2").tobytes())
                            uv = np.empty((height, width, 2), "<u2")
                            uv[..., 0], uv[..., 1] = codes[1] << 4, codes[2] << 4
                            payload.extend(uv.tobytes())
                args = [
                    "ffmpeg", "-v", "warning", "-nostdin", "-y", "-f", "rawvideo",
                    "-pixel_format", "p416le", "-video_size", f"{width}x{height}",
                    "-framerate", "24", "-color_range", "tv", "-colorspace", "bt709",
                    "-color_primaries", "unknown", "-color_trc", "unknown",
                    "-i", "pipe:0", "-an",
                    "-c:v", "prores_videotoolbox", "-profile:v", "4",
                    "-pix_fmt", "p416le", "-allow_sw", "1",
                    "-color_range", "tv", "-colorspace", "bt709",
                    "-color_primaries", "unknown", "-color_trc", "unknown", str(target),
                ]
                result = subprocess.run(args, input=payload, capture_output=True, timeout=15)
                logs.extend(result.stderr)
                result.check_returncode()
                decoded = subprocess.run([
                    "ffmpeg", "-v", "error", "-xerror", "-nostdin", "-i", str(target),
                    "-an", "-pix_fmt", "yuv444p12le", "-f", "rawvideo", "pipe:1",
                ], capture_output=True, timeout=15)
                logs.extend(decoded.stderr)
                decoded.check_returncode()
                pixels = np.frombuffer(decoded.stdout, "<u2").reshape(len(values), 3, height, width)
                expected = np.asarray(values, dtype=np.int32)[:, :, None, None]
                error = int(np.abs(pixels.astype(np.int32) - expected).max())
                evidence.update(available=error == 0, max_code_error=error,
                                method="36-flat-frames-adjacent-12bit-Y-Cb-Cr",
                                decoded_means=pixels.mean(axis=(2, 3)).tolist())
                if error:
                    evidence["reason"] = "原生编码未保留测试中的相邻 12-bit 码值。"
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            evidence["reason"] = "Apple 12-bit 编码会话验证失败；请检查本机媒体服务后刷新。"
            logs.extend(str(exc).encode())
        try:
            (directory / "probe.log").write_bytes(logs)
            (directory / "probe.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        except OSError as exc:
            # Optional capability diagnostics must not disable existing formats.
            evidence.update(available=False, reason=f"无法保存 12-bit 验证记录：{exc}")
        _cache[key] = time.monotonic(), evidence
        return evidence
