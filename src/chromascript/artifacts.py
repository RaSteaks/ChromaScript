"""Frozen artifact locations and bounded ZIP64 downloads without a stored archive."""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Generator
from pathlib import Path
from typing import Any

from .errors import ChromaError
from .exports import MANIFEST, contained_path, motion_bundle
from .images import file_hash
from .store import Store

ArchiveMember = tuple[str, Path, str]
CHUNK_BYTES = 1024**2


def artifact_path(store: Store, artifact: dict[str, Any]) -> Path:
    """Historical records default to workspace; external roots come only from snapshots."""
    if artifact.get("storage", "workspace") == "workspace":
        return store.file(artifact["relative_path"])
    if artifact.get("storage") != "delivery":
        raise ChromaError("artifact_storage", "成果存储位置无效。")
    job = store.get("job", artifact["job_id"])
    bundle = motion_bundle(job, store.get("snapshot", job["id"]))
    return contained_path(bundle.artifact_root(), artifact["relative_path"])


def archive_members(store: Store, artifact: dict[str, Any]) -> list[ArchiveMember]:
    """Enumerate only published, verified sequence members, never a directory glob."""
    if (
        artifact.get("representation") != "zip-stream"
        or artifact.get("storage") != "delivery"
        or artifact["relative_path"] != "masters"
    ):
        raise ChromaError("artifact_archive", "请选择已完成的浮点母版归档。")
    job = store.get("job", artifact["job_id"])
    snapshot = store.get("snapshot", job["id"])
    bundle = motion_bundle(job, snapshot)
    base = bundle.artifact_root()
    if base != bundle.target:
        raise ChromaError("archive_pending", "母版尚未发布，请等待动画导出完成。")
    try:
        delivery = json.loads(contained_path(base, MANIFEST).read_text())
        sequence = contained_path(base, "masters/sequence.json")
        sequence_entry = delivery["files"]["masters/sequence.json"]
        if file_hash(sequence) != sequence_entry["sha256"]:
            raise ValueError("sequence manifest changed")
        frames = json.loads(sequence.read_text())["frames"]
        count = snapshot["scene"]["frames"]
        if set(frames) != {str(index) for index in range(count)}:
            raise ValueError("incomplete sequence")
        names = [f"masters/{index:08d}.exr" for index in range(count)]
        members: list[ArchiveMember] = []
        for index, name in enumerate(names):
            entry = delivery["files"][name]
            frame = frames[str(index)]
            if not frame["passed"] or frame["sha256"] != entry["sha256"]:
                raise ValueError("unverified frame")
        for name in [*names, "masters/sequence.json"]:
            entry = delivery["files"][name]
            path = contained_path(base, name)
            if not path.is_file() or path.stat().st_size != entry["bytes"]:
                raise ValueError("member missing or changed")
            members.append((name, path, entry["sha256"]))
        return members
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ChromaError(
            "archive_changed", "母版文件缺失或内容已改变，请检查交付文件夹。"
        ) from exc


class _ZipSink:
    """An unseekable sink drained after each block; no archive-sized memory buffer."""

    def __init__(self) -> None:
        self.parts: list[bytes] = []
        self.offset = 0

    def write(self, data: bytes) -> int:
        if data:
            self.parts.append(data)
        self.offset += len(data)
        return len(data)

    def tell(self) -> int:
        return self.offset

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.parts.clear()

    def drain(self) -> list[bytes]:
        parts, self.parts = self.parts, []
        return parts


def stream_zip(members: list[ArchiveMember]) -> Generator[bytes, None, None]:
    """Stream already-compressed EXRs with ZIP64 data descriptors and per-file hashes."""
    sink = _ZipSink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for name, path, expected_hash in members:
            # Recheck links at open time; a download must not follow user-added links.
            base = path.parents[len(Path(name).parts) - 1]
            if base.resolve() != base:
                raise ChromaError("export_directory_changed", "母版目录路径已改变。")
            contained_path(base, name)
            info = zipfile.ZipInfo.from_file(path, arcname=name)
            digest = hashlib.sha256()
            with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as member:
                yield from sink.drain()
                while chunk := source.read(CHUNK_BYTES):
                    digest.update(chunk)
                    member.write(chunk)
                    yield from sink.drain()
                if digest.hexdigest() != expected_hash:
                    # No central directory is sent after failure: a changed delivery
                    # cannot masquerade as a complete verified ZIP download.
                    raise ChromaError("archive_changed", "母版在下载时发生变化，请检查交付文件。")
            yield from sink.drain()
    yield from sink.drain()
