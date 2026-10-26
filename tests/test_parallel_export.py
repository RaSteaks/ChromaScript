"""Parallel production preserves frame order, decoded pixels and bounded recovery."""

import json
import multiprocessing
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from chromascript import motion_parallel, motion_render, motion_worker
from chromascript.errors import ChromaError
from chromascript.exports import motion_bundle
from chromascript.images import read_image
from chromascript.models import MotionRenderSettings
from chromascript.motion import MotionOutput, MotionSpec, resolve_motion_output
from chromascript.operations import catalogue, execute
from chromascript.service import Service

SOURCE = '''import * as THREE from "three";
export default function createScene(){
 const scene=new THREE.Scene(),camera=new THREE.Camera();
 const material=new THREE.ShaderMaterial({transparent:true,depthTest:false,
 uniforms:{frame:{value:0}},vertexShader:`void main(){gl_Position=vec4(position.xy,0.,1.);}`,
 fragmentShader:`uniform float frame; void main(){gl_FragColor=vec4(-.25,2.+frame*.25,.1234567,.5);}`});
 scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2,2),material));
 return {scene,camera,update({frame}){material.uniforms.frame.value=frame;}};
}'''


@pytest.fixture
def webkit(monkeypatch):
    monkeypatch.setattr(motion_render, "RUNTIME", Path(__file__).resolve().parents[1] /
                        "frontend/tests/float-export-runner.mjs")


def make_job(tmp_path, workers, depth=32, video="srgb"):
    service = Service(tmp_path / "workspace")
    project = service.create_project("parallel")['project']['id']
    spec = MotionSpec(code=SOURCE, source_color_space="ACEScg", width=32, height=18, frames=5,
                      fps={"numerator": 30000, "denominator": 1001})
    saved = service.create_motion_scene(project, spec=spec)
    output = MotionOutput(master_bit_depth=depth, video=video, render_scale=2, render_workers=workers)
    job = service.submit_motion_render(saved["record"]["id"], saved["revision_id"], "parallel",
        export_directory=str(tmp_path / "delivery"), output=output)["job"]
    return service, job, motion_bundle(job, service.store.get("snapshot", job["id"]))


@pytest.mark.parametrize("value", [0, 3, 8, True, "2", 2.0])
def test_worker_count_is_a_strict_bounded_contract(value):
    with pytest.raises(ValidationError):
        MotionOutput(render_workers=value)


def test_workers_discovery_and_simultaneous_storage_budget(tmp_path):
    service = Service(tmp_path)
    assert service.get_motion_delivery_options()["render_workers"] == [1, 2, 4]
    assert service.get_motion_delivery_options("remotion-sdr-v1")["render_workers"] == [1]
    with pytest.raises(ChromaError, match="React SDR"):
        resolve_motion_output("remotion-sdr-v1", MotionOutput(master_bit_depth=None, video="srgb", render_workers=2))
    settings = MotionRenderSettings(width=32, height=18, frames=5)
    serial = service.estimate_motion_render(settings, output=MotionOutput(video="srgb"))
    parallel = service.estimate_motion_render(settings, output=MotionOutput(video="srgb", render_workers=4))
    assert parallel["intermediate_budget_bytes"] == serial["intermediate_budget_bytes"] * 4
    assert parallel["estimated_output_bytes"] == serial["estimated_output_bytes"]
    assert parallel["estimated_peak_bytes"] >= parallel["estimated_output_bytes"] + parallel["intermediate_budget_bytes"]
    shared = execute(service, "estimate_motion_render", {
        "settings": settings.model_dump(),
        "output": {"video": "srgb", "render_workers": 4},
    })
    assert shared["intermediate_budget_bytes"] == parallel["intermediate_budget_bytes"]
    for operation in catalogue():
        if operation["name"] in {"chromascript.submit_motion_render", "chromascript.estimate_motion_render"}:
            field = operation["inputSchema"]["$defs"]["MotionOutput"]["properties"]["render_workers"]
            assert field["enum"] == [1, 2, 4] and field["default"] == 1


def test_memory_admission_accounts_for_all_producers(tmp_path, monkeypatch):
    monkeypatch.setattr("chromascript.motion.psutil.virtual_memory",
                        lambda: SimpleNamespace(available=32 * 18 * (4 * 96 + 96) * 2))
    make_job(tmp_path / "serial", 1)
    with pytest.raises(ChromaError, match="渲染并发") as error:
        make_job(tmp_path / "parallel", 4)
    assert error.value.code == "motion_memory"


@pytest.mark.integration
@pytest.mark.parametrize("workers", [2, 4])
@pytest.mark.parametrize("depth,video", [(None, "srgb"), (16, "pq"), (32, "none")])
def test_real_parallel_webkit_export(tmp_path, webkit, workers, depth, video):
    service, job, bundle = make_job(tmp_path, workers, depth, video)
    motion_worker.run_motion(service.store.root, job["id"])
    result = service.get_job(job["id"])["job"]
    assert result["status"] == "completed", result["error"]
    report = service.validate_delivery_output(job["id"])
    assert report["verification"]["render_workers"] == workers
    assert report["performance"]["float_transport"]["frames"] == 5
    assert report["performance"]["float_transport"]["workers"] == workers
    if video != "none":
        assert report["verification"]["video"]["passed"]
        assert report["verification"]["video"]["decode_verified"]
        stream = next(s for s in report["probe"]["streams"] if s["codec_type"] == "video")
        assert stream["nb_read_frames"] == "5"
        assert stream["r_frame_rate"] == "30000/1001"
    if depth is not None:
        for index in range(5):
            pixels = read_image(bundle.target / f"masters/{index:08d}.exr")[0]
            expected = np.array([-.25, 2 + index * .25, .1234567, .5], np.float32)
            if depth == 16:
                expected = expected.astype(np.float16).astype(np.float32)
            np.testing.assert_allclose(pixels, np.broadcast_to(expected, pixels.shape), rtol=0, atol=1e-7)
    assert not list(bundle.target.parent.rglob("*.f32"))
    assert not [p for p in multiprocessing.active_children() if p.name.startswith("chroma-frame-")]


@pytest.mark.integration
def test_parallel_consumer_failure_resume_preserves_verified_masters(tmp_path, monkeypatch, webkit):
    service, job, bundle = make_job(tmp_path, 2)
    encoder = motion_worker.encode

    def fail(preset, settings, path, frames, progress, **kwargs):
        next(frames)
        next(frames)
        raise ChromaError("encode_failed", "test consumer failed")

    monkeypatch.setattr(motion_worker, "encode", fail)
    motion_worker.run_motion(service.store.root, job["id"])
    assert service.get_job(job["id"])["job"]["status"] == "failed"
    journal = bundle.output / "masters/sequence.jsonl"
    assert [json.loads(line)["frame"] for line in journal.read_text().splitlines()] == [0, 1]
    times = [(bundle.output / f"masters/{i:08d}.exr").stat().st_mtime_ns for i in [0, 1]]
    monkeypatch.setattr(motion_worker, "encode", encoder)
    service.resume_job(job["id"])
    motion_worker.run_motion(service.store.root, job["id"])
    result = service.get_job(job["id"])["job"]
    assert result["status"] == "completed", result["error"]
    assert times == [(bundle.target / f"masters/{i:08d}.exr").stat().st_mtime_ns for i in [0, 1]]


@pytest.mark.integration
def test_parallel_early_close_releases_shared_slots_and_children(tmp_path, monkeypatch, webkit):
    names = []
    create = motion_parallel.SharedMemory

    def track(*args, **kwargs):
        shared = create(*args, **kwargs)
        names.append(shared.name)
        return shared

    monkeypatch.setattr(motion_parallel, "SharedMemory", track)
    spec = MotionSpec(code=SOURCE, width=32, height=18, frames=5)
    output = MotionOutput(master_bit_depth=None, video="srgb", render_workers=2)
    with closing(motion_parallel.iter_parallel_frames(spec, output, tmp_path, list(range(5)),
                                                     config_hash="test")) as frames:
        frame = next(frames)
        assert frame.index == 0
        # Each producer owns one raw file and one final-resolution shared slot.
        assert len(list(tmp_path.rglob("*.f32"))) <= 2
    assert not list(tmp_path.rglob("*.f32"))
    assert not [p for p in multiprocessing.active_children() if p.name.startswith("chroma-frame-")]
    for name in names:
        with pytest.raises(FileNotFoundError):
            create(name=name)


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["source", "gpu"])
def test_parallel_producer_failure_stops_all_workers(tmp_path, webkit, failure):
    source = SOURCE.replace("material.uniforms.frame.value=frame;",
                            'if(frame===1)throw new Error("parallel fixture failure");material.uniforms.frame.value=frame;') if failure == "source" else SOURCE
    spec = MotionSpec(code=source, width=32, height=18, frames=4)
    output = MotionOutput(master_bit_depth=None, video="srgb", render_workers=2)
    expected = {"renderer": "different frozen GPU"} if failure == "gpu" else None
    with pytest.raises(ChromaError) as error, closing(motion_parallel.iter_parallel_frames(
        spec, output, tmp_path, [0, 1, 2, 3], config_hash="test", expected_gpu=expected,
    )) as frames:
        list(frames)
    assert error.value.code == ("gpu_changed" if failure == "gpu" else "motion_render")
    assert not list(tmp_path.rglob("*.f32"))
    assert not [p for p in multiprocessing.active_children() if p.name.startswith("chroma-frame-")]
