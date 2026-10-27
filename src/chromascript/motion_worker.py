"""Three.js float readback -> ACEScg EXR masters -> independent sRGB and HDR views."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import traceback
import zipfile
from collections.abc import Generator
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

import numpy as np

from .artifacts import register_delivery_archive
from .audio import mix_audio
from .color import color_info, render_output, transform
from .encoding import check_probe, encode
from .errors import ChromaError
from .exports import DESTINATION_LAYOUT, RenderBundle, bundle_name, motion_bundle, publish_bundle
from .frame_manifest import FrameManifest
from .images import file_hash, read_image, resize_srgb, write_image
from .models import Artifact
from .motion import (
    MotionOutput,
    MotionSpec,
    motion_identity,
    motion_video_preset,
    resolve_motion_output,
)
from .motion_color import decode_float_frame
from .motion_processing import prepare_float_frame
from .motion_render import iter_float_frames, render_float_frames
from .motion_sources import write_sources
from .motion_timing import motion_time_plan
from .motion_video_plan import VideoExecution
from .presets import builtins
from .prores import prores_capability
from .store import Store, canonical, now
from .subtitles import write_subtitles
from .video_transport import VideoFrame


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    temporary.replace(path)


def run_motion(root: Path, job_id: str) -> None:
    store = Store(root)
    job = store.get("job", job_id)
    snapshot = store.get("snapshot", job_id)
    if snapshot.get("engine") == "remotion-sdr-v1":
        from .motion_sdr_worker import run_sdr_motion

        run_sdr_motion(root, job_id)
        return
    start = time.monotonic()
    artifacts = list(job["artifacts"])
    directory = store.file(f"jobs/{job_id}/output")
    direct = snapshot.get("storage_layout") == DESTINATION_LAYOUT
    bundle: RenderBundle | None = None

    def progress(stage: str, amount: float, completed: int | None = None) -> None:
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

    def artifact(
        path: Path, kind: str, mime: str, metadata: dict[str, Any] | None = None,
        *, representation: Literal["file", "zip-stream"] = "file",
    ) -> None:
        # Bundle-relative IDs survive the staging-directory rename unchanged.
        relative = str(path.relative_to(directory)) if direct else store.relative_file(path)
        suffix = representation if representation != "file" else ""
        aid = hashlib.sha256((job_id + relative + suffix).encode()).hexdigest()[:32]
        value = Artifact(
            id=aid,
            project_id=job["project_id"],
            job_id=job_id,
            name="ACEScg-masters.zip" if representation == "zip-stream" else path.name,
            kind=kind,
            relative_path=relative,
            storage="delivery" if direct else "workspace",
            representation=representation,
            mime_type=mime,
            size_bytes=None if representation == "zip-stream" else path.stat().st_size,
            created_at=now(),
            metadata=metadata or {},
        )
        store.put("artifact", aid, value.model_dump())
        if aid not in artifacts:
            artifacts.append(aid)
        store.update("job", job_id, lambda current: {**current, "artifacts": artifacts})

    def finish() -> None:
        register_delivery_archive(store, job_id)
        store.update(
            "job", job_id,
            lambda current: {
                **current,
                "status": "completed" if current["status"] != "cancelled" else "cancelled",
                "stage": "completed" if current["status"] != "cancelled" else "cancelled",
                "progress": 1,
                "export_completed": current["status"] != "cancelled",
                "elapsed_seconds": time.monotonic() - start,
                "updated_at": now(),
            },
        )
        if bundle is not None:
            try:
                bundle.discard_transport()
            except (OSError, ChromaError) as exc:
                # Publication is already durable; a small leftover transport must
                # not turn a verified delivery into a failed render.
                print(f"Retained disposable render state: {exc}", flush=True)
        print(f"Completed HDR animation {job_id}", flush=True)

    try:
        if hashlib.sha256(canonical(snapshot).encode()).hexdigest() != job["snapshot_hash"]:
            raise ChromaError("snapshot_corrupt", "动画快照校验失败。")
        if snapshot["runtime"] != motion_identity():
            raise ChromaError("runtime_changed", "动画运行环境已改变，请重新提交任务。")
        if snapshot["color_configuration"] != color_info():
            raise ChromaError("config_changed", "OCIO 配置已改变，请重新提交。")
        spec = MotionSpec.model_validate(snapshot["scene"])
        # Recovery may trust only the frozen destination, never a mutated job pointer.
        destination = Path(snapshot["export_directory"]) / bundle_name(spec.name, job_id)
        if str(destination) != job.get("export_path"):
            raise ChromaError("snapshot_corrupt", "动画导出位置与冻结快照不一致。")
        output = resolve_motion_output(spec.engine, MotionOutput.model_validate(snapshot["output"]))
        time_plan = motion_time_plan(spec.settings(), output)
        settings = time_plan.settings(spec.settings())
        if snapshot.get("time_plan", time_plan.model_dump()) != time_plan.model_dump():
            raise ChromaError("snapshot_corrupt", "输出时间采样计划与冻结快照不一致。")
        video_plan = VideoExecution.model_validate(snapshot["video_execution"]) if snapshot.get("video_execution") else None
        gpu_video = video_plan is not None and video_plan.processing == "gpu"
        if output.video == "prores-4444-acescct":
            precision = prores_capability(root)
            if not precision["available"] or precision["identity"] != snapshot.get("precision_verification", {}).get("identity"):
                raise ChromaError("motion_encoder", "原生 12-bit 编码能力已改变，请重新提交。")
        if direct:
            bundle = motion_bundle(job, snapshot)
            directory, published = bundle.prepare()
            if published:
                if output.master_bit_depth is not None:
                    artifact(
                        directory / "masters", "master-archive", "application/zip",
                        {"frames": settings.frames, "color_space": "ACEScg", "on_demand": True},
                        representation="zip-stream",
                    )
                progress("exporting", 0.98, settings.frames)
                finish()
                return
        else:
            # Historical snapshots retain their original workspace/copy layout.
            directory.mkdir(parents=True, exist_ok=True)
        # Freeze every chapter alongside the entry so exported sources remain reproducible.
        sources = write_sources(directory, spec.engine, spec.code, spec.files)
        # Every retained graph is exported with its original linear-space declaration
        # in animation.json; relative module paths remain portable within each layer.
        for index, layer in enumerate(spec.preserved_layers):
            sources += write_sources(directory / "preserved-layers" / str(index), spec.engine, layer.code, layer.files)
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

        master_depth = output.master_bit_depth
        work = bundle.work if bundle is not None else store.file(f"jobs/{job_id}/motion")
        (work / "transport.json").unlink(missing_ok=True)
        stage_seconds = {"float_decode": 0.0, "exr_write_and_hash": 0.0, "exr_verify": 0.0,
                         "manifest_record": 0.0}
        thumbnail = directory / "thumbnail.png"
        video_preset = video_plan.preset if video_plan is not None else motion_video_preset(output.video) if output.video != "none" else None
        encode_speed = video_plan.speed if video_plan is not None else "slow"
        video = directory / f"animation-{output.video}.{video_preset.container}" if video_preset else None
        # Encode beside the resumable transport, then publish on the same volume.
        encoded_video = work / video.name if video is not None and bundle is not None else video
        info = None
        paths: list[Path] = []
        gpu: dict[str, Any] = {}
        masters = directory / "masters"
        manifest_file = masters / "sequence.json"

        def parallel_pixels(frame_indices: list[int], master_directory: Path | None = None,
                            missing_indices: list[int] | None = None,
                            expected: dict[str, Any] | None = None) -> Generator[np.ndarray | VideoFrame, None, None]:
            from .motion_parallel import iter_parallel_frames

            with closing(iter_parallel_frames(
                spec, output, work, frame_indices, config_hash=snapshot["color_configuration"]["sha256"],
                masters=master_directory, missing=missing_indices, expected_gpu=expected, bundle=bundle,
                thumbnail=thumbnail if master_directory is None else None,
                cancelled=lambda: store.get("job", job_id)["status"] == "cancelled",
                video_input=video_plan.pixel_format if gpu_video and video_plan is not None else None,
            )) as prepared:
                for frame in prepared:
                    if frame.record is not None:
                        # Only this coordinator appends the recovery journal; workers
                        # never race a full manifest or acknowledge an unrecorded frame.
                        manifest.record(frame.index, frame.record)
                    for stage, seconds in frame.seconds.items():
                        stage_seconds[stage] = stage_seconds.get(stage, 0) + seconds
                    progress("encoding" if video_preset else "rendering",
                             (0.95 if video_preset else 0.7) * (frame.index + 1) / settings.frames,
                             frame.index + 1)
                    if gpu_video:
                        assert video_plan is not None and video_plan.pixel_format is not None
                        yield VideoFrame(memoryview(frame.pixels), video_plan.pixel_format)
                    else:
                        yield frame.pixels
            gpu.update(json.loads((work / "gpu.json").read_text()))

        if master_depth is not None:
            masters.mkdir(exist_ok=True)
            master_preset = builtins()[f"exr-{output.master_bit_depth}-zip"]
            manifest = FrameManifest(manifest_file, {
                "settings": settings.model_dump(),
                "color_configuration": snapshot["color_configuration"],
                "preset": master_preset.model_dump(),
            })
            entries = manifest.frames
            # Each cached master is tied to the frozen source/runtime/configuration.
            # Hash failures invalidate only that frame; output retries retain good masters.
            todo = []
            for i in range(settings.frames):
                path = masters / f"{i:08d}.exr"
                if not path.is_file() or entries.get(str(i), {}).get("sha256") != file_hash(path):
                    todo.append(i)
            if direct and len(todo) < settings.frames and not (work / "gpu-verified.json").is_file():
                # A fresh GPU probe cannot prove which backend produced existing
                # masters. Missing recovery identity must never certify a mixed cache.
                raise ChromaError("gpu_changed", "母版缓存缺少已验证的 GPU 身份，请重新提交任务。")

            def save_master(index: int, raw: Path) -> np.ndarray:
                if bundle is not None:
                    bundle.check_volume()
                # Persist actual GPU identity before the first reusable EXR. This also
                # covers interruption after the last frame but before browser shutdown.
                if not (work / "gpu-verified.json").exists():
                    atomic_json(work / "gpu-verified.json", json.loads((work / "gpu.json").read_text()))
                prepared = prepare_float_frame(index, raw, spec, output,
                    snapshot["color_configuration"]["sha256"], masters / f"{index:08d}.exr")
                for stage, seconds in prepared.seconds.items():
                    stage_seconds[stage] += seconds
                stage_start = time.monotonic()
                assert prepared.record is not None
                manifest.record(index, prepared.record)
                stage_seconds["manifest_record"] += time.monotonic() - stage_start
                progress("encoding" if video_preset else "rendering",
                         (0.95 if video_preset else 0.7) * len(entries) / settings.frames, len(entries))
                return prepared.pixels

            def accept_frame(index: int, raw: Path) -> None:
                save_master(index, raw)

            progress("rendering", 0)
            expected_gpu = (
                json.loads((work / "gpu-verified.json").read_text())
                if (work / "gpu-verified.json").is_file() else None
            )
            paths = [masters / f"{i:08d}.exr" for i in range(settings.frames)]
            if output.render_workers > 1 and video_preset is not None:
                assert encoded_video is not None
                with closing(parallel_pixels(list(range(settings.frames)), masters, todo, expected_gpu)) as frames:
                    info = encode(video_preset, settings, encoded_video, frames,
                                  lambda count: progress("encoding", 0.95 * count / settings.frames, count),
                                  audio_path=audio_path if audio_info is not None else None, speed=encode_speed)
            elif output.render_workers > 1 and todo:
                with closing(parallel_pixels(todo, masters, todo, expected_gpu)) as frames:
                    for _ in frames:
                        pass  # Master pixels have already been written and verified.
            elif video_preset is not None:
                assert encoded_video is not None

                def master_video_frames() -> Generator[np.ndarray, None, None]:
                    assert video_preset is not None
                    if not todo and expected_gpu is not None:
                        gpu.update(expected_gpu)
                        for path in paths:
                            yield render_output(read_image(path)[0], video_preset.color.output, False)
                        return
                    missing = set(todo)
                    with closing(iter_float_frames(
                        spec, work, todo, expected_gpu=expected_gpu, render_scale=output.render_scale, time_plan=time_plan,
                    )) as frames:
                        for index, path in enumerate(paths):
                            if index in missing:
                                frame, raw = next(frames)
                                pixels = save_master(frame, raw)
                            else:
                                pixels = read_image(path)[0]
                            # Feed the actual verified storage pixels, including Half
                            # rounding. Reusing this readback avoids a second EXR decode
                            # without changing the historical master-to-video result.
                            yield render_output(pixels, video_preset.color.output, False)
                        # Resume the suspended producer once more to ACK its last raw
                        # file and check process completion before certifying the GPU.
                        next(frames, None)
                    gpu.update(json.loads((work / "gpu.json").read_text()))
                    atomic_json(work / "gpu-verified.json", gpu)

                with closing(master_video_frames()) as frames:
                    info = encode(video_preset, settings, encoded_video, frames,
                                  lambda count: progress("encoding", 0.95 * count / settings.frames, count),
                                  audio_path=audio_path if audio_info is not None else None, speed=encode_speed)
            elif todo or expected_gpu is None:
                gpu = render_float_frames(spec, work, todo, accept_frame, expected_gpu=expected_gpu, render_scale=output.render_scale, time_plan=time_plan)
                atomic_json(work / "gpu-verified.json", gpu)
            else:
                gpu = expected_gpu
            manifest.finish()
            # Publish representative master artifacts for original-pixel inspection on MCP,
            # CLI and HTTP; the sequence manifest enumerates all frames and hashes.
            for index in sorted({0, settings.frames // 2, settings.frames - 1}):
                path = paths[index]
                artifact(
                    path,
                    "master-frame",
                    "image/x-exr",
                    {
                        "frame": index,
                        "color_space": "ACEScg",
                        "storage": f"float{output.master_bit_depth}",
                    },
                )
            artifact(manifest_file, "sequence-manifest", "application/json")
            write_image(
                thumbnail,
                resize_srgb(transform(read_image(paths[0])[0], "sRGB"), 1280),
                space="sRGB",
                depth=8,
                disk_reserve_bytes=0,  # Animation admission already accounts for disk usage.
            )
        sidecars = write_subtitles(snapshot.get("subtitles"), directory)
        for path in sidecars.values():
            artifact(path, "subtitle", "application/x-subrip")
        archive = None
        if bundle is None and master_depth is not None:
            # Preserve the legacy layout only for its frozen snapshots. New jobs
            # register a virtual archive after publication and never store a ZIP.
            archive = directory / "ACEScg-masters.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as legacy_zip:
                for path in [*paths, manifest_file]:
                    legacy_zip.write(path, f"masters/{path.name}")
                    progress("packaging", 0.72)
                for name, path in sidecars.items():
                    legacy_zip.write(path, name)
            artifact(archive, "master-archive", "application/zip", {"frames": settings.frames})
        verification: dict[str, Any] = {
            "passed": True,
            "time_plan": time_plan.model_dump(),
            "scene_linear_master": master_depth is not None,
            "delivery_mode": "video-only" if master_depth is None else "exr-master",
            "source_precision": "RGBA32F scene/processing; packed video readback" if gpu_video else "RGBA32F GPU readback",
            "source_color_space": spec.source_color_space,
            "preserved_layer_spaces": [layer.source_color_space for layer in spec.preserved_layers],
            "linear_processing": "source, spatial/temporal sampling, layer compositing and master conversion; display rendering only at preview/video exits",
            "master_bit_depth": output.master_bit_depth,
            "verified_frame_rate": f"{settings.fps.numerator}/{settings.fps.denominator}",
            # Float transport/storage evidence is independent of an explicit
            # reviewer attestation; arbitrary shader semantics are never auto-certified.
            "float_pipeline_verified": True,
            "source_color_contract": snapshot.get("source_color_review", {"status": "unreviewed"}),
            "scene_reference_basis": "declared source contract; see source_color_contract",
            "sampling": {"render_scale": output.render_scale, "spatial_samples": output.render_scale**2, "temporal_samples": output.temporal_samples, "shutter_angle": output.shutter_angle},
            "render_workers": output.render_workers,
            "video_execution": video_plan.model_dump() if video_plan is not None else None,
        }
        declaration_path = None
        if video_preset is not None:
            assert video is not None and encoded_video is not None
            preset = video_preset

            def video_frames() -> Generator[np.ndarray | VideoFrame, None, None]:
                if output.render_workers > 1:
                    with closing(parallel_pixels(list(range(settings.frames)))) as frames:
                        yield from frames
                    return
                if gpu_video:
                    from .motion_video_render import iter_video_frames

                    assert video_plan is not None and video_plan.pixel_format is not None
                    with closing(iter_video_frames(
                        spec, work, list(range(settings.frames)), preset.color.output,
                        render_scale=output.render_scale, time_plan=time_plan, thumbnail=thumbnail,
                        cancelled=lambda: store.get("job", job_id)["status"] == "cancelled",
                        pixel_format=video_plan.pixel_format,
                    )) as packets:
                        for index, data in enumerate(packets):
                            if bundle is not None:
                                bundle.check_volume()
                            progress("encoding", 0.95 * index / settings.frames, index)
                            yield VideoFrame(data, video_plan.pixel_format)
                    gpu.update(json.loads((work / "gpu.json").read_text()))
                    return
                # One float frame is consumed by the encoder before requesting the
                # next. No EXR, whole-sequence cache, or second GPU render is involved.
                # Interrupted video retries start at frame zero: no reusable EXRs exist.
                with closing(iter_float_frames(
                    spec, work, list(range(settings.frames)), render_scale=output.render_scale, time_plan=time_plan
                )) as frames:
                    for index, raw in frames:
                        if bundle is not None:
                            bundle.check_volume()
                        progress("encoding", 0.95 * index / settings.frames, index)
                        # The current transport already occupies disk and is
                        # removed before the next one; do not reserve it twice.
                        if shutil.disk_usage(work).free <= 0:
                            raise ChromaError("disk_space", "导出盘剩余空间不足，已停止仅视频渲染。请释放空间后重新执行。")
                        pixels = decode_float_frame(raw.read_bytes(), spec.width, spec.height, output.render_scale, spec.source_color_space)
                        if index == 0:
                            write_image(thumbnail, resize_srgb(transform(pixels, "sRGB"), 1280), space="sRGB", depth=8,
                                        disk_reserve_bytes=0)  # No hidden animation reserve.
                        # Flatten alpha in linear ACEScg, then apply exactly one ACES
                        # output transform. Half storage never quantizes this path.
                        yield render_output(pixels, preset.color.output, False)
                    gpu.update(json.loads((work / "gpu.json").read_text()))

            # Explicit close tears down the browser/transport even if FFmpeg fails
            # while the producer is suspended at a yielded frame.
            if info is None:
                progress("encoding", 0, 0)
                with closing(video_frames()) as frames:
                    info = encode(
                        preset, settings, encoded_video, frames,
                        lambda count: progress("encoding", 0.95 * count / settings.frames, count),
                        audio_path=audio_path if audio_info is not None else None,
                        video_input=video_plan.pixel_format if gpu_video and video_plan is not None else None,
                        speed=encode_speed,
                    )
            video_verification = check_probe(info, preset, settings)
            if not video_verification["passed"]:
                raise ChromaError(
                    "verification_failed",
                    "视频实际属性未通过验证。",
                    verification=video_verification,
                )
            verification["video"] = video_verification
            if preset.color_metadata_policy == "declared-scene":
                verification["precision_verification"] = snapshot["precision_verification"]
                declaration_path = directory / "color-encoding.json"
                declaration_path.write_text(json.dumps({
                    "reference": "scene", "encoded_color_space": "ACEScct",
                    "primaries": "AP1", "white_point": "D60", "transfer": "ACEScct",
                    "matrix": "BT.709", "data_range": "limited", "alpha": False,
                    "display_transform_applied": False, "transport": "p416le",
                    "verified_precision_bits": 12, "source_sha256": spec.source_digest(),
                    "config": snapshot["color_configuration"],
                    "resolve": {"input": "ACEScct", "data_levels": "Video", "import_verified": False},
                    "standard_aces_archive": False,
                }, ensure_ascii=False, indent=2))
                artifact(declaration_path, "color-encoding", "application/json")
            if encoded_video != video:
                encoded_video.replace(video)
            artifact(video, "video", "video/quicktime" if preset.container == "mov" else "video/mp4", {"verification": video_verification})
        artifact(
            thumbnail,
            "preview-image",
            "image/png",
            {
                "color_space": "sRGB",
                "display_transform": "ACES 2.0 SDR",
                "scene_linear_master": False,
            },
        )
        report = directory / "verification.json"
        report.write_text(
            json.dumps(
                {
                    "engine": "three-float-v1",
                    "verification": verification,
                    "gpu": gpu,
                    "probe": info,
                    "audio": audio_info,
                    "source_sha256": spec.source_digest(),
                    "video_sha256": file_hash(video) if video else None,
                    "fps": settings.fps.model_dump(),
                    "color_configuration": snapshot["color_configuration"],
                    "performance": {
                        "elapsed_before_publication_seconds": time.monotonic() - start,
                        "master_stage_seconds": stage_seconds,
                        "float_transport": json.loads((work / "transport.json").read_text())
                        if (work / "transport.json").is_file() else None,
                        "transport_stages_overlap": True,
                    },
                    "notes": [
                        f"RGBA32F {spec.source_color_space} readback converted to ACEScg using pinned OCIO; Alpha bypasses the transform",
                        "No EXR masters written; float frames stream directly to the video encoder" if master_depth is None else "EXR masters contain no display transform; sRGB previews use the ACES SDR view",
                        "PQ/HLG use one ACES 1000-nit output rendering; no invented mastering metadata",
                        "Alpha bypasses OCIO; video flattens on black in scene-linear ACEScg",
                        "Interrupted video-only jobs restart at frame zero; no reusable frame cache" if master_depth is None else "Cached EXR frames are checksummed; source/runtime/config drift refuses retry",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        artifact(report, "report", "application/json", {"verification": verification})
        progress("exporting", 0.98)
        files = {
            **sidecars,
            **{str(p.relative_to(directory)): p for p in (*sources, description, frozen, thumbnail, report)},
            **({video.name: video} if video else {}),
            **({declaration_path.name: declaration_path} if declaration_path else {}),
            **({archive.name: archive} if archive else {}),
            **({f"masters/{p.name}": p for p in [*paths, manifest_file]} if master_depth is not None else {}),
        }
        if audio_info is not None:
            files["audio.wav"] = audio_path
        if bundle is not None:
            bundle.publish(files, lambda: progress("exporting", 0.98))
            if master_depth is not None:
                artifact(
                    directory / "masters", "master-archive", "application/zip",
                    {"frames": settings.frames, "color_space": "ACEScg", "on_demand": True},
                    representation="zip-stream",
                )
        else:
            publish_bundle(
                files, destination, Path(snapshot["export_root"]),
                {"job_id": job_id, "snapshot_sha256": job["snapshot_hash"]},
                lambda: progress("exporting", 0.98),
            )
        finish()
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
