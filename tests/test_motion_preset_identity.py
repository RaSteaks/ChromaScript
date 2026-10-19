"""Changed encoding presets invalidate queued/retried motion jobs before browser launch."""

from pathlib import Path

import pytest

from chromascript import motion
from chromascript.errors import ChromaError
from chromascript.motion import MotionOutput, blank_motion
from chromascript.service import Service
from chromascript.worker import run


@pytest.mark.parametrize("engine", ["three-float-v1", "remotion-sdr-v1"])
def test_preset_update_refuses_resume_and_worker(tmp_path, monkeypatch, engine):
    environment = motion.motion_environment()
    monkeypatch.setattr(
        motion, "motion_environment", lambda: {**environment, "render_available": True}
    )
    service = Service(tmp_path)
    pid = service.create_project("preset identity")["project"]["id"]
    spec = blank_motion(engine).model_copy(update={"width": 16, "height": 16, "frames": 1})
    scene = service.create_motion_scene(pid, spec=spec)
    job = service.submit_motion_render(
        scene["record"]["id"],
        scene["revision_id"],
        "preset",
        output=MotionOutput(master_bit_depth=None, video="srgb"),
    )["job"]
    service.store.update("job", job["id"], lambda j: {**j, "status": "failed"})
    source = Path(motion.__file__).with_name("presets.py")
    read_bytes = Path.read_bytes

    # Simulate an installed preset update without touching the checkout. The actual
    # fingerprint and both rejection paths run; no rendering/browser code is reached.
    def changed(path):
        data = read_bytes(path)
        return data.replace(b"crf=18,", b"crf=30,") if path == source else data

    monkeypatch.setattr(Path, "read_bytes", changed)
    with pytest.raises(ChromaError) as error:
        service.resume_job(job["id"])
    assert error.value.code == "runtime_changed"
    run(service.store.root, job["id"])
    assert service.get_job(job["id"])["job"]["error"]["code"] == "runtime_changed"
    assert not list(tmp_path.glob("exports/**/*"))
