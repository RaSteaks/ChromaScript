"""Browser animations preserve revisions, isolation and actual file time/precision."""

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from chromascript.errors import ChromaError
from chromascript.models import Rational
from chromascript.motion import MotionSpec, motion_environment, templates
from chromascript.motion_worker import browser_pixels
from chromascript.operations import REGISTRY, execute
from chromascript.service import Service
from chromascript.worker import run


@pytest.mark.parametrize(
    "changes",
    [
        {"width": 31},
        {"height": 4098},
        {"frames": 0},
        {"code": ""},
        {"unexpected": True},
        {"fps": {"numerator": 241}},
    ],
)
def test_strict_browser_spec(changes):
    with pytest.raises(ValidationError):
        MotionSpec.model_validate({**templates()["title"].model_dump(), **changes})


def test_motion_revisions_are_atomic_and_separate_from_wgsl(tmp_path):
    service = Service(tmp_path)
    project = service.create_project("motion")["project"]["id"]
    saved = service.create_motion_scene(project, template_id="three")
    original = saved["scene"]
    updated = service.update_motion_scene(
        saved["record"]["id"],
        MotionSpec.model_validate({**original, "name": "updated"}),
        saved["revision_id"],
    )
    assert updated["revision_id"] != saved["revision_id"]
    with pytest.raises(ChromaError, match="Read the latest"):
        service.update_motion_scene(
            saved["record"]["id"], MotionSpec.model_validate(original), saved["revision_id"]
        )
    assert len(service.store.list("motion_revision")) == 2
    assert (
        service.get_motion_scene(saved["record"]["id"], saved["revision_id"])["scene"] == original
    )
    assert service.get_motion_scene(saved["record"]["id"])["scene"]["name"] == "updated"
    assert not service.store.list("scene") and not service.store.list("revision")
    assert service.list_motion_scenes(project)["total"] == 1


def test_submission_freezes_source_rate_destination_and_idempotency(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "chromascript.motion.motion_environment", lambda: {"render_available": True}
    )
    monkeypatch.setattr("chromascript.motion.motion_identity", lambda: {"test_runtime": 1})
    service = Service(tmp_path)
    project = service.create_project("frozen")["project"]["id"]
    spec = templates()["title"].model_copy(
        update={
            "width": 32,
            "height": 18,
            "frames": 3,
            "fps": Rational(numerator=30000, denominator=1001),
        }
    )
    saved = service.create_motion_scene(project, spec=spec)
    args = {
        "scene_id": saved["record"]["id"],
        "expected_revision_id": saved["revision_id"],
        "idempotency_key": "same-request",
    }
    job = execute(service, "submit_motion_render", args)["job"]
    assert execute(service, "submit_motion_render", args)["job"]["id"] == job["id"]
    snapshot = service.store.get("snapshot", job["id"])
    assert snapshot["engine"] == "remotion" and "color_configuration" not in snapshot
    assert snapshot["settings"]["fps"]["numerator"] == 30000
    assert snapshot["settings"]["fps"]["denominator"] == 1001
    service.update_motion_scene(
        saved["record"]["id"],
        spec.model_copy(update={"code": spec.code + "\n// newer draft"}),
        saved["revision_id"],
    )
    assert snapshot["scene"]["code"] == spec.code
    with pytest.raises(ChromaError) as conflict:
        execute(service, "submit_motion_render", args)
    assert conflict.value.as_dict()["code"] == "revision_conflict"
    outside = service.submit_motion_render(
        saved["record"]["id"],
        service.get_motion_scene(saved["record"]["id"])["revision_id"],
        "outside",
        export_directory=str(tmp_path.parent / "outside"),
    )["job"]
    assert Path(outside["export_path"]).parent == tmp_path.parent / "outside"
    assert service.store.get("snapshot", outside["id"])["export_root"] == str(
        tmp_path.parent / "outside"
    )
    assert set(REGISTRY) >= {
        "get_motion_capabilities",
        "list_motion_templates",
        "list_motion_scenes",
        "get_motion_scene",
        "create_motion_scene",
        "update_motion_scene",
        "compile_motion_preview",
        "submit_motion_render",
    }


def test_job_kind_filter_precedes_pagination(tmp_path):
    service = Service(tmp_path)
    for i in range(5):
        service.store.put(
            "job", str(i), {"id": str(i), "project_id": "p", "kind": "motion" if i < 2 else "frame"}
        )
    reply = service.list_jobs("p", kind="motion", limit=1)
    assert reply["total"] == 2 and reply["items"][0]["id"] == "1"


def test_transparency_flattens_browser_light_without_aces(tmp_path):
    pixels = np.zeros((16, 16, 4), dtype=np.uint8)
    pixels[..., 0] = 255
    pixels[..., 3] = 128
    path = tmp_path / "alpha.png"
    Image.fromarray(pixels).save(path)
    spec = templates()["title"].model_copy(update={"width": 16, "height": 16})
    actual = browser_pixels(path, spec)
    expected = 1.055 * (128 / 255) ** (1 / 2.4) - 0.055
    assert actual[0, 0, 0] == pytest.approx(expected)
    assert np.all(actual[..., 3] == 1) and np.all(actual[..., 1:3] == 0)


@pytest.mark.skipif(
    not motion_environment()["preview_available"],
    reason="local npm animation dependencies required",
)
def test_preview_compiles_unsaved_source_without_state_writes(tmp_path):
    service = Service(tmp_path)
    spec = templates()["title"]
    before = service.store.list("motion_revision")
    reply = execute(service, "compile_motion_preview", {"spec": spec.model_dump()})
    assert reply["transient"] and reply["channel"] and "Content-Security-Policy" in reply["html"]
    retried = execute(service, "compile_motion_preview", {"spec": spec.model_dump()})
    assert retried["channel"] != reply["channel"]
    assert retried["channel"] in retried["html"] and reply["channel"] not in retried["html"]
    assert "connect-src 'none'" in reply["html"] and "unsafe-eval" not in reply["html"]
    assert service.store.list("motion_revision") == before
    assert not service.store.list("job") and not service.store.list("artifact")


@pytest.mark.skipif(
    not motion_environment()["preview_available"],
    reason="local npm animation dependencies required",
)
@pytest.mark.parametrize(
    "code",
    [
        'import data from "/etc/passwd"; export default () => null;',
        'export {default} from "../package.json";',
        'const x = import("http://example.invalid/code.js"); export default () => null;',
        'const name = "three"; const x = import(name); export default () => null;',
        'const x = require("fs"); export default () => null;',
        'import fs = require("fs"); export default () => null;',
    ],
)
def test_source_imports_cannot_escape_browser_allowlist(tmp_path, code):
    with pytest.raises(ChromaError) as error:
        Service(tmp_path).compile_motion_preview(MotionSpec(code=code))
    assert error.value.as_dict()["code"] == "motion_compile"


@pytest.mark.integration
@pytest.mark.skipif(
    not motion_environment()["render_available"], reason="local Chrome/npm/FFmpeg required"
)
@pytest.mark.parametrize("template", ["title", "three"])
def test_real_browser_video_and_source_bundle(tmp_path: Path, template):
    service = Service(tmp_path)
    project = service.create_project("actual frames")["project"]["id"]
    spec = templates()[template].model_copy(
        update={
            "width": 320,
            "height": 180,
            "frames": 3,
            "fps": Rational(numerator=30000, denominator=1001),
        }
    )
    saved = service.create_motion_scene(project, spec=spec)
    job = service.submit_motion_render(saved["record"]["id"], saved["revision_id"], template)["job"]
    run(tmp_path, job["id"])
    result = service.get_job(job["id"])
    assert result["job"]["status"] == "completed", result["job"]["error"]
    assert result["job"]["export_completed"]
    bundle = Path(result["job"]["export_path"])
    report = json.loads((bundle / "verification.json").read_text())
    assert report["verification"]["passed"]
    assert report["verification"]["verified_frame_rate"] == "30000/1001"
    assert report["verification"]["source_precision"] == "8-bit browser screenshot"
    assert report["verification"]["scene_linear_master"] is False
    assert report["verification"]["color_encoding"]["output_transform"] is None
    assert report["source_sha256"] == hashlib.sha256(spec.code.encode()).hexdigest()
    assert (bundle / "Animation.tsx").read_text() == spec.code
    assert not list(tmp_path.rglob("*.exr"))
    frames = sorted((tmp_path / "jobs" / job["id"] / "motion" / "frames").glob("*.png"))
    assert len(frames) == spec.frames
    assert frames[0].read_bytes() != frames[-1].read_bytes()
