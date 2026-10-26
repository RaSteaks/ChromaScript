"""Verified frames survive interruption without quadratic manifest writes."""

import json

import pytest

from chromascript.frame_manifest import FrameManifest


def test_legacy_snapshot_incremental_recovery_and_torn_tail(tmp_path):
    path = tmp_path / "sequence.json"
    path.write_text(json.dumps({"frames": {"0": {"sha256": "old"}}}))
    manifest = FrameManifest(path, {"settings": {"frames": 3}})
    manifest.record(1, {"sha256": "verified"})
    intact = manifest.journal.read_bytes()
    manifest.journal.write_bytes(intact + b'{"frame":2,"entry":')
    recovered = FrameManifest(path, manifest.metadata)
    assert recovered.frames == {"0": {"sha256": "old"}, "1": {"sha256": "verified"}}
    assert recovered.journal.read_bytes() == intact
    recovered.record(2, {"sha256": "last"})
    journal = recovered.journal.read_bytes()
    recovered.finish()
    assert not recovered.journal.exists()
    assert json.loads(path.read_text()) == {"settings": {"frames": 3}, "frames": recovered.frames}
    # A crash between snapshot replacement and journal removal replays harmlessly.
    recovered.journal.write_bytes(journal)
    assert FrameManifest(path).frames == recovered.frames


def test_replacement_record_and_linear_journal_size(tmp_path):
    manifest = FrameManifest(tmp_path / "manifest.json")
    for index in range(1000):
        manifest.record(index, {"sha256": "a" * 64, "size_bytes": 100})
    first_size = manifest.journal.stat().st_size
    for index in range(1000, 2000):
        manifest.record(index, {"sha256": "b" * 64, "size_bytes": 100})
    assert manifest.journal.stat().st_size < first_size * 2.1
    assert not manifest.path.exists()  # No full sequence rewrite per frame.
    manifest.record(2, {"sha256": "replacement"})
    assert FrameManifest(manifest.path).frames["2"] == {"sha256": "replacement"}
    manifest.finish()
    assert len(json.loads(manifest.path.read_text())["frames"]) == 2000


def test_complete_corrupt_record_is_not_silently_certified(tmp_path):
    manifest = FrameManifest(tmp_path / "manifest.json")
    manifest.journal.write_bytes(b'{"frame":\n')
    with pytest.raises(json.JSONDecodeError):
        FrameManifest(manifest.path)
