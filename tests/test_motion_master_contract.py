"""Master delivery verifies storage, reviewer provenance, sampling and native tags separately."""

from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from chromascript.color import transform
from chromascript.delivery import configured_output
from chromascript.encoding import check_probe, encode
from chromascript.errors import ChromaError
from chromascript.exports import motion_bundle
from chromascript.models import DeliveryConfiguration, MotionRate, MotionRenderSettings
from chromascript.motion import MotionOutput, MotionSpec, templates
from chromascript.motion_color import decode_float_frame
from chromascript.motion_render import render_float_frames
from chromascript.operations import execute
from chromascript.service import Service


@pytest.mark.parametrize("value", [True, "2", 2.0, 3])
def test_sampling_contract_rejects_noninteger_or_unsupported_values(value):
    with pytest.raises(ValidationError):
        MotionOutput(render_scale=value)


def test_custom_specification_preserves_exact_time_and_master_defaults():
    # Accept 8K, sub-fps, high-fps and long source specifications without allocating frames.
    for rate in [(480, 1), (1, 2), (120000, 1001)]:
        spec = MotionSpec(code=templates()["radiance"].code, width=7680, height=4320,
                          frames=100000, fps={"numerator": rate[0], "denominator": rate[1]})
        assert isinstance(spec.settings(), MotionRenderSettings)
        assert spec.settings().fps.ffmpeg == f"{rate[0]}/{rate[1]}"
    assert MotionOutput().video == "none" and MotionOutput().render_scale == 2


def test_review_is_explicit_revision_bound_and_frozen_with_job(tmp_path):
    service = Service(tmp_path / "workspace")
    project = service.create_project("review")["project"]["id"]
    saved = service.create_motion_scene(project)
    assert saved["scene"]["engine"] == "three-float-v1"
    assert saved["color_review"]["status"] == "unreviewed"
    args = {"scene_id": saved["record"]["id"], "expected_revision_id": saved["revision_id"],
            "reviewer": "test reviewer", "notes": "Checked linear RGB, no display transform and alpha association."}
    reviewed = execute(service, "record_motion_color_review", args)
    assert reviewed["revision_id"] == saved["revision_id"]
    assert reviewed["color_review"]["status"] == "reviewed"
    job = service.submit_motion_render(saved["record"]["id"], saved["revision_id"], "reviewed",
                                       export_directory=str(tmp_path / "delivery"))["job"]
    assert service.store.get("snapshot", job["id"])["source_color_review"] == reviewed["color_review"]
    changed = service.update_motion_scene(saved["record"]["id"],
        MotionSpec.model_validate({**saved["scene"], "code": saved["scene"]["code"] + "\n// edited"}), saved["revision_id"])
    assert changed["color_review"]["status"] == "unreviewed"
    with pytest.raises(ChromaError, match="修订"):
        execute(service, "record_motion_color_review", args)
    assert service.get_motion_scene(saved["record"]["id"], saved["revision_id"])["color_review"]["status"] == "reviewed"


def test_supersampling_integrates_light_and_coverage_before_unassociation():
    # Only one of four subpixels contributes: averaging straight RGB would darken
    # the edge twice and make this energy/coverage assertion fail.
    pixels = np.zeros((2, 2, 4), dtype="<f4")
    pixels[0, 0] = [-0.5, 4, 0.1234567, 0.5]
    actual = decode_float_frame(pixels.tobytes(), 1, 1, 2)
    expected = transform(np.array([[[-1, 8, 0.2469134, 0.125]]], np.float32),
                         "ACEScg", source="Linear Rec.709 (sRGB)")
    np.testing.assert_allclose(actual, expected, atol=1e-6)


@pytest.mark.parametrize("engine", ["three-float-v1", "remotion-sdr-v1"])
def test_missing_destination_volume_never_falls_back_to_workspace(tmp_path, engine):
    service = Service(tmp_path / "workspace")
    project = service.create_project("external")["project"]["id"]
    saved = service.create_motion_scene(project, engine=engine)
    job = service.submit_motion_render(saved["record"]["id"], saved["revision_id"], engine,
        export_directory=str(tmp_path / "delivery"), output=MotionOutput() if engine == "three-float-v1" else MotionOutput(master_bit_depth=None, video="srgb"))["job"]
    bundle = motion_bundle(job, service.store.get("snapshot", job["id"]))
    assert bundle.volume is not None
    # Persistent UUIDs identify current macOS volumes; other filesystems retain
    # the device fallback. Simulate a replacement using the frozen identity kind.
    if "uuid" in bundle.volume:
        bundle.volume["uuid"] = "00000000-0000-0000-0000-000000000000"
    else:
        bundle.volume["device"] += 1
    with pytest.raises(ChromaError, match="磁盘"):
        bundle.prepare()
    assert not bundle.staging.exists()
    assert not list(service.store.root.glob("jobs/**/*.exr"))


@pytest.mark.integration
def test_actual_supersampled_edge_retains_hdr_radiance(tmp_path):
    spec = MotionSpec(width=16, height=16, frames=1, code='''import * as THREE from "three";
export default function createScene() {
 const scene=new THREE.Scene(), camera=new THREE.Camera();
 // Deliberately offset the edge within one output pixel to exercise real coverage.
 const material=new THREE.ShaderMaterial({transparent:true, depthTest:false,
 vertexShader:`varying vec2 p; void main(){p=uv;gl_Position=vec4(position,1.0);}`,
 fragmentShader:`varying vec2 p; void main(){gl_FragColor=p.x<0.53?vec4(4.,4.,4.,1.):vec4(0.);}`});
 scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2,2),material));
 return {scene,camera,update(){}};
}''')
    results = {}
    for scale in [1, 2]:
        def accept(index, path, scale=scale):
            results[scale] = decode_float_frame(path.read_bytes(), 16, 16, scale)
        render_float_frames(spec, tmp_path / str(scale), [0], accept, render_scale=scale)
    assert results[1][8, 8, 3] == 0
    assert results[2][8, 8, 3] == 0.5
    np.testing.assert_allclose(results[2][8, 8, :3], [4, 4, 4], atol=2e-5)


@pytest.mark.integration
def test_video_requires_matching_native_container_and_stream_tags(tmp_path: Path):
    preset = configured_output(DeliveryConfiguration())
    settings = MotionRenderSettings(width=32, height=32, frames=2, fps=MotionRate(numerator=480))
    pixels = np.ones((32, 32, 4), np.float32)
    info = encode(preset, settings, tmp_path / "tagged.mp4", [pixels, pixels], lambda _: None)
    verified = check_probe(info, preset, settings)
    assert verified["passed"], verified["issues"]
    assert verified["native_color_tags_verified"]
    assert info["container_color"][0]["primaries"] == 1
    assert info["container_color"][0]["transfer"] == 13
    assert verified["video"]["color_transfer"] == "iec61966-2-1"
    for field in ["color_primaries", "color_transfer", "color_space"]:
        missing = deepcopy(info)
        missing["streams"][0].pop(field, None)
        assert not check_probe(missing, preset, settings)["passed"]
    missing = deepcopy(info)
    missing["container_color"] = []
    assert not check_probe(missing, preset, settings)["passed"]
    mismatch = deepcopy(info)
    mismatch["container_color"][0]["transfer"] = 1
    assert not check_probe(mismatch, preset, settings)["passed"]
