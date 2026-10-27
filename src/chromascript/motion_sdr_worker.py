"""Offline browser screenshots -> exact rational-rate video, separate from ACES masters."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .artifacts import register_delivery_archive
from .audio import mix_audio
from .color import transform
from .encoding import check_probe, encode
from .errors import ChromaError
from .exports import DESTINATION_LAYOUT, RenderBundle, bundle_name, motion_bundle, publish_bundle
from .images import file_hash, resize_srgb, write_image
from .models import Artifact
from .motion import (
    FRONTEND,
    MotionOutput,
    MotionSpec,
    browser_executable,
    motion_identity,
    motion_video_preset,
)
from .motion import SDR_RUNTIME as RUNTIME
from .motion_sources import source_hash, write_sources
from .motion_timing import motion_time_plan
from .motion_video_plan import VideoExecution
from .store import Store, canonical, now
from .subtitles import write_subtitles


def browser_pixels(path: Path, spec: MotionSpec, render_scale: int = 1) -> np.ndarray:
    with Image.open(path) as image:
        if image.size != (spec.width * render_scale, spec.height * render_scale):
            raise ChromaError("motion_frame", "浏览器帧的实际尺寸与动画规格不一致。")
        pixels = np.asarray(image.convert("RGBA"), dtype=np.float32) / 255.0
    # SDR video outputs are opaque. Composite the display-referred source on black in
    # linear sRGB light; never apply an ACES view to an already-rendered screenshot.
    rgb = pixels[..., :3]
    linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    linear *= pixels[..., 3:4]
    if render_scale > 1:
        # Integrate in display-linear light, after alpha association. Supersampling
        # improves edges but does not turn the 8-bit screenshot source into HDR.
        linear = linear.reshape(spec.height, render_scale, spec.width, render_scale, 3).mean(axis=(1, 3))
        pixels = np.ones((spec.height, spec.width, 4), dtype=np.float32)
    pixels[..., :3] = np.where(
        linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055
    )
    pixels[..., 3] = 1
    return pixels


def run_sdr_motion(root: Path, job_id: str) -> None:
    store = Store(root)
    job = store.get("job", job_id)
    snapshot = store.get("snapshot", job_id)
    start = time.monotonic()
    artifacts = list(job["artifacts"])
    directory = store.file(f"jobs/{job_id}/output")
    direct = snapshot.get("storage_layout") == DESTINATION_LAYOUT
    bundle: RenderBundle | None = None

    def progress(stage: str, amount: float, completed: int | None = None) -> None:
        if bundle is not None:
            bundle.check_volume()
        def update(current: dict[str, Any]) -> dict[str, Any]:
            if current["status"] == "cancelled":
                raise ChromaError("cancelled", "动画渲染已取消。")
            return {
                **current,
                "stage": stage,
                "progress": amount,
                "updated_at": now(),
                "elapsed_seconds": time.monotonic() - start,
                "completed_frames": completed
                if completed is not None
                else current["completed_frames"],
            }

        store.update("job", job_id, update)

    def artifact(path: Path, kind: str, mime: str, metadata: dict[str, Any] | None = None) -> None:
        # Store resolves symlinked macOS temporary/workspace roots once.
        relative = str(path.relative_to(directory)) if direct else store.relative_file(path)
        aid = hashlib.sha256((job_id + relative).encode()).hexdigest()[:32]
        value = Artifact(
            id=aid,
            project_id=job["project_id"],
            job_id=job_id,
            name=path.name,
            kind=kind,
            relative_path=relative,
            storage="delivery" if direct else "workspace",
            mime_type=mime,
            size_bytes=path.stat().st_size,
            created_at=now(),
            metadata=metadata or {},
        )
        store.put("artifact", aid, value.model_dump())
        if aid not in artifacts:
            artifacts.append(aid)
        store.update("job", job_id, lambda current: {**current, "artifacts": artifacts})

    def finish() -> None:
        register_delivery_archive(store, job_id)
        store.update("job", job_id, lambda current: {
            **current,
            "status": "completed" if current["status"] != "cancelled" else "cancelled",
            "stage": "completed" if current["status"] != "cancelled" else "cancelled",
            "progress": 1, "export_completed": current["status"] != "cancelled",
            "elapsed_seconds": time.monotonic() - start, "updated_at": now(),
        })
        if bundle is not None:
            try:
                bundle.discard_transport()
            except (OSError, ChromaError) as exc:
                print(f"Retained disposable render state: {exc}", flush=True)

    try:
        if hashlib.sha256(canonical(snapshot).encode()).hexdigest() != job["snapshot_hash"]:
            raise ChromaError("snapshot_corrupt", "动画快照校验失败。")
        if snapshot["runtime"] != motion_identity():
            raise ChromaError("runtime_changed", "动画运行环境已改变，请重新提交任务。")
        spec = MotionSpec.model_validate(snapshot["scene"])
        # Recovery may trust only the frozen destination, never a mutated job pointer.
        destination = Path(snapshot["export_directory"]) / bundle_name(spec.name, job_id)
        if str(destination) != job.get("export_path"):
            raise ChromaError("snapshot_corrupt", "动画导出位置与冻结快照不一致。")
        output = MotionOutput.model_validate(snapshot["output"])
        time_plan = motion_time_plan(spec.settings(), output)
        settings = time_plan.settings(spec.settings())
        if snapshot.get("time_plan", time_plan.model_dump()) != time_plan.model_dump():
            raise ChromaError("snapshot_corrupt", "输出时间采样计划与冻结快照不一致。")
        if direct:
            bundle = motion_bundle(job, snapshot)
            directory, published = bundle.prepare()
            if published:
                progress("exporting", 0.98, settings.frames)
                finish()
                return
        else:
            directory.mkdir(parents=True, exist_ok=True)
        # Freeze every chapter alongside the entry so exported sources remain reproducible.
        sources = write_sources(directory, spec.engine, spec.code, spec.files)
        description = directory / "animation.json"
        description.write_text(json.dumps(spec.model_dump(), ensure_ascii=False, indent=2))
        audio_path = directory / "audio.wav"
        audio_info = mix_audio(store, snapshot.get("audio"), settings, audio_path)
        if audio_info is not None:
            artifact(audio_path, "audio", "audio/wav", audio_info)

        frozen = directory / "scene-snapshot.json"
        frozen.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
        for path in (*sources, description, frozen):
            artifact(path, "source", "application/json" if path.suffix == ".json" else "text/plain")

        work = bundle.work if bundle is not None else store.file(f"jobs/{job_id}/motion")
        frames = work / "frames"
        frames.mkdir(parents=True, exist_ok=True)
        progress("compiling", 0)
        # Node inherits the worker's process group. Remotion handles SIGTERM and
        # closes its separate Chromium group. Runtime never downloads a browser.
        with (work / "renderer.log").open("w") as log:
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
                            "directory": str(work),
                            "frames": str(frames),
                            "browser": browser_executable(),
                            "scale": output.render_scale,
                            "time_plan": time_plan.model_dump(),
                            "python": sys.executable,
                        }
                    )
                )
                process.stdin.close()
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict) and "completed" in event:
                        count = int(event["completed"])
                        progress("rendering", 0.75 * count / settings.frames, count)
                code = process.wait()
                if code:
                    log.flush()
                    raise ChromaError(
                        "motion_render",
                        (work / "renderer.log").read_text()[-6000:] or "本地浏览器渲染未完成。",
                    )
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()

        # Renderer filenames may have zero padding; verify the actual complete
        # frame-index set instead of trusting a prefix or directory entry count.
        paths: dict[int, Path] = {}
        for path in frames.glob("frame-*.png"):
            match = re.fullmatch(r"frame-(\d+)\.png", path.name)
            if match:
                paths[int(match[1])] = path
        if set(paths) != set(range(settings.frames)):
            raise ChromaError("motion_frames", "浏览器输出帧不完整，请重试任务。")
        thumbnail = directory / "thumbnail.png"
        write_image(
            thumbnail, resize_srgb(browser_pixels(paths[0], spec, output.render_scale), 1280), space="sRGB", depth=8,
            disk_reserve_bytes=0,  # Use the same no-reserve animation disk policy.
        )
        artifact(
            thumbnail,
            "preview-image",
            "image/png",
            {"frame": 0, "color_space": "sRGB", "engine": "remotion", "scene_linear_master": False},
        )

        video_plan = VideoExecution.model_validate(snapshot["video_execution"]) if snapshot.get("video_execution") else None
        preset = video_plan.preset if video_plan is not None else motion_video_preset(output.video)

        # SDR conversion changes display encoding only; never apply an ACES view
        # to a browser screenshot or represent it as a scene-linear HDR master.
        def video_pixels(path: Path) -> np.ndarray:
            pixels = browser_pixels(path, spec, output.render_scale)
            return (
                pixels
                if preset.color.output == "sRGB"
                else transform(pixels, "Gamma 2.4 Encoded Rec.709", source="sRGB")
            )

        # ProRes changes the container, never the screenshot's source precision.
        video = directory / f"animation.{preset.container}"
        encoded_video = work / video.name if bundle is not None else video
        progress("encoding", 0.75)
        info = encode(
            preset,
            settings,
            encoded_video,
            (video_pixels(paths[i]) for i in range(settings.frames)),
            lambda count: progress("encoding", 0.75 + 0.2 * count / settings.frames),
            # Audio is an independent optional input, never part of the pixel stream.
            audio_path=audio_path if audio_info is not None else None,
            speed=video_plan.speed if video_plan is not None else "slow",
        )
        verification = check_probe(info, preset, settings)
        # Padding an 8-bit screenshot into the encoder's 16-bit input pipe adds
        # no precision. File tags declare sRGB; no ACES transform was performed.
        verification["time_plan"] = time_plan.model_dump()
        verification["source_precision"] = "8-bit browser screenshot"
        verification["scene_linear_master"] = False
        verification["float_pipeline_verified"] = False
        verification["source_color_contract"] = {"status": "not_applicable"}
        verification["sampling"] = {"render_scale": output.render_scale, "spatial_samples": output.render_scale**2, "temporal_samples": output.temporal_samples, "shutter_angle": output.shutter_angle}
        verification["color_encoding"] = {
            **verification.get("color_encoding", {}),
            "output_transform": None,
            "source": "browser sRGB",
        }
        if not verification["passed"]:
            raise ChromaError(
                "verification_failed", "实际视频属性未通过验证。", verification=verification
            )
        if encoded_video != video:
            encoded_video.replace(video)
        artifact(
            video,
            "video",
            "video/quicktime" if preset.container == "mov" else "video/mp4",
            {"engine": "remotion", "verification": verification, "source_precision": "8-bit"},
        )
        report = directory / "verification.json"
        report.write_text(
            json.dumps(
                {
                    "engine": "remotion",
                    "verification": verification,
                    "probe": info,
                    "audio": audio_info,
                    "source_sha256": source_hash(spec.code, spec.files),
                    "video_sha256": file_hash(video),
                    "fps": settings.fps.model_dump(),
                    "notes": [
                        "Browser sRGB screenshot source; no ACEScg/HDR/float master",
                        "Transparent source is composited on black in linear sRGB",
                        "Animation time should depend only on frame index; no embedded audio or network assets; use project audio tools",
                        "Retry rerenders browser frames from the frozen source",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        artifact(report, "report", "application/json", {"verification": verification})
        progress("exporting", 0.98)
        files = {str(p.relative_to(directory)): p for p in (*sources, description, frozen, thumbnail, video, report)}
        sidecars = write_subtitles(snapshot.get("subtitles"), directory)
        files.update(sidecars)
        for path in sidecars.values():
            artifact(path, "subtitle", "application/x-subrip")
        if audio_info is not None:
            files["audio.wav"] = audio_path
        if bundle is not None:
            bundle.publish(files, lambda: progress("exporting", 0.98))
        else:
            publish_bundle(files, destination, Path(snapshot["export_root"]),
                {"job_id": job_id, "snapshot_sha256": job["snapshot_hash"]},
                lambda: progress("exporting", 0.98))
        finish()
        print(f"Completed browser animation {job_id}", flush=True)
    except BaseException as exc:
        traceback.print_exc()
        error = (
            exc.as_dict()
            if isinstance(exc, ChromaError)
            else {"code": "motion_failed", "message": str(exc), "details": {}}
        )
        store.update(
            "job",
            job_id,
            lambda current: {
                **current,
                "status": "failed" if current["status"] != "cancelled" else "cancelled",
                "stage": "failed" if current["status"] != "cancelled" else "cancelled",
                "error": error,
                "updated_at": now(),
                "elapsed_seconds": time.monotonic() - start,
            },
        )
