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

from pydantic import Field

from .color import color_info, output_metadata
from .encoding import available_encoders
from .errors import ChromaError
from .exports import bundle_name
from .exports import export_directory as resolve_export_directory
from .models import Job, Model, Rational, RenderSettings
from .motion_color import display_shader
from .operations import operation
from .store import Store, canonical, identifier, now

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
RUNTIME = FRONTEND / "motion" / "runtime.mjs"
SDR_RUNTIME = FRONTEND / "motion" / "sdr-runtime.mjs"
MotionEngine = Literal["three-float-v1", "remotion-sdr-v1"]
IMPORTS = ["three"]


class MotionOutput(Model):
    master_bit_depth: Literal[16, 32] | None = 32
    video: Literal["none", "pq", "hlg", "srgb", "rec709"] = "pq"


class MotionSpec(Model):
    engine: MotionEngine = "three-float-v1"
    name: str = Field(default="未命名动画", min_length=1, max_length=120)
    code: str = Field(
        min_length=1,
        max_length=200_000,
        description="For three-float-v1: Three.js factory returning scene/camera/update; linear Rec.709 float RGB converts to ACEScg. For remotion-sdr-v1: default React TSX component using useCurrentFrame; display-referred sRGB screenshot.",
    )
    width: int = Field(default=1920, ge=16, le=4096)
    height: int = Field(default=1080, ge=16, le=4096)
    frames: int = Field(default=150, ge=1, le=36_000)
    fps: Rational = Field(default_factory=lambda: Rational(numerator=30))

    def settings(self) -> RenderSettings:
        return RenderSettings(
            width=self.width, height=self.height, frames=self.frames, fps=self.fps, samples=1
        )


def blank_motion(engine: MotionEngine = "remotion-sdr-v1") -> MotionSpec:
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
        "contract": "motion-dual-v1",
        "engines": {
            "three-float-v1": {
                "imports": ["three"],
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
        "output": "HDR and SDR templates; engine-specific EXR/PQ/HLG/sRGB/Rec.709 delivery",
        "preview": "sRGB: OCIO ACES view for float scenes; native sRGB for SDR scenes",
        "source_precision": "engine-specific: RGBA32F float or 8-bit sRGB screenshot",
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
        Path(__file__).with_name("motion_color.py"),
        Path(__file__).with_name("color.py"),
        FRONTEND / "package-lock.json",
        Path(__file__),
        Path(__file__).with_name("motion_worker.py"),
        Path(__file__).with_name("motion_render.py"),
        Path(__file__).with_name("encoding.py"),
        Path(__file__).with_name("images.py"),
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
def compile_preview(payload: str, renderer_fingerprint: str = "") -> dict[str, Any]:
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
            input=canonical({"spec": json.loads(payload), "display": display_shader()}),
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
        "Discover HDR/SDR source engines, per-engine imports and precision, dependencies and preview/master color spaces."
    )
    def get_motion_capabilities(self) -> dict[str, Any]:
        return motion_environment()

    @operation(
        "Discover valid HDR/SDR outputs for the selected source engine; SDR screenshots never claim float/HDR source precision."
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
            {"value": v, "label": label, "available": "libx264" in encoders}
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
                    "color_encoding": output_metadata(v.upper()),
                }
                for v in ("pq", "hlg")
            ]
        return {
            "contract": "motion-dual-v1",
            "engine": engine,
            "master_bit_depths": [16, 32] if floating else [],
            "master_color_space": "ACEScg" if floating else None,
            "alpha": "straight" if floating else "composited on black",
            "default": (
                MotionOutput() if floating else MotionOutput(master_bit_depth=None, video="srgb")
            ).model_dump(),
            "videos": choices,
            "scene_linear_master": floating,
            "preview": "sRGB SDR; independent of delivery",
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
        return {"record": record, "scene": spec.model_dump(), "revision_id": rid}

    @operation(
        "Create a blank animation by default (SDR unless engine is specified), or provide an optional template_id or full spec. No template is required."
    )
    def create_motion_scene(
        self,
        project_id: str,
        template_id: str | None = None,
        spec: MotionSpec | None = None,
        engine: MotionEngine | None = None,
    ) -> dict[str, Any]:
        self._motion_project(project_id, True)
        if spec is None:
            if template_id is None:
                spec = blank_motion(engine or "remotion-sdr-v1")
            else:
                if template_id not in templates():
                    raise ChromaError(
                        "motion_template", "请选择列出的示例模板，或省略 template_id 创建空白动画。"
                    )
                spec = templates()[template_id]
        if engine is not None and spec.engine != engine:
            raise ChromaError("motion_engine", "所选引擎与模板或源码的引擎不一致。")
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

    @operation(
        "Compile an unsaved HDR/SDR scene into a network-disabled sandbox preview, without saving or submitting a job."
    )
    def compile_motion_preview(self, spec: MotionSpec) -> dict[str, Any]:
        # Changing the compiler/bridge cannot resurrect cached HTML from an older runtime.
        result = compile_preview(canonical(spec.model_dump()), motion_identity()["renderer_sha256"])
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
                pixels = decode_float_frame(path.read_bytes(), width, height)
                png = root / "preview.png"
                write_image(png, transform(pixels, "sRGB"), space="sRGB", depth=8)
                result.update(
                    frame=index,
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

    @operation(
        "Queue engine-specific animation rendering: float EXR plus HDR/SDR videos, or SDR screenshot video. Freeze source, rational rate, runtime, color and output settings."
    )
    def submit_motion_render(
        self,
        scene_id: str,
        expected_revision_id: str,
        idempotency_key: str,
        export_directory: str | None = None,
        output: MotionOutput | None = None,
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
        floating = spec.engine == "three-float-v1"
        output = output or (
            MotionOutput() if floating else MotionOutput(master_bit_depth=None, video="srgb")
        )
        if not floating and (
            output.master_bit_depth is not None or output.video not in {"srgb", "rec709"}
        ):
            raise ChromaError(
                "motion_output",
                "SDR 网页源码支持 sRGB/Rec.709 视频，不能生成原生 HDR 或 ACEScg 浮点母版。",
            )
        if floating and output.master_bit_depth is None:
            raise ChromaError("motion_output", "浮点场景需要选择 16/32-bit EXR 母版精度。")
        if output.video != "none":
            if spec.width % 2 or spec.height % 2:
                raise ChromaError(
                    "motion_dimensions", "HEVC 4:2:0 要求偶数宽高；EXR 可使用奇数尺寸。"
                )
            encoder = "libx265" if output.video in {"pq", "hlg"} else "libx264"
            if encoder not in available_encoders():
                raise ChromaError("motion_encoder", f"本机 FFmpeg 缺少 {encoder}。")
        # Budget raw transport, EXR, archive and both destination copies on one volume.
        required = spec.width * spec.height * 16 * spec.frames * 5 + 64 * 1024**2
        if shutil.disk_usage(self.store.root).free < required:
            raise ChromaError("disk_space", "没有足够空间存储动画帧，请缩短动画或降低分辨率。")
        destination = resolve_export_directory(
            export_directory if export_directory is not None else str(self.store.root / "exports")
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
        }
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
