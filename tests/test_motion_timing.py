"""Source time survives output sampling, actual EXR/video encoding and preview."""

import base64
import io
import json
import subprocess
from contextlib import closing
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from chromascript import motion_output_preview, motion_render, motion_sdr_worker, motion_worker
from chromascript.errors import ChromaError
from chromascript.exports import motion_bundle
from chromascript.images import read_image
from chromascript.models import MotionRenderSettings
from chromascript.motion import MotionOutput, MotionSpec
from chromascript.motion_color import decode_float_frame
from chromascript.motion_sdr_sampling import combine_sdr_samples
from chromascript.motion_timing import MotionTiming, motion_time_plan
from chromascript.operations import execute
from chromascript.service import Service


@pytest.mark.parametrize("fps,frames,expected", [(30, 600, 300), (24, 600, 240), (30, 601, 301), (120, 600, 1200)])
def test_exact_output_duration_and_tail_policy(fps, frames, expected):
    source = MotionRenderSettings(frames=frames, fps={"numerator": 60})
    plan = motion_time_plan(source, MotionTiming(fps={"numerator": fps}))
    assert source.frames == frames and source.fps.numerator == 60
    assert plan.frames == expected
    excess = Fraction(plan.frames, fps) - Fraction(frames, 60)
    assert 0 <= excess < Fraction(1, fps)


def test_ntsc_and_schema_are_shared_without_revision_changes(tmp_path):
    source = MotionRenderSettings(frames=600, fps={"numerator": 60000, "denominator": 1001})
    service = Service(tmp_path)
    reply = execute(service, "get_motion_time_plan", {
        "settings": source.model_dump(),
        "output": {"fps": {"numerator": 30000, "denominator": 1001}, "temporal_samples": 8},
    })
    assert reply["plan"]["frames"] == 300
    assert reply["source_duration"] == reply["output_duration"] == 10.01
    assert not service.store.list("job") and not service.store.list("motion_revision")


@pytest.mark.parametrize("value", [True, 2, 0, 4.0, "4", 32])
def test_sample_count_is_strict(value):
    with pytest.raises(ValidationError):
        MotionOutput(temporal_samples=value)


def test_sdr_temporal_average_is_linear_and_alpha_associated(tmp_path):
    black, white, hidden = [tmp_path / name for name in ("black.png", "white.png", "hidden.png")]
    Image.new("RGBA", (2, 2), (0, 0, 0, 255)).save(black)
    Image.new("RGBA", (2, 2), (255, 255, 255, 255)).save(white)
    Image.new("RGBA", (2, 2), (255, 0, 0, 0)).save(hidden)
    output = tmp_path / "average.png"
    combine_sdr_samples([black, white], output)
    with Image.open(output) as image:
        assert image.getpixel((0, 0)) == (188, 188, 188, 255)
    combine_sdr_samples([white, hidden], output)
    with Image.open(output) as image:
        assert image.getpixel((0, 0)) == (255, 255, 255, 128)


def test_output_storage_counts_final_frames_and_bounded_temporal_batch(tmp_path):
    service = Service(tmp_path)
    source = MotionRenderSettings(width=16, height=8, frames=6, fps={"numerator": 60})
    output = MotionOutput(master_bit_depth=None, video="srgb", render_scale=1, fps={"numerator": 30})
    fast = service.estimate_motion_render(source, "remotion-sdr-v1", output)
    sampled = service.estimate_motion_render(source, "remotion-sdr-v1", output.model_copy(update={"temporal_samples": 4}))
    assert fast["settings"]["frames"] == 3
    assert fast["intermediate_budget_bytes"] == 16 * 8 * 4 * 3
    assert sampled["intermediate_budget_bytes"] == 16 * 8 * 4 * 7
    assert fast["video_budget_bytes"] == sampled["video_budget_bytes"]


SOURCE = '''import * as THREE from "three";
export default function createScene({fps, frames}) {
 const scene=new THREE.Scene(), camera=new THREE.Camera();
 const material=new THREE.ShaderMaterial({transparent:true,depthTest:false,
 uniforms:{f:{value:0},t:{value:0},ratio:{value:fps/frames}},
 vertexShader:`void main(){gl_Position=vec4(position.xy,0.,1.);}`,
 fragmentShader:`uniform float f,t,ratio;void main(){gl_FragColor=vec4(f*f,t,ratio,.5);}`});
 scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2,2),material));
 return {scene,camera,update({frame,time}){material.uniforms.f.value=frame;material.uniforms.t.value=time;}};
}'''


@pytest.mark.integration
def test_float_temporal_integration_weights_color_by_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(motion_render, "RUNTIME", Path(__file__).resolve().parents[1] / "frontend/tests/float-export-runner.mjs")
    spec = MotionSpec(code=SOURCE.replace("vec4(f*f,t,ratio,.5)", "vec4(f,0.,0.,f/10.)"),
                      source_color_space="ACEScg", width=16, height=8, frames=6, fps={"numerator": 60})
    plan = motion_time_plan(spec.settings(), MotionTiming(fps={"numerator": 30}, temporal_samples=4))
    with closing(motion_render.iter_float_frames(spec, tmp_path, [1], time_plan=plan)) as frames:
        _, raw = next(frames)
        pixels = decode_float_frame(raw.read_bytes(), 16, 8, source="ACEScg")
    # E[RGB*alpha]/E[alpha], not E[straight RGB]. Exposure samples span 1.625..2.375.
    np.testing.assert_allclose(pixels[0, 0], [2.0390625, 0, 0, .2], atol=1e-6)


@pytest.mark.integration
@pytest.mark.parametrize("samples,workers,red", [(1, 1, 4.0), (4, 1, 4.078125), (4, 2, 4.078125)])
def test_webkit_export_and_output_preview_share_time_samples(tmp_path, monkeypatch, samples, workers, red):
    monkeypatch.setattr(motion_render, "RUNTIME", Path(__file__).resolve().parents[1] / "frontend/tests/float-export-runner.mjs")
    service = Service(tmp_path / "workspace")
    project = service.create_project("time sampling")["project"]["id"]
    spec = MotionSpec(code=SOURCE, source_color_space="ACEScg", width=16, height=8, frames=6, fps={"numerator": 60})
    saved = service.create_motion_scene(project, spec=spec)
    output = MotionOutput(fps={"numerator": 30}, temporal_samples=samples, render_scale=1,
                          render_workers=workers, video="srgb", video_processing="reference", video_encoder="software")
    job = service.submit_motion_render(saved["record"]["id"], saved["revision_id"], "time",
                                       output=output, export_directory=str(tmp_path / "delivery"))["job"]
    assert job["settings"]["frames"] == 3 and job["settings"]["fps"]["numerator"] == 30
    assert service.get_motion_scene(saved["record"]["id"])["scene"] == spec.model_dump()
    retained = None
    if samples == 4 and workers == 2:
        encoder = motion_worker.encode
        bundle = motion_bundle(job, service.store.get("snapshot", job["id"]))
        def fail(preset, settings, path, frames, progress, **kwargs):
            next(frames)
            raise ChromaError("encode_failed", "test timing recovery")
        monkeypatch.setattr(motion_worker, "encode", fail)
        motion_worker.run_motion(service.store.root, job["id"])
        assert service.store.get("job", job["id"])["status"] == "failed"
        retained = (bundle.output / "masters/00000000.exr").stat().st_mtime_ns
        monkeypatch.setattr(motion_worker, "encode", encoder)
        service.resume_job(job["id"])
    motion_worker.run_motion(service.store.root, job["id"])
    finished = service.store.get("job", job["id"])
    assert finished["status"] == "completed", finished.get("error")
    directory = Path(finished["export_path"])
    if retained is not None:
        assert (directory / "masters/00000000.exr").stat().st_mtime_ns == retained
    pixels, metadata = read_image(directory / "masters" / "00000001.exr")
    assert tuple(metadata["FramesPerSecond"]) == (30, 1)
    np.testing.assert_allclose(pixels[0, 0], [red, 1 / 30, 10, .5], atol=1e-5)
    verification = json.loads((directory / "verification.json").read_text())["verification"]
    assert verification["video"]["passed"]
    assert verification["time_plan"]["frames"] == 3
    preview = service.render_motion_output_preview(spec, output, start_frame=1, frame_count=1)
    single = service.render_motion_frame(spec, frame=1, output=output)
    assert preview["images"] == single["images"]
    with Image.open(io.BytesIO(base64.b64decode(preview["images"][0]["data"]))) as image:
        assert image.size == (16, 8)
        displayed = np.asarray(image.convert("RGB"), dtype=float)
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(directory / "animation-srgb.mp4"),
                              "-vf", "select=eq(n\\,1)", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, check=True).stdout
    np.testing.assert_allclose(np.frombuffer(decoded, np.uint8).reshape(8, 16, 3), displayed, atol=5)
    assert service.store.get("snapshot", job["id"])["scene"]["fps"]["numerator"] == 60


@pytest.mark.integration
@pytest.mark.parametrize("samples", [1, 4, 8])
def test_webkit_remotion_preserves_original_composition_and_matches_preview(tmp_path, monkeypatch, samples):
    driver = Path(__file__).resolve().parents[1] / "frontend/tests/sdr-export-runner.mjs"
    monkeypatch.setattr(motion_sdr_worker, "RUNTIME", driver)
    monkeypatch.setattr(motion_output_preview, "SDR_RUNTIME", driver)
    service = Service(tmp_path / "workspace")
    project = service.create_project("SDR sampling")["project"]["id"]
    spec = MotionSpec(engine="remotion-sdr-v1", width=16, height=8, frames=6, fps={"numerator": 60}, code='''
import {useCurrentFrame,useVideoConfig} from 'remotion';
export default function Scene(){const frame=useCurrentFrame();const {fps,durationInFrames}=useVideoConfig();
return <div style={{width:'100%',height:'100%',background:`rgb(${frame*frame*10},${fps},${durationInFrames})`}}/>;}
''')
    output = MotionOutput(master_bit_depth=None, video="srgb", render_scale=1,
                          fps={"numerator": 30}, temporal_samples=samples, video_encoder="software")
    saved = service.create_motion_scene(project, spec=spec)
    job = service.submit_motion_render(saved["record"]["id"], saved["revision_id"], "sdr-time",
                                       output=output, export_directory=str(tmp_path / "delivery"))["job"]
    motion_sdr_worker.run_sdr_motion(service.store.root, job["id"])
    finished = service.store.get("job", job["id"])
    assert finished["status"] == "completed", finished.get("error")
    directory = Path(finished["export_path"])
    report = json.loads((directory / "verification.json").read_text())
    assert report["verification"]["passed"] and report["verification"]["time_plan"]["frames"] == 3
    preview = service.render_motion_output_preview(spec, output, start_frame=1, frame_count=1)
    with Image.open(io.BytesIO(base64.b64decode(preview["images"][0]["data"]))) as image:
        pixels = np.asarray(image.convert("RGB"))
    # useVideoConfig remains 60fps / 6 source frames inside Freeze at every sample.
    np.testing.assert_allclose(pixels[0, 0, 1:], [60, 6], atol=1)
    if samples == 1:
        assert pixels[0, 0, 0] == 40
    else:
        assert 41 <= pixels[0, 0, 0] <= 45
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(directory / "animation.mp4"),
                              "-vf", "select=eq(n\\,1)", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, check=True).stdout
    np.testing.assert_allclose(np.frombuffer(decoded, np.uint8).reshape(8, 16, 3), pixels, atol=5)


@pytest.mark.integration
@pytest.mark.parametrize("workers", [1, 2])
def test_gpu_video_only_sampling_uses_output_rate_and_matches_review(tmp_path, monkeypatch, workers):
    monkeypatch.setattr(motion_render, "RUNTIME", Path(__file__).resolve().parents[1] / "frontend/tests/float-export-runner.mjs")
    service = Service(tmp_path / "workspace")
    project = service.create_project("GPU timing")["project"]["id"]
    spec = MotionSpec(code=SOURCE, source_color_space="ACEScg", width=16, height=8, frames=6, fps={"numerator": 60})
    saved = service.create_motion_scene(project, spec=spec)
    output = MotionOutput(master_bit_depth=None, video="srgb", render_scale=1, render_workers=workers,
                          fps={"numerator": 30}, temporal_samples=4, video_processing="auto", video_encoder="software")
    job = service.submit_motion_render(saved["record"]["id"], saved["revision_id"], "gpu-time",
                                       output=output, export_directory=str(tmp_path / "delivery"))["job"]
    motion_worker.run_motion(service.store.root, job["id"])
    finished = service.store.get("job", job["id"])
    assert finished["status"] == "completed", finished.get("error")
    directory = Path(finished["export_path"])
    report = json.loads((directory / "verification.json").read_text())
    assert report["verification"]["video"]["passed"]
    assert report["verification"]["video_execution"]["processing"] == "gpu"
    assert report["verification"]["time_plan"]["frames"] == 3
    assert not (directory / "masters").exists()
    preview = service.render_motion_output_preview(spec, output, start_frame=1, frame_count=1)
    with Image.open(io.BytesIO(base64.b64decode(preview["images"][0]["data"]))) as image:
        pixels = np.asarray(image.convert("RGB"))
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(directory / "animation-srgb.mp4"),
                              "-vf", "select=eq(n\\,1)", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, check=True).stdout
    np.testing.assert_allclose(np.frombuffer(decoded, np.uint8).reshape(8, 16, 3), pixels, atol=5)
