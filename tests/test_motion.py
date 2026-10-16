"""Browser animations preserve revisions, isolation and actual file time/precision."""

import hashlib
import json
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from chromascript.color import transform
from chromascript.errors import ChromaError
from chromascript.images import read_image
from chromascript.models import Rational
from chromascript.motion import MotionOutput, MotionSpec, motion_environment, templates
from chromascript.motion_color import decode_float_frame
from chromascript.motion_render import render_float_frames
from chromascript.operations import REGISTRY, execute
from chromascript.service import Service
from chromascript.worker import run


@pytest.mark.parametrize(
    "changes",
    [
        {"width": 0},
        {"height": 2_147_483_648},
        {"frames": 0},
        {"code": ""},
        {"unexpected": True},
        {"fps": {"numerator": 0}},
        {"width": True},
        {"fps": {"numerator": 24.5}},
    ],
)
def test_strict_browser_spec(changes):
    with pytest.raises(ValidationError):
        MotionSpec.model_validate({**templates()["radiance"].model_dump(), **changes})


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
    spec = templates()["radiance"].model_copy(
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
    assert snapshot["engine"] == "three-float-v1" and "color_configuration" in snapshot
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
        "get_motion_delivery_options",
        "render_motion_frame",
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


def test_float_decoder_preserves_extended_rgb_alpha_and_row_order():
    # A linear associated buffer must be flipped/unassociated before AP1 conversion.
    pixels = np.zeros((16, 16, 4), dtype="<f4")
    pixels[..., :3] = [-0.125, 2.0, 0.1234567]
    pixels[..., 3] = 0.5
    pixels[0, 0] = [0, 0, 0, 0]
    expected = pixels[::-1].copy()
    expected[..., :3] = np.divide(
        expected[..., :3],
        expected[..., 3:4],
        out=np.zeros_like(expected[..., :3]),
        where=expected[..., 3:4] > 0,
    )
    expected = transform(expected, "ACEScg", source="Linear Rec.709 (sRGB)")
    actual = decode_float_frame(pixels.tobytes(), 16, 16)
    np.testing.assert_array_equal(actual, expected)
    assert np.max(actual[..., :3]) > 1
    np.testing.assert_array_equal(actual[..., 3], pixels[::-1, :, 3])
    for invalid in [
        b"short",
        np.full((16, 16, 4), np.nan, dtype="<f4").tobytes(),
        np.full((16, 16, 4), 2, dtype="<f4").tobytes(),
    ]:
        with pytest.raises(ChromaError):
            decode_float_frame(invalid, 16, 16)


def test_output_contract_rejects_sdr_and_invalid_depth(tmp_path):
    for changes in [{"video": "invalid"}, {"master_bit_depth": 8}, {"unexpected": True}]:
        with pytest.raises(ValidationError):
            MotionOutput.model_validate(changes)
    service = Service(tmp_path)
    project = service.create_project("odd EXR")["project"]["id"]
    spec = templates()["radiance"].model_copy(update={"width": 31, "height": 17, "frames": 1})
    saved = service.create_motion_scene(project, spec=spec)
    with pytest.raises(ChromaError, match="偶数"):
        # The default is master-only; explicitly request a subsampled video.
        service.submit_motion_render(saved["record"]["id"], saved["revision_id"], "invalid", output=MotionOutput(video="pq"))
    job = service.submit_motion_render(
        saved["record"]["id"], saved["revision_id"], "exr", output=MotionOutput(video="none")
    )["job"]
    assert service.store.get("snapshot", job["id"])["output"]["video"] == "none"


@pytest.mark.skipif(
    not motion_environment()["preview_available"],
    reason="local npm animation dependencies required",
)
def test_preview_compiles_unsaved_source_without_state_writes(tmp_path):
    service = Service(tmp_path)
    spec = templates()["radiance"]
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
@pytest.mark.parametrize(
    "template,depth,video", [("radiance", 32, "pq"), ("three", 16, "hlg"), ("radiance", 32, "none")]
)
def test_real_hdr_masters_video_and_recovery(tmp_path: Path, template, depth, video, monkeypatch):
    service = Service(tmp_path)
    project = service.create_project("actual float frames")["project"]["id"]
    spec = templates()[template].model_copy(
        update={
            "width": 64 if video != "none" else 63,
            "height": 36,
            "frames": 3,
            "fps": Rational(numerator=30000, denominator=1001),
        }
    )
    saved = service.create_motion_scene(project, spec=spec)
    job = service.submit_motion_render(
        saved["record"]["id"],
        saved["revision_id"],
        template,
        output=MotionOutput(master_bit_depth=depth, video=video),
    )["job"]
    run(tmp_path, job["id"])
    result = service.get_job(job["id"])
    assert result["job"]["status"] == "completed", result["job"]["error"]
    assert result["job"]["export_completed"]
    bundle = Path(result["job"]["export_path"])
    report = json.loads((bundle / "verification.json").read_text())
    assert report["verification"]["passed"] and report["verification"]["scene_linear_master"]
    assert report["verification"]["verified_frame_rate"] == "30000/1001"
    assert report["gpu"]["format"] == "RGBA32F"
    assert report["source_sha256"] == hashlib.sha256(spec.code.encode()).hexdigest()
    assert (bundle / "Animation.ts").read_text() == spec.code
    frames = sorted((bundle / "masters").glob("*.exr"))
    assert len(frames) == spec.frames
    pixels, metadata = read_image(frames[0])
    assert metadata["storage"] == ("float" if depth == 32 else "half")
    assert metadata["colorInteropID"] == "lin_ap1_scene"
    assert pixels[..., :3].max() > 1
    assert not np.array_equal(pixels, read_image(frames[-1])[0])
    if video != "none":
        verified = report["verification"]["video"]
        assert verified["passed"] and verified["verified_frame_rate"] == "30000/1001"
        assert report["probe"]["streams"][0]["color_transfer"] == (
            "smpte2084" if video == "pq" else "arib-std-b67"
        )
        if video == "pq":
            # Decode the actual video to PQ RGB, then apply ST 2084 EOTF. The
            # radiance fixture must contain >100-nit luminance, not just HDR tags.
            raw = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    str(bundle / "animation-pq.mp4"),
                    "-frames:v",
                    "1",
                    "-vf",
                    "zscale=matrixin=2020_ncl:matrix=gbr:rangein=limited:range=full,format=gbrpf32le",
                    "-f",
                    "rawvideo",
                    "pipe:1",
                ],
                capture_output=True,
                check=True,
            )
            gbr = np.frombuffer(raw.stdout, dtype="<f4").reshape(3, spec.height, spec.width)
            rgb = np.clip(gbr[[2, 0, 1]].astype(np.float64), 0, 1)
            power = rgb ** (1 / (2523 / 32))
            nits = 10000 * (
                np.maximum(power - 3424 / 4096, 0) / (2413 / 128 - 2392 / 128 * power)
            ) ** (1 / (2610 / 16384))
            luminance = np.einsum("c,chw->hw", [0.2627, 0.678, 0.0593], nits)
            assert luminance.max() > 100
    else:
        assert report["probe"] is None and not list(bundle.glob("*.mp4"))
    manifest = json.loads((bundle / "masters" / "sequence.json").read_text())
    assert all(entry["passed"] for entry in manifest["frames"].values())
    master = next(a for a in result["artifacts"] if a["kind"] == "master-frame")
    assert service.inspect_frame(master["id"])["high_rgb_values"] > 0
    assert service.sample_pixels(master["id"], [[0, 0]])["color_space"] == "ACEScg"
    # Retry export from verified cached masters; re-entering the browser would fail.
    service.store.update("job", job["id"], lambda j: {**j, "status": "failed"})
    service.resume_job(job["id"])
    monkeypatch.setattr(
        "chromascript.motion_worker.render_float_frames",
        lambda *a, **k: pytest.fail("valid master cache must be reused"),
    )
    run(tmp_path, job["id"])
    assert service.get_job(job["id"])["job"]["status"] == "completed"


@pytest.mark.integration
def test_model_frame_preview_is_transient_and_has_hdr_statistics(tmp_path):
    service = Service(tmp_path)
    spec = templates()["radiance"].model_copy(update={"width": 64, "height": 36, "frames": 3})
    result = execute(service, "render_motion_frame", {"spec": spec.model_dump(), "frame": 2})
    assert result["transient"] and result["frame"] == 2
    assert result["high_rgb_values"] > 0
    assert result["images"][0]["mime_type"] == "image/png"
    assert not service.store.list("job") and not service.store.list("artifact")
    assert not service.store.list("motion_revision")


@pytest.mark.integration
def test_float_transport_waits_for_each_consumed_frame(tmp_path):
    # A deliberately slower consumer must not allow Node to accumulate RGBA32F
    # files while EXR readback/hash verification is still using the current frame.
    spec = templates()["radiance"].model_copy(update={"width": 16, "height": 16, "frames": 3})
    (tmp_path / "frame-99.f32").write_bytes(b"interrupted disposable transport")
    received = []

    def accept(index, path):
        time.sleep(0.15)
        assert list(tmp_path.glob("frame-*.f32")) == [path]
        assert path.stat().st_size == 16 * 16 * 16
        assert decode_float_frame(path.read_bytes(), 16, 16)[..., :3].max() > 1
        received.append(index)

    gpu = render_float_frames(spec, tmp_path, [0, 1, 2], accept)
    assert gpu["format"] == "RGBA32F" and received == [0, 1, 2]
    assert not list(tmp_path.glob("frame-*.f32"))


@pytest.mark.integration
def test_real_transparent_float_frame_preserves_alpha_and_negative_radiance(tmp_path):
    # Normal alpha blending stores associated radiance in the GPU target. Verify
    # its real readback, not just the decoder with synthetic bytes.
    spec = MotionSpec(
        width=16,
        height=16,
        frames=1,
        code="""import * as THREE from "three";
export default function createScene() {
 const scene = new THREE.Scene(); const camera = new THREE.Camera();
 const material = new THREE.ShaderMaterial({transparent:true, depthTest:false,
  vertexShader:`void main(){gl_Position=vec4(position,1.0);}`,
  fragmentShader:`void main(){gl_FragColor=vec4(-0.25,4.125,0.1234567,0.5);}`});
 scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2,2),material));
 return {scene,camera,update(){}};
}""",
    )
    result = Service(tmp_path).render_motion_frame(spec)
    expected = transform(
        np.array([-0.25, 4.125, 0.1234567, 0.5], dtype=np.float32),
        "ACEScg",
        source="Linear Rec.709 (sRGB)",
    )
    np.testing.assert_allclose(result["minimum"], expected, rtol=1e-6, atol=1e-6)
    np.testing.assert_allclose(result["maximum"], expected, rtol=1e-6, atol=1e-6)


def test_legacy_sdr_revisions_open_without_migration_or_source_changes(tmp_path):
    service = Service(tmp_path)
    project = service.create_project("legacy SDR")["project"]["id"]
    saved = service.create_motion_scene(project, template_id="title")
    revision = service.store.get("motion_revision", saved["revision_id"])
    revision["scene"].pop("engine")
    service.store.put("motion_revision", saved["revision_id"], revision)
    opened = service.get_motion_scene(saved["record"]["id"])
    assert opened["scene"]["engine"] == "remotion-sdr-v1"
    assert opened["revision_id"] == saved["revision_id"]
    assert opened["scene"]["code"] == saved["scene"]["code"]
    assert "engine" not in service.store.get("motion_revision", saved["revision_id"])["scene"]
    options = service.get_motion_delivery_options("remotion-sdr-v1")
    assert {v["value"] for v in options["videos"]} == {"srgb", "rec709"}
    with pytest.raises(ChromaError, match="SDR"):
        service.submit_motion_render(
            opened["record"]["id"], opened["revision_id"], "reject-hdr", output=MotionOutput()
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "template,video",
    [("title", "srgb"), ("sdr-three", "srgb"), ("radiance", "srgb"), ("three", "srgb")],
)
def test_all_source_families_export_actual_sdr_video(tmp_path, template, video):
    service = Service(tmp_path / "workspace")
    project = service.create_project("SDR delivery")["project"]["id"]
    spec = templates()[template].model_copy(update={"width": 64, "height": 36, "frames": 2})
    saved = service.create_motion_scene(project, spec=spec)
    floating = spec.engine == "three-float-v1"
    job = service.submit_motion_render(
        saved["record"]["id"],
        saved["revision_id"],
        template,
        export_directory=str(tmp_path / "chosen-disk"),
        output=MotionOutput(master_bit_depth=32 if floating else None, video=video),
    )["job"]
    run(service.store.root, job["id"])
    result = service.get_job(job["id"])
    assert result["job"]["status"] == "completed", result["job"]["error"]
    bundle = Path(result["job"]["export_path"])
    # Real SDR and float browser exports never stage their media in workspace.
    assert bundle.parent == tmp_path / "chosen-disk"
    assert all(a["storage"] == "delivery" for a in result["artifacts"])
    assert not list(service.store.root.glob("jobs/**/*.png"))
    assert not list(service.store.root.glob("jobs/**/*.mp4"))
    report = json.loads((bundle / "verification.json").read_text())
    assert report["verification"]["scene_linear_master"] == floating
    assert report["probe"]["streams"][0]["codec_name"] == "h264"
    assert report["probe"]["streams"][0]["color_transfer"] == (
        "iec61966-2-1" if video == "srgb" else "bt709"
    )
    assert bool(list(bundle.rglob("*.exr"))) == floating
    if not floating:
        assert report["verification"]["source_precision"] == "8-bit browser screenshot"
        assert report["verification"]["color_encoding"]["output_transform"] is None
        assert (bundle / "Animation.tsx").read_text() == spec.code


@pytest.mark.integration
def test_sdr_native_model_preview_has_no_hdr_precision_claim(tmp_path):
    service = Service(tmp_path)
    spec = templates()["title"].model_copy(update={"width": 64, "height": 36, "frames": 2})
    reply = service.render_motion_frame(spec, frame=1)
    assert reply["images"][0]["mime_type"] == "image/png"
    assert reply["scene_linear_master"] is False and reply["color_space"] == "sRGB"
    assert not service.store.list("job")
