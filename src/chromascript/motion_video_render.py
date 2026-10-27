"""GPU video packets cross Node/Python as bounded binary data, without raw files."""

from __future__ import annotations

import json
import os
import selectors
import struct
import subprocess
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import cast

import numpy as np

from . import motion_render
from .errors import ChromaError
from .images import resize_srgb, write_image
from .motion import FRONTEND, MotionSpec, browser_executable
from .motion_color import VideoOutput, video_shader
from .motion_timing import MotionTimePlan
from .store import canonical
from .video_transport import VideoPixelFormat, video_frame_bytes


def iter_video_frames(
    spec: MotionSpec, directory: Path, indices: list[int], output: str, *,
    render_scale: int = 1, thumbnail: Path | None = None,
    cancelled: Callable[[], bool] | None = None,
    pixel_format: VideoPixelFormat | None = None,
    time_plan: MotionTimePlan | None = None,
) -> Generator[bytes, None, None]:
    if output not in {"sRGB", "PQ", "HLG", "ACEScct"}:
        raise ChromaError("video_output", "GPU 视频出口不支持此色彩编码。")
    contract = video_shader(spec.source_color_space, cast(VideoOutput, output), pixel_format)
    frame_bytes = video_frame_bytes(spec.width, spec.height, contract["pixel_format"])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "transport.json").unlink(missing_ok=True)
    log_path = directory / "renderer.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(["node", str(motion_render.RUNTIME), "video"], cwd=FRONTEND,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
        try:
            assert process.stdin is not None and process.stdout is not None
            stdout_fd = process.stdout.fileno()
            process.stdin.write((canonical({
                "spec": spec.model_dump(), "display": spec.display(), "frames": str(directory),
                "browser": browser_executable(), "frame_indices": indices, "render_scale": render_scale,
                "time_plan": time_plan.model_dump() if time_plan else None,
                "video_capture": contract, "thumbnail": thumbnail is not None,
            }) + "\n").encode())
            process.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + 60 * render_scale**2 * (time_plan.temporal_samples if time_plan else 1)

                def receive(size: int) -> bytes:
                    result = bytearray()
                    while len(result) < size:
                        if cancelled is not None and cancelled():
                            raise ChromaError("cancelled", "GPU 视频渲染已取消。")
                        if time.monotonic() > deadline:
                            raise ChromaError("motion_timeout", "GPU 视频帧处理超时。")
                        if not selector.select(timeout=.25):
                            continue
                        chunk = os.read(stdout_fd, min(size - len(result), 4 * 1024**2))
                        if not chunk:
                            raise ChromaError("motion_render", log_path.read_text(errors="replace")[-6000:] or "GPU 视频流提前结束。")
                        result.extend(chunk)
                    return bytes(result)

                for index in indices:
                    header_size = struct.unpack("<I", receive(4))[0]
                    if not 1 <= header_size <= 4096:
                        raise ChromaError("motion_frame", "GPU 视频帧头长度无效。")
                    header = json.loads(receive(header_size))
                    thumb_bytes = spec.width * spec.height * 4 if thumbnail is not None and index == 0 else 0
                    if header != {"frame": index, "bytes": frame_bytes, "thumbnail_bytes": thumb_bytes}:
                        raise ChromaError("motion_frame", "GPU 视频帧索引或尺寸不符。")
                    pixels = receive(int(frame_bytes))
                    if thumb_bytes:
                        image = np.frombuffer(receive(thumb_bytes), np.uint8).reshape(spec.height, spec.width, 4)
                        # This is already a GPU-rendered, opaque sRGB display copy.
                        # CPU resizing changes only preview size, never applies ACES again.
                        display = np.ascontiguousarray(image[::-1], dtype=np.float32)
                        display /= np.float32(255)
                        assert thumbnail is not None
                        write_image(thumbnail, resize_srgb(display, 1280), space="sRGB", depth=8, disk_reserve_bytes=0)
                    yield pixels
                    process.stdin.write((canonical({"accepted": index}) + "\n").encode())
                    process.stdin.flush()
                    deadline = time.monotonic() + 60 * render_scale**2 * (time_plan.temporal_samples if time_plan else 1)
            process.stdin.close()
            if process.wait(timeout=15):
                raise ChromaError("motion_render", log_path.read_text(errors="replace")[-6000:])
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for stream in (process.stdin, process.stdout):
                if stream is not None and not stream.closed:
                    stream.close()
