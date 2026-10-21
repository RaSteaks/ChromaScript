"""Offline float browser execution with bounded progress and no source evaluation in Node."""

from __future__ import annotations

import json
import selectors
import subprocess
import time
from collections.abc import Generator
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from .errors import ChromaError
from .motion import FRONTEND, RUNTIME, MotionSpec, browser_executable
from .store import canonical


def iter_float_frames(
    spec: MotionSpec,
    directory: Path,
    indices: list[int],
    max_dimension: int | None = None,
    expected_gpu: dict[str, Any] | None = None,
    render_scale: int = 1,
    cancelled: Callable[[], bool] | None = None,
    on_process: Callable[[subprocess.Popen[Any]], None] | None = None,
) -> Generator[tuple[int, Path], None, None]:
    directory.mkdir(parents=True, exist_ok=True)
    # Interrupted transport is disposable, unlike the independently verified EXRs.
    for raw in directory.glob("frame-*.f32"):
        raw.unlink()
    log_path = directory / "renderer.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            ["node", str(RUNTIME), "frames"],
            cwd=FRONTEND,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
        )
        if on_process is not None:
            on_process(process)
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(
                canonical(
                    {
                        "spec": spec.model_dump(),
                        "display": spec.display(),
                        "frames": str(directory),
                        "max_dimension": max_dimension,
                        "browser": browser_executable(),
                        "frame_indices": indices,
                        "render_scale": render_scale,
                    }
                ) + "\n"
            )
            process.stdin.flush()
            received: set[int] = set()
            # A stalled shader/source must not occupy a worker forever. Successful
            # frame progress resets the deadline; total sequence length is unrestricted.
            frame_timeout = 60 * render_scale**2
            deadline = time.monotonic() + frame_timeout
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    if cancelled is not None and cancelled():
                        raise ChromaError("cancelled", "采样估算已取消。")
                    if time.monotonic() > deadline:
                        raise ChromaError(
                                "motion_timeout", f"单帧浮点渲染超过 {frame_timeout} 秒，请降低采样或简化场景后重试。"
                        )
                    if not selector.select(timeout=0.5):
                        continue
                    line = process.stdout.readline()
                    if not line:
                        break
                    event = json.loads(line)
                    frame = event.get("frame")
                    # Exact order is required by direct video. Index lookup also
                    # avoids a linear membership scan for every frame of long clips.
                    if (type(frame) is not int or len(received) >= len(indices)
                            or frame != indices[len(received)] or frame in received):
                        raise ChromaError("motion_frames", "浮点渲染器返回了无效帧索引。")
                    # A resumed sequence must not mix frames from different GPU
                    # backends even if the browser binary and source are unchanged.
                    if (
                        expected_gpu is not None
                        and json.loads((directory / "gpu.json").read_text()) != expected_gpu
                    ):
                        raise ChromaError("gpu_changed", "GPU 后端已改变，请重新提交任务。")
                    raw = directory / f"frame-{frame}.f32"
                    # Yield keeps Node waiting for ACK until EXR verification or
                    # direct video encoding consumes the frame. Close also removes
                    # the current transport on cancellation/encoder failure.
                    try:
                        yield frame, raw
                    finally:
                        raw.unlink(missing_ok=True)
                    # Backpressure covers the consumer (EXR verification or video
                    # encoding), not just Node's file write or pipe buffering.
                    process.stdin.write(canonical({"accepted": frame}) + "\n")
                    process.stdin.flush()
                    received.add(frame)
                    deadline = time.monotonic() + frame_timeout
            process.stdin.close()
            if process.wait(timeout=10) or received != set(indices):
                raise ChromaError("motion_render", log_path.read_text()[-6000:] or "浮点帧不完整。")

        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for pipe in (process.stdin, process.stdout):
                if pipe is not None and not pipe.closed:
                    pipe.close()
            # A failed capture may leave a partial transport before yielding it.
            for raw in directory.glob("frame-*.f32"):
                raw.unlink(missing_ok=True)


def render_float_frames(
    spec: MotionSpec,
    directory: Path,
    indices: list[int],
    on_frame: Callable[[int, Path], None],
    max_dimension: int | None = None,
    expected_gpu: dict[str, Any] | None = None,
    render_scale: int = 1,
) -> dict[str, Any]:
    # Keep the callback API for previews and resumable EXRs; streaming video
    # consumes the same verified transport lazily without an extra render pass.
    with closing(iter_float_frames(
        spec, directory, indices, max_dimension, expected_gpu, render_scale
    )) as frames:
        for index, raw in frames:
            on_frame(index, raw)
    return json.loads((directory / "gpu.json").read_text())
