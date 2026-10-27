"""Bounded output review through the export renderer, without jobs or revisions."""

import base64
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

import numpy as np
import psutil

from .color import render_output, transform
from .errors import ChromaError
from .images import resize_srgb, write_image
from .motion import FRONTEND, SDR_RUNTIME, MotionOutput, MotionSpec, browser_executable
from .motion_color import decode_float_frame
from .motion_render import iter_float_frames
from .motion_sdr_worker import browser_pixels
from .motion_timing import motion_time_plan
from .store import canonical


def render_output_preview(spec: MotionSpec, output: MotionOutput, start: int, count: int) -> dict[str, Any]:
    plan = motion_time_plan(spec.settings(), output)
    if not 0 <= start < plan.frames or not 1 <= count <= 60:
        raise ChromaError("frame_bounds", "选择有效起始帧；输出预览每次支持 1–60 帧。")
    required = spec.width * spec.height * (output.render_scale**2 * (136 if output.temporal_samples > 1 else 96) + 96)
    if required > psutil.virtual_memory().available * 0.7:
        raise ChromaError("motion_memory", "输出预览按正式规格采样，当前内存不足；请降低空间采样或尺寸。")
    indices = list(range(start, min(plan.frames, start + count)))
    images: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="chromascript-output-preview-") as temporary:
        directory = Path(temporary)

        def accept(frame: int, pixels: np.ndarray) -> None:
            # Evaluate at the exact export raster/sampling, then shrink the display
            # copy only. HDR review is explicitly sRGB, never mislabelled HDR video.
            preview = resize_srgb(pixels, 480)
            path = directory / "preview.png"
            write_image(path, preview, space="sRGB", depth=8)
            images.append({"frame": frame, "mime_type": "image/png", "color_space": "sRGB",
                           "width": preview.shape[1], "height": preview.shape[0], "bit_depth": 8,
                           "data": base64.b64encode(path.read_bytes()).decode()})

        if spec.engine == "three-float-v1":
            with closing(iter_float_frames(spec, directory, indices, render_scale=output.render_scale,
                                           time_plan=plan)) as frames:
                for frame, raw in frames:
                    pixels = decode_float_frame(raw.read_bytes(), spec.width, spec.height,
                                                output.render_scale, spec.source_color_space)
                    if output.master_bit_depth == 16:
                        pixels = pixels.astype("float16").astype("float32")
                    accept(frame, render_output(pixels, "sRGB", False) if output.video != "none"
                           else transform(pixels, "sRGB"))
        else:
            try:
                result = subprocess.run(
                    ["node", str(SDR_RUNTIME), "frames"], cwd=FRONTEND,
                    input=canonical({"spec": spec.model_dump(), "time_plan": plan.model_dump(),
                                     "directory": str(directory), "frames": str(directory / "frames"),
                                     "browser": browser_executable(), "scale": output.render_scale,
                                     "python": sys.executable, "frame_indices": indices}),
                    capture_output=True, text=True,
                    timeout=60 * len(indices) * output.temporal_samples * output.render_scale**2,
                )
            except subprocess.TimeoutExpired as exc:
                raise ChromaError("motion_timeout", "输出预览超时，请减少预览帧数或采样。") from exc
            if result.returncode:
                raise ChromaError("motion_render", result.stderr[-6000:])
            for frame in indices:
                accept(frame, browser_pixels(directory / "frames" / f"frame-{frame}.png", spec, output.render_scale))
    return {"transient": True, "time_plan": plan.model_dump(), "images": images,
            "source_duration": spec.settings().duration,
            "output_duration": plan.settings(spec.settings()).duration,
            "display": "sRGB review; export sampling before codec compression"}
