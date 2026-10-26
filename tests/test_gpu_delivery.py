"""Real GPU-only delivery: shared operation model, frozen plan, codecs and cleanup."""

import json
import wave
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest

from chromascript import motion_render, motion_worker
from chromascript.audio import AudioClip, AudioTimeline
from chromascript.exports import motion_bundle
from chromascript.motion import MotionOutput, MotionSpec
from chromascript.motion_video_render import iter_video_frames
from chromascript.service import Service

SOURCE = '''import * as THREE from "three";
export default function createScene(){
 const scene=new THREE.Scene(),camera=new THREE.Camera();
 const material=new THREE.ShaderMaterial({transparent:true,depthTest:false,
 uniforms:{frame:{value:0}},vertexShader:`varying vec2 p;void main(){p=uv;gl_Position=vec4(position.xy,0.,1.);}`,
 fragmentShader:`varying vec2 p;uniform float frame;
 void main(){gl_FragColor=vec4(.25+p.x*4.,.25+p.y*2.+frame*.125,.1234567,.25+p.x*.75);}`});
 scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2,2),material));
 return {scene,camera,update({frame}){material.uniforms.frame.value=frame;}};
}'''


@pytest.mark.integration
@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("video,pixel_format", [
    ("srgb", "nv12"), ("pq", "p010le"), ("hlg", "p010le"),
    ("prores-hq-srgb", "p210le"), ("prores-hq-pq", "p210le"), ("prores-hq-hlg", "p210le"),
    ("prores-4444-acescct", "p416le"),
])
def test_complete_gpu_delivery(tmp_path, monkeypatch, workers, video, pixel_format):
    monkeypatch.setattr(motion_render, "RUNTIME", Path(__file__).resolve().parents[1] /
                        "frontend/tests/float-export-runner.mjs")
    service = Service(tmp_path / "workspace")
    project = service.create_project("GPU video")['project']['id']
    spec = MotionSpec(code=SOURCE, source_color_space="ACEScg", width=64, height=48, frames=3,
                      fps={"numerator": 30000, "denominator": 1001})
    saved = service.create_motion_scene(project, spec=spec)
    output = MotionOutput(master_bit_depth=None, video=video, render_scale=2, render_workers=workers)
    job = service.submit_motion_render(saved["record"]["id"], saved["revision_id"], "gpu-job",
        export_directory=str(tmp_path / "delivery"), output=output)["job"]
    snapshot = service.store.get("snapshot", job["id"])
    assert snapshot["video_execution"]["processing"] == "gpu"
    assert snapshot["video_execution"]["pixel_format"] == pixel_format
    if video in {"srgb", "pq", "hlg"}:
        assert snapshot["video_execution"]["hardware"]
        assert snapshot["video_execution"]["capability"]["hardware_required"]
    motion_worker.run_motion(service.store.root, job["id"])
    result = service.get_job(job["id"])["job"]
    assert result["status"] == "completed", result["error"]
    bundle = motion_bundle(job, snapshot)
    report = json.loads((bundle.target / "verification.json").read_text())
    assert report["verification"]["video"]["passed"]
    assert report["verification"]["video"]["decode_verified"]
    assert report["probe"]["encoder_transport"]["pixel_format"] == pixel_format
    assert report["performance"]["float_transport"]["frames"] == 3
    assert not list(tmp_path.rglob("*.f32")) and not list(tmp_path.rglob("*.exr"))
    assert (bundle.target / "thumbnail.png").is_file()


@pytest.mark.integration
def test_gpu_video_preserves_audio_sample_window(tmp_path, monkeypatch):
    monkeypatch.setattr(motion_render, "RUNTIME", Path(__file__).resolve().parents[1] /
                        "frontend/tests/float-export-runner.mjs")
    service = Service(tmp_path / "workspace")
    project = service.create_project("GPU audio")['project']['id']
    source = tmp_path / "tone.wav"
    samples = (np.sin(np.arange(12000) * 2 * np.pi * 440 / 48000) * 2**27).astype("<i4")
    with wave.open(str(source), "wb") as stream:
        stream.setparams((2, 4, 48000, 0, "NONE", "not compressed"))
        stream.writeframes(np.repeat(samples[:, None], 2, axis=1).tobytes())
    asset = service.import_audio(project, "tone", str(source))["asset"]
    service.set_project_audio(project, "0", AudioTimeline(clips=[AudioClip(asset_id=asset["id"])]))
    spec = MotionSpec(code=SOURCE, source_color_space="ACEScg", width=64, height=48, frames=3,
                      fps={"numerator": 30000, "denominator": 1001})
    saved = service.create_motion_scene(project, spec=spec)
    job = service.submit_motion_render(saved["record"]["id"], saved["revision_id"], "gpu-audio",
        export_directory=str(tmp_path / "delivery"), output=MotionOutput(master_bit_depth=None, video="srgb"))["job"]
    motion_worker.run_motion(service.store.root, job["id"])
    result = service.get_job(job["id"])["job"]
    assert result["status"] == "completed", result["error"]
    report = service.validate_delivery_output(job["id"])
    assert report["probe"]["audio_verification"]["passed"]
    assert report["probe"]["audio_verification"]["expected_samples"] == 4805


@pytest.mark.integration
def test_gpu_binary_early_close_reaps_producer(tmp_path, monkeypatch):
    from chromascript import motion_video_render

    monkeypatch.setattr(motion_render, "RUNTIME", Path(__file__).resolve().parents[1] /
                        "frontend/tests/float-export-runner.mjs")
    original = motion_video_render.subprocess.Popen
    processes = []

    def observe(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(motion_video_render.subprocess, "Popen", observe)
    spec = MotionSpec(code=SOURCE, width=32, height=18, frames=6)
    with closing(iter_video_frames(spec, tmp_path, list(range(6)), "sRGB")) as frames:
        assert len(next(frames)) == 32 * 18 * 3 // 2
    assert len(processes) == 1 and processes[0].poll() is not None
    assert not list(tmp_path.glob("*.f32"))
