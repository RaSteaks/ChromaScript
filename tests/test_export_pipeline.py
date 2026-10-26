"""Final delivery: one EXR decode, exact Half semantics, recovery and WebKit GPU transport."""

import json
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest

from chromascript import motion_render, motion_worker
from chromascript.color import render_output
from chromascript.errors import ChromaError
from chromascript.exports import motion_bundle
from chromascript.images import read_image
from chromascript.motion import MotionOutput, MotionSpec
from chromascript.service import Service

SOURCE = '''import * as THREE from "three";
export default function createScene() {
 const scene=new THREE.Scene(), camera=new THREE.Camera();
 const material=new THREE.ShaderMaterial({transparent:true,depthTest:false,
 uniforms:{frame:{value:0}},
 vertexShader:`void main(){gl_Position=vec4(position.xy,0.,1.);}`,
 fragmentShader:`uniform float frame; void main(){gl_FragColor=vec4(-0.25,2.125+frame*0.25,0.1234567,0.5);}`});
 scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2,2), material));
 return {scene,camera,update({frame}){material.uniforms.frame.value=frame;}};
}'''


def make_job(tmp_path, depth, video="srgb", width=32, height=18):
    service = Service(tmp_path / "workspace")
    project = service.create_project("pipeline")['project']['id']
    spec = MotionSpec(code=SOURCE, source_color_space="ACEScg", width=width, height=height,
                      frames=3, fps={"numerator": 30000, "denominator": 1001})
    saved = service.create_motion_scene(project, spec=spec)
    job = service.submit_motion_render(saved["record"]["id"], saved["revision_id"], "pipeline",
        export_directory=str(tmp_path / "delivery"),
        output=MotionOutput(master_bit_depth=depth, video=video, render_scale=2, video_processing="reference", video_encoder="software"))["job"]
    return service, job, motion_bundle(job, service.store.get("snapshot", job["id"]))


@pytest.fixture
def float_transport(monkeypatch):
    calls, closed = [], []

    def frames(spec, directory, indices, **kwargs):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "gpu.json").write_text(json.dumps({"format": "RGBA32F", "renderer": "fixture"}))
        try:
            for index in indices:
                assert not list(directory.glob("frame-*.f32"))
                values = np.empty((spec.height * 2, spec.width * 2, 4), np.float32)
                values[:] = [-0.125, 1.0625 + index * 0.125, 0.06172835, 0.5]
                path = directory / f"frame-{index}.f32"
                path.write_bytes(values.tobytes())
                calls.append(index)
                try:
                    yield index, path
                finally:
                    path.unlink(missing_ok=True)
        finally:
            closed.append(True)

    monkeypatch.setattr(motion_worker, "iter_float_frames", frames)
    return calls, closed


@pytest.mark.parametrize("depth", [16, 32])
def test_combined_delivery_encodes_verified_pixels_without_second_decode(tmp_path, monkeypatch, float_transport, depth):
    service, job, bundle = make_job(tmp_path, depth)
    reads, encoded = [], []
    encoder = motion_worker.encode

    def read(path, **kwargs):
        reads.append(path.name)
        return read_image(path, **kwargs)

    def encode(preset, settings, path, frames, progress, **kwargs):
        def inspect():
            for index, pixels in enumerate(frames):
                # Compare against actual EXR storage, including its Half rounding.
                master = read_image(bundle.output / f"masters/{index:08d}.exr")[0]
                np.testing.assert_array_equal(pixels, render_output(master, preset.color.output, False))
                encoded.append(index)
                yield pixels
        return encoder(preset, settings, path, inspect(), progress, **kwargs)

    monkeypatch.setattr(motion_worker, "read_image", read)
    monkeypatch.setattr("chromascript.motion_processing.read_image", read)
    monkeypatch.setattr(motion_worker, "encode", encode)
    motion_worker.run_motion(service.store.root, job["id"])
    result = service.get_job(job["id"])["job"]
    assert result["status"] == "completed", result["error"]
    assert float_transport == ([0, 1, 2], [True]) and encoded == [0, 1, 2]
    # One validation read per EXR, plus the existing first-frame thumbnail.
    assert reads == ["00000000.exr", "00000001.exr", "00000002.exr", "00000000.exr"]
    assert not list(bundle.target.rglob("*.jsonl"))
    report = service.validate_delivery_output(job["id"])
    assert report["verification"]["video"]["decode_verified"]
    assert report["verification"]["verified_frame_rate"] == "30000/1001"


def test_encoder_failure_keeps_verified_journal_and_resume_renders_only_missing(tmp_path, monkeypatch, float_transport):
    service, job, bundle = make_job(tmp_path, 32)
    encoder = motion_worker.encode

    def fail(preset, settings, path, frames, progress, **kwargs):
        next(frames)
        raise ChromaError("encode_failed", "injected consumer failure")

    monkeypatch.setattr(motion_worker, "encode", fail)
    motion_worker.run_motion(service.store.root, job["id"])
    assert service.get_job(job["id"])["job"]["status"] == "failed"
    assert float_transport == ([0], [True])
    assert (bundle.output / "masters/sequence.jsonl").is_file()
    assert not list(bundle.work.glob("*.f32"))
    monkeypatch.setattr(motion_worker, "encode", encoder)
    service.resume_job(job["id"])
    motion_worker.run_motion(service.store.root, job["id"])
    result = service.get_job(job["id"])["job"]
    assert result["status"] == "completed", result["error"]
    assert float_transport == ([0, 1, 2], [True, True])


@pytest.mark.integration
@pytest.mark.parametrize("depth", [None, 16, 32])
def test_webkit_final_export_transport_real_exr_and_codec(tmp_path, monkeypatch, depth):
    # This starts ONLY Playwright WebKit, compiling the actual final-raster bridge
    # and exercising the real Python/Node transport, native OCIO, EXR and FFmpeg.
    monkeypatch.setattr(motion_render, "RUNTIME", Path(__file__).resolve().parents[1] /
                        "frontend/tests/float-export-runner.mjs")
    service, job, bundle = make_job(tmp_path, depth, "pq")
    motion_worker.run_motion(service.store.root, job["id"])
    result = service.get_job(job["id"])["job"]
    assert result["status"] == "completed", result["error"]
    report = service.validate_delivery_output(job["id"])
    assert report["verification"]["video"]["passed"]
    assert report["gpu"]["sampling"]["render_scale"] == 2
    assert report["performance"]["float_transport"]["frames"] == 3
    assert report["performance"]["float_transport"]["bytes"] == 32 * 18 * 4 * 16 * 3
    if depth is not None:
        for index in range(3):
            pixels = read_image(bundle.target / f"masters/{index:08d}.exr")[0]
            expected = np.array([-0.25, 2.125 + index * 0.25, 0.1234567, 0.5], np.float32)
            if depth == 16:
                expected = expected.astype(np.float16).astype(np.float32)
            np.testing.assert_allclose(pixels, np.broadcast_to(expected, pixels.shape), rtol=0, atol=1e-7)
    assert not list(bundle.target.parent.rglob("*.f32"))


@pytest.mark.integration
def test_webkit_early_close_reaps_lookahead_and_raw_file(tmp_path, monkeypatch):
    monkeypatch.setattr(motion_render, "RUNTIME", Path(__file__).resolve().parents[1] /
                        "frontend/tests/float-export-runner.mjs")
    spec = MotionSpec(code=SOURCE, source_color_space="ACEScg", width=32, height=18, frames=3)
    processes = []
    with closing(motion_render.iter_float_frames(spec, tmp_path, [0, 1, 2],
                                                on_process=processes.append)) as frames:
        _, raw = next(frames)
        assert raw.is_file()
    assert processes[0].poll() is not None
    assert not list(tmp_path.glob("*.f32"))
