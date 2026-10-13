"""Independent job process: immutable source -> resumable float cache -> verified deliveries."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import traceback
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np
import psutil

from .color import color_info, output_metadata, render_output, transform
from .encoding import check_image_metadata, check_probe, encode
from .environment import runtime_identity
from .errors import ChromaError
from .exports import bundle_name, publish_bundle
from .gpu import Renderer
from .images import file_hash, read_image, resize_srgb, write_image
from .models import Artifact, DeliveryPreset, RenderSettings, SceneSpec
from .store import Store, canonical, now


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    partial.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    os.replace(partial, path)


def run(root: Path, job_id: str) -> None:
    store = Store(root)
    job = store.get("job", job_id)
    if job["kind"] == "motion":
        # Display-referred browser frames must bypass WGSL validation and ACES processing.
        from .motion_worker import run_motion

        run_motion(root, job_id)
        return
    snapshot = store.get("snapshot", job_id)
    start = time.monotonic()
    process = psutil.Process()
    peak = 0
    settings = RenderSettings.model_validate(snapshot["settings"])
    scene = SceneSpec.model_validate(snapshot["scene"])
    outputs = [DeliveryPreset.model_validate(p) for p in snapshot["outputs"]]
    frame_indices = snapshot.get("frame_indices") or list(
        range(settings.start_frame, settings.start_frame + settings.frames)
    )
    config_hash = snapshot["color_configuration"]["sha256"]
    artifacts: list[str] = list(job["artifacts"])
    directory = store.file(f"jobs/{job_id}/output")
    directory.mkdir(parents=True, exist_ok=True)
    bundle_files: dict[str, Path] = {}

    def progress(stage: str, amount: float, completed: int | None = None) -> None:
        nonlocal peak
        peak = max(peak, process.memory_info().rss)

        def change(current: dict[str, Any]) -> dict[str, Any]:
            if current["status"] == "cancelled":
                raise ChromaError("cancelled", "Job was cancelled")
            return {
                **current,
                "stage": stage,
                "progress": min(1, max(0, amount)),
                "updated_at": now(),
                "completed_frames": completed
                if completed is not None
                else current["completed_frames"],
                "elapsed_seconds": time.monotonic() - start,
                "peak_rss_bytes": peak,
            }

        store.update("job", job_id, change)

    def artifact(path: Path, kind: str, mime: str, metadata: dict[str, Any] | None = None) -> str:
        relative = str(path.relative_to(store.root))
        id = hashlib.sha256((job_id + relative).encode()).hexdigest()[:32]
        size = (
            path.stat().st_size
            if path.is_file()
            else sum(p.stat().st_size for p in path.iterdir() if p.is_file())
        )
        a = Artifact(
            id=id,
            project_id=job["project_id"],
            job_id=job_id,
            name=path.name,
            kind=kind,
            relative_path=relative,
            mime_type=mime,
            size_bytes=size,
            created_at=now(),
            metadata=metadata or {},
        )
        store.put("artifact", id, a.model_dump())
        if id not in artifacts:
            artifacts.append(id)
        store.update("job", job_id, lambda j: {**j, "artifacts": artifacts})
        return id

    try:
        if snapshot.get("runtime") and snapshot["runtime"] != runtime_identity():
            raise ChromaError("runtime_changed", "Frozen renderer or dependencies changed")
        if config_hash != color_info()["sha256"]:
            raise ChromaError(
                "config_changed", "Installed ACES configuration differs from the frozen snapshot"
            )
        if hashlib.sha256(canonical(snapshot).encode()).hexdigest() != job["snapshot_hash"]:
            raise ChromaError("snapshot_corrupt", "Job snapshot checksum mismatch")
        progress("compiling", 0)
        renderer = Renderer()
        renderer.validate(scene)
        source_path = directory / "scene-snapshot.json"
        atomic_json(source_path, snapshot)
        artifact(source_path, "source", "application/json")
        if snapshot.get("export_directory"):
            # Export the frozen revision, never the scene's potentially newer editor draft.
            scene_path = directory / "scene.json"
            atomic_json(scene_path, scene.model_dump())
            code_path = directory / "shader.wgsl"
            code_path.write_text(scene.code, encoding="utf-8")
            artifact(scene_path, "source", "application/json")
            artifact(code_path, "source", "text/plain")
            bundle_files.update(
                {"scene.json": scene_path, "shader.wgsl": code_path, "scene-snapshot.json": source_path}
            )
        # Cache identity excludes output transforms and frame count, so matching frames
        # can be shared across multiple delivery jobs without changing scene time.
        render_key = hashlib.sha256(
            canonical(
                {
                    "scene": scene.model_dump(),
                    "width": settings.width,
                    "height": settings.height,
                    "fps": settings.fps.model_dump(),
                    "samples": settings.samples,
                    "config": config_hash,
                    "renderer": "0.1.0",
                    "runtime": snapshot.get("runtime"),
                    "gpu_backend": snapshot.get("gpu_backend"),
                }
            ).encode()
        ).hexdigest()
        cache = store.file(f"cache/{render_key}/frames")
        cache.mkdir(parents=True, exist_ok=True)
        manifest_path = cache.parent / "manifest.json"
        manifest = (
            json.loads(manifest_path.read_text()) if manifest_path.exists() else {"frames": {}}
        )
        for index, frame_index in enumerate(frame_indices):
            path = cache / f"{frame_index:08d}.exr"
            entry = manifest["frames"].get(str(frame_index))
            reusable = path.exists() and entry and file_hash(path) == entry["sha256"]
            if not reusable:
                progress("rendering", 0.5 * index / len(frame_indices), index)
                pixels = renderer.render(
                    scene,
                    settings,
                    frame_index,
                    partial(progress, "rendering", 0.5 * index / len(frame_indices), index),
                )
                manifest["frames"][str(frame_index)] = write_image(
                    path,
                    pixels,
                    config_hash=config_hash,
                    fps=(settings.fps.numerator, settings.fps.denominator),
                    frame_index=frame_index,
                )
                atomic_json(manifest_path, manifest)
            progress("rendering", 0.5 * (index + 1) / len(frame_indices), index + 1)
        render_seconds = time.monotonic() - start
        selected = (
            frame_indices
            if snapshot["kind"] == "frame"
            else sorted(
                set([frame_indices[0], frame_indices[len(frame_indices) // 2], frame_indices[-1]])
            )
        )
        for frame_index in selected:
            path = cache / f"{frame_index:08d}.exr"
            artifact(
                path,
                "master-frame",
                "image/x-exr",
                {"frame": frame_index, "color_space": "ACEScg", "storage": "float32"},
            )
            pixels, _ = read_image(path)
            preview = transform(pixels, "sRGB")
            # Filter the display copy in linear light before final quantization;
            # the original ACEScg cache and the single ACES rendering stay separate.
            png = directory / f"preview-{frame_index:08d}.png"
            write_image(png, resize_srgb(preview, 1280), space="sRGB", depth=8)
            artifact(
                png,
                "preview-image",
                "image/png",
                {"frame": frame_index, "color_space": "sRGB", "display_transform": True},
            )
            if frame_index == frame_indices[0]:
                # The thumbnail is the first delivered frame's SDR display copy;
                # raw ACEScg masters and HDR video keep their independent transforms.
                bundle_files["thumbnail.png"] = png

        results = []

        def encoded_progress(output_id: str, position: int, count: int) -> None:
            progress(
                f"encoding:{output_id}",
                0.5 + 0.45 * (position + count / len(frame_indices)) / max(1, len(outputs)),
            )

        for output_index, preset in enumerate(outputs):
            progress(f"encoding:{preset.id}", 0.5 + 0.45 * output_index / max(1, len(outputs)))
            if preset.container in {"exr", "png", "tiff", "dpx"}:
                sequence = directory / preset.id
                sequence.mkdir(exist_ok=True)
                hashes = []
                for i, frame_index in enumerate(frame_indices):
                    pixels, _ = read_image(cache / f"{frame_index:08d}.exr")
                    pixels = render_output(pixels, preset.color.output, preset.alpha)
                    if not preset.alpha:
                        pixels = pixels[..., :3]
                    output = sequence / f"{frame_index:08d}.{preset.container}"
                    saved = write_image(
                        output,
                        pixels,
                        space=preset.color.output,
                        depth=preset.bit_depth,
                        compression=preset.compression,
                        alpha_mode=preset.alpha_mode,
                        timecode_start=settings.timecode,
                        config_hash=config_hash,
                        fps=(settings.fps.numerator, settings.fps.denominator),
                        frame_index=frame_index,
                    )
                    hashes.append({"frame": frame_index, **saved})
                    progress(
                        f"encoding:{preset.id}",
                        0.5
                        + 0.45
                        * (output_index + (i + 1) / len(frame_indices))
                        / max(1, len(outputs)),
                    )
                # Read actual image storage rather than certifying by requested extension.
                checked, metadata = read_image(
                    sequence / f"{frame_indices[0]:08d}.{preset.container}"
                )
                expected_channels = 4 if preset.alpha else 3
                storage_issues = check_image_metadata(metadata, preset)
                passed = not storage_issues and (
                    checked.shape == (settings.height, settings.width, expected_channels)
                    and np.isfinite(checked).all()
                )
                manifest_file = sequence / "sequence.json"
                atomic_json(
                    manifest_file,
                    {
                        "preset": preset.model_dump(),
                        "settings": settings.model_dump(),
                        "frames": hashes,
                        "color_configuration": snapshot["color_configuration"],
                        "color_encoding": output_metadata(preset.color.output),
                        "first_image_metadata": metadata,
                    },
                )
                artifact(manifest_file, "sequence-manifest", "application/json")
                artifact(
                    sequence,
                    "image-sequence",
                    "application/x-chromascript-sequence",
                    {"preset_id": preset.id, "frame_count": len(frame_indices)},
                )
                artifact(
                    sequence / f"{frame_indices[0]:08d}.{preset.container}",
                    "delivered-frame",
                    {
                        "exr": "image/x-exr",
                        "png": "image/png",
                        "tiff": "image/tiff",
                        "dpx": "image/x-dpx",
                    }[preset.container],
                    {
                        "preset_id": preset.id,
                        "frame": frame_indices[0],
                        "color_space": preset.color.output,
                    },
                )
                result = {
                    "preset_id": preset.id,
                    "passed": bool(passed),
                    "issues": storage_issues,
                    "color_signaling": "image header and sequence sidecar",
                    "color_encoding": output_metadata(preset.color.output),
                    "storage": metadata,
                    "frame_count": len(frame_indices),
                }
                for path in sequence.iterdir():
                    if path.is_file():
                        bundle_files[str(path.relative_to(directory))] = path
            else:
                target = directory / f"{preset.id}.{preset.container}"

                def converted_frames(preset: DeliveryPreset = preset):
                    for frame_index in frame_indices:
                        pixels, _ = read_image(cache / f"{frame_index:08d}.exr")
                        value = render_output(pixels, preset.color.output, preset.alpha)
                        if preset.alpha_mode == "premultiplied":
                            value[..., :3] *= value[..., 3:4]
                        yield value

                actual_settings = settings.model_copy(update={"frames": len(frame_indices)})
                info = encode(
                    preset,
                    actual_settings,
                    target,
                    converted_frames(),
                    partial(encoded_progress, preset.id, output_index),
                )
                result = {
                    "preset_id": preset.id,
                    **check_probe(info, preset, actual_settings),
                    "probe": info,
                    "file_sha256": file_hash(target),
                }
                artifact(
                    target,
                    "video",
                    "video/quicktime"
                    if preset.container == "mov"
                    else "video/mp4"
                    if preset.container == "mp4"
                    else "application/mxf"
                    if preset.container == "mxf"
                    else "video/x-matroska",
                    {"preset_id": preset.id, "verification": result},
                )
                bundle_files[target.name] = target
            results.append(result)
            # Evidence is bound to the actual configuration, geometry and encoder input.
            evidence_key = hashlib.sha256(
                canonical(
                    {
                        "preset": preset.model_dump(),
                        "settings": settings.model_dump(),
                        "config": config_hash,
                        "runtime": snapshot.get("runtime"),
                    }
                ).encode()
            ).hexdigest()
            store.put(
                "evidence",
                evidence_key,
                {
                    "id": evidence_key,
                    "preset_id": preset.id,
                    "preset": preset.model_dump(),
                    "settings": settings.model_dump(),
                    "config_sha256": config_hash,
                    "runtime": snapshot.get("runtime"),
                    "passed": result["passed"],
                    "job_id": job_id,
                    "checked_at": now(),
                    "result": result,
                },
            )
            if not result["passed"]:
                raise ChromaError(
                    "delivery_verification", "Output failed specification checks", result=result
                )
        progress("verifying", 0.97)
        report = {
            "job_id": job_id,
            "passed": all(r["passed"] for r in results),
            "settings": settings.model_dump(),
            "source_revision": job["revision_id"],
            "snapshot_sha256": job["snapshot_hash"],
            "color_configuration": snapshot["color_configuration"],
            "gpu": renderer.info,
            "runtime": snapshot.get("runtime", runtime_identity()),
            "outputs": results,
            "render_seconds": render_seconds,
            "elapsed_seconds": time.monotonic() - start,
            "peak_rss_bytes": peak,
            "notes": [
                "HDR is numerically verified, not a calibrated display measurement",
                "Float32 master cache is retained for resume and repeat exports",
                "Peak RSS is sampled for the render worker; excludes the encoder process and GPU allocations",
                "No audio, DCP or IMF",
            ],
        }
        report_path = directory / "verification.json"
        atomic_json(report_path, report)
        artifact(report_path, "report", "application/json")
        exported = False
        if snapshot.get("export_directory"):
            bundle_files["verification.json"] = report_path
            target = Path(snapshot["export_directory"]) / bundle_name(scene.name, job_id)
            if str(target) != job.get("export_path"):
                raise ChromaError("snapshot_corrupt", "Frozen export destination differs from the job")
            progress("exporting", 0.98)
            publish_bundle(
                bundle_files,
                target,
                Path(snapshot["export_root"]),
                {"job_id": job_id, "snapshot_sha256": job["snapshot_hash"]},
                partial(progress, "exporting", 0.98),
            )
            exported = True
        store.update(
            "job",
            job_id,
            lambda j: {
                **j,
                "status": "completed" if j["status"] != "cancelled" else "cancelled",
                "stage": "completed" if j["status"] != "cancelled" else "cancelled",
                "progress": 1.0,
                "updated_at": now(),
                "elapsed_seconds": time.monotonic() - start,
                "peak_rss_bytes": peak,
                "export_completed": exported,
            },
        )
        print(f"Completed {job_id} in {time.monotonic() - start:.2f}s", flush=True)
    except BaseException as exc:
        traceback.print_exc()
        error = (
            exc.as_dict()
            if isinstance(exc, ChromaError)
            else {"code": "worker_failed", "message": str(exc), "details": {}}
        )
        store.update(
            "job",
            job_id,
            lambda j: {
                **j,
                "status": "failed" if j["status"] != "cancelled" else "cancelled",
                "stage": "failed" if j["status"] != "cancelled" else "cancelled",
                "error": error,
                "updated_at": now(),
                "elapsed_seconds": time.monotonic() - start,
                "peak_rss_bytes": peak,
            },
        )
        raise


if __name__ == "__main__":
    run(Path(sys.argv[1]), sys.argv[2])
