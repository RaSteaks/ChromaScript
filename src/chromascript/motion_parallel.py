"""Bounded parallel frame production, ordered into one exact-timebase encoder."""

from __future__ import annotations

import json
import multiprocessing
import time
from collections.abc import Callable, Generator
from contextlib import ExitStack, closing
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from multiprocessing.shared_memory import SharedMemory
from multiprocessing.synchronize import Event
from pathlib import Path
from typing import Any

import numpy as np
import psutil

from . import motion_render
from .color import render_output, transform
from .errors import ChromaError
from .exports import RenderBundle
from .images import read_image, resize_srgb, write_image
from .motion import MotionOutput, MotionSpec, motion_video_preset
from .motion_processing import PreparedFrame, prepare_float_frame
from .store import canonical
from .video_transport import VideoPixelFormat, video_frame_bytes


def iter_parallel_frames(
    spec: MotionSpec, output: MotionOutput, directory: Path, indices: list[int], *,
    config_hash: str, masters: Path | None = None, missing: list[int] | None = None,
    expected_gpu: dict[str, Any] | None = None, bundle: RenderBundle | None = None,
    thumbnail: Path | None = None, cancelled: Callable[[], bool] | None = None,
    video_input: VideoPixelFormat | None = None,
) -> Generator[PreparedFrame, None, None]:
    """Pixels occupy a shared slot valid until the consumer advances the iterator.

    Frame-sized work units avoid waiting for an entire distant video segment.
    Workers retain independent scene state and use absolute frame/time indices.
    Only the parent writes recovery records and feeds the single video encoder.
    """
    if not indices:
        return
    count = min(output.render_workers, len(indices))
    context = multiprocessing.get_context("spawn")
    stop = context.Event()
    processes: list[BaseProcess] = []
    connections: list[Connection] = []
    slots: list[SharedMemory] = []
    arrays: list[np.ndarray] = []
    started = time.monotonic()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "transport.json").unlink(missing_ok=True)
    needed = set(indices if missing is None else missing)
    try:
        for worker in range(count):
            assigned = indices[worker::count]
            size = video_frame_bytes(spec.width, spec.height, video_input) if video_input else spec.width * spec.height * 16
            slot = SharedMemory(create=True, size=size)
            slots.append(slot)
            arrays.append(np.ndarray((size,), np.uint8, buffer=slot.buf) if video_input else
                          np.ndarray((spec.height, spec.width, 4), np.float32, buffer=slot.buf))
            parent, child = context.Pipe()
            connections.append(parent)
            process: BaseProcess = context.Process(target=_produce, name=f"chroma-frame-{worker}", args=(
                child, stop, slot.name, spec.model_dump(), output.model_dump(), assigned,
                [index for index in assigned if index in needed], directory / f"parallel-{worker}",
                masters, config_hash, expected_gpu, bundle, thumbnail, motion_render.RUNTIME, video_input,
            ))
            try:
                process.start()
            finally:
                child.close()
            processes.append(process)
        gpu = expected_gpu
        # Verify every device before allowing any child to write reusable masters.
        for connection, process in zip(connections, processes, strict=True):
            message = _receive(connection, process, cancelled, output.render_scale)
            if message["kind"] != "gpu":
                raise ChromaError("motion_parallel", "并行渲染器未返回 GPU 身份。")
            if gpu is not None and message["gpu"] != gpu:
                raise ChromaError("gpu_changed", "并行工作进程的 GPU 身份不一致，请使用单路渲染。")
            gpu = message["gpu"]
        for name in (["gpu.json", "gpu-verified.json"] if masters is not None else ["gpu.json"]):
            path = directory / name
            partial = path.with_suffix(".partial")
            partial.write_text(canonical(gpu))
            partial.replace(path)
        for connection in connections:
            connection.send("gpu-accepted")
        for position, index in enumerate(indices):
            owner = position % count
            message = _receive(connections[owner], processes[owner], cancelled, output.render_scale)
            if message["kind"] != "frame" or message["index"] != index:
                raise ChromaError("motion_frames", "并行渲染器返回了无效帧顺序。")
            yield PreparedFrame(index, arrays[owner], message["record"], message["seconds"])
            # Backpressure covers the encoder/master consumer, not only IPC.
            connections[owner].send("next")
        timings = []
        for connection, process in zip(connections, processes, strict=True):
            message = _receive(connection, process, cancelled, output.render_scale)
            if message["kind"] != "done":
                raise ChromaError("motion_frames", "并行渲染未完整结束。")
            timings.append(message["transport"])
        (directory / "transport.json").write_text(canonical({
            "frames": len(indices), "workers": count,
            "bytes": sum(item.get("bytes", 0) for item in timings),
            "total_ms": (time.monotonic() - started) * 1000,
            "worker_timings": timings, "ordered_single_encoder": True,
        }))
    finally:
        stop.set()
        for connection in connections:
            try:
                connection.send("stop")
            except (OSError, EOFError):
                pass  # A completed/failed producer may already have closed its pipe.
            connection.close()
        for process in processes:
            process.join(timeout=3)
            if process.is_alive():
                # Cancellation must also reap a stalled producer's Node/browser tree.
                try:
                    children = psutil.Process(process.pid).children(recursive=True)
                except psutil.Error:
                    children = []
                for child in children:
                    try:
                        child.terminate()
                    except psutil.Error:
                        pass
                process.terminate()
                process.join(timeout=2)
                if process.is_alive():
                    process.kill()
                    process.join()
                _, alive = psutil.wait_procs(children, timeout=2)
                for child in alive:
                    try:
                        child.kill()
                    except psutil.Error:
                        pass
            process.close()
        arrays.clear()
        for slot in slots:
            slot.close()
            slot.unlink()
        for worker in range(count):
            for raw in (directory / f"parallel-{worker}").glob("frame-*.f32"):
                raw.unlink(missing_ok=True)


def _receive(connection: Connection, process: BaseProcess, cancelled: Callable[[], bool] | None,
             render_scale: int) -> dict[str, Any]:
    deadline = time.monotonic() + 60 * render_scale**2
    while time.monotonic() < deadline:
        if cancelled is not None and cancelled():
            raise ChromaError("cancelled", "并行渲染已取消。")
        if connection.poll(0.25):
            try:
                message: dict[str, Any] = connection.recv()
            except EOFError:
                raise ChromaError("motion_parallel", "并行渲染进程提前退出。") from None
            if message["kind"] == "error":
                error = message["error"]
                raise ChromaError(error["code"], error["message"], **error.get("details", {}))
            return message
        if not process.is_alive():
            raise ChromaError("motion_parallel", "并行渲染进程提前退出。", exit_code=process.exitcode)
    raise ChromaError("motion_timeout", "并行帧处理超时，请减少并发路数或采样后重试。")


def _produce(
    connection: Connection, stop: Event, slot_name: str, scene: dict[str, Any],
    recipe: dict[str, Any], indices: list[int], missing: list[int], directory: Path,
    masters: Path | None, config_hash: str, expected_gpu: dict[str, Any] | None,
    bundle: RenderBundle | None, thumbnail: Path | None, runtime: Path,
    video_input: VideoPixelFormat | None,
) -> None:
    try:
        spec, output = MotionSpec.model_validate(scene), MotionOutput.model_validate(recipe)
        # Test drivers may supply WebKit; production inherits its frozen runtime path.
        motion_render.RUNTIME = runtime
        if video_input is not None:
            _produce_video(connection, stop, slot_name, spec, output, indices, directory,
                           bundle, thumbnail, video_input)
            return
        with ExitStack() as stack:
            shared = stack.enter_context(closing(SharedMemory(name=slot_name)))
            pixels = np.ndarray((spec.height, spec.width, 4), np.float32, buffer=shared.buf)
            frames = stack.enter_context(closing(motion_render.iter_float_frames(
                spec, directory, missing, expected_gpu=expected_gpu, render_scale=output.render_scale,
                cancelled=stop.is_set,
            ))) if missing else None
            first = next(frames) if frames is not None else None
            gpu = json.loads((directory / "gpu.json").read_text()) if frames is not None else expected_gpu
            connection.send({"kind": "gpu", "gpu": gpu})
            if connection.recv() != "gpu-accepted":
                return
            needed = set(missing)
            color = motion_video_preset(output.video).color.output if output.video != "none" else None
            for index in indices:
                if stop.is_set():
                    return
                if bundle is not None:
                    bundle.check_volume()
                master = masters / f"{index:08d}.exr" if masters is not None else None
                if index in needed:
                    assert frames is not None
                    frame, raw = first if first is not None else next(frames)
                    first = None
                    if frame != index:
                        raise ChromaError("motion_frames", "并行渲染帧索引不符。")
                    prepared = prepare_float_frame(index, raw, spec, output, config_hash, master)
                else:
                    assert master is not None
                    prepared = PreparedFrame(index, read_image(master)[0], None, {})
                if index == 0 and thumbnail is not None:
                    write_image(thumbnail, resize_srgb(transform(prepared.pixels, "sRGB"), 1280),
                                space="sRGB", depth=8, disk_reserve_bytes=0)
                start = time.monotonic()
                displayed = render_output(prepared.pixels, color, False) if color is not None else prepared.pixels
                prepared.seconds["output_transform"] = time.monotonic() - start
                np.copyto(pixels, displayed)
                connection.send({"kind": "frame", "index": index,
                                 "record": prepared.record, "seconds": prepared.seconds})
                if connection.recv() != "next":
                    return
            if frames is not None:
                next(frames, None)  # Last ACK and renderer exit/GPU verification.
            transport = json.loads((directory / "transport.json").read_text()) if frames is not None else {}
            connection.send({"kind": "done", "transport": transport})
    except Exception as exc:
        error = exc.as_dict() if isinstance(exc, ChromaError) else {
            "code": "motion_parallel", "message": str(exc) or type(exc).__name__, "details": {},
        }
        try:
            connection.send({"kind": "error", "error": error})
        except (OSError, EOFError):
            pass  # Parent cancellation already owns cleanup and reporting.
    finally:
        connection.close()


def _produce_video(connection: Connection, stop: Event, slot_name: str, spec: MotionSpec,
                   output: MotionOutput, indices: list[int], directory: Path,
                   bundle: RenderBundle | None, thumbnail: Path | None, video_input: VideoPixelFormat) -> None:
    from .motion_video_render import iter_video_frames

    with closing(SharedMemory(name=slot_name)) as shared, closing(iter_video_frames(
        spec, directory, indices, motion_video_preset(output.video).color.output,
        render_scale=output.render_scale, thumbnail=thumbnail, cancelled=stop.is_set, pixel_format=video_input,
    )) as frames:
        pixels = np.ndarray((video_frame_bytes(spec.width, spec.height, video_input),), np.uint8, buffer=shared.buf)
        first = next(frames)
        connection.send({"kind": "gpu", "gpu": json.loads((directory / "gpu.json").read_text())})
        if connection.recv() != "gpu-accepted":
            return
        for position, index in enumerate(indices):
            if stop.is_set():
                return
            if bundle is not None:
                bundle.check_volume()
            data = first if position == 0 else next(frames)
            np.copyto(pixels, np.frombuffer(data, np.uint8))
            connection.send({"kind": "frame", "index": index, "record": None, "seconds": {}})
            if connection.recv() != "next":
                return
        next(frames, None)
        connection.send({"kind": "done", "transport": json.loads((directory / "transport.json").read_text())})
