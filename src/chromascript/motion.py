"""Revision-safe Three.js float scenes with ACEScg masters and separate display views."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import psutil
from pydantic import Field, StrictBool, field_validator, model_validator

from .audio import freeze_audio
from .color import color_info, output_metadata, video_tag_issue
from .encoding import available_encoders
from .errors import ChromaError
from .exports import DESTINATION_LAYOUT, bundle_name, volume_identity
from .exports import export_directory as resolve_export_directory
from .models import ColorPipeline, DeliveryPreset, Job, Model, MotionRate, MotionRenderSettings
from .motion_color import (
    SOURCE_SPACE,
    SOURCE_SPACES,
    MotionSourceSpace,
    display_shader,
    working_space,
)
from .motion_hdr_preview import HDRPreviewChunk, encode_hdr_preview
from .motion_sources import source_hash, validate_sources
from .operations import operation
from .presets import builtins
from .prores import intermediate_preset, prores_capability
from .store import Store, canonical, identifier, now

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
RUNTIME = FRONTEND / "motion" / "runtime.mjs"
SDR_RUNTIME = FRONTEND / "motion" / "sdr-runtime.mjs"
MotionEngine = Literal["three-float-v1", "remotion-sdr-v1"]
IMPORTS = ["three"]


class MotionOutput(Model):
    # None opts into direct video encoding; historical EXR defaults stay unchanged.
    master_bit_depth: Literal[16, 32] | None = 32
    # Codec and transfer are explicit together; legacy values retain their meaning.
    video: Literal[
        "none", "pq", "hlg", "srgb", "rec709",
        "prores-hq-srgb", "prores-hq-pq", "prores-hq-hlg",
        "prores-4444-acescct",
    ] = "none"
    # Spatial supersampling is independent of preview resolution and source precision.
    render_scale: Literal[1, 2, 4] = 2

    @field_validator("render_scale", mode="before")
    @classmethod
    def integer_scale(cls, value: Any) -> Any:
        # Literal equality alone accepts True and 2.0; the public contract is integer-only.
        if type(value) is not int:
            raise ValueError("render_scale must be the integer 1, 2 or 4")
        return value


def motion_video_preset(video: str) -> DeliveryPreset:
    """One encoding contract for discovery, submission and both motion workers."""
    if video == "prores-4444-acescct":
        return intermediate_preset()
    presets = builtins()
    prores_spaces: dict[str, Literal["sRGB", "PQ", "HLG"]] = {
        "prores-hq-srgb": "sRGB", "prores-hq-pq": "PQ", "prores-hq-hlg": "HLG",
    }
    if video in prores_spaces:
        # Keep legacy ProRes presets intact. HQ is opaque 10-bit 4:2:2 in MOV;
        # its codec does not choose the display transform or imply source precision.
        return DeliveryPreset.model_validate({
            **presets["prores-3"].model_dump(),
            "id": f"motion-{video}",
            "name": f"Apple ProRes 422 HQ · {prores_spaces[video]}",
            "color": ColorPipeline(output=prores_spaces[video]).model_dump(),
        })
    ids = {"pq": "hdr-pq", "hlg": "hdr-hlg", "srgb": "h264-srgb-preview", "rec709": "h264-review"}
    if video not in ids:
        raise ChromaError("motion_output", "请选择有效的视频输出。")
    return presets[ids[video]]


def prores_hq_frame_bits(width: int, height: int) -> int:
    """Plan progressive, opaque prores_ks HQ from its compressed macroblock target."""
    # FFmpeg 7.1 proresenc_kostya.c: HQ br_tab and prores_mb_limits. Each
    # macroblock covers 16x16 pixels, including partial edge blocks. This is
    # a planning target, not a guaranteed size or a forced encoder bitrate.
    # https://ffmpeg.org/doxygen/7.1/proresenc__kostya_8c_source.html
    blocks = ((width + 15) // 16) * ((height + 15) // 16)
    bits_per_block = next(
        (bits for limit, bits in ((1620, 1566), (2700, 1216), (6075, 1070)) if blocks <= limit),
        950,
    )
    return blocks * bits_per_block


def resolve_motion_output(engine: MotionEngine, output: MotionOutput | None) -> MotionOutput:
    """Estimate and submission agree on engine-specific master defaults."""
    floating = engine == "three-float-v1"
    output = output or (
        MotionOutput() if floating else MotionOutput(master_bit_depth=None, video="srgb")
    )
    if not floating and (
        output.master_bit_depth is not None or output.video not in {"srgb", "rec709", "prores-hq-srgb"}
    ):
        raise ChromaError(
            "motion_output",
            "SDR 网页源码支持 sRGB/Rec.709 视频，不能生成原生 HDR 或 ACEScg 浮点母版。",
        )
    if floating and output.master_bit_depth is None and output.video == "none":
        raise ChromaError("motion_output", "仅视频模式需要选择视频格式；或选择 EXR 母版精度。")
    return output


class MotionSource(Model):
    # Omitted fields retain historical linear Rec.709 semantics, never new defaults.
    source_color_space: MotionSourceSpace = Field(default=SOURCE_SPACE, description="Scene-linear RGB basis for numeric colors, shaders and rendering. No PQ/sRGB encoding here.")
    code: str = Field(
        min_length=1,
        max_length=200_000,
        description="Float: Three.js factory returning scene/camera/update in source_color_space. SDR: default React TSX component, display-referred sRGB screenshot.",
    )
    # Extra modules belong to this revision; code remains the backward-compatible entry.
    files: dict[str, str] = Field(
        default_factory=dict,
        max_length=128,
        description="Relative TS/TSX/JS/JSX chapter paths mapped to source; import them from code.",
    )
    @model_validator(mode="after")
    def valid_sources(self) -> MotionSource:
        validate_sources(self.code, self.files)
        return self


class MotionSpec(MotionSource):
    engine: MotionEngine = "three-float-v1"
    name: str = Field(default="未命名动画", min_length=1, max_length=120)
    # Preserved source graphs render as ordered background layers in their original
    # linear basis. New source renders natively on top; layers do not share depth.
    preserved_layers: list[MotionSource] = Field(default_factory=list, max_length=16)
    width: int = Field(default=1920, ge=1, le=2_147_483_647, strict=True)
    height: int = Field(default=1080, ge=1, le=2_147_483_647, strict=True)
    frames: int = Field(default=150, ge=1, le=2_147_483_647, strict=True)
    fps: MotionRate = Field(default_factory=MotionRate)

    @model_validator(mode="after")
    def valid_pipeline(self) -> MotionSpec:
        if self.engine == "remotion-sdr-v1" and (self.source_color_space != SOURCE_SPACE or self.preserved_layers):
            raise ValueError("SDR 网页源不支持场景线性颜色空间或浮点升级图层")
        graphs = [self, *self.preserved_layers]
        if sum(len(s.code) + sum(map(len, s.files.values())) for s in graphs) > 2_000_000:
            raise ValueError("包含保留图层在内的全部源码最多 2000000 字符")
        return self

    def source_digest(self) -> str:
        # Preserve historical attestations, while binding all new color semantics
        # and retained source graphs to the evidence exported with a revision.
        if self.source_color_space == SOURCE_SPACE and not self.preserved_layers:
            return source_hash(self.code, self.files)
        return hashlib.sha256(canonical({
            "code": self.code, "files": self.files, "source_color_space": self.source_color_space,
            "preserved_layers": [layer.model_dump() for layer in self.preserved_layers],
        }).encode()).hexdigest()

    def display(self, hdr_preview: bool = False) -> dict[str, Any]:
        return {**display_shader(self.source_color_space),
                **({"hdr": display_shader(self.source_color_space, hdr=True)} if hdr_preview else {}),
                "layers": [
            working_space(layer.source_color_space) for layer in self.preserved_layers
        ]}

    def settings(self) -> MotionRenderSettings:
        return MotionRenderSettings(
            width=self.width, height=self.height, frames=self.frames, fps=self.fps, samples=1
        )


def blank_motion(
    engine: MotionEngine = "three-float-v1", source_color_space: MotionSourceSpace | None = None,
) -> MotionSpec:
    """An empty runnable contract, without example objects, text, lights or animation."""
    if engine == "remotion-sdr-v1":
        code = """// An empty SDR composition. Add React, SVG or ThreeCanvas content here.
export default function Animation() {
  return null;
}
"""
    else:
        code = """import * as THREE from "three";

export default function createScene({width, height}: {width: number; height: number}) {
  // Empty linear-light scene. The host owns ACEScg masters and display transforms.
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 100);
  camera.position.z = 5;
  return {scene, camera, update() {
    // Set animation state from the supplied frame/time when adding content.
  }};
}
"""
    return MotionSpec(
        name="空白 SDR 动画" if engine == "remotion-sdr-v1" else "空白浮点动画",
        engine=engine,
        source_color_space=source_color_space or ("Linear Rec.2020" if engine == "three-float-v1" else SOURCE_SPACE),
        code=code,
    )


def templates() -> dict[str, MotionSpec]:
    from .motion_sdr_templates import sdr_templates

    return {
        **sdr_templates(),
        "radiance": MotionSpec(
            name="HDR 光能渐变",
            code="""import * as THREE from "three";

export default function createScene() {
  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 2);
  camera.position.z = 1;
  // Scene-linear radiance exceeds 1.0; display rendering belongs to the host.
  const material = new THREE.ShaderMaterial({
    uniforms: {time: {value: 0}},
    vertexShader: `varying vec2 p; void main(){p=uv;gl_Position=vec4(position,1.0);}`,
    fragmentShader: `uniform float time; varying vec2 p;
      void main(){float dx=p.x-0.5-0.2*sin(time);float glow=exp(-32.0*dx*dx);
      vec3 radiance=mix(vec3(0.01,0.025,0.06),vec3(12.0,3.0,0.4),glow);
      gl_FragColor=vec4(radiance*(0.2+0.8*p.y),1.0);}`,
    toneMapped: false
  });
  scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2, 2), material));
  return {scene, camera, update({time}: {time:number}) {material.uniforms.time.value = time;}};
}
""",
        ),
        "three": MotionSpec(
            name="HDR 发光几何",
            code="""import * as THREE from "three";

export default function createScene({width, height}: {width:number; height:number}) {
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(42, width / height, 0.1, 100);
  camera.position.z = 5;
  // Three.js computes in linear Rec.709; the host converts float pixels to AP1.
  const material = new THREE.MeshStandardMaterial({
    color: new THREE.Color(0.08, 0.3, 0.5),
    emissive: new THREE.Color(0.1, 0.6, 1.5), emissiveIntensity: 4,
    metalness: 0.35, roughness: 0.3
  });
  const mesh = new THREE.Mesh(new THREE.TorusKnotGeometry(0.85,0.27,128,24), material);
  scene.add(mesh, new THREE.AmbientLight(0xffffff,0.7));
  const light = new THREE.DirectionalLight(0xffffff,3); light.position.set(3,4,5); scene.add(light);
  return {scene,camera,update({time}: {time:number}) {mesh.rotation.set(time*0.35,time*0.6,0.15);}};
}
""",
        ),
    }


def browser_executable() -> str | None:
    candidates = [
        os.environ.get("CHROMASCRIPT_CHROME", ""),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("chromium") or "",
        shutil.which("google-chrome") or "",
    ]
    return next((p for p in candidates if p and Path(p).is_file()), None)


def motion_environment() -> dict[str, Any]:
    packages: dict[str, str | None] = {}
    for name in [
        "remotion",
        "@remotion/renderer",
        "three",
        "esbuild",
        "typescript",
        "react",
        "react-dom",
        "@remotion/player",
        "@remotion/bundler",
        "@remotion/three",
        "@react-three/fiber",
    ]:
        path = FRONTEND / "node_modules" / name / "package.json"
        packages[name] = json.loads(path.read_text())["version"] if path.is_file() else None
    return {
        "node": shutil.which("node"),
        "browser": browser_executable(),
        "packages": packages,
        "preview_available": bool(shutil.which("node") and all(packages.values())),
        "render_available": bool(
            shutil.which("node") and all(packages.values()) and browser_executable()
        ),
        "imports": IMPORTS,
        "source_modules": {
            "field": "files", "max_files": 128, "max_total_characters": 2_000_000,
            "extensions": [".ts", ".tsx", ".js", ".jsx"],
            "resolution": "relative virtual paths only; explicit extension or extensionless/index",
            "entry": {"three-float-v1": "Animation.ts", "remotion-sdr-v1": "Animation.tsx"},
        },
        "contract": "motion-dual-v1",
        # Discoverable workflow pointers are identical through the shared registry.
        "color_authoring": {
            "documentation_topic": "motion-color",
            "documentation_resource": "chromascript://docs/motion-color",
            "upgrade_operation": "chromascript.upgrade_motion_scene",
            "read_operation": "chromascript.get_motion_scene",
            "requires_expected_revision_id": True,
            "upgrade_preserves_colors": True,
            "preserved_layers_share_depth": False,
            "master_space": "ACEScg",
            "display_encoding_stage": "preview/video exits only",
        },
        "engines": {
            "three-float-v1": {
                "imports": ["three"],
                "source_color_spaces": SOURCE_SPACES,
                "default_source_color_space": "Linear Rec.2020",
                "upgrade": "preserve source as a linear-composited background layer; new source uses target space",
                "source_precision": "RGBA32F",
                "scene_linear_master": True,
            },
            "remotion-sdr-v1": {
                "imports": [
                    "react",
                    "react/jsx-runtime",
                    "remotion",
                    "@remotion/three",
                    "@react-three/fiber",
                    "three",
                ],
                "source_precision": "8-bit sRGB screenshot",
                "scene_linear_master": False,
            },
        },
        "output": "HDR and SDR templates; engine-specific EXR, H.264, HEVC and ProRes 422 HQ MOV delivery",
        "preview": "Automatic low-resolution PQ/HEVC video for wide-gamut float scenes on HDR-capable browsers; OCIO sRGB fallback. Native sRGB for SDR scenes.",
        "hdr_preview": {
            "operation": "chromascript.encode_motion_hdr_preview",
            "max_dimensions": [1280, 720], "fps": "project rational rate; no sampling", "max_chunk_frames": 64,
            "binary_operation": "/api/operations/encode_motion_hdr_preview/binary",
            "input_formats": ["rgba32f-linear", "p010-pq"],
            "output": "PQ", "target_peak_nits": 1000, "transient": True,
            "input": "JSON Base64 or authenticated binary P010 PQ; legacy bottom-up premultiplied RGBA32F remains supported",
            "display_verified": False,
        },
        "source_precision": "engine-specific: RGBA32F float or 8-bit sRGB screenshot",
        "specification": {
            "integer_max": 2_147_483_647,
            "custom_dimensions": True,
            "custom_rational_fps": True,
            "limits": "Actual GPU, memory, storage and encoder capabilities; no silent resizing",
        },
        "color_configuration": color_info(),
    }


def motion_identity() -> dict[str, Any]:
    digest = hashlib.sha256()
    for path in [
        RUNTIME,
        SDR_RUNTIME,
        FRONTEND / "motion" / "preview.tsx",
        Path(__file__).with_name("motion_sdr_worker.py"),
        FRONTEND / "motion" / "float-runtime.mjs",
        FRONTEND / "motion" / "hdr-preview.mjs",
        FRONTEND / "motion" / "hdr-capture.mjs",
        FRONTEND / "motion" / "source-color.mjs",
        FRONTEND / "motion" / "source-modules.mjs",
        Path(__file__).with_name("motion_sources.py"),
        Path(__file__).with_name("motion_color.py"),
        Path(__file__).with_name("models.py"),
        # Workers resolve video presets at execution time. A preset change must
        # reject queued/retried jobs rather than silently change frozen delivery.
        Path(__file__).with_name("presets.py"),
        Path(__file__).with_name("color.py"),
        FRONTEND / "package-lock.json",
        Path(__file__),
        Path(__file__).with_name("motion_worker.py"),
        Path(__file__).with_name("motion_render.py"),
        Path(__file__).with_name("artifacts.py"),
        Path(__file__).with_name("exports.py"),
        Path(__file__).with_name("encoding.py"),
        Path(__file__).with_name("images.py"),
        Path(__file__).with_name("prores.py"),
        Path(__file__).with_name("motion_storage.py"),
        Path(__file__).with_name("motion_sampling.py"),
    ]:
        digest.update(path.read_bytes())
    env = motion_environment()
    browser = env["browser"]
    # The lock file cannot freeze system executables. A retry must not publish
    # new pixels/bitstreams under an old Node/FFmpeg identity after an upgrade.
    executables: dict[str, str | None] = {}
    for name, flag in (("node", "--version"), ("ffmpeg", "-version")):
        executable = shutil.which(name)
        if executable:
            try:
                version = subprocess.run(
                    [executable, flag], capture_output=True, text=True, timeout=5
                )
                executables[name] = (version.stdout or version.stderr).splitlines()[0]
            except (OSError, subprocess.TimeoutExpired, IndexError):
                executables[name] = "unavailable"
        else:
            executables[name] = None
    return {
        "renderer_sha256": digest.hexdigest(),
        "color_configuration": color_info(),
        "packages": env["packages"],
        "browser": browser,
        "browser_mtime_ns": Path(browser).stat().st_mtime_ns if browser else None,
        "executables": executables,
        "platform": platform.platform(),
    }


@lru_cache(maxsize=4)
def compile_preview(
    payload: str, renderer_fingerprint: str = "", hdr_preview: bool = False,
) -> dict[str, Any]:
    # Compilation only handles source as data. User JavaScript executes in an
    # opaque-origin sandbox, never in Node or the authenticated editor document.
    env = motion_environment()
    if not env["preview_available"]:
        raise ChromaError("motion_unavailable", "动画依赖未安装，请运行 ./install.sh 后重试。")
    try:
        result = subprocess.run(
            [
                env["node"],
                str(
                    SDR_RUNTIME
                    if json.loads(payload).get("engine") == "remotion-sdr-v1"
                    else RUNTIME
                ),
                "preview",
            ],
            input=canonical({"spec": json.loads(payload), "display": MotionSpec.model_validate_json(payload).display(hdr_preview),
                             "hdr_preview": hdr_preview}),
            capture_output=True,
            text=True,
            cwd=FRONTEND,
            timeout=45,
        )
    except subprocess.TimeoutExpired as exc:
        raise ChromaError("motion_timeout", "动画编译超时，草稿已保留，请简化代码后重试。") from exc
    if result.returncode:
        raise ChromaError("motion_compile", result.stderr[-6000:] or "动画编译未完成。")
    return json.loads(result.stdout)


class MotionOperations:
    """Shared operations inherited by Service and reflected into every transport."""

    store: Store
    export_roots: list[Path]

    def _motion_project(self, project_id: str, writable: bool = False) -> dict[str, Any]:
        project = self.store.get("project", project_id)
        if writable and project["archived"]:
            raise ChromaError("project_archived", "请先恢复项目再编辑动画。")
        return project

    def _motion_scene(
        self, scene_id: str, revision_id: str | None = None
    ) -> tuple[dict[str, Any], MotionSpec, str]:
        record = self.store.get("motion_scene", scene_id)
        revision = self.store.get("motion_revision", revision_id or record["revision_id"])
        if revision["scene_id"] != scene_id:
            raise ChromaError("revision_mismatch", "修订版本属于另一个动画。")
        # Historical documents predate engine tags and are unambiguously SDR.
        # Normalize only the response; never rewrite their source or revision IDs.
        source = {**revision["scene"], "engine": revision["scene"].get("engine", "remotion-sdr-v1")}
        return record, MotionSpec.model_validate(source), revision["id"]

    @operation(
        "Discover source engines, native linear Rec.709/BT.2020/AP1/AP0 authoring spaces, color-preserving upgrade operation and agent guide, dependencies and preview/master spaces."
    )
    def get_motion_capabilities(self) -> dict[str, Any]:
        return motion_environment()

    @operation(
        "Discover valid display outputs and float-only ACEScct ProRes 4444 with measured 12-bit capability evidence. Diagnostic failure disables only 4444; SDR screenshots retain their source precision."
    )
    def get_motion_delivery_options(
        self, engine: MotionEngine = "three-float-v1"
    ) -> dict[str, Any]:
        encoders = available_encoders()
        floating = engine == "three-float-v1"
        choices = (
            [{"value": "none", "label": "仅 ACEScg EXR 母版", "available": True}]
            if floating
            else []
        )
        choices += [
            {"value": v, "label": label, "available": "libx264" in encoders and v != "rec709",
             "reason": video_tag_issue("Rec.709") if v == "rec709" else "" if "libx264" in encoders else "本机缺少编码器"}
            for v, label in [
                ("srgb", "SDR · sRGB · H.264"),
                ("rec709", "SDR · Rec.709 / Gamma 2.4 · H.264"),
            ]
        ]
        if floating:
            choices += [
                {
                    "value": v,
                    "label": f"HEVC Main10 · {v.upper()} · 1000 nits",
                    "available": "libx265" in encoders,
                    "reason": "" if "libx265" in encoders else "本机缺少编码器",
                    "color_encoding": output_metadata(v.upper()),
                }
                for v in ("pq", "hlg")
            ]
        # The menu comes from the same registry exposed to HTTP, CLI and MCP.
        # Encoder presence enables selection; only a decoded file verifies delivery.
        for transfer in (["srgb", "pq", "hlg"] if floating else ["srgb"]):
            value = f"prores-hq-{transfer}"
            preset = motion_video_preset(value)
            encoding = output_metadata(preset.color.output)
            if not floating:
                # A wider codec stores the screenshot without an ACES display view.
                encoding = {**encoding, "output_transform": None, "source": "browser sRGB"}
            choices.append({
                "value": value,
                "label": f"Apple ProRes 422 HQ · MOV · {'sRGB' if transfer == 'srgb' else transfer.upper() + ' · 1000 nits'}",
                "available": preset.codec in encoders,
                "reason": "" if preset.codec in encoders else "本机缺少 prores_ks 编码器",
                "color_encoding": encoding,
            })
        if floating:
            capability = (prores_capability(self.store.root) if "prores_videotoolbox" in encoders else
                          {"available": False, "reason": "本机 FFmpeg 缺少 Apple VideoToolbox ProRes 编码器。"})
            choices.append({
                "value": "prores-4444-acescct",
                "label": "ProRes 4444 · ACEScct · 12-bit · MOV",
                "available": capability["available"], "reason": capability["reason"],
                "color_encoding": {**output_metadata("ACEScct"),
                    "requires_manual_video_interpretation": True},
                "precision_verification": capability,
            })
        return {
            "contract": "motion-dual-v1",
            "engine": engine,
            "master_bit_depths": [16, 32] if floating else [],
            "video_only": True,
            "master_color_space": "ACEScg" if floating else None,
            "alpha": "straight" if floating else "composited on black",
            "default": (
                MotionOutput() if floating else MotionOutput(master_bit_depth=None, video="srgb")
            ).model_dump(),
            "videos": choices,
            "scene_linear_master": floating,
            "master_archive": "zip-stream" if floating else None,
            "preview": "sRGB SDR; independent of delivery",
            "render_scales": [1, 2, 4],
            "direct_to_destination": True,
        }

    @operation(
        "Read all HDR float-factory and SDR React/ThreeCanvas templates, each with an explicit engine."
    )
    def list_motion_templates(self) -> dict[str, Any]:
        return {
            "items": [
                {"id": key, "name": spec.name, "spec": spec.model_dump()}
                for key, spec in templates().items()
            ]
        }

    @operation("List a project's saved HDR and SDR animations with bounded pagination.")
    def list_motion_scenes(
        self, project_id: str, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        self._motion_project(project_id)
        if not 1 <= limit <= 200 or offset < 0:
            raise ChromaError("pagination", "limit must be 1..200 and offset non-negative")
        values = [s for s in self.store.list("motion_scene") if s["project_id"] == project_id]
        return {
            "items": values[offset : offset + limit],
            "total": len(values),
            "limit": limit,
            "offset": offset,
        }

    @operation(
        "Read current or historical immutable HDR/SDR animation source and its rational timebase."
    )
    def get_motion_scene(self, scene_id: str, revision_id: str | None = None) -> dict[str, Any]:
        record, spec, rid = self._motion_scene(scene_id, revision_id)
        return {
            "record": record, "scene": spec.model_dump(), "revision_id": rid,
            "color_review": self._motion_color_review(spec, rid),
        }

    def _motion_color_review(self, spec: MotionSpec, rid: str) -> dict[str, Any]:
        # An attestation applies to one immutable revision, never inferred from HDR pixels.
        try:
            review = self.store.get("motion_color_review", rid)
        except ChromaError as exc:
            if exc.as_dict()["code"] != "not_found":
                raise
            review = None
        digest = spec.source_digest()
        if review and review.get("source_sha256") == digest:
            return review
        return {
            "status": "unreviewed" if spec.engine == "three-float-v1" else "not_applicable",
            "source_sha256": digest,
        }

    @operation("Record an explicit reviewer attestation of the scene-linear source contract for one saved animation revision; this is not automatic shader certification.")
    def record_motion_color_review(
        self, scene_id: str, expected_revision_id: str, reviewer: str, notes: str
    ) -> dict[str, Any]:
        record, spec, rid = self._motion_scene(scene_id)
        self._motion_project(record["project_id"], True)
        if rid != expected_revision_id:
            raise ChromaError("revision_conflict", "动画修订已改变，请重新审核最新源码。")
        if spec.engine != "three-float-v1":
            raise ChromaError("motion_review", "SDR 网页源不适用场景线性源码审核。")
        if not 1 <= len(reviewer.strip()) <= 120 or not 1 <= len(notes.strip()) <= 4000:
            raise ChromaError("motion_review", "请填写审核人（1–120 字）和审核依据（1–4000 字）。")
        self.store.put("motion_color_review", rid, {
            "id": rid, "scene_id": scene_id, "status": "reviewed",
            "source_sha256": spec.source_digest(),
            "source_color_space": spec.source_color_space,
            "reviewer": reviewer.strip(), "notes": notes.strip(), "reviewed_at": now(),
            "basis": "explicit reviewer attestation; not automatic semantic verification",
        })
        return self.get_motion_scene(scene_id)

    @operation(
        "Create a blank float animation by default, or provide an explicit SDR engine, optional template_id or full spec. No template is required."
    )
    def create_motion_scene(
        self,
        project_id: str,
        template_id: str | None = None,
        spec: MotionSpec | None = None,
        engine: MotionEngine | None = None,
        source_color_space: MotionSourceSpace | None = None,
    ) -> dict[str, Any]:
        self._motion_project(project_id, True)
        if engine == "remotion-sdr-v1" and source_color_space not in {None, SOURCE_SPACE}:
            raise ChromaError("motion_color", "SDR 网页源不支持场景线性颜色空间。请选择浮点动画。")
        if spec is None:
            if template_id is None:
                spec = blank_motion(engine or "three-float-v1", source_color_space)
            else:
                if template_id not in templates():
                    raise ChromaError(
                        "motion_template", "请选择列出的示例模板，或省略 template_id 创建空白动画。"
                    )
                spec = templates()[template_id]
        if engine is not None and spec.engine != engine:
            raise ChromaError("motion_engine", "所选引擎与模板或源码的引擎不一致。")
        if source_color_space is not None and spec.source_color_space != source_color_space:
            raise ChromaError("motion_color", "源码颜色空间与模板或 spec 不一致；已有动画请使用升级操作以保持颜色。")
        sid, rid = identifier(), identifier()
        self.store.put(
            "motion_revision",
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
            "motion_scene",
            sid,
            {
                "id": sid,
                "project_id": project_id,
                "name": spec.name,
                "revision_id": rid,
                "updated_at": now(),
            },
        )
        return self.get_motion_scene(sid)

    @operation(
        "Save HDR/SDR animation source with revision-conflict detection; preserves every previous revision."
    )
    def update_motion_scene(
        self, scene_id: str, spec: MotionSpec, expected_revision_id: str
    ) -> dict[str, Any]:
        record, _, _ = self._motion_scene(scene_id)
        self._motion_project(record["project_id"], True)
        rid = identifier()
        self.store.revise_scene(
            scene_id,
            expected_revision_id,
            {
                "id": rid,
                "scene_id": scene_id,
                "project_id": record["project_id"],
                "parent_revision_id": expected_revision_id,
                "scene": spec.model_dump(),
                "created_at": now(),
            },
            motion=True,
        )
        return self.get_motion_scene(scene_id)

    @operation("Upgrade a saved float animation to another linear source space without reinterpreting old RGB. Preserve original source graphs as background layers; create an empty native foreground for continued authoring. Revision checked; old revisions remain unchanged.")
    def upgrade_motion_scene(
        self, scene_id: str, expected_revision_id: str, source_color_space: MotionSourceSpace,
    ) -> dict[str, Any]:
        record, spec, rid = self._motion_scene(scene_id)
        self._motion_project(record["project_id"], True)
        if rid != expected_revision_id:
            raise ChromaError("revision_conflict", "动画修订已改变，请保留草稿并重新读取。", current_revision_id=rid)
        if spec.engine != "three-float-v1":
            raise ChromaError("motion_color", "SDR 截图不能升级为原生宽色域场景。")
        if source_color_space == spec.source_color_space:
            return self.get_motion_scene(scene_id)
        previous = MotionSource(code=spec.code, files=spec.files, source_color_space=spec.source_color_space)
        native = blank_motion(source_color_space=source_color_space)
        layers = list(spec.preserved_layers)
        if spec.code != native.code or spec.files:
            if len(layers) >= 16:
                raise ChromaError("motion_color", "已达 16 个保留图层；请整理源码后再升级。")
            layers.append(previous)
        # Keep arbitrary shader algorithms intact. Source literals cannot safely be
        # rewritten as colors, so conversion happens once at a linear image boundary.
        upgraded = MotionSpec.model_validate({
            **spec.model_dump(), "source_color_space": source_color_space,
            "code": native.code, "files": {},
            "preserved_layers": [layer.model_dump() for layer in layers],
        })
        return self.update_motion_scene(scene_id, upgraded, expected_revision_id)

    @operation(
        "Compile an unsaved HDR/SDR scene into a network-disabled sandbox preview, without saving or submitting a job."
    )
    def compile_motion_preview(self, spec: MotionSpec, hdr_preview: StrictBool = False) -> dict[str, Any]:
        # Changing the compiler/bridge cannot resurrect cached HTML from an older runtime.
        result = compile_preview(canonical(spec.model_dump()), motion_identity()["renderer_sha256"], hdr_preview)
        # Cached compilation gets a fresh bridge identity on every mount, so retry
        # cannot collide with the still-mounted previous successful picture.
        channel = identifier()
        return {
            **result,
            "html": result["html"].replace(result["channel"], channel),
            "channel": channel,
            "source_sha256": hashlib.sha256(canonical(spec.model_dump()).encode()).hexdigest(),
            "transient": True,
        }

    @operation(
        "Encode a bounded 720p chunk at the exact project rational frame rate as verified PQ/HEVC fMP4. Accept GPU-rendered P010 PQ or legacy scene-linear RGBA32F; use binary HTTP to avoid Base64 overhead. No jobs or files are retained; equivalent on HTTP, MCP and CLI.",
        serialized=False,
    )
    def encode_motion_hdr_preview(self, chunk: HDRPreviewChunk) -> dict[str, Any]:
        return encode_hdr_preview(chunk)

    @operation(
        "Render one unsaved HDR/SDR frame to a transient sRGB PNG for model inspection. HDR sources include original ACEScg statistics; SDR sources declare 8-bit screenshot precision."
    )
    def render_motion_frame(self, spec: MotionSpec, frame: int = 0) -> dict[str, Any]:
        import base64
        import tempfile

        from .color import transform
        from .images import write_image
        from .motion_color import decode_float_frame
        from .motion_render import render_float_frames

        if not 0 <= frame < spec.frames:
            raise ChromaError("frame_bounds", "帧索引超出动画范围。")
        if spec.engine == "remotion-sdr-v1":
            from .motion_sdr_preview import render_sdr_frame

            return render_sdr_frame(spec, frame)
        scale = min(1.0, 480 / max(spec.width, spec.height))
        width, height = (
            max(1, int(spec.width * scale + 0.5)),
            max(1, int(spec.height * scale + 0.5)),
        )
        with tempfile.TemporaryDirectory(prefix="chromascript-hdr-preview-") as temporary:
            root = Path(temporary)
            result: dict[str, Any] = {}

            def accept(index: int, path: Path) -> None:
                pixels = decode_float_frame(path.read_bytes(), width, height, source=spec.source_color_space)
                png = root / "preview.png"
                write_image(png, transform(pixels, "sRGB"), space="sRGB", depth=8)
                result.update(
                    frame=index,
                    source_color_space=spec.source_color_space,
                    transient=True,
                    color_space="ACEScg",
                    minimum=pixels.min(axis=(0, 1)).tolist(),
                    maximum=pixels.max(axis=(0, 1)).tolist(),
                    high_rgb_values=int((pixels[..., :3] > 1).sum()),
                    images=[
                        {
                            "data": base64.b64encode(png.read_bytes()).decode(),
                            "mime_type": "image/png",
                            "width": width,
                            "height": height,
                            "bit_depth": 8,
                            "color_space": "sRGB",
                        }
                    ],
                )

            result["gpu"] = render_float_frames(spec, root, [frame], accept, max_dimension=480)
            result["color_configuration"] = color_info()
            return result

    @operation("Estimate animation master/intermediate/video/audio disk budgets and current destination/workspace free space without rendering or creating files. ZIP size is content-dependent. Pass project_id to include its current soundtrack.")
    def estimate_motion_render(
        self,
        settings: MotionRenderSettings,
        engine: MotionEngine = "three-float-v1",
        output: MotionOutput | None = None,
        export_directory: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        output = resolve_motion_output(engine, output)
        destination = resolve_export_directory(
            export_directory if export_directory is not None else str(self.store.root / "exports")
        )
        from .motion_storage import estimate_storage

        if project_id is not None:
            self._motion_project(project_id)
        return estimate_storage(self.store, settings, engine, output, destination, project_id)

    @operation(
        "Queue engine-specific animation rendering: float EXR with optional HDR/SDR video, direct float video (master_bit_depth=null), or SDR screenshot video. Freeze source, rational rate, runtime, color and output settings."
    )
    def submit_motion_render(
        self,
        scene_id: str,
        expected_revision_id: str,
        idempotency_key: str,
        export_directory: str | None = None,
        output: MotionOutput | None = None,
        storage_estimate_id: str | None = None,
    ) -> dict[str, Any]:
        record, spec, rid = self._motion_scene(scene_id)
        self._motion_project(record["project_id"], True)
        if rid != expected_revision_id:
            raise ChromaError(
                "revision_conflict",
                "动画已被其他客户端修改；请保留草稿并重新读取。",
                current_revision_id=rid,
            )
        if not 1 <= len(idempotency_key) <= 200:
            raise ChromaError("idempotency_key", "Supply a key of 1..200 characters")
        if not motion_environment()["render_available"]:
            raise ChromaError(
                "motion_unavailable",
                "本地浮点渲染需要动画依赖和 Chrome；请运行 ./install.sh 并安装 Chrome。",
            )
        output = resolve_motion_output(spec.engine, output)
        precision = None
        if output.video == "prores-4444-acescct":
            precision = prores_capability(self.store.root)
            if not precision["available"]:
                raise ChromaError("motion_encoder", precision["reason"])
        if output.video != "none":
            if output.video == "rec709":
                raise ChromaError("video_color_tags", video_tag_issue("Rec.709") or "缺少精确 Gamma 标签")
            if spec.width % 2 or spec.height % 2:
                raise ChromaError(
                    "motion_dimensions", "动画视频交付要求偶数宽高；EXR 可使用奇数尺寸。"
                )
            encoder = motion_video_preset(output.video).codec
            if encoder not in available_encoders():
                raise ChromaError("motion_encoder", f"本机 FFmpeg 缺少 {encoder}。")
        destination = resolve_export_directory(
            export_directory if export_directory is not None else str(self.store.root / "exports")
        )
        pixels = spec.width * spec.height
        # Budget all simultaneous CPU copies plus GPU targets before allocating. The
        # browser additionally checks its actual texture/renderbuffer/viewport limits.
        working_bytes = pixels * (output.render_scale ** 2 * 96 + 96) + spec.frames * 1024
        if spec.preserved_layers:
            # Layers reuse one scratch RGBA32F target, not one full raster per layer.
            working_bytes += pixels * output.render_scale**2 * 16
        if working_bytes > psutil.virtual_memory().available * 0.7:
            raise ChromaError("motion_memory", "当前可用内存不足；请降低超采样倍率、尺寸或释放内存。", required_bytes=working_bytes)
        estimate = self.estimate_motion_render(
            spec.settings(), spec.engine, output, str(destination), record["project_id"]
        )
        sample = None
        if storage_estimate_id:
            from .motion_sampling import checked_storage_sample
            from .motion_storage import estimate_storage
            sample = checked_storage_sample(
                self.store, storage_estimate_id, spec, output, destination, record["project_id"]
            )
            estimate = estimate_storage(
                self.store, spec.settings(), spec.engine, output, destination,
                record["project_id"], sample["measurements"],
            )
        if not estimate["sufficient"]:
            raise ChromaError(
                "disk_space", "导出盘或工作区没有足够空间存储动画中间帧和交付文件。",
                destination=str(destination), required_bytes=estimate["destination_required_bytes"],
                estimate=estimate,
            )
        snapshot = {
            "engine": spec.engine,
            "output": output.model_dump(),
            "color_configuration": color_info(),
            "kind": "motion",
            "scene": spec.model_dump(),
            "revision_id": rid,
            "settings": spec.settings().model_dump(),
            "runtime": motion_identity(),
            "export_directory": str(destination),
            "export_root": str(destination),  # Freeze this selection, never a global allowlist.
            "storage_layout": DESTINATION_LAYOUT,
            # Persist the volume identity so remounts cannot redirect a frozen job.
            "destination_volume": volume_identity(destination),
            "source_color_review": self._motion_color_review(spec, rid),
        }
        # Dynamic free space is never part of an idempotent render's identity.
        if sample:
            snapshot["storage_estimate"] = {
                "id": sample["id"], "identity": sample["identity"], "measurements": sample["measurements"],
            }
        if precision:
            snapshot["precision_verification"] = precision
        # Both animation engines share the same immutable project audio contract.
        if audio := freeze_audio(self.store, record["project_id"], spec.settings()):
            snapshot["audio"] = audio
        digest = hashlib.sha256(canonical(snapshot).encode()).hexdigest()
        stamp = now()
        job = Job(
            id=identifier(),
            project_id=record["project_id"],
            scene_id=scene_id,
            revision_id=rid,
            kind="motion",
            status="queued",
            settings=spec.settings(),
            created_at=stamp,
            updated_at=stamp,
            snapshot_hash=digest,
            idempotency_key=idempotency_key,
        )
        job.export_path = str(destination / bundle_name(spec.name, job.id))
        return {"job": self.store.create_job(idempotency_key, digest, job.model_dump(), snapshot)}
