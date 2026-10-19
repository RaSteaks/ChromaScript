"""Project copies retain authoring content without sharing mutable source ownership."""

import sqlite3
from unittest.mock import patch

import pytest

from chromascript.audio import AudioClip, AudioSynthesis, AudioTimeline
from chromascript.errors import ChromaError
from chromascript.images import file_hash
from chromascript.motion import blank_motion
from chromascript.operations import execute
from chromascript.service import Service


def populated_project(tmp_path):
    service = Service(tmp_path)
    pid = service.create_project("original", "description")["project"]["id"]
    scene = service.create_scene(pid)
    service.update_scene(
        scene["record"]["id"],
        service.scene(scene["record"]["id"])[1].model_copy(update={"name": "latest"}),
        scene["revision_id"],
    )
    for engine in ("three-float-v1", "remotion-sdr-v1"):
        spec = blank_motion(engine).model_copy(
            update={"files": {"chapter.ts": "export const n = 1;"}}
        )
        service.create_motion_scene(pid, spec=spec)
    assets = [
        service.create_audio(pid, name, AudioSynthesis(duration_seconds=0.1))["asset"]
        for name in ("attached", "unused")
    ]
    service.set_project_audio(
        pid,
        "0",
        AudioTimeline(
            clips=[
                AudioClip(
                    asset_id=assets[0]["id"],
                    start_seconds=2.5,
                    source_start_seconds=0.01,
                    duration_seconds=0.05,
                    gain_db=-6,
                ),
                AudioClip(asset_id=assets[0]["id"], start_seconds=3),
            ]
        ),
    )
    return service, pid, assets


def test_clone_preserves_current_sources_and_remaps_audio(tmp_path):
    service, pid, assets = populated_project(tmp_path)
    # The shared registry is the CLI/MCP/HTTP contract; archived sources remain readable.
    service.archive_project(pid)
    result = execute(service, "clone_project", {"project_id": pid, "name": "copy"})
    clone = result["project"]["id"]
    assert not result["project"]["archived"]
    assert result["project"]["description"] == "description"
    assert len(result["scenes"]) == 1
    assert result["scenes"][0]["name"] == "latest"
    assert service.list_revisions(result["scenes"][0]["id"])["total"] == 1
    original_motion = sorted(
        (
            service.get_motion_scene(s["id"])["scene"]
            for s in service.list_motion_scenes(pid)["items"]
        ),
        key=lambda s: s["engine"],
    )
    cloned_motion = sorted(
        (
            service.get_motion_scene(s["id"])["scene"]
            for s in service.list_motion_scenes(clone)["items"]
        ),
        key=lambda s: s["engine"],
    )
    assert cloned_motion == original_motion
    old_audio, new_audio = service.get_project_audio(pid), result["audio"]
    assert old_audio["revision_id"] != new_audio["revision_id"]
    new_assets = {asset["name"]: asset for asset in new_audio["assets"]}
    assert set(new_assets) == {"attached", "unused"}
    for old in assets:
        new = new_assets[old["name"]]
        assert new["id"] != old["id"] and new["project_id"] == clone
        assert new["metadata"] == old["metadata"]
        assert file_hash(service.store.file(new["relative_path"])) == old["sha256"]
        assert service.get_artifact(new["id"])["artifact"]["project_id"] == clone
    for old, new in zip(
        old_audio["timeline"]["clips"], new_audio["timeline"]["clips"], strict=True
    ):
        assert new == {**old, "asset_id": new_assets["attached"]["id"]}
    # Edits in the copy cannot change the original audio revision or timeline.
    service.set_project_audio(clone, new_audio["revision_id"], AudioTimeline())
    assert service.get_project_audio(pid) == old_audio
    assert service.store.list("job") == []


@pytest.mark.parametrize("failure", ["copy", "corrupt", "database"])
def test_failed_clone_leaves_no_partial_project_or_audio(tmp_path, failure):
    service, pid, assets = populated_project(tmp_path)
    if failure == "corrupt":
        service.store.file(assets[0]["relative_path"]).write_bytes(b"changed")
    if failure == "database":
        with service.store.connect() as db:
            db.execute(
                "CREATE TRIGGER fail_clone BEFORE INSERT ON records WHEN NEW.kind='project' "
                "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END"
            )
    with service.store.connect() as db:
        before = db.execute("SELECT * FROM records ORDER BY kind,id").fetchall()
    files = {p.name: p.read_bytes() for p in (tmp_path / "audio").iterdir()}

    # Fail after a copy has started to verify cleanup of partially written files too.
    def no_space(src, dst):
        dst.write(src.read(16))
        raise OSError("injected full disk")

    context = patch("chromascript.service.shutil.copyfileobj", side_effect=no_space)
    if failure == "copy":
        with context, pytest.raises(ChromaError, match="Project copy failed"):
            service.clone_project(pid, "copy")
    elif failure == "corrupt":
        with pytest.raises(ChromaError, match="Audio source changed"):
            service.clone_project(pid, "copy")
    else:
        with pytest.raises(sqlite3.IntegrityError, match="injected write failure"):
            service.clone_project(pid, "copy")
    with service.store.connect() as db:
        assert db.execute("SELECT * FROM records ORDER BY kind,id").fetchall() == before
    assert {p.name: p.read_bytes() for p in (tmp_path / "audio").iterdir()} == files
