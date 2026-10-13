"""Revision-safe React animations; browser pixels never masquerade as ACEScg masters."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator

from .errors import ChromaError
from .exports import bundle_name
from .exports import export_directory as resolve_export_directory
from .models import Job, Model, Rational, RenderSettings
from .operations import operation
from .store import Store, canonical, identifier, now

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
RUNTIME = FRONTEND / "motion" / "runtime.mjs"
IMPORTS = [
    "react",
    "react/jsx-runtime",
    "remotion",
    "@remotion/three",
    "@react-three/fiber",
    "three",
]


class MotionSpec(Model):
    name: str = Field(default="未命名动画", min_length=1, max_length=120)
    code: str = Field(
        min_length=1,
        max_length=200_000,
        description="TSX module with a default React component; animate from useCurrentFrame().",
    )
    width: int = Field(default=1920, ge=16, le=4096)
    height: int = Field(default=1080, ge=16, le=4096)
    frames: int = Field(default=150, ge=1, le=36_000)
    fps: Rational = Field(default_factory=lambda: Rational(numerator=30))

    @model_validator(mode="after")
    def video_dimensions(self) -> MotionSpec:
        # This engine currently delivers H.264 4:2:0; reject odd dimensions before
        # previewing a specification that cannot be encoded without cropping.
        if self.width % 2 or self.height % 2:
            raise ValueError("MP4 画面的宽和高必须为偶数。")
        return self

    def settings(self) -> RenderSettings:
        return RenderSettings(
            width=self.width, height=self.height, frames=self.frames, fps=self.fps, samples=1
        )


def templates() -> dict[str, MotionSpec]:
    return {
        "title": MotionSpec(
            name="文字与图形",
            code="""import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from "remotion";

export default function Animation() {
  const frame = useCurrentFrame();
  const {fps, width} = useVideoConfig();
  // Frame-based time makes seeking, preview and offline rendering agree.
  const enter = spring({frame, fps, config: {damping: 18}});
  const slide = interpolate(enter, [0, 1], [80, 0]);
  return (
    <AbsoluteFill style={{backgroundColor: "#172b3a", color: "#e7f0f4", justifyContent: "center", padding: width * 0.08, fontFamily: '"PingFang SC", sans-serif'}}>
      <div style={{opacity: enter, transform: `translateY(${slide}px)`}}>
        <div style={{fontSize: width * 0.018, letterSpacing: "0.2em", color: "#8cc8d2"}}>CHROMASCRIPT / MOTION</div>
        <h1 style={{fontSize: width * 0.075, margin: "24px 0", fontWeight: 600}}>让代码动起来</h1>
        <div style={{height: 5, width: width * 0.32 * enter, backgroundColor: "#8cc8d2"}} />
        <p style={{fontSize: width * 0.023, opacity: 0.8}}>编辑左侧代码，即时预览你的下一帧。</p>
      </div>
    </AbsoluteFill>
  );
}
""",
        ),
        "three": MotionSpec(
            name="三维旋转几何",
            code="""import {AbsoluteFill, useCurrentFrame, useVideoConfig} from "remotion";
import {ThreeCanvas} from "@remotion/three";

export default function Animation() {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  // Never accumulate rotation with useFrame/delta: every frame is independent.
  const time = frame / fps;
  return (
    <AbsoluteFill style={{backgroundColor: "#172b3a"}}>
      <ThreeCanvas width={width} height={height} camera={{position: [0, 0, 5], fov: 42}} gl={{antialias: true}}>
        <ambientLight intensity={0.7} />
        <directionalLight position={[3, 4, 5]} intensity={3} />
        <directionalLight position={[-3, -2, 2]} intensity={1.5} color="#b3dde8" />
        <mesh rotation={[time * 0.35, time * 0.6, 0.15]}>
          <torusKnotGeometry args={[0.85, 0.27, 128, 24]} />
          <meshStandardMaterial color="#88bbc8" metalness={0.45} roughness={0.28} />
        </mesh>
      </ThreeCanvas>
      <div style={{position: "absolute", left: "7%", bottom: "8%", color: "#e7f0f4", fontFamily: '"PingFang SC", sans-serif', fontSize: width * 0.027}}>Three.js · 每一帧由代码定义</div>
    </AbsoluteFill>
  );
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
        "@remotion/player",
        "@remotion/renderer",
        "@remotion/bundler",
        "@remotion/three",
        "@react-three/fiber",
        "react",
        "react-dom",
        "three",
        "esbuild",
        "typescript",
    ]:
        path = FRONTEND / "node_modules" / name / "package.json"
        packages[name] = json.loads(path.read_text())["version"] if path.is_file() else None
    return {
        "node": shutil.which("node"),
        "browser": browser_executable(),
        "packages": packages,
        "preview_available": bool(shutil.which("node") and all(packages.values())),
        "render_available": bool(
            shutil.which("node")
            and all(packages.values())
            and browser_executable()
            and shutil.which("ffmpeg")
        ),
        "imports": IMPORTS,
        "output": "MP4 / H.264 / yuv420p / 8-bit sRGB / no audio",
        "source_precision": "8-bit browser screenshot; no scene-linear master",
    }


def motion_identity() -> dict[str, Any]:
    digest = hashlib.sha256()
    for path in [
        RUNTIME,
        FRONTEND / "motion" / "preview.tsx",
        FRONTEND / "package-lock.json",
        Path(__file__),
        Path(__file__).with_name("motion_worker.py"),
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
            [env["node"], str(RUNTIME), "preview"],
            input=payload,
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
        return record, MotionSpec.model_validate(revision["scene"]), revision["id"]

    @operation(
        "Discover local Remotion/Three.js dependencies, allowed imports and honest browser output precision."
    )
    def get_motion_capabilities(self) -> dict[str, Any]:
        return motion_environment()

    @operation("Read complete editable TSX templates for 2D Remotion and Three.js animations.")
    def list_motion_templates(self) -> dict[str, Any]:
        return {
            "items": [
                {"id": key, "name": spec.name, "spec": spec.model_dump()}
                for key, spec in templates().items()
            ]
        }

    @operation("List a project's saved React animations with bounded pagination.")
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
        "Read current or historical immutable React animation source and its rational timebase."
    )
    def get_motion_scene(self, scene_id: str, revision_id: str | None = None) -> dict[str, Any]:
        record, spec, rid = self._motion_scene(scene_id, revision_id)
        return {"record": record, "scene": spec.model_dump(), "revision_id": rid}

    @operation(
        "Create a React animation from a local template or an explicit strict TSX specification."
    )
    def create_motion_scene(
        self, project_id: str, template_id: str = "title", spec: MotionSpec | None = None
    ) -> dict[str, Any]:
        self._motion_project(project_id, True)
        if spec is None:
            if template_id not in templates():
                raise ChromaError("motion_template", "请选择文字与图形或三维旋转几何模板。")
            spec = templates()[template_id]
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
        "Save React animation source with revision-conflict detection; preserves every previous revision."
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
        "Compile an unsaved TSX animation into a network-disabled sandbox preview, without saving or submitting a job."
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
        "Queue local Remotion frame rendering and exact-rate H.264 MP4 export from a frozen saved revision."
    )
    def submit_motion_render(
        self,
        scene_id: str,
        expected_revision_id: str,
        idempotency_key: str,
        export_directory: str | None = None,
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
                "本地渲染需要动画依赖、FFmpeg 和 Chrome；请运行 ./install.sh 并安装 Chrome。",
            )
        # PNG screenshots are display-referred 8-bit sRGB, not float ACEScg.
        # Preserve the rational rate in Python/FFmpeg even though JS time is numeric.
        required = spec.width * spec.height * 4 * spec.frames + 64 * 1024**2
        if shutil.disk_usage(self.store.root).free < required:
            raise ChromaError("disk_space", "没有足够空间存储动画帧，请缩短动画或降低分辨率。")
        destination = resolve_export_directory(
            export_directory if export_directory is not None else str(self.store.root / "exports")
        )
        snapshot = {
            "engine": "remotion",
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
