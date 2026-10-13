"""Transport-neutral product service with revision-safe editing and durable jobs."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Literal

import numpy as np
import psutil
from pydantic import ValidationError

from .appearance import ColorAppearanceRequest, resolve_color
from .color import color_info, transform
from .encoding import available_encoders, pixel_format_info, probe
from .environment import runtime_identity
from .errors import ChromaError
from .exports import bundle_name
from .exports import export_directory as resolve_export_directory
from .gpu import Renderer
from .images import read_image
from .models import (
    ColorPipeline,
    DeliveryConfiguration,
    DeliveryPreset,
    DeliveryRequest,
    Job,
    Project,
    Rational,
    RenderSettings,
    SceneSpec,
)
from .operations import catalogue, operation
from .presets import FRAME_RATES, RESOLUTIONS, builtins
from .preview import LivePreviewRequest, preview_image
from .store import Store, canonical, identifier, now
from .templates import TEMPLATES, template_list


class Service:
    def __init__(self, root: Path, export_roots: list[Path] | None = None):
        self.operation_lock = threading.RLock()
        self.store = Store(root)
        self.export_roots = [self.store.root, *[p.resolve() for p in export_roots or []]]
        self._renderer: Renderer | None = None

    @property
    def renderer(self) -> Renderer:
        if self._renderer is None:
            self._renderer = Renderer()
        return self._renderer

    def project(self, id: str, writable: bool = False) -> dict[str, Any]:
        value = self.store.get("project", id)
        if writable and value["archived"]:
            raise ChromaError("project_archived", "Restore the project before editing")
        return value

    def scene(
        self, id: str, revision_id: str | None = None
    ) -> tuple[dict[str, Any], SceneSpec, str]:
        record = self.store.get("scene", id)
        rev = self.store.get("revision", revision_id or record["revision_id"])
        if rev["scene_id"] != id:
            raise ChromaError("revision_mismatch", "Revision belongs to another scene")
        return record, SceneSpec.model_validate(rev["scene"]), rev["id"]

    def preset(self, id: str) -> DeliveryPreset:
        # Executable presets always pass current validation, including legacy records.
        return DeliveryPreset.model_validate(self._preset_payload(id))

    def _preset_payload(self, id: str) -> dict[str, Any]:
        # Read/export must remain available when a historical preset no longer
        # satisfies current output constraints; never silently rewrite saved data.
        return builtins()[id].model_dump() if id in builtins() else self.store.get("preset", id)

    def page(self, values: list[dict[str, Any]], limit: int, offset: int) -> dict[str, Any]:
        if not 1 <= limit <= 200 or offset < 0:
            raise ChromaError("pagination", "limit must be 1..200 and offset non-negative")
        return {
            "items": values[offset : offset + limit],
            "total": len(values),
            "offset": offset,
            "limit": limit,
        }

    @operation("Query GPU, formats, output sizes, frame rates, and all callable operations.")
    def get_capabilities(self) -> dict[str, Any]:
        return {
            "version": "0.1.0",
            "executable": str(Path(sys.executable).with_name("chromascript")),
            "workspace": str(self.store.root),
            # Discovery must stay available even when a previous export path is now unwritable.
            "delivery_export": {
                "default_directory": str(self.store.root / "exports"),
                "allowed_roots": [str(p) for p in self.export_roots],
            },
            "gpu": self.renderer.info,
            "encoders": sorted(available_encoders()),
            "resolutions": RESOLUTIONS,
            "frame_rates": FRAME_RATES,
            "render_precision": "float32",
            "samples": [1, 4, 16],
            "operations": catalogue(),
            "color": color_info(),
            "limits": {"max_dimension": 8192, "max_frames": 216000},
            "acceptance_target": {"width": 3840, "height": 2160, "fps": "60/1", "frames": 300},
        }

    @operation("Inspect native runtime, GPU, disk, fixed OCIO configuration, and FFmpeg.")
    def check_environment(self) -> dict[str, Any]:
        return {
            "python": sys.version,
            "gpu": self.renderer.info,
            "disk": shutil.disk_usage(self.store.root)._asdict(),
            "ffmpeg": subprocess.run(
                ["ffmpeg", "-version"], capture_output=True, text=True
            ).stdout.splitlines()[0],
            "color": color_info(),
            "workspace": str(self.store.root),
        }

    @operation(
        "Estimate uncompressed working storage and bounded frame memory; speed requires measurement."
    )
    def estimate_render(
        self, settings: RenderSettings, preset_ids: list[str] | None = None
    ) -> dict[str, Any]:
        return self._estimate_outputs(settings, [self.preset(id) for id in preset_ids or []])

    def _estimate_outputs(
        self, settings: RenderSettings, outputs: list[DeliveryPreset]
    ) -> dict[str, Any]:
        # Estimates accept frozen output descriptions without a preset-library lookup.
        pixels = settings.width * settings.height
        cache = pixels * settings.frames * 16
        output = sum(
            pixels * settings.frames * (p.bit_depth / 8) * (4 if p.alpha else 3)
            for p in outputs
            if p.container in {"exr", "png", "tiff", "dpx"}
        )
        return {
            "cache_upper_bound_bytes": cache,
            "image_outputs_upper_bound_bytes": int(output),
            "minimum_free_bytes": int(cache + output + 1024**3),
            "frame_buffer_bytes": pixels * 16,
            "duration_seconds": settings.duration,
            "speed_estimate": None,
            "note": "压缩体积和速度取决于画面；视频编码另需磁盘空间。",
        }

    @operation("Read scene, color, delivery, or model workflow documentation.")
    def get_documentation(self, topic: str = "workflow") -> dict[str, Any]:
        from .documentation import DOCUMENTS

        if topic not in DOCUMENTS:
            raise ChromaError("unknown_topic", f"Available topics: {', '.join(DOCUMENTS)}")
        return {"topic": topic, "text": DOCUMENTS[topic]}

    @operation("Create a local procedural-video project.")
    def create_project(self, name: str, description: str = "") -> dict[str, Any]:
        if not name.strip() or len(name) > 120:
            raise ChromaError("invalid_name", "Project name needs 1..120 characters")
        stamp = now()
        p = Project(
            id=identifier(), name=name, description=description, created_at=stamp, updated_at=stamp
        )
        return {"project": self.store.put("project", p.id, p.model_dump())}

    @operation("List projects with bounded pagination.")
    def list_projects(
        self, include_archived: bool = False, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        return self.page(
            [p for p in self.store.list("project") if include_archived or not p["archived"]],
            limit,
            offset,
        )

    @operation("Read project, scenes, and default color pipeline.")
    def get_project(self, project_id: str) -> dict[str, Any]:
        try:
            pipeline = self.store.get("pipeline", project_id)
        except ChromaError:
            pipeline = ColorPipeline().model_dump()
        return {
            "project": self.project(project_id),
            "scenes": [s for s in self.store.list("scene") if s["project_id"] == project_id],
            "color_pipeline": pipeline,
        }

    @operation("Update project name and description.")
    def update_project(
        self, project_id: str, name: str | None = None, description: str | None = None
    ) -> dict[str, Any]:
        self.project(project_id, True)
        if name is not None and (not name.strip() or len(name) > 120):
            raise ChromaError("invalid_name", "Project name needs 1..120 characters")

        def change(p: dict[str, Any]) -> dict[str, Any]:
            return {
                **p,
                **({"name": name} if name is not None else {}),
                **({"description": description} if description is not None else {}),
                "updated_at": now(),
            }

        return {"project": self.store.update("project", project_id, change)}

    @operation("Clone current scene revisions into a new project, without render files.")
    def clone_project(self, project_id: str, name: str) -> dict[str, Any]:
        original = self.project(project_id)
        clone = self.create_project(name, original["description"])["project"]
        for s in self.store.list("scene"):
            if s["project_id"] == project_id:
                self.create_scene(clone["id"], spec=self.scene(s["id"])[1])
        try:
            self.store.put("pipeline", clone["id"], self.store.get("pipeline", project_id))
        except ChromaError:
            pass
        return self.get_project(clone["id"])

    @operation("Archive or restore a project after active jobs finish or are cancelled.")
    def archive_project(self, project_id: str, archived: bool = True) -> dict[str, Any]:
        self.project(project_id)
        if any(
            j["project_id"] == project_id and j["status"] in {"queued", "running"}
            for j in self.store.list("job")
        ):
            raise ChromaError("active_jobs", "Finish or cancel active jobs first")
        return {
            "project": self.store.update(
                "project", project_id, lambda p: {**p, "archived": archived, "updated_at": now()}
            )
        }

    @operation("List auditable built-in scene templates.")
    def list_templates(self) -> dict[str, Any]:
        return {"templates": template_list()}

    @operation("Read a complete editable SceneSpec including its WGSL.")
    def get_template(self, template_id: str) -> dict[str, Any]:
        if template_id not in TEMPLATES:
            raise ChromaError("unknown_template", "Choose an ID from list_templates")
        return {"template_id": template_id, "scene": TEMPLATES[template_id].model_dump()}

    @operation("Create a scene from a template or explicit shader specification.")
    def create_scene(
        self, project_id: str, template_id: str = "particles", spec: SceneSpec | None = None
    ) -> dict[str, Any]:
        self.project(project_id, True)
        spec = spec or SceneSpec.model_validate(self.get_template(template_id)["scene"])
        sid, rid = identifier(), identifier()
        self.store.put(
            "revision",
            rid,
            {
                "id": rid,
                "scene_id": sid,
                "project_id": project_id,
                "parent_revision_id": None,
                "scene": spec.model_dump(),
                "created_at": now(),
            },
        )
        self.store.put(
            "scene",
            sid,
            {
                "id": sid,
                "project_id": project_id,
                "name": spec.name,
                "revision_id": rid,
                "updated_at": now(),
            },
        )
        return self.get_scene(sid)

    @operation("Read current or historical scene code, parameter values, and revision ID.")
    def get_scene(self, scene_id: str, revision_id: str | None = None) -> dict[str, Any]:
        record, spec, rid = self.scene(scene_id, revision_id)
        return {"record": record, "scene": spec.model_dump(), "revision_id": rid}

    @operation(
        "Replace scene code/metadata using expected_revision_id; jobs retain frozen versions."
    )
    def update_scene(
        self, scene_id: str, spec: SceneSpec, expected_revision_id: str
    ) -> dict[str, Any]:
        original, _, _ = self.scene(scene_id)
        self.project(original["project_id"], True)
        rid = identifier()
        # History and current revision are one transaction across all clients.
        self.store.revise_scene(
            scene_id,
            expected_revision_id,
            {
                "id": rid,
                "scene_id": scene_id,
                "project_id": original["project_id"],
                "parent_revision_id": expected_revision_id,
                "scene": spec.model_dump(),
                "created_at": now(),
            },
        )
        return self.get_scene(scene_id)

    @operation("Change only selected parameter values with revision conflict detection.")
    def set_parameters(
        self, scene_id: str, values: dict[str, Any], expected_revision_id: str
    ) -> dict[str, Any]:
        _, spec, _ = self.scene(scene_id)
        data = spec.model_dump()
        data["values"] = {**data["values"], **values}
        return self.update_scene(scene_id, SceneSpec.model_validate(data), expected_revision_id)

    @operation("List immutable scene revisions.")
    def list_revisions(self, scene_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self.scene(scene_id)
        return self.page(
            [
                {k: v for k, v in r.items() if k != "scene"}
                for r in self.store.list("revision")
                if r["scene_id"] == scene_id
            ],
            limit,
            offset,
        )

    @operation("Restore a historical scene as a new revision.")
    def restore_revision(
        self, scene_id: str, revision_id: str, expected_revision_id: str
    ) -> dict[str, Any]:
        return self.update_scene(
            scene_id, self.scene(scene_id, revision_id)[1], expected_revision_id
        )

    @operation("Compile WGSL on the native GPU and return compiler diagnostics.")
    def validate_scene(self, scene_id: str, revision_id: str | None = None) -> dict[str, Any]:
        _, spec, rid = self.scene(scene_id, revision_id)
        return {**self.renderer.validate(spec), "revision_id": rid}

    @operation(
        "Render an unsaved draft as a bounded, single-sample sRGB PNG without creating a job."
    )
    def render_live_preview(self, request: LivePreviewRequest) -> dict[str, Any]:
        settings = request.settings()
        # Use the draft's logical coordinates and rational time at reduced raster
        # resolution; scene storage and full-quality delivery settings stay untouched.
        image = self.renderer.render(
            request.spec,
            settings,
            request.frame,
            resolution=(request.width, request.height),
        )
        return {
            "images": [preview_image(image)],
            "frame": request.frame,
            "fps": request.fps.model_dump(),
            "samples": settings.samples,
            "transient": True,
        }

    @operation("List fixed ACES configuration, spaces, displays, and checksum.")
    def list_color_spaces(self) -> dict[str, Any]:
        return color_info()

    @operation("List exact output views including PQ and the official HLG extension.")
    def list_output_transforms(self) -> dict[str, Any]:
        return color_info()

    @operation(
        "Save the project default color pipeline; explicit delivery presets remain authoritative."
    )
    def set_color_pipeline(self, project_id: str, pipeline: ColorPipeline) -> dict[str, Any]:
        self.project(project_id, True)
        transform(np.array([0.18] * 3, dtype=np.float32), pipeline.output)
        return {
            "pipeline": self.store.put("pipeline", project_id, pipeline.model_dump()),
            "configuration": color_info(),
        }

    @operation("Inspect a color or pick OKLCH appearance through the inverse ACES sRGB preview.")
    def resolve_color(self, request: ColorAppearanceRequest) -> dict[str, Any]:
        # Read-only conversion is shared by the picker, CLI and MCP; saving remains
        # an explicit revision-checked scene update through the existing operations.
        return resolve_color(request)

    @operation("Convert 1..4096 RGB samples using the fixed OCIO configuration.")
    def convert_colors(
        self, colors: list[list[float]], output: str = "ACEScg", source: str = "sRGB"
    ) -> dict[str, Any]:
        if not 1 <= len(colors) <= 4096 or any(len(c) != 3 for c in colors):
            raise ChromaError("color_samples", "Provide 1..4096 RGB triplets")
        values = transform(
            np.array(colors, dtype=np.float32).reshape(1, len(colors), 3), output, source
        )
        return {
            "colors": values.reshape(-1, 3).tolist(),
            "source": source,
            "output": output,
            "config": color_info()["sha256"],
        }

    @operation("Validate pipeline with neutral and high-exposure reference values.")
    def validate_color_pipeline(self, pipeline: ColorPipeline) -> dict[str, Any]:
        return {
            "valid": True,
            "pipeline": pipeline.model_dump(),
            "configuration": color_info(),
            "samples": self.convert_colors(
                [[0.18] * 3, [1.0] * 3, [16.0] * 3], pipeline.output, "ACEScg"
            ),
        }

    @operation("List standardized/custom presets and exact-combination verification evidence.")
    def list_delivery_presets(self) -> dict[str, Any]:
        presets = {
            **{id: preset.model_dump() for id, preset in builtins().items()},
            **{p["id"]: p for p in self.store.list("preset")},
        }
        encoders = available_encoders()
        evidence = self.store.list("evidence")
        items: list[dict[str, Any]] = []
        for record in presets.values():
            try:
                p = DeliveryPreset.model_validate(record)
            except ValidationError as error:
                # One unsupported historical output must not hide all other presets.
                # Keep its parameters/evidence readable, but never certify it as usable.
                items.append(
                    {
                        **record,
                        "availability": "unavailable",
                        "validation_error": error.errors()[0]["msg"],
                        "evidence": [e for e in evidence if e["preset_id"] == record["id"]],
                        "precision_note": "",
                    }
                )
                continue
            items.append(
                {
                    **p.model_dump(),
                    "availability": "experimental"
                    if p.container in {"exr", "png", "tiff", "dpx"} or p.codec in encoders
                    else "unavailable",
                    "evidence": [e for e in evidence if e["preset_id"] == p.id],
                    "precision_note": "This FFmpeg backend accepts at most 10-bit codec input"
                    if p.codec in {"prores_ks", "dnxhd"}
                    else "",
                }
            )
        return {"presets": items}

    @operation("Read complete preset parameters.")
    def get_delivery_preset(self, preset_id: str) -> dict[str, Any]:
        return {"preset": self._preset_payload(preset_id)}

    @operation("Save a custom preset under a new ID; built-ins cannot be overwritten.")
    def save_delivery_preset(self, preset: DeliveryPreset) -> dict[str, Any]:
        if preset.id in builtins():
            raise ChromaError("builtin_preset", "Copy the built-in to a distinct ID")
        custom = DeliveryPreset.model_validate({**preset.model_dump(), "builtin": False})
        return {"preset": self.store.put("preset", custom.id, custom.model_dump())}

    @operation("Import a versioned JSON preset.")
    def import_delivery_preset(self, preset: DeliveryPreset) -> dict[str, Any]:
        return self.save_delivery_preset(preset)

    @operation("Export complete preset JSON.")
    def export_delivery_preset(self, preset_id: str) -> dict[str, Any]:
        return {"filename": preset_id + ".json", "json": self._preset_payload(preset_id)}

    @operation("Validate exact output geometry, codec precision, alpha, and standard timebase.")
    def validate_delivery(self, preset_id: str, settings: RenderSettings) -> dict[str, Any]:
        return self._validate_output(self.preset(preset_id), settings)

    def _validate_output(
        self, original: DeliveryPreset, settings: RenderSettings
    ) -> dict[str, Any]:
        # The same physical checks cover legacy presets and directly configured outputs.
        if (
            original.family == "平台构图"
            and original.settings
            and (original.settings.width, original.settings.height)
            != (settings.width, settings.height)
        ):
            raise ChromaError(
                "preset_geometry",
                f"Preset needs {original.settings.width}x{original.settings.height}; use a matching batch item",
            )
        p = DeliveryPreset.model_validate(
            {**original.model_dump(), "settings": settings.model_dump()}
        )
        if p.container not in {"exr", "png", "tiff", "dpx"} and p.codec not in available_encoders():
            raise ChromaError("encoder_unavailable", f"Encoder {p.codec} unavailable")
        if p.container not in {"exr", "png", "tiff", "dpx"}:
            descriptor = pixel_format_info(p.pixel_format)
            component_depths = [c["bit_depth"] for c in descriptor.get("components", [])[:3]]
            if not component_depths or min(component_depths) < p.bit_depth:
                raise ChromaError(
                    "precision_unavailable",
                    "Pixel input has less precision than the requested chain",
                )
            if p.alpha and (
                not descriptor["flags"].get("alpha")
                or p.codec not in {"prores_ks", "prores_videotoolbox", "ffv1"}
            ):
                raise ChromaError(
                    "alpha_unavailable",
                    "Codec/pixel-format combination does not support alpha here",
                )
        if (
            p.alpha
            and p.container == "mov"
            and not pixel_format_info(p.pixel_format)["flags"].get("alpha")
        ):
            raise ChromaError("alpha_unavailable", "Select an alpha-capable codec input")
        if p.codec in {"prores_ks", "dnxhd"} and p.bit_depth > 10:
            raise ChromaError(
                "precision_unavailable", "This backend cannot supply true 12-bit codec input"
            )
        if p.codec in {"v210", "r210"} and settings.width % 2:
            raise ChromaError("geometry", "10-bit uncompressed MOV requires even width")
        if p.codec == "dnxhd" and (settings.width < 256 or settings.height < 120):
            raise ChromaError("geometry", "DNxHR needs at least 256x120")
        if p.container == "mxf" and settings.fps.ffmpeg not in {
            "24/1",
            "25/1",
            "30/1",
            "50/1",
            "60/1",
            "24000/1001",
            "30000/1001",
            "60000/1001",
        }:
            raise ChromaError("timebase", "MXF needs a standard broadcast timebase")
        return {
            "valid": True,
            "preset": p.model_dump(),
            "estimate": self._estimate_outputs(settings, [original]),
            "evidence": [e for e in self.store.list("evidence") if e["preset_id"] == original.id],
        }

    def submit(
        self,
        scene_id: str,
        settings: RenderSettings,
        presets: list[DeliveryPreset],
        kind: Literal["frame", "preview", "sequence", "delivery", "probe"],
        key: str,
        revision_id: str | None = None,
        frame_indices: list[int] | None = None,
        export_directory: str | None = None,
    ) -> dict[str, Any]:
        record, spec, rid = self.scene(scene_id, revision_id)
        self.project(record["project_id"], True)
        if not key or len(key) > 200:
            raise ChromaError("idempotency_key", "Supply a key of 1..200 characters")
        if frame_indices is not None and (
            not frame_indices
            or len(frame_indices) > 256
            or min(frame_indices) < 0
            or max(frame_indices) > 10_000_000
            or len(set(frame_indices)) != len(frame_indices)
        ):
            raise ChromaError("frame_indices", "Provide 1..256 non-negative frame indices")
        for p in presets:
            self._validate_output(p, settings)
        frame_count = len(frame_indices) if frame_indices else settings.frames
        required = self._estimate_outputs(
            settings.model_copy(update={"frames": frame_count}), presets
        )["minimum_free_bytes"]
        if shutil.disk_usage(self.store.root).free < required:
            raise ChromaError(
                "disk_space", "Insufficient float-cache space", required_bytes=required
            )
        snapshot = {
            "scene": spec.model_dump(),
            "revision_id": rid,
            "settings": settings.model_dump(),
            "outputs": [p.model_dump() for p in presets],
            "kind": kind,
            "frame_indices": frame_indices,
            "color_configuration": color_info(),
            "renderer": "chromascript-0.1.0",
            "runtime": runtime_identity(),
            "gpu_backend": self.renderer.info,
        }
        destination = None
        if kind == "delivery":
            # Freeze the resolved parent and its permitted root in the same identity
            # as the scene/outputs. Changing a destination requires a new request key.
            destination = self.get_delivery_export_options(export_directory)["directory"]
            root = max(
                (p for p in self.export_roots if Path(destination).is_relative_to(p)),
                key=lambda p: len(p.parts),
            )
            snapshot.update(export_directory=destination, export_root=str(root))
        digest = hashlib.sha256(canonical(snapshot).encode()).hexdigest()
        stamp = now()
        job = Job(
            id=identifier(),
            project_id=record["project_id"],
            scene_id=scene_id,
            revision_id=rid,
            kind=kind,
            status="queued",
            settings=settings,
            outputs=presets,
            frame_indices=frame_indices,
            created_at=stamp,
            updated_at=stamp,
            snapshot_hash=digest,
            idempotency_key=key,
        )
        if destination is not None:
            job.export_path = str(Path(destination) / bundle_name(spec.name, job.id))
        return {"job": self.store.create_job(key, digest, job.model_dump(), snapshot)}

    @operation("Queue one unbaked EXR frame and its sRGB PNG preview.")
    def render_frame(
        self,
        scene_id: str,
        frame: int = 0,
        width: int = 960,
        height: int = 540,
        samples: Literal[1, 4, 16] = 4,
        idempotency_key: str = "",
        revision_id: str | None = None,
        fps: Rational | None = None,
    ) -> dict[str, Any]:
        return self.submit(
            scene_id,
            RenderSettings(
                width=width,
                height=height,
                frames=1,
                start_frame=frame,
                samples=samples,
                fps=fps or Rational(),
            ),
            [],
            "frame",
            idempotency_key or identifier(),
            revision_id,
        )

    @operation("Queue selected keyframes, preserving original frame indices and timebase.")
    def render_keyframes(
        self,
        scene_id: str,
        frame_indices: list[int],
        settings: RenderSettings,
        idempotency_key: str = "",
        revision_id: str | None = None,
    ) -> dict[str, Any]:
        return self.submit(
            scene_id,
            settings,
            [],
            "frame",
            idempotency_key or identifier(),
            revision_id=revision_id,
            frame_indices=frame_indices,
        )

    @operation(
        "Queue a fixed sRGB motion preview, independent of delivery color; retrieve PNG artifacts as native model images."
    )
    def render_preview(
        self,
        scene_id: str,
        settings: RenderSettings | None = None,
        idempotency_key: str = "",
        revision_id: str | None = None,
    ) -> dict[str, Any]:
        return self.submit(
            scene_id,
            settings or RenderSettings(width=960, height=540, frames=120),
            # Never inherit a project/delivery Gamma for the browser's sRGB viewer.
            [self.preset("h264-srgb-preview")],
            "preview",
            idempotency_key or identifier(),
            revision_id,
        )

    @operation("Queue a floating-point EXR image sequence.")
    def render_sequence(
        self,
        scene_id: str,
        settings: RenderSettings,
        preset_id: str = "exr-16-zip",
        idempotency_key: str = "",
        revision_id: str | None = None,
    ) -> dict[str, Any]:
        p = self.preset(preset_id)
        if p.container != "exr":
            raise ChromaError("sequence_preset", "Choose an EXR master preset")
        # Sequence exports honor the same explicit immutable revision as previews.
        return self.submit(
            scene_id, settings, [p], "sequence", idempotency_key or identifier(), revision_id
        )

    @operation(
        "Submit frozen multi-preset delivery; identical idempotency keys never duplicate work."
    )
    def submit_delivery(
        self,
        scene_id: str,
        settings: RenderSettings,
        preset_ids: list[str],
        idempotency_key: str,
        revision_id: str | None = None,
        export_directory: str | None = None,
    ) -> dict[str, Any]:
        if not preset_ids or len(preset_ids) > 40 or len(set(preset_ids)) != len(preset_ids):
            raise ChromaError("preset_selection", "Choose 1..40 distinct presets")
        return self.submit(
            scene_id,
            settings,
            [self.preset(id) for id in preset_ids],
            "delivery",
            idempotency_key,
            revision_id,
            export_directory=export_directory,
        )

    @operation(
        "Read allowed export locations and validate a delivery parent directory without writing."
    )
    def get_delivery_export_options(self, export_directory: str | None = None) -> dict[str, Any]:
        default = str(self.store.root / "exports")
        directory = resolve_export_directory(
            default if export_directory is None else export_directory, self.export_roots
        )
        return {
            "directory": str(directory),
            "default_directory": default,
            "allowed_roots": [str(p) for p in self.export_roots],
        }

    @operation("Query linked delivery dropdown options from the installed codec capabilities.")
    def get_delivery_options(
        self, configuration: DeliveryConfiguration | None = None, changed_field: str | None = None
    ) -> dict[str, Any]:
        from .delivery import delivery_options

        return delivery_options(configuration, changed_field)

    @operation("Validate user-selected delivery fields without creating or saving a preset.")
    def validate_delivery_configuration(
        self, configuration: DeliveryConfiguration, settings: RenderSettings
    ) -> dict[str, Any]:
        from .delivery import configured_output

        output = configured_output(configuration)
        result = self._validate_output(output, settings)
        result["output"] = result.pop("preset")
        return {**result, "configuration": configuration.model_dump()}

    @operation("Estimate storage for directly configured delivery formats.")
    def estimate_delivery(
        self, settings: RenderSettings, configurations: list[DeliveryConfiguration]
    ) -> dict[str, Any]:
        from .delivery import configured_output

        if not 1 <= len(configurations) <= 20:
            raise ChromaError("delivery_count", "请选择 1–20 个交付目标。")
        return self._estimate_outputs(settings, [configured_output(c) for c in configurations])

    @operation("Submit user-selected formats directly, without preset IDs or a saved preset.")
    def submit_configured_delivery(
        self,
        scene_id: str,
        settings: RenderSettings,
        configurations: list[DeliveryConfiguration],
        idempotency_key: str,
        revision_id: str | None = None,
        export_directory: str | None = None,
    ) -> dict[str, Any]:
        from .delivery import configured_output

        if not 1 <= len(configurations) <= 20:
            raise ChromaError("delivery_count", "请选择 1–20 个交付目标。")
        # Compare frozen outputs: Auto and explicit Video/Full can be identical
        # even when the user's editable configuration objects differ.
        outputs = [configured_output(c) for c in configurations]
        if len({canonical(output.model_dump()) for output in outputs}) != len(outputs):
            raise ChromaError("duplicate_deliveries", "交付设置重复，请调整或移除重复目标。")
        return self.submit(
            scene_id,
            settings,
            outputs,
            "delivery",
            idempotency_key,
            revision_id,
            export_directory=export_directory,
        )

    @operation("List jobs and stages with pagination.")
    def list_jobs(
        self, project_id: str | None = None, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        return self.page(
            [
                j
                for j in self.store.list("job")
                if project_id is None or j["project_id"] == project_id
            ],
            limit,
            offset,
        )

    @operation("Read durable progress, errors, and artifact IDs.")
    def get_job(self, job_id: str) -> dict[str, Any]:
        return {
            "job": self.store.get("job", job_id),
            "artifacts": [a for a in self.store.list("artifact") if a["job_id"] == job_id],
        }

    @operation("Read a bounded tail of the worker log.")
    def get_job_log(self, job_id: str, max_chars: int = 8000) -> dict[str, Any]:
        self.store.get("job", job_id)
        if not 1 <= max_chars <= 64000:
            raise ChromaError("log_limit", "max_chars must be 1..64000")
        p = self.store.file(f"jobs/{job_id}/worker.log")
        return {"text": p.read_text(errors="replace")[-max_chars:] if p.exists() else ""}

    @operation("Cancel queued/running work; completed master frames remain resumable.")
    def cancel_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get("job", job_id)
        if job["status"] not in {"queued", "running"}:
            return {"job": job}
        # A completion racing cancellation must remain a completed job.
        job = self.store.update(
            "job",
            job_id,
            lambda j: (
                {**j, "status": "cancelled", "stage": "cancelled", "updated_at": now()}
                if j["status"] in {"queued", "running"}
                else j
            ),
        )
        if job["status"] != "cancelled":
            return self.get_job(job_id)
        if job.get("pid"):
            try:
                p = psutil.Process(job["pid"])
                if abs(p.create_time() - (job.get("process_started") or 0)) < 0.1:
                    os.killpg(p.pid, signal.SIGTERM)
            except (ProcessLookupError, psutil.NoSuchProcess):
                pass
        return self.get_job(job_id)

    @operation("Resume a failed/cancelled/interrupted job using its frozen code and color config.")
    def resume_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get("job", job_id)
        if job["status"] not in {"failed", "cancelled", "interrupted"}:
            raise ChromaError("job_state", "Job is not resumable")
        snapshot = self.store.get("snapshot", job_id)
        if hashlib.sha256(canonical(snapshot).encode()).hexdigest() != job["snapshot_hash"]:
            raise ChromaError("snapshot_corrupt", "Frozen configuration checksum mismatch")
        if snapshot.get("runtime") != runtime_identity():
            raise ChromaError(
                "runtime_changed", "Renderer or dependencies changed; submit a new job"
            )
        if color_info()["sha256"] != snapshot["color_configuration"]["sha256"]:
            raise ChromaError("config_changed", "Installed color configuration changed")
        self.store.update(
            "job",
            job_id,
            lambda j: {
                **j,
                "status": "queued",
                "stage": "queued",
                "pid": None,
                "process_started": None,
                "error": None,
                "updated_at": now(),
            },
        )
        return self.get_job(job_id)

    @operation("Retry export while reusing verified master frames.")
    def retry_export(self, job_id: str) -> dict[str, Any]:
        return self.resume_job(job_id)

    @operation("List managed artifacts with optional project/job filtering.")
    def list_artifacts(
        self,
        project_id: str | None = None,
        job_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self.page(
            [
                a
                for a in self.store.list("artifact")
                if (project_id is None or a["project_id"] == project_id)
                and (job_id is None or a["job_id"] == job_id)
            ],
            limit,
            offset,
        )

    @operation("Read artifact metadata; bounded PNGs can be returned as native MCP images.")
    def get_artifact(self, artifact_id: str, include_image: bool = False) -> dict[str, Any]:
        a = self.store.get("artifact", artifact_id)
        path = self.store.file(a["relative_path"])
        result: dict[str, Any] = {
            "artifact": a,
            "resource_uri": f"chromascript://artifacts/{artifact_id}",
            "local_path": str(path),
        }
        # Return usable local/media references without placing bearer secrets in URLs.
        if path.is_file():
            from .runtime import endpoint

            try:
                descriptor = endpoint(self.store.root)
                result["url"] = descriptor["url"] + f"/api/artifacts/{artifact_id}"
                result["requires_local_session"] = True
            except ChromaError:
                pass
        if include_image:
            if a["mime_type"] != "image/png" or a["size_bytes"] > 8 * 1024**2:
                raise ChromaError("image_preview", "Select a bounded PNG preview")
            result["images"] = [
                {"mime_type": "image/png", "data": base64.b64encode(path.read_bytes()).decode()}
            ]
        if (
            a["mime_type"] in {"application/json", "text/plain"}
            and path.stat().st_size <= 2 * 1024**2
        ):
            result["text"] = path.read_text()
        return result

    @operation("Copy a managed artifact to a configured export root without overwriting files.")
    def export_artifact(self, artifact_id: str, destination: str) -> dict[str, Any]:
        a = self.store.get("artifact", artifact_id)
        source = self.store.file(a["relative_path"])
        target = Path(destination).expanduser().resolve()
        if not any(target.is_relative_to(root) and target != root for root in self.export_roots):
            raise ChromaError("export_root", "Destination must be inside a configured export root")
        if target.exists():
            raise ChromaError("file_exists", "Choose a new destination")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target) if source.is_dir() else shutil.copy2(source, target)
        return {"destination": str(target), "artifact_id": artifact_id}

    @operation("Inspect original image range, storage depth, negative/high values, and alpha.")
    def inspect_frame(self, artifact_id: str) -> dict[str, Any]:
        a = self.store.get("artifact", artifact_id)
        if not a["mime_type"].startswith("image/"):
            raise ChromaError("image_required", "Select an individual image artifact")
        pixels, metadata = read_image(self.store.file(a["relative_path"]))
        rgb = pixels[..., :3]
        return {
            "metadata": metadata,
            "minimum": pixels.min(axis=(0, 1)).tolist(),
            "maximum": pixels.max(axis=(0, 1)).tolist(),
            "mean": pixels.mean(axis=(0, 1)).tolist(),
            "negative_rgb_values": int((rgb < 0).sum()),
            "high_rgb_values": int((rgb > 1).sum()),
            "non_finite": int((~np.isfinite(pixels)).sum()),
            "alpha_present": pixels.shape[2] == 4,
        }

    @operation("Sample straight original image pixels using top-left integer coordinates.")
    def sample_pixels(self, artifact_id: str, points: list[list[int]]) -> dict[str, Any]:
        a = self.store.get("artifact", artifact_id)
        if not a["mime_type"].startswith("image/") or not 1 <= len(points) <= 4096:
            raise ChromaError("pixel_samples", "Select an image and 1..4096 points")
        pixels, metadata = read_image(self.store.file(a["relative_path"]))
        values = []
        for point in points:
            if len(point) != 2 or not (
                0 <= point[0] < pixels.shape[1] and 0 <= point[1] < pixels.shape[0]
            ):
                raise ChromaError("pixel_bounds", "Coordinate outside image")
            x, y = point
            values.append({"x": x, "y": y, "values": pixels[y, x].tolist()})
        return {
            "pixels": values,
            "color_space": metadata.get(
                "chromascript:color_space", metadata.get("oiio:ColorSpace")
            ),
            "association": "straight",
        }

    @operation("Compare equal-size images in the same stored color space.")
    def compare_frames(self, first_artifact_id: str, second_artifact_id: str) -> dict[str, Any]:
        aa, bb = (
            self.store.get("artifact", first_artifact_id),
            self.store.get("artifact", second_artifact_id),
        )
        a, am = read_image(self.store.file(aa["relative_path"]))
        b, bm = read_image(self.store.file(bb["relative_path"]))
        first_space = (
            aa["metadata"].get("color_space")
            or am.get("chromascript:color_space")
            or am.get("oiio:ColorSpace")
        )
        second_space = (
            bb["metadata"].get("color_space")
            or bm.get("chromascript:color_space")
            or bm.get("oiio:ColorSpace")
        )
        if a.shape != b.shape or not first_space or first_space != second_space:
            raise ChromaError(
                "comparison_space", "Images must share size, channels, and color space"
            )
        diff = a.astype(np.float64) - b
        return {
            "max_absolute_error": float(np.abs(diff).max()),
            "mean_absolute_error": float(np.abs(diff).mean()),
            "rmse": float(np.sqrt(np.mean(diff**2))),
        }

    @operation("Inspect actual video streams, decoded frame count, timebase and color metadata.")
    def probe_media(self, artifact_id: str) -> dict[str, Any]:
        a = self.store.get("artifact", artifact_id)
        return {"probe": probe(self.store.file(a["relative_path"]))}

    @operation("Read the completed delivery verification report.")
    def validate_delivery_output(self, job_id: str) -> dict[str, Any]:
        artifacts = [
            a
            for a in self.store.list("artifact")
            if a["job_id"] == job_id and a["kind"] == "report"
        ]
        if not artifacts:
            raise ChromaError("report_pending", "The job has not produced a report yet")
        return json.loads(self.store.file(artifacts[0]["relative_path"]).read_text())

    @operation(
        "Submit multiple resolutions/frame rates as idempotent, separately tracked batch jobs."
    )
    def submit_batch(
        self, scene_id: str, deliveries: list[DeliveryRequest], idempotency_key: str
    ) -> dict[str, Any]:
        if not 1 <= len(deliveries) <= 20 or not 1 <= len(idempotency_key) <= 180:
            raise ChromaError("batch_size", "Provide 1..20 deliveries and a 1..180 character key")
        _, _, revision = self.scene(scene_id)
        for item in deliveries:
            for preset_id in item.preset_ids:
                self.validate_delivery(preset_id, item.settings)
        jobs = []
        try:
            for index, item in enumerate(deliveries):
                jobs.append(
                    self.submit_delivery(
                        scene_id,
                        item.settings,
                        item.preset_ids,
                        f"{idempotency_key}:{index}",
                        revision,
                    )["job"]
                )
        except ChromaError as exc:
            raise ChromaError(
                exc.code, exc.message, **{**exc.details, "created_job_ids": [j["id"] for j in jobs]}
            ) from exc
        return {"jobs": jobs, "batch_key": idempotency_key}

    @operation(
        "Submit the project's default color output, selecting a suitable standardized preset."
    )
    def submit_default_delivery(
        self, scene_id: str, settings: RenderSettings, idempotency_key: str
    ) -> dict[str, Any]:
        record, _, _ = self.scene(scene_id)
        pipeline = self.get_project(record["project_id"])["color_pipeline"]
        selected = {
            "ACEScg": "exr-16-zip",
            "ACES2065-1": "aces2065-archive",
            "sRGB": "png-16",
            "Rec.709": "prores-3",
            "PQ": "hdr-pq",
            "HLG": "hdr-hlg",
        }[pipeline["output"]]
        return self.submit_delivery(scene_id, settings, [selected], idempotency_key)

    @operation(
        "Read the exact-combination verification matrix; format families are not blanket-certified."
    )
    def get_verification_matrix(
        self, preset_id: str | None = None, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        return self.page(
            [
                e
                for e in self.store.list("evidence")
                if preset_id is None or e["preset_id"] == preset_id
            ],
            limit,
            offset,
        )
