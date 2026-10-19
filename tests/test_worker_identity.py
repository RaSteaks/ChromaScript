"""Frozen WGSL jobs cannot mix GPU provenance or changed delivery processing."""

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from chromascript import environment, worker
from chromascript.errors import ChromaError
from chromascript.images import file_hash, read_image
from chromascript.models import RenderSettings
from chromascript.service import Service


def frozen_job(tmp_path):
    service = Service(tmp_path / "workspace")
    # Simulate adapter identities only; EXR storage, checksums and recovery use
    # the real production path so the test needs no physical second GPU.
    service._renderer = SimpleNamespace(info={"backend_type": "backend-A"})
    project = service.create_project("GPU identity")["project"]["id"]
    scene = service.create_scene(project)
    job = service.render_sequence(
        scene["record"]["id"],
        RenderSettings(width=16, height=16, frames=2, samples=1),
        "exr-32-zip",
        "identity",
    )["job"]
    return service, job


def test_gpu_drift_refuses_cached_masters_and_same_gpu_can_resume(tmp_path, monkeypatch):
    service, job = frozen_job(tmp_path)
    calls = []
    backend = "backend-A"
    interrupt = True

    class TestRenderer:
        def __init__(self):
            self.info = {"backend_type": backend}

        def validate(self, scene):
            calls.append((backend, "validate"))

        def render(self, scene, settings, frame, *args):
            calls.append((backend, frame))
            if interrupt and frame == 1:
                raise RuntimeError("injected interruption after the first cached EXR")
            return np.ones((settings.height, settings.width, 4), np.float32)

    monkeypatch.setattr(worker, "Renderer", TestRenderer)
    with pytest.raises(RuntimeError, match="injected interruption"):
        worker.run(service.store.root, job["id"])
    cached = list(service.store.root.glob("cache/*/frames/*.exr"))
    assert len(cached) == 1
    original_hash = file_hash(cached[0])
    pixels, metadata = read_image(cached[0])
    assert metadata["storage"] == "float"
    np.testing.assert_array_equal(pixels, np.ones((16, 16, 4), np.float32))
    calls.clear()
    backend = "backend-B"
    interrupt = False
    service.resume_job(job["id"])
    # Mismatched workers must reject before shader compilation or even checking
    # a cache checksum. This prevents valid old files from certifying mixed GPUs.
    with monkeypatch.context() as patch:
        patch.setattr(worker, "file_hash", lambda _: pytest.fail("cache was read before GPU check"))
        with pytest.raises(ChromaError, match="Frozen GPU backend") as error:
            worker.run(service.store.root, job["id"])
    assert error.value.code == "gpu_changed"
    assert not calls
    assert file_hash(cached[0]) == original_hash
    assert list(cached[0].parent.glob("*.exr")) == cached
    output = service.store.root / "jobs" / job["id"] / "output"
    assert not (output / "verification.json").exists()
    failed = service.get_job(job["id"])["job"]
    assert failed["status"] == "failed" and failed["error"]["code"] == "gpu_changed"

    backend = "backend-A"
    service.resume_job(job["id"])
    worker.run(service.store.root, job["id"])
    assert calls == [("backend-A", "validate"), ("backend-A", 1)]
    assert file_hash(cached[0]) == original_hash
    assert service.get_job(job["id"])["job"]["status"] == "completed"
    report = json.loads((output / "verification.json").read_text())
    assert report["passed"] and report["gpu"] == {"backend_type": "backend-A"}


@pytest.mark.parametrize("module", ["encoding.py", "worker.py"])
def test_changed_delivery_processing_refuses_frozen_job(tmp_path, monkeypatch, module):
    service, job = frozen_job(tmp_path)
    service.store.update("job", job["id"], lambda value: {**value, "status": "failed"})
    source = Path(environment.__file__).with_name(module)
    read_bytes = Path.read_bytes

    def changed_source(path):
        # Model a code update without editing the checkout or replacing the
        # identity function; both resume and worker checks read the changed bytes.
        value = read_bytes(path)
        return value + b"\n# changed delivery processing\n" if path == source else value

    monkeypatch.setattr(Path, "read_bytes", changed_source)
    with pytest.raises(ChromaError, match="Renderer or dependencies changed") as error:
        service.resume_job(job["id"])
    assert error.value.code == "runtime_changed"
    with pytest.raises(ChromaError, match="Frozen renderer or dependencies changed") as error:
        worker.run(service.store.root, job["id"])
    assert error.value.code == "runtime_changed"
    assert not list(service.store.root.glob("cache/*/frames/*.exr"))
