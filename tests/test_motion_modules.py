"""Multi-file source is one revision, one timebase and one frozen render graph."""

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from chromascript.errors import ChromaError
from chromascript.images import read_image
from chromascript.motion import MotionOutput, MotionSpec, templates
from chromascript.motion_sources import source_hash, write_sources
from chromascript.operations import catalogue, execute
from chromascript.service import Service
from chromascript.worker import run


def chapters(engine):
    # The shipped examples also exercise nested imports and portable chapter boundaries.
    name = "float.json" if engine == "three-float-v1" else "sdr.json"
    path = Path(__file__).resolve().parents[1] / "examples" / "motion-chapters" / name
    return MotionSpec.model_validate(
        {
            **json.loads(path.read_text()),
            "width": 32,
            "height": 18,
            "frames": 4,
        }
    )


@pytest.mark.parametrize(
    "files",
    [
        {"../escape.ts": "export {};"},
        {"/tmp/escape.ts": "export {};"},
        {"a\\b.ts": "export {};"},
        {"a//b.ts": "export {};"},
        {"Animation.ts": "export {};"},
        {"ANIMATION.tsx": "export {};"},
        {"one.ts": "export {};", "ONE.ts": "export {};"},
        {"chapters/intro.ts": "export {};", "Chapters/outro.ts": "export {};"},
        {"chapters/Intro/a.ts": "export {};", "chapters/intro/b.ts": "export {};"},
        {"Chapters/outro.ts": "export {};", "chapters/intro.ts": "export {};"},
        {"bad.json": "{}"},
        {"one.ts": " "},
        {"one.ts": 1},
        {"one.ts": "x" * 200001},
        {f"chapter{i}.ts": "export {};" for i in range(129)},
        {f"chapter{i}.ts": "x" * 200000 for i in range(10)},
    ],
)
def test_module_paths_sizes_and_types_are_validated(files):
    with pytest.raises(ValidationError):
        MotionSpec(code="export {};", files=files)


@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.parametrize("engine", ["three-float-v1", "remotion-sdr-v1"])
def test_compiler_and_export_agree_on_directory_spelling(tmp_path, conflict, engine):
    # Test the compiler directly so Python validation cannot hide a compiler bypass.
    files = {"chapters/Intro/a.ts": "export {};", "chapters/Intro/b.ts": "export {};"}
    if conflict:
        files["chapters/intro/b.ts"] = files.pop("chapters/Intro/b.ts")
    spec = {"engine": engine, "code": "export {};", "files": files}
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            """
import {sourceModules} from './motion/source-modules.mjs';
let input = '';
for await (const chunk of process.stdin) input += chunk;
sourceModules(JSON.parse(input), new Set(), process.cwd());
""",
        ],
        cwd=Path(__file__).resolve().parents[1] / "frontend",
        input=json.dumps(spec),
        text=True,
        capture_output=True,
        check=False,
    )
    if conflict:
        assert result.returncode != 0 and "目录大小写不一致" in result.stderr
        with pytest.raises(ValueError, match="目录大小写不一致"):
            write_sources(tmp_path, engine, spec["code"], files)
        assert not list(tmp_path.iterdir())
    else:
        assert result.returncode == 0, result.stderr
        paths = write_sources(tmp_path, engine, spec["code"], files)
        actual = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()}
        assert actual == {p.relative_to(tmp_path).as_posix() for p in paths}


@pytest.mark.parametrize("engine", ["three-float-v1", "remotion-sdr-v1"])
def test_compile_graph_and_chapter_changes_affect_preview(tmp_path, engine):
    service = Service(tmp_path)
    spec = chapters(engine)
    reply = execute(service, "compile_motion_preview", {"spec": spec.model_dump()})
    assert reply["transient"] and "Content-Security-Policy" in reply["html"]
    files = {**spec.files, "unused.ts": "export const value = 1;"}
    second = service.compile_motion_preview(spec.model_copy(update={"files": files}))
    assert reply["source_sha256"] != second["source_sha256"]
    assert not service.store.list("motion_revision")
    for operation in catalogue():
        if operation["name"] in {
            "chromascript.create_motion_scene",
            "chromascript.update_motion_scene",
            "chromascript.compile_motion_preview",
            "chromascript.render_motion_frame",
        }:
            assert "files" in operation["inputSchema"]["$defs"]["MotionSpec"]["properties"]


@pytest.mark.parametrize("engine", ["three-float-v1", "remotion-sdr-v1"])
@pytest.mark.parametrize(
    "source",
    [
        'export {default} from "./missing";',
        'import "/etc/passwd";',
        'export {default} from "../../outside.ts";',
        'import "https://example.invalid/remote.js";',
        'import "node:fs";',
        'const name="./intro"; import(name);',
        'require("fs");',
        'import fs = require("fs");',
        'import type {Thing} from "./missing";',
    ],
)
def test_all_chapters_enforce_offline_import_policy(tmp_path, engine, source):
    # Even an unused chapter must not bypass validation with a type-only import.
    spec = chapters(engine)
    spec.files["chapters/invalid.ts"] = source
    with pytest.raises(ChromaError, match="invalid.ts"):
        Service(tmp_path).compile_motion_preview(spec)


def test_revision_review_and_job_freeze_whole_graph(tmp_path, monkeypatch):
    monkeypatch.setattr("chromascript.motion.motion_identity", lambda: {"test": 1})
    monkeypatch.setattr(
        "chromascript.motion.motion_environment", lambda: {"render_available": True}
    )
    service = Service(tmp_path)
    project = service.create_project("chapters")["project"]["id"]
    spec = chapters("three-float-v1")
    saved = execute(
        service, "create_motion_scene", {"project_id": project, "spec": spec.model_dump()}
    )
    scene_id, revision = saved["record"]["id"], saved["revision_id"]
    review = service.record_motion_color_review(scene_id, revision, "test", "linear colors")
    job = service.submit_motion_render(scene_id, revision, "frozen")["job"]
    frozen = service.store.get("snapshot", job["id"])
    changed = spec.model_copy(
        update={
            "files": {
                **spec.files,
                "shared/colors.js": "export const red=[1,0,0]; export const green=[0,1,0];",
            }
        }
    )
    updated = execute(
        service,
        "update_motion_scene",
        {"scene_id": scene_id, "expected_revision_id": revision, "spec": changed.model_dump()},
    )
    assert frozen["scene"]["files"] == spec.files
    assert service.get_motion_scene(scene_id, revision)["scene"]["files"] == spec.files
    assert updated["color_review"]["status"] == "unreviewed"
    assert service.get_motion_scene(scene_id, revision)["color_review"] == review["color_review"]
    assert source_hash(spec.code, spec.files) != source_hash(changed.code, changed.files)
    with pytest.raises(ChromaError, match="Read the latest"):
        service.update_motion_scene(scene_id, spec, revision)


@pytest.mark.integration
@pytest.mark.parametrize("engine", ["three-float-v1", "remotion-sdr-v1"])
def test_actual_chapter_render_and_portable_source_bundle(tmp_path, engine):
    service = Service(tmp_path / "workspace")
    project = service.create_project("joint render")["project"]["id"]
    spec = chapters(engine)
    saved = service.create_motion_scene(project, spec=spec)
    floating = engine == "three-float-v1"
    job = service.submit_motion_render(
        saved["record"]["id"],
        saved["revision_id"],
        "render-chapters",
        export_directory=str(tmp_path / "output"),
        output=MotionOutput(
            master_bit_depth=32 if floating else None,
            video="none" if floating else "srgb",
            render_scale=1,
        ),
    )["job"]
    # Editing after submission cannot affect the queued chapter content.
    service.update_motion_scene(
        saved["record"]["id"], templates()["radiance"], saved["revision_id"]
    )
    run(service.store.root, job["id"])
    result = service.get_job(job["id"])
    assert result["job"]["status"] == "completed", result["job"]["error"]
    bundle = Path(result["job"]["export_path"])
    entry = "Animation.ts" if floating else "Animation.tsx"
    assert (bundle / "sources" / entry).read_text() == spec.code
    for path, contents in spec.files.items():
        assert (bundle / "sources" / path).read_text() == contents
    report = json.loads((bundle / "verification.json").read_text())
    assert report["source_sha256"] == source_hash(spec.code, spec.files)
    assert report["fps"]["numerator"] == 30000 and report["fps"]["denominator"] == 1001
    if floating:
        frames = sorted((bundle / "masters").glob("*.exr"))
        assert len(frames) == 4
        first, _ = read_image(frames[0])
        last, _ = read_image(frames[2])
        assert not np.allclose(first, last)
        assert first[..., 0].mean() > first[..., 1].mean()
        assert last[..., 1].mean() > last[..., 0].mean()
    else:
        assert report["probe"]["streams"][0]["nb_frames"] == "4"
