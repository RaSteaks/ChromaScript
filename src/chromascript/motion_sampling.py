"""Cancellable content sampling; estimates never save drafts or create render jobs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import psutil

from .color import render_output
from .encoding import _boxes, check_probe, encode
from .errors import ChromaError
from .exports import export_directory as resolve_directory
from .exports import volume_identity
from .images import write_image
from .motion import (
    MotionOutput,
    MotionSpec,
    motion_identity,
    motion_video_preset,
    resolve_motion_output,
)
from .motion_color import decode_float_frame
from .motion_render import iter_float_frames
from .motion_storage import estimate_storage
from .motion_timing import motion_time_plan
from .operations import operation
from .prores import prores_capability, prores_identity
from .store import Store, canonical, identifier, now


def content_identity(spec: MotionSpec, output: MotionOutput) -> str:
    """Name-only edits do not alter compression, but all source/time/color edits do."""
    return hashlib.sha256(canonical({
        "source": spec.source_digest(), "settings": spec.settings().model_dump(),
        "engine": spec.engine, "output": output.model_dump(),
        "runtime": motion_identity(), "encoder": prores_identity(),
    }).encode()).hexdigest()


def sample_indices(frames: int) -> list[int]:
    count = min(12, frames)
    return [0] if count == 1 else [i * (frames - 1) // (count - 1) for i in range(count)]


def movie_measurements(path: Path, count: int, on_process: Any = None) -> dict[str, Any]:
    """Count actual packets and scale sample-table arrays separately from headers."""
    process = subprocess.Popen([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_packets",
        "-show_entries", "packet=size", "-of", "json", str(path),
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if on_process:
        on_process(process)
    try:
        stdout, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise ChromaError("estimate_timeout", "采样视频包检查超时。") from None
    if process.returncode:
        raise ChromaError("estimate_probe", stderr.decode(errors="replace")[-2000:])
    packets = [int(p["size"]) for p in json.loads(stdout)["packets"]]
    if len(packets) != count:
        raise ChromaError("estimate_probe", "采样视频包数不符。")
    tables = 0
    with path.open("rb") as stream:
        def visit(start: int, end: int, depth: int = 0) -> None:
            nonlocal tables
            if depth > 8:
                return
            for kind, payload, stop in _boxes(stream, start, end):
                if kind in {b"moov", b"trak", b"mdia", b"minf", b"stbl"}:
                    visit(payload, stop, depth + 1)
                elif kind in {b"stsz", b"stco", b"co64"}:
                    # These arrays grow with sample/chunk count; fixed box headers
                    # and constant-duration stts stay counted exactly once.
                    tables += max(0, stop - payload - (12 if kind == b"stsz" else 8))
        visit(0, path.stat().st_size)
    return {
        "video_packet_bytes": packets, "video_table_bytes": tables,
        "video_fixed_bytes": path.stat().st_size - sum(packets) - tables,
    }


class SamplingTask:
    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.processes: list[subprocess.Popen[Any]] = []
        self.thread: threading.Thread | None = None

    def register(self, process: subprocess.Popen[Any]) -> None:
        with self.lock:
            self.processes.append(process)
        if self.cancelled.is_set():
            self.cancel()

    def cancel(self) -> None:
        self.cancelled.set()
        # Snapshot ownership before waiting; late registrations see cancelled and
        # terminate themselves. Never hold a registry lock while reaping children.
        with self.lock:
            processes = list(self.processes)
        for process in processes:
            if process.poll() is not None:
                continue
            try:
                children = psutil.Process(process.pid).children(recursive=True)
            except (OSError, psutil.Error):
                children = []
            for child in children:
                try:
                    child.terminate()
                except psutil.Error:
                    pass
            try:
                process.terminate()
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            except OSError:
                pass
            # A child can ignore TERM even after its parent has exited.
            for child in children:
                try:
                    child.kill()
                except psutil.Error:
                    pass
            psutil.wait_procs(children, timeout=1)


_tasks: dict[tuple[str, str], SamplingTask] = {}
_tasks_lock = threading.RLock()


def alive(record: dict[str, Any]) -> bool:
    try:
        return psutil.Process(record["pid"]).create_time() == record["process_started"]
    except psutil.AccessDenied:
        # Inability to inspect another process is not evidence that it is dead.
        return True
    except (KeyError, psutil.Error):
        return False


def cleanup_sample(record: dict[str, Any]) -> None:
    """Only remove this task's marked directory on the original volume."""
    temporary = record.get("temporary")
    if not temporary:
        return
    path = Path(temporary)
    try:
        destination = resolve_directory(record["destination"])
        if (path.parent != destination or not path.name.startswith(".chromascript-sample-")
                or volume_identity(destination) != record["volume"]):
            return
        if (path / "owner").read_text() == record["id"]:
            shutil.rmtree(path)
    except (OSError, ChromaError):
        pass


def recover_samples(store: Store) -> None:
    for record in store.list("motion_estimate"):
        # A live daemon does not imply its individual sampling thread is alive.
        with _tasks_lock:
            task = _tasks.get((str(store.root), record["id"]))
            dead_local = record.get("pid") == os.getpid() and (
                task is None or task.thread is None or not task.thread.is_alive()
            )
        if record["status"] == "running" and (dead_local or not alive(record)):
            store.update("motion_estimate", record["id"], lambda r: {
                **r, "status": "interrupted", "error": "服务中断，请重新采样。",
            })
            cleanup_sample(record)


def close_samples(store: Store) -> None:
    with _tasks_lock:
        tasks = [task for (root, _), task in _tasks.items() if root == str(store.root)]
    for task in tasks:
        task.cancel()
    for task in tasks:
        if task.thread:
            task.thread.join(timeout=5)


def run_sample(store: Store, estimate_id: str, task: SamplingTask) -> None:
    try:
        _run_sample(store, estimate_id, task)
    except Exception as exc:
        error = str(exc)
        try:
            store.update("motion_estimate", estimate_id, lambda r: {
                **r, "status": "failed", "error": error, "finished_at": now(),
            })
        except Exception:
            # Recovery detects the missing local thread once storage is writable.
            pass
    finally:
        with _tasks_lock:
            _tasks.pop((str(store.root), estimate_id), None)


def _run_sample(store: Store, estimate_id: str, task: SamplingTask) -> None:
    record = store.get("motion_estimate", estimate_id)
    spec = MotionSpec.model_validate(record["spec"])
    output = MotionOutput.model_validate(record["output"])
    time_plan = motion_time_plan(spec.settings(), output)
    destination = Path(record["destination"])
    logs = store.root / "estimates" / estimate_id
    temporary: Path | None = None
    successful = False
    finished = threading.Event()
    timed_out = threading.Event()

    def watch() -> None:
        deadline = time.monotonic() + 600
        while not finished.wait(0.25):
            if time.monotonic() > deadline:
                timed_out.set()
                task.cancel()
                return
            if store.get("motion_estimate", estimate_id).get("cancel_requested"):
                task.cancel()
                return

    watchdog = threading.Thread(target=watch, daemon=True)

    def check() -> None:
        if task.cancelled.is_set():
            raise ChromaError("cancelled", "采样估算已取消。")
        if volume_identity(resolve_directory(str(destination))) != record["volume"]:
            raise ChromaError("export_volume_changed", "采样目标磁盘已断开或替换。")

    try:
        logs.mkdir(parents=True, exist_ok=True)
        watchdog.start()
        check()
        destination.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".chromascript-sample-", dir=destination))
        (temporary / "owner").write_text(estimate_id)
        # Retain local ownership even if persisting the path fails (e.g. full
        # workspace disk), so finally can still remove our disposable samples.
        record = {**record, "temporary": str(temporary)}
        store.update("motion_estimate", estimate_id, lambda r: {**r, "temporary": str(temporary)})
        indices = sample_indices(time_plan.frames)
        measured: dict[str, Any] = {
            "sample_count": len(indices), "frame_indices": indices,
            # animation.json and the separate verbatim source graphs are both
            # retained. Report/snapshot/log sizes stay explicitly unmeasured.
            "source_metadata_bytes": len(json.dumps(spec.model_dump(), ensure_ascii=False, indent=2).encode()) + sum(
                len(graph.code.encode()) + sum(len(code.encode()) for code in graph.files.values())
                for graph in [spec, *spec.preserved_layers]
            ),
        }
        if output.master_bit_depth:
            measured["master_bytes"] = []
        preset = motion_video_preset(output.video) if output.video.startswith("prores-") else None

        def frames() -> Any:
            with closing(iter_float_frames(
                spec, temporary, indices, render_scale=output.render_scale, time_plan=time_plan,
                cancelled=task.cancelled.is_set, on_process=task.register,
            )) as iterator:
                for step, (index, raw) in enumerate(iterator):
                    check()
                    pixels = decode_float_frame(
                        raw.read_bytes(), spec.width, spec.height, output.render_scale, spec.source_color_space
                    )
                    if output.master_bit_depth:
                        image = temporary / "sample.exr"
                        write_image(image, pixels, depth=output.master_bit_depth,
                                    fps=(time_plan.fps.numerator, time_plan.fps.denominator), frame_index=index,
                                    disk_reserve_bytes=0)
                        measured["master_bytes"].append(image.stat().st_size)
                        image.unlink()
                        # Match master+video jobs: the movie consumes the selected
                        # stored precision, not an invented pristine float source.
                        if output.master_bit_depth == 16:
                            pixels = pixels.astype("float16").astype("float32")
                    def update_progress(r: dict[str, Any], completed: int = step + 1) -> dict[str, Any]:
                        return {**r, "completed_frames": completed, "sample_count": len(indices)}
                    store.update("motion_estimate", estimate_id, update_progress)
                    if preset:
                        yield render_output(pixels, preset.color.output, False)

        if preset:
            movie = temporary / "sample.mov"
            settings = time_plan.settings(spec.settings()).model_copy(update={"frames": len(indices)})
            with closing(frames()) as iterator:
                info = encode(preset, settings, movie, iterator, lambda _: check(), on_process=task.register)
            verification = check_probe(info, preset, settings)
            if not verification["passed"]:
                raise ChromaError("estimate_verification", "采样视频验证失败。", issues=verification["issues"])
            measured.update(movie_measurements(movie, len(indices), task.register))
        else:
            for _ in frames():
                pass
        check()
        store.update("motion_estimate", estimate_id, lambda r: {
            **r, "measurements": measured,
        })
        successful = True
    except Exception as exc:
        error_message = str(exc)
        store.update("motion_estimate", estimate_id, lambda r: {
            **r, "status": "cancelled" if task.cancelled.is_set() and not timed_out.is_set() else "failed",
            "error": "采样超过 10 分钟，请降低采样规格后重试。" if timed_out.is_set() else error_message,
            "finished_at": now(),
        })
    finally:
        finished.set()
        if watchdog.ident is not None:
            watchdog.join(timeout=1)
        if temporary:
            # Keep diagnostics, not large samples; do not follow a replaced volume.
            try:
                if volume_identity(destination) == record["volume"]:
                    for path in temporary.glob("*.log"):
                        shutil.copyfile(path, logs / path.name)
            except (OSError, ChromaError):
                pass
        cleanup_sample(record)
        if successful:
            # Only expose a reusable estimate after its sample files have been
            # removed, otherwise an immediate submission counts their disk twice.
            store.update("motion_estimate", estimate_id, lambda r: {
                **r, "status": "cancelled" if task.cancelled.is_set() else "completed",
                "finished_at": now(),
            })
        with _tasks_lock:
            _tasks.pop((str(store.root), estimate_id), None)


class MotionSamplingOperations:
    store: Store

    @operation("Start cancellable full-resolution sampling of up to 12 float draft frames. EXR/ProRes mean sizes estimate storage without percentage reserves. No revisions or render jobs are created; name-only changes preserve validity.")
    def start_motion_render_estimate(
        self, project_id: str, spec: MotionSpec, output: MotionOutput,
        export_directory: str, idempotency_key: str,
    ) -> dict[str, Any]:
        self.store.get("project", project_id)
        output = resolve_motion_output(spec.engine, output)
        if spec.engine != "three-float-v1" or not (output.master_bit_depth or output.video.startswith("prores-")):
            raise ChromaError("estimate_unsupported", "采样估算支持浮点 EXR 和 ProRes；其他视频使用规格估算。")
        if not 1 <= len(idempotency_key) <= 200:
            raise ChromaError("idempotency_key", "提供 1–200 字符的请求标识。")
        if output.video == "prores-4444-acescct" and not prores_capability(self.store.root)["available"]:
            raise ChromaError("motion_encoder", "本机未通过 12-bit ProRes 4444 验证。")
        required = spec.width * spec.height * ((136 if output.temporal_samples > 1 else 96) * output.render_scale**2 + 96)
        if required > psutil.virtual_memory().available * 0.7:
            raise ChromaError("motion_memory", "当前内存不足以按正式规格采样。")
        destination = resolve_directory(export_directory)
        identity = content_identity(spec, output)
        request_hash = hashlib.sha256(canonical([project_id, identity, str(destination)]).encode()).hexdigest()
        estimate_id = identifier()
        process = psutil.Process()
        record: dict[str, Any] = {
            "id": estimate_id, "project_id": project_id, "identity": identity,
            "spec": spec.model_dump(), "output": output.model_dump(),
            "destination": str(destination), "volume": volume_identity(destination),
            "status": "running", "created_at": now(), "completed_frames": 0,
            "sample_count": min(12, motion_time_plan(spec.settings(), output).frames), "request_key": idempotency_key,
            "request_hash": request_hash, "pid": process.pid, "process_started": process.create_time(),
        }
        # One workspace lease spans discovery and publication across all project
        # databases, including legacy global records. Keep the local task lock
        # until its thread exists so recovery cannot interrupt a new reservation.
        with _tasks_lock, (self.store.root / 'sampling.lock').open('a') as lease:
            fcntl.flock(lease, fcntl.LOCK_EX)
            recover_samples(self.store)
            estimates = self.store.list('motion_estimate')
            prior = next((r for r in estimates if r.get('request_key') == idempotency_key), None)
            if prior:
                if prior["request_hash"] != request_hash:
                    raise ChromaError("idempotency_conflict", "采样请求标识已用于不同内容。")
                return {"estimate": self._sample_public(prior)}
            if any(r.get('status') == 'running' for r in estimates):
                raise ChromaError("estimate_busy", "已有采样任务运行中，请等待或取消后重试。")
            # Store routes the authoritative sampling record to its owning project;
            # backup/restore and project busy checks now see the same reservation.
            self.store.put('motion_estimate', estimate_id, record)
            task = SamplingTask()
            thread = threading.Thread(target=run_sample, args=(self.store, estimate_id, task), daemon=True)
            task.thread = thread
            _tasks[str(self.store.root), estimate_id] = task
            try:
                thread.start()
            except Exception as exc:
                error = str(exc)
                _tasks.pop((str(self.store.root), estimate_id), None)
                self.store.update("motion_estimate", estimate_id, lambda r: {
                    **r, "status": "failed", "error": error, "finished_at": now(),
                })
                raise
        return {"estimate": self._sample_public(record)}

    def _sample_public(self, record: dict[str, Any]) -> dict[str, Any]:
        public: dict[str, Any] = {k: record[k] for k in (
            "id", "project_id", "identity", "status", "sample_count", "completed_frames",
            "created_at", "finished_at", "error",
        ) if k in record}
        if record["status"] == "completed":
            spec, output = MotionSpec.model_validate(record["spec"]), MotionOutput.model_validate(record["output"])
            destination = resolve_directory(record["destination"])
            if volume_identity(destination) != record["volume"]:
                raise ChromaError("estimate_stale", "采样目标磁盘已改变，请重新采样。")
            public["budget"] = estimate_storage(
                self.store, spec.settings(), spec.engine, output, destination, record["project_id"], record["measurements"]
            )
        return public

    @operation("Read sampling progress or refresh disk/audio estimates without rendering again.", serialized=False)
    def get_motion_render_estimate(self, estimate_id: str) -> dict[str, Any]:
        recover_samples(self.store)
        return {"estimate": self._sample_public(self.store.get("motion_estimate", estimate_id))}

    @operation("Cancel the identified sample, terminate owned processes with bounded TERM/KILL escalation, and clean disposable files.", serialized=False)
    def cancel_motion_render_estimate(self, estimate_id: str) -> dict[str, Any]:
        record = self.store.get("motion_estimate", estimate_id)
        if record["status"] == "running":
            record = self.store.update("motion_estimate", estimate_id, lambda r: {**r, "cancel_requested": True})
        with _tasks_lock:
            task = _tasks.get((str(self.store.root), estimate_id))
        if task:
            task.cancel()
        return {"estimate": self._sample_public(record)}

def checked_storage_sample(
    store: Store, estimate_id: str, spec: MotionSpec, output: MotionOutput,
    destination: Path, project_id: str,
) -> dict[str, Any]:
    record = store.get("motion_estimate", estimate_id)
    if (record["status"] != "completed" or record["project_id"] != project_id
            or record["identity"] != content_identity(spec, output)
            or record["destination"] != str(destination)
            or record["volume"] != volume_identity(destination)):
        raise ChromaError("estimate_stale", "采样结果与当前源码、输出或磁盘不一致，请重新采样。")
    return record
