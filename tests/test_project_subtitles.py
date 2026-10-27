"""Project-owned subtitle revisions freeze into real delivery folders and ZIPs."""

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from chromascript import motion_sdr_worker
from chromascript.artifacts import archive_members, stream_zip
from chromascript.errors import ChromaError
from chromascript.models import Rational, RenderSettings
from chromascript.motion import MotionOutput, MotionSpec
from chromascript.operations import execute
from chromascript.project_storage import migrate
from chromascript.service import Service
from chromascript.subtitles import ProjectSubtitles, freeze_subtitles
from chromascript.worker import run


def subtitles(text="原始字幕", language="zh-en"):
    return ProjectSubtitles.model_validate({"tracks": [{
        "language": language, "fps": {"numerator": 30000, "denominator": 1001},
        "frames": 60, "cues": [{"start_frame": 0, "end_frame": 60, "zh": text, "en": "Original"}],
    }]})


@pytest.mark.parametrize("partitioned", [False, True])
def test_project_storage_versions_selection_and_migration(tmp_path, partitioned):
    service = Service(tmp_path / "workspace")
    if partitioned:
        migrate(service.store)
    pid = service.create_project("项目字幕")["project"]["id"]
    assert service.get_project_subtitles(pid)["revision_id"] == "0"
    saved = execute(service, "set_project_subtitles", {
        "project_id": pid, "expected_revision_id": "0", "subtitles": subtitles().model_dump(),
    })
    original_path = Path(saved["files"][0]["local_path"])
    assert original_path.read_text().endswith("原始字幕\nOriginal\n\n")
    if partitioned:
        assert original_path.is_relative_to(service.store.project_directory(pid) / "字幕")
    with pytest.raises(ChromaError, match="latest project subtitles"):
        service.set_project_subtitles(pid, "0", subtitles("失效草稿"))
    updated = service.set_project_subtitles(pid, saved["revision_id"], subtitles("新版字幕"))
    assert saved["revision_id"] != updated["revision_id"]
    assert "原始字幕" in original_path.read_text()
    assert "新版字幕" in Path(updated["files"][0]["local_path"]).read_text()
    if not partitioned:
        migrate(service.store)
    folder = service.store.project_directory(pid)
    folder.rename(folder.with_name("用户重命名项目"))
    restored = Service(service.store.root)
    located = restored.get_project_subtitles(pid)
    assert Path(located["files"][0]["local_path"]).is_file()
    backup = tmp_path / "backup"
    restored.export_project_folder(pid, str(backup))
    assert len(list((backup / "字幕").rglob("*.srt"))) == 2
    empty = restored.set_project_subtitles(pid, updated["revision_id"], ProjectSubtitles())
    assert empty["files"] == []
    assert freeze_subtitles(restored.store, pid, RenderSettings()) is None
    # Clearing future selection preserves both previous authored versions.
    assert len(list((restored.store.project_directory(pid) / "字幕").rglob("*.srt"))) == 2


def test_duplicate_language_missing_project_and_archived_project(tmp_path):
    with pytest.raises(ValidationError, match="one subtitle track"):
        ProjectSubtitles(tracks=subtitles().tracks * 2)
    service = Service(tmp_path)
    with pytest.raises(ChromaError, match="does not exist"):
        service.set_project_subtitles("missing", "0", subtitles())
    pid = service.create_project("archived")["project"]["id"]
    service.store.update("project", pid, lambda p: {**p, "archived": True})
    with pytest.raises(ChromaError, match="Restore"):
        service.set_project_subtitles(pid, "0", subtitles())


@pytest.mark.parametrize("partitioned", [False, True])
def test_clone_preserves_independent_selected_subtitles(tmp_path, partitioned):
    service = Service(tmp_path)
    if partitioned:
        migrate(service.store)
    pid = service.create_project("original")["project"]["id"]
    saved = service.set_project_subtitles(pid, "0", subtitles())
    clone = service.clone_project(pid, "copy")["project"]["id"]
    copied = service.get_project_subtitles(clone)
    assert copied["subtitles"] == saved["subtitles"]
    assert copied["revision_id"] != saved["revision_id"]
    original_path = Path(saved["files"][0]["local_path"])
    copied_path = Path(copied["files"][0]["local_path"])
    assert original_path != copied_path
    assert original_path.read_bytes() == copied_path.read_bytes()
    original_path.unlink()
    assert copied_path.is_file()


def test_freeze_trim_missing_changed_and_duration(tmp_path):
    service = Service(tmp_path)
    pid = service.create_project("timing")["project"]["id"]
    saved = service.set_project_subtitles(pid, "0", subtitles())
    settings = RenderSettings(frames=30, start_frame=15, fps=Rational(numerator=30000, denominator=1001))
    frozen = freeze_subtitles(service.store, pid, settings)
    assert frozen["files"]["subtitles.zh-en.srt"].startswith("1\n00:00:00,000 --> 00:00:01,001\n")
    with pytest.raises(ChromaError, match="未覆盖"):
        freeze_subtitles(service.store, pid, settings.model_copy(update={"frames": 60}))
    path = Path(saved["files"][0]["local_path"])
    path.write_text("externally changed")
    with pytest.raises(ChromaError, match="外部修改"):
        freeze_subtitles(service.store, pid, settings)
    path.unlink()
    with pytest.raises(ChromaError, match="文件缺失"):
        freeze_subtitles(service.store, pid, settings)


def test_multiple_destinations_freeze_versions_and_job_identity(tmp_path):
    service = Service(tmp_path / "workspace")
    service._renderer = SimpleNamespace(info={"backend_type": "test"})
    pid = service.create_project("versions")["project"]["id"]
    scene = service.create_scene(pid)
    saved = service.set_project_subtitles(pid, "0", subtitles())
    settings = RenderSettings(width=16, height=16, frames=2, samples=1)
    def submit(key, destination):
        return service.submit_delivery(scene["record"]["id"], settings, ["exr-16-zip"], key,
                                       export_directory=str(tmp_path / destination))["job"]
    a = submit("a", "output-a")
    assert submit("a", "output-a")["id"] == a["id"]
    b = submit("b", "output-b")
    frozen = service.store.get("snapshot", a["id"])["subtitles"]
    assert service.store.get("snapshot", b["id"])["subtitles"] == frozen
    service.set_project_subtitles(pid, saved["revision_id"], subtitles("新版字幕"))
    with pytest.raises(ChromaError, match="different render settings"):
        submit("a", "output-a")
    c = submit("c", "output-c")
    assert "新版字幕" in service.store.get("snapshot", c["id"])["subtitles"]["files"]["subtitles.zh-en.srt"]
    assert service.store.get("snapshot", a["id"])["subtitles"] == frozen


@pytest.mark.gpu
def test_wgsl_worker_delivers_frozen_subtitles_and_full_zip(tmp_path):
    service = Service(tmp_path / "workspace")
    migrate(service.store)
    pid = service.create_project("delivery")["project"]["id"]
    scene = service.create_scene(pid)
    saved = service.set_project_subtitles(pid, "0", subtitles())
    job = service.submit_delivery(
        scene["record"]["id"], RenderSettings(width=16, height=16, frames=2, samples=1),
        ["exr-16-zip"], "delivery",
    )["job"]
    service.set_project_subtitles(pid, saved["revision_id"], subtitles("新版字幕"))
    # Recovery must use frozen text, even if the original saved SRT disappears.
    Path(saved["files"][0]["local_path"]).unlink()
    run(service.store.root, job["id"])
    result = service.get_job(job["id"])
    assert result["job"]["status"] == "completed", result["job"]["error"]
    target = Path(job["export_path"])
    assert "原始字幕" in (target / "subtitles.zh-en.srt").read_text()
    manifest = json.loads((target / "delivery.json").read_text())
    assert "subtitles.zh-en.srt" in manifest["files"]
    artifact = next(a for a in result["artifacts"] if a["kind"] == "delivery-archive")
    data = b"".join(stream_zip(archive_members(service.store, artifact)))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.read("subtitles.zh-en.srt") == (target / "subtitles.zh-en.srt").read_bytes()
        assert "shader.wgsl" in archive.namelist()
    destination = tmp_path / "delivery.zip"
    service.export_artifact(artifact["id"], str(destination))
    assert zipfile.is_zipfile(destination)
    folder = service.store.project_directory(pid)
    folder.rename(folder.with_name("relocated"))
    members = archive_members(service.store, artifact)
    subtitle_path = next(path for name, path, _ in members if name == "subtitles.zh-en.srt")
    subtitle_path.write_bytes(b"x" * subtitle_path.stat().st_size)
    with pytest.raises(ChromaError, match="下载时发生变化"):
        service.export_artifact(artifact["id"], str(tmp_path / "corrupt.zip"))
    assert not (tmp_path / "corrupt.zip").exists()


def test_sdr_delivery_copies_sidecars_without_browser_launch(tmp_path, monkeypatch):
    # Only browser capture is replaced; real worker, video encoding and ZIP run.
    for module in ("motion", "motion_sdr_worker", "service"):
        monkeypatch.setattr(f"chromascript.{module}.motion_identity", lambda: {"fixture": 1})
    monkeypatch.setattr("chromascript.motion.motion_environment", lambda: {"render_available": True})
    monkeypatch.setattr(motion_sdr_worker, "browser_executable", lambda: "unused-fixture")

    class CaptureInput(io.StringIO):
        def close(self):
            request = json.loads(self.getvalue())
            frames = Path(request["frames"])
            for index in range(request["spec"]["frames"]):
                Image.new("RGBA", (16, 16), (24, 48, 72, 255)).save(frames / f"frame-{index}.png")
            super().close()

    process = SimpleNamespace(stdin=CaptureInput(), stdout=io.StringIO('{"completed":2}\n'),
                              wait=lambda: 0, poll=lambda: 0)
    monkeypatch.setattr(motion_sdr_worker, "subprocess", SimpleNamespace(
        Popen=lambda *args, **kwargs: process, PIPE=motion_sdr_worker.subprocess.PIPE,
    ))
    service = Service(tmp_path / "workspace")
    pid = service.create_project("SDR字幕")["project"]["id"]
    scene = service.create_motion_scene(pid, engine="remotion-sdr-v1")
    spec = MotionSpec.model_validate(scene["scene"]).model_copy(update={"width": 16, "height": 16, "frames": 2})
    scene = service.update_motion_scene(scene["record"]["id"], spec, scene["revision_id"])
    service.set_project_subtitles(pid, "0", subtitles())
    job = service.submit_motion_render(scene["record"]["id"], scene["revision_id"], "sdr-sidecar",
        output=MotionOutput(master_bit_depth=None, video="srgb", render_scale=1,
                            video_processing="reference", video_encoder="software"))["job"]
    run(service.store.root, job["id"])
    result = service.get_job(job["id"])
    assert result["job"]["status"] == "completed", result["job"]["error"]
    artifact = next(a for a in result["artifacts"] if a["kind"] == "delivery-archive")
    with zipfile.ZipFile(io.BytesIO(b"".join(stream_zip(archive_members(service.store, artifact))))) as archive:
        assert "原始字幕" in archive.read("subtitles.zh-en.srt").decode()
        assert any(name.endswith(".mp4") for name in archive.namelist())
