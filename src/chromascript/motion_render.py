"""Offline float browser execution with bounded progress and no source evaluation in Node."""

from __future__ import annotations

import json
import selectors
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .errors import ChromaError
from .motion import FRONTEND, RUNTIME, MotionSpec, browser_executable
from .motion_color import display_shader
from .store import canonical


def render_float_frames(
    spec: MotionSpec,
    directory: Path,
    indices: list[int],
    on_frame: Callable[[int, Path], None],
    max_dimension: int | None = None,
    expected_gpu: dict[str, Any] | None = None,
) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
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
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(
                canonical(
                    {
                        "spec": spec.model_dump(),
                        "display": display_shader(),
                        "frames": str(directory),
                        "max_dimension": max_dimension,
                        "browser": browser_executable(),
                        "frame_indices": indices,
                    }
                )
            )
            process.stdin.close()
            received: set[int] = set()
            # A stalled shader/source must not occupy a worker forever. Successful
            # frame progress resets the deadline; total sequence length is unrestricted.
            deadline = time.monotonic() + 60
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    if time.monotonic() > deadline:
                        raise ChromaError(
                            "motion_timeout", "单帧浮点渲染超过 60 秒，请简化场景后重试。"
                        )
                    if not selector.select(timeout=0.5):
                        continue
                    line = process.stdout.readline()
                    if not line:
                        break
                    event = json.loads(line)
                    frame = event.get("frame")
                    if type(frame) is not int or frame not in indices or frame in received:
                        raise ChromaError("motion_frames", "浮点渲染器返回了无效帧索引。")
                    # A resumed sequence must not mix frames from different GPU
                    # backends even if the browser binary and source are unchanged.
                    if (
                        expected_gpu is not None
                        and json.loads((directory / "gpu.json").read_text()) != expected_gpu
                    ):
                        raise ChromaError("gpu_changed", "GPU 后端已改变，请重新提交任务。")
                    on_frame(frame, directory / f"frame-{frame}.f32")
                    received.add(frame)
                    deadline = time.monotonic() + 60
            if process.wait(timeout=10) or received != set(indices):
                raise ChromaError("motion_render", log_path.read_text()[-6000:] or "浮点帧不完整。")
            probe = json.loads((directory / "gpu.json").read_text())
            return probe
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
