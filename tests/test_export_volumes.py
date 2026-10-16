"""Persistent destination identity survives remounts without trusting replacement disks."""

import ctypes
import errno
import json
import os
import struct
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from chromascript import exports
from chromascript.errors import ChromaError
from chromascript.exports import RenderBundle, volume_identity

VOLUME_UUID = "1b445d18-7e5c-4ab5-ac8d-80bed8585521"


def renumber_devices(monkeypatch, affected=lambda path: True):
    original = Path.stat

    def stat(path, *args, **kwargs):
        saved = original(path, *args, **kwargs)
        if not affected(path):
            return saved
        # Preserve file type/size while simulating the new device number assigned
        # by the OS after remounting; no real volumes or user files are touched.
        fields = list(saved)
        fields[2] += 1000
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "stat", stat)


@pytest.mark.parametrize("published", [False, True])
def test_remount_preserves_resume_artifact_access_and_request_identity(tmp_path, monkeypatch, published):
    monkeypatch.setattr(exports, "_volume_uuid", lambda path: VOLUME_UUID)
    destination = tmp_path / "exports"
    frozen = volume_identity(destination)
    assert "device" not in frozen
    # A JSON round trip models loading the immutable snapshot in a new process.
    bundle = RenderBundle(destination / "job", destination, {"job_id": "job"}, json.loads(json.dumps(frozen)))
    output, complete = bundle.prepare()
    assert not complete
    frame = output / "frame.exr"
    frame.write_bytes(b"verified master")
    if published:
        bundle.publish({"frame.exr": frame}, lambda: None)
        bundle.discard_transport()

    renumber_devices(monkeypatch)
    assert volume_identity(destination) == frozen
    recovered, complete = bundle.prepare()
    assert complete == published
    assert recovered == bundle.artifact_root()
    assert (recovered / "frame.exr").read_bytes() == b"verified master"


@pytest.mark.parametrize("operation", ["prepare", "artifact_root"])
@pytest.mark.parametrize("replacement_uuid", ["another-volume", None])
def test_replaced_or_unidentifiable_disk_is_rejected(tmp_path, monkeypatch, operation, replacement_uuid):
    monkeypatch.setattr(exports, "_volume_uuid", lambda path: VOLUME_UUID)
    bundle = RenderBundle(tmp_path / "job", tmp_path, {"job_id": "job"}, volume_identity(tmp_path))
    bundle.prepare()
    # Even a replacement reusing the same device number must fail UUID comparison.
    monkeypatch.setattr(exports, "_volume_uuid", lambda path: replacement_uuid)
    with pytest.raises(ChromaError) as error:
        getattr(bundle, operation)()
    assert error.value.code == "export_volume_unavailable"


def test_nested_mount_is_rejected_even_with_matching_anchor_uuid(tmp_path, monkeypatch):
    monkeypatch.setattr(exports, "_volume_uuid", lambda path: VOLUME_UUID)
    destination = tmp_path / "nested"
    bundle = RenderBundle(destination / "job", destination, {"job_id": "job"}, volume_identity(destination))
    destination.mkdir()
    renumber_devices(monkeypatch, lambda path: path == destination)
    with pytest.raises(ChromaError) as error:
        bundle.prepare()
    assert error.value.code == "export_volume_unavailable"
    assert not bundle.staging.exists()


def test_uuid_boundary_is_preserved_when_apfs_volumes_share_device_number(tmp_path, monkeypatch):
    def uuid(path):
        return VOLUME_UUID if path.is_relative_to(tmp_path) else "system-volume"

    monkeypatch.setattr(exports, "_volume_uuid", uuid)
    destination = tmp_path / "exports"
    frozen = volume_identity(destination)
    assert frozen == {"anchor": str(tmp_path), "uuid": VOLUME_UUID}
    bundle = RenderBundle(destination / "job", destination, {"job_id": "job"}, frozen)
    bundle.prepare()
    assert volume_identity(destination) == frozen
    # A newly redirected subtree can also share st_dev with its frozen anchor.
    monkeypatch.setattr(exports, "_volume_uuid",
                        lambda path: "other-volume" if path == destination else uuid(path))
    with pytest.raises(ChromaError) as error:
        bundle.artifact_root()
    assert error.value.code == "export_volume_unavailable"


def test_missing_anchor_does_not_recreate_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(exports, "_volume_uuid", lambda path: VOLUME_UUID)
    anchor = tmp_path / "disconnected"
    destination = anchor / "exports"
    bundle = RenderBundle(destination / "job", destination, {"job_id": "job"},
                          {"anchor": str(anchor), "uuid": VOLUME_UUID})
    with pytest.raises(ChromaError) as error:
        bundle.prepare()
    assert error.value.code == "export_volume_unavailable"
    assert not anchor.exists()


def test_uuid_read_error_is_reported_at_submission_and_recovery(tmp_path, monkeypatch):
    def unreadable(path):
        raise PermissionError(errno.EACCES, "unreadable volume")

    monkeypatch.setattr(exports, "_volume_uuid", unreadable)
    bundle = RenderBundle(tmp_path / "job", tmp_path, {"job_id": "job"},
                          {"anchor": str(tmp_path), "uuid": VOLUME_UUID})
    for operation in (lambda: volume_identity(tmp_path), bundle.prepare):
        with pytest.raises(ChromaError) as error:
            operation()
        assert error.value.code == "export_volume_unavailable"


def test_legacy_device_only_snapshot_keeps_strict_check(tmp_path, monkeypatch):
    monkeypatch.setattr(exports, "_volume_uuid", lambda path: None)
    frozen = volume_identity(tmp_path)
    assert "device" in frozen and "uuid" not in frozen
    bundle = RenderBundle(tmp_path / "job", tmp_path, {"job_id": "job"}, frozen)
    bundle.prepare()
    # A UUID discovered later cannot retroactively establish a legacy disk's ID.
    monkeypatch.setattr(exports, "_volume_uuid", lambda path: VOLUME_UUID)
    renumber_devices(monkeypatch)
    with pytest.raises(ChromaError) as error:
        bundle.prepare()
    assert error.value.code == "export_volume_unavailable"


@pytest.mark.parametrize("value", [VOLUME_UUID, str(UUID(int=0))])
def test_darwin_uuid_attribute_decoding(monkeypatch, value):
    def getattrlist(path, request, result, size, options):
        assert path == b"/Volumes/test"
        assert struct.unpack("=HHIIIII", request.raw[:24]) == (5, 0, 0, 0x80040000, 0, 0, 0)
        assert size == 20 and options == 0
        result.raw = struct.pack("=I", 20) + UUID(value).bytes
        return 0

    monkeypatch.setattr(exports.sys, "platform", "darwin")
    monkeypatch.setattr(exports.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(getattrlist=getattrlist))
    expected = value if UUID(value).int else None
    assert exports._volume_uuid(Path("/Volumes/test")) == expected


@pytest.mark.parametrize("error", [errno.ENOTSUP, errno.ENOSYS, errno.EIO])
def test_darwin_uuid_attribute_errors(monkeypatch, error):
    def getattrlist(*args):
        ctypes.set_errno(error)
        return -1

    monkeypatch.setattr(exports.sys, "platform", "darwin")
    monkeypatch.setattr(exports.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(getattrlist=getattrlist))
    if error == errno.EIO:
        with pytest.raises(OSError):
            exports._volume_uuid(Path("/Volumes/test"))
    else:
        assert exports._volume_uuid(Path("/Volumes/test")) is None


def test_non_darwin_keeps_device_fallback(monkeypatch):
    monkeypatch.setattr(exports.sys, "platform", "linux")
    assert exports._volume_uuid(Path("/")) is None
