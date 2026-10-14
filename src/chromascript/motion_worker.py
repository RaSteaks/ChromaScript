"""Three.js float readback -> ACEScg EXR masters -> independent sRGB and HDR views."""

from __future__ import annotations

import hashlib
import json
import time
import traceback
import zipfile
from pathlib import Path
from typing import Any, Literal

import numpy as np

from .color import color_info, render_output, transform
from .encoding import check_image_metadata, check_probe, encode
from .errors import ChromaError
from .exports import DESTINATION_LAYOUT, RenderBundle, bundle_name, motion_bundle, publish_bundle
from .images import file_hash, read_image, resize_srgb, write_image
from .models import Artifact
from .motion import MotionOutput, MotionSpec, motion_identity
from .motion_color import decode_float_frame
from .motion_render import render_float_frames
from .presets import builtins
from .store import Store, canonical, now


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
        relative = str(path.relative_to(directory if direct else store.root))
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
        settings = spec.settings()
        if direct:
            bundle = motion_bundle(job, snapshot)
            directory, published = bundle.prepare()
            if published:
                artifact(
                    directory / "masters", "master-archive", "application/zip",
                    {"frames": spec.frames, "color_space": "ACEScg", "on_demand": True},
                    representation="zip-stream",
                )
                progress("exporting", 0.98, spec.frames)
                finish()
                return
        else:
            # Historical snapshots retain their original workspace/copy layout.
            directory.mkdir(parents=True, exist_ok=True)
        source = directory / "Animation.ts"
        source.write_text(spec.code, encoding="utf-8")
        description = directory / "animation.json"
        description.write_text(json.dumps(spec.model_dump(), ensure_ascii=False, indent=2))
        frozen = directory / "scene-snapshot.json"
        frozen.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
        for path in (source, description, frozen):
            artifact(path, "source", "text/plain" if path.suffix == ".ts" else "application/json")

        output = MotionOutput.model_validate(snapshot["output"])
        master_depth = output.master_bit_depth
        if master_depth is None:
            raise ChromaError("motion_output", "浮点场景缺少母版精度。")
        work = bundle.work if bundle is not None else store.file(f"jobs/{job_id}/motion")
        masters = directory / "masters"
        masters.mkdir(exist_ok=True)
        manifest_file = masters / "sequence.json"
        old = json.loads(manifest_file.read_text()) if manifest_file.exists() else {}
        entries = old.get("frames", {})
        # Each cached master is tied to the frozen source/runtime/configuration.
        # Hash failures invalidate only that frame; output retries retain good masters.
        todo = []
        for i in range(spec.frames):
            path = masters / f"{i:08d}.exr"
            if not path.is_file() or entries.get(str(i), {}).get("sha256") != file_hash(path):
                todo.append(i)
        if direct and len(todo) < spec.frames and not (work / "gpu-verified.json").is_file():
            # A fresh GPU probe cannot prove which backend produced existing
            # masters. Missing recovery identity must never certify a mixed cache.
            raise ChromaError("gpu_changed", "母版缓存缺少已验证的 GPU 身份，请重新提交任务。")
        preset = builtins()[f"exr-{output.master_bit_depth}-zip"]

        def accept_frame(index: int, raw: Path) -> None:
            # Persist actual GPU identity before the first reusable EXR. This also
            # covers interruption after the last frame but before browser shutdown.
            if not (work / "gpu-verified.json").exists():
                atomic_json(work / "gpu-verified.json", json.loads((work / "gpu.json").read_text()))
            pixels = decode_float_frame(raw.read_bytes(), spec.width, spec.height)
            target = masters / f"{index:08d}.exr"
            result = write_image(
                target,
                pixels,
                depth=master_depth,
                config_hash=snapshot["color_configuration"]["sha256"],
                fps=(spec.fps.numerator, spec.fps.denominator),
                frame_index=index,
            )
            decoded, metadata = read_image(target)
            expected = (
                pixels
                if output.master_bit_depth == 32
                else pixels.astype(np.float16).astype(np.float32)
            )
            issues = check_image_metadata(metadata, preset)
            if issues or not np.array_equal(decoded, expected):
                raise ChromaError(
                    "verification_failed", "EXR 实际存储或像素回读不符。", issues=issues
                )
            entries[str(index)] = {
                **result,
                "metadata": metadata,
                "minimum": decoded.min(axis=(0, 1)).tolist(),
                "maximum": decoded.max(axis=(0, 1)).tolist(),
                "high_rgb_values": int((decoded[..., :3] > 1).sum()),
                "negative_rgb_values": int((decoded[..., :3] < 0).sum()),
                "round_trip_max_error": float(np.max(np.abs(decoded - pixels))),
                "passed": True,
            }
            atomic_json(
                manifest_file,
                {
                    "frames": entries,
                    "settings": settings.model_dump(),
                    "color_configuration": snapshot["color_configuration"],
                    "preset": preset.model_dump(),
                },
            )
            progress("rendering", 0.7 * len(entries) / spec.frames, len(entries))

        progress("rendering", 0)
        if todo or not (work / "gpu-verified.json").is_file():
            expected_gpu = (
                json.loads((work / "gpu-verified.json").read_text())
                if (work / "gpu-verified.json").is_file()
                else None
            )
            gpu = render_float_frames(spec, work, todo, accept_frame, expected_gpu=expected_gpu)
            atomic_json(work / "gpu-verified.json", gpu)
        else:
            gpu = json.loads((work / "gpu-verified.json").read_text())
        paths = [masters / f"{i:08d}.exr" for i in range(spec.frames)]
        # Publish representative master artifacts for original-pixel inspection on MCP,
        # CLI and HTTP; the sequence manifest enumerates all frames and hashes.
        for index in sorted({0, spec.frames // 2, spec.frames - 1}):
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
        thumbnail = directory / "thumbnail.png"
        write_image(
            thumbnail,
            resize_srgb(transform(read_image(paths[0])[0], "sRGB"), 1280),
            space="sRGB",
            depth=8,
        )
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
        archive = None
        if bundle is None:
            # Preserve the legacy layout only for its frozen snapshots. New jobs
            # register a virtual archive after publication and never store a ZIP.
            archive = directory / "ACEScg-masters.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as legacy_zip:
                for path in [*paths, manifest_file]:
                    legacy_zip.write(path, f"masters/{path.name}")
                    progress("packaging", 0.72)
            artifact(archive, "master-archive", "application/zip", {"frames": spec.frames})
        verification: dict[str, Any] = {
            "passed": True,
            "scene_linear_master": True,
            "source_precision": "RGBA32F GPU readback",
            "master_bit_depth": output.master_bit_depth,
            "verified_frame_rate": f"{spec.fps.numerator}/{spec.fps.denominator}",
        }
        video = None
        info = None
        if output.video != "none":
            preset = builtins()[
                {
                    "pq": "hdr-pq",
                    "hlg": "hdr-hlg",
                    "srgb": "h264-srgb-preview",
                    "rec709": "h264-review",
                }[output.video]
            ]
            video = directory / f"animation-{output.video}.mp4"
            # Keep encoder logs/partials outside the published payload. The final
            # video moves on the same export volume, without another data copy.
            encoded_video = work / video.name if bundle is not None else video
            progress("encoding", 0.75)
            info = encode(
                preset,
                settings,
                encoded_video,
                (render_output(read_image(path)[0], preset.color.output, False) for path in paths),
                lambda count: progress("encoding", 0.75 + 0.2 * count / spec.frames),
            )
            video_verification = check_probe(info, preset, settings)
            if not video_verification["passed"]:
                raise ChromaError(
                    "verification_failed",
                    "视频实际属性未通过验证。",
                    verification=video_verification,
                )
            verification["video"] = video_verification
            if encoded_video != video:
                encoded_video.replace(video)
            artifact(video, "video", "video/mp4", {"verification": video_verification})
        report = directory / "verification.json"
        report.write_text(
            json.dumps(
                {
                    "engine": "three-float-v1",
                    "verification": verification,
                    "gpu": gpu,
                    "probe": info,
                    "source_sha256": hashlib.sha256(spec.code.encode()).hexdigest(),
                    "video_sha256": file_hash(video) if video else None,
                    "fps": spec.fps.model_dump(),
                    "color_configuration": snapshot["color_configuration"],
                    "notes": [
                        "RGBA32F linear Rec.709 readback converted to ACEScg using pinned OCIO",
                        "EXR masters contain no display transform; sRGB previews use the ACES SDR view",
                        "PQ/HLG use one ACES 1000-nit output rendering; no invented mastering metadata",
                        "Alpha bypasses OCIO; video flattens on black in scene-linear ACEScg",
                        "Cached EXR frames are checksummed; source/runtime/config drift refuses retry",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        artifact(report, "report", "application/json")
        progress("exporting", 0.98)
        files = {
            **{p.name: p for p in (source, description, frozen, thumbnail, report)},
            **({video.name: video} if video else {}),
            **({archive.name: archive} if archive else {}),
            **{f"masters/{p.name}": p for p in [*paths, manifest_file]},
        }
        if bundle is not None:
            bundle.publish(files, lambda: progress("exporting", 0.98))
            artifact(
                directory / "masters", "master-archive", "application/zip",
                {"frames": spec.frames, "color_space": "ACEScg", "on_demand": True},
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
