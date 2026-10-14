"""Publish verified delivery bundles without replacing existing user files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from .errors import ChromaError
from .images import file_hash
from .store import canonical

MANIFEST = "delivery.json"
DESTINATION_LAYOUT = "destination-v1"


def contained_path(directory: Path, relative: str) -> Path:
    """Resolve a bundle member without following user-added links or traversal."""
    value = Path(relative)
    if not relative or value.is_absolute() or ".." in value.parts:
        raise ChromaError("export_path", "交付文件名无效。")
    path = directory
    for part in value.parts:
        path /= part
        if path.is_symlink():
            raise ChromaError("export_directory_changed", "交付文件路径已变为符号链接。")
    if not path.resolve().is_relative_to(directory.resolve()):
        raise ChromaError("export_path", "交付文件路径超出任务目录。")
    return path


def export_directory(value: str) -> Path:
    """Accept any user-selected absolute destination writable by the local process."""
    try:
        path = Path(value).expanduser()
        if not value.strip() or "\x00" in value or not path.is_absolute():
            raise ValueError("请输入绝对目录路径，例如 /Users/name/Movies。")
        path = path.resolve()
        parent = path
        while not parent.exists():
            parent = parent.parent
        if not parent.is_dir():
            raise ValueError("导出位置或它的上级路径是文件，请选择目录。")
        if not os.access(parent, os.W_OK | os.X_OK):
            raise ValueError("导出目录不可写，请检查权限或选择其他目录。")
        return path
    except (OSError, ValueError, RuntimeError) as exc:
        raise ChromaError("export_directory", str(exc)) from exc


def bundle_name(scene_name: str, job_id: str) -> str:
    # Preserve readable Chinese names, excluding separators and filesystem syntax.
    name = re.sub(r"[^\w-]+", "-", scene_name, flags=re.UNICODE).strip("-_")[:48]
    return f"{name or 'scene'}-{job_id}"


def _existing_bundle(target: Path, identity: dict[str, str]) -> bool:
    if not target.exists() and not target.is_symlink():
        return False
    try:
        if target.is_symlink() or not target.is_dir():
            raise ValueError("not a bundle directory")
        manifest = target / MANIFEST
        if manifest.is_symlink():
            raise ValueError("linked manifest")
        saved = json.loads(manifest.read_text())
        if any(saved.get(key) != value for key, value in identity.items()):
            raise ValueError("another delivery")
        entries = saved["files"]
        # A retry after publication may reuse only a complete, unchanged bundle.
        # Verify its own manifest: regenerated reports have different elapsed times.
        actual = [p for p in target.rglob("*") if not p.is_dir()]
        if any(p.is_symlink() for p in target.rglob("*")) or not entries:
            raise ValueError("invalid bundle contents")
        if {str(p.relative_to(target)) for p in actual} != {*entries, MANIFEST}:
            raise ValueError("bundle contents changed")
        for path in actual:
            if path == manifest:
                continue
            expected = entries[str(path.relative_to(target))]
            if path.stat().st_size != expected["bytes"] or file_hash(path) != expected["sha256"]:
                raise ValueError("bundle file changed")
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ChromaError(
            "export_conflict",
            "交付文件夹已存在或内容已改变，未覆盖任何文件。请更换导出位置并重新提交。",
            destination=str(target),
        ) from exc


class RenderBundle:
    """One resumable render on the selected volume, published by directory rename."""

    def __init__(self, target: Path, root: Path, identity: dict[str, str]):
        self.target = target
        self.root = root
        self.identity = identity
        self.staging = target.parent / f".chromascript-{identity['job_id']}.partial"
        self.output = self.staging / "output"
        self.work = self.staging / "motion"
        self.owner = self.staging / "owner.json"

    def _check_parent(self) -> None:
        # Reads also check the frozen path, but do not require write permission.
        parent = self.target.parent
        if parent.resolve() != parent or not parent.is_relative_to(self.root):
            raise ChromaError("export_directory_changed", "导出路径发生变化，请检查目录后重新提交。")

    def _check_identity(self, path: Path) -> dict[str, Any]:
        try:
            if path.is_symlink():
                raise ValueError("linked identity")
            saved: dict[str, Any] = json.loads(path.read_text())
            if any(saved.get(key) != value for key, value in self.identity.items()):
                raise ValueError("another render")
            return saved
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            raise ChromaError(
                "export_conflict", "任务交付目录或临时目录的身份不符，未覆盖任何文件。"
            ) from exc

    def _check_staging(self, *, check_files: bool = False) -> None:
        if self.staging.is_symlink() or not self.staging.is_dir():
            raise ChromaError("export_conflict", "任务临时目录无效，未覆盖任何文件。")
        self._check_identity(self.owner)
        for path in (self.output, self.work):
            if path.is_symlink():
                raise ChromaError("export_directory_changed", "任务临时目录已变为符号链接。")
        if check_files and any(path.is_symlink() for path in self.staging.rglob("*")):
            # Check before source/EXR writes, not only at publication: resume must
            # never overwrite a user-added link's target outside the frozen bundle.
            raise ChromaError("export_directory_changed", "任务临时文件已变为符号链接。")

    def artifact_root(self) -> Path:
        """Locate stable artifact IDs before and after publication without rehashing a sequence."""
        self._check_parent()
        if self.target.exists() or self.target.is_symlink():
            if self.target.is_symlink() or not self.target.is_dir():
                raise ChromaError("export_conflict", "任务交付目录无效。")
            self._check_identity(self.target / MANIFEST)
            return self.target
        self._check_staging()
        return self.output

    def prepare(self) -> tuple[Path, bool]:
        self._check_parent()
        if _existing_bundle(self.target, self.identity):
            # A crash after rename must finish the job without rewriting its report/video.
            return self.target, True
        try:
            export_directory(str(self.target.parent)).mkdir(parents=True, exist_ok=True)
            try:
                self.staging.mkdir()
            except FileExistsError:
                self._check_staging(check_files=True)
            else:
                with self.owner.open("x", encoding="utf-8") as stream:
                    json.dump(self.identity, stream)
            self.output.mkdir(exist_ok=True)
            self.work.mkdir(exist_ok=True)
            return self.output, False
        except OSError as exc:
            raise ChromaError("export_failed", "导出盘无法创建任务目录，请检查空间和权限。") from exc

    def publish(self, files: dict[str, Path], check_cancelled: Callable[[], None]) -> Path:
        """Hash the existing render in place; never copy it to another staging directory."""
        self._check_parent()
        self._check_staging(check_files=True)
        if _existing_bundle(self.target, self.identity):
            return self.target
        try:
            entries: dict[str, Any] = {}
            for name, source in files.items():
                check_cancelled()
                if name == MANIFEST or source != contained_path(self.output, name):
                    raise ChromaError("export_path", "交付文件不属于任务临时目录。")
                digest = hashlib.sha256()
                with source.open("rb") as stream:
                    while chunk := stream.read(1024**2):
                        digest.update(chunk)
                        check_cancelled()
                entries[name] = {"bytes": source.stat().st_size, "sha256": digest.hexdigest()}
            actual = {
                str(path.relative_to(self.output))
                for path in self.output.rglob("*")
                if not path.is_dir()
            }
            if actual - {MANIFEST} != set(files) or any(
                path.is_symlink() for path in self.output.rglob("*")
            ):
                raise ChromaError("export_conflict", "临时交付目录出现未登记文件，未发布。")
            manifest = self.output / MANIFEST
            partial = self.staging / "delivery.partial.json"
            partial.write_text(
                json.dumps({**self.identity, "files": entries}, ensure_ascii=False, indent=2)
            )
            partial.replace(manifest)
            check_cancelled()
            if not _existing_bundle(self.target, self.identity):
                # Staging is a child of the frozen destination: rename is on the same volume.
                self.output.rename(self.target)
            return self.target
        except OSError as exc:
            # Verified EXRs remain in staging on ENOSPC/cancellation/publication failure.
            raise ChromaError(
                "export_failed", "交付发布失败，请检查磁盘空间和权限后继续任务。", reason=str(exc)
            ) from exc

    def discard_transport(self) -> None:
        """Remove only this render's disposable state after its output has been renamed."""
        if self.staging.exists() and not self.output.exists():
            self._check_parent()
            self._check_staging()
            shutil.rmtree(self.staging)


def motion_bundle(job: dict[str, Any], snapshot: dict[str, Any]) -> RenderBundle:
    """Derive external storage exclusively from a checksummed, frozen motion snapshot."""
    if (
        snapshot.get("storage_layout") != DESTINATION_LAYOUT
        or snapshot.get("engine") != "three-float-v1"
        or job.get("kind") != "motion"
        or hashlib.sha256(canonical(snapshot).encode()).hexdigest() != job["snapshot_hash"]
    ):
        raise ChromaError("snapshot_corrupt", "动画存储快照校验失败。")
    parent = Path(snapshot["export_directory"])
    target = parent / bundle_name(snapshot["scene"]["name"], job["id"])
    if not parent.is_absolute() or str(target) != job.get("export_path"):
        raise ChromaError("snapshot_corrupt", "动画导出位置与冻结快照不一致。")
    return RenderBundle(
        target,
        Path(snapshot["export_root"]),
        {"job_id": job["id"], "snapshot_sha256": job["snapshot_hash"]},
    )


def publish_bundle(
    files: dict[str, Path],
    target: Path,
    root: Path,
    identity: dict[str, str],
    check_cancelled: Callable[[], None],
) -> Path:
    """Stage on the destination volume; expose the bundle only after all copies finish."""
    parent = export_directory(str(target.parent))
    # This is the job's frozen destination boundary, not a startup allowlist.
    # Keep legacy broader boundaries valid while rejecting moved/symlinked paths.
    if parent != target.parent or not parent.is_relative_to(root):
        raise ChromaError("export_directory_changed", "导出路径发生变化，请检查目录后重新提交。")
    if _existing_bundle(target, identity):
        return target
    staging: Path | None = None
    try:
        parent.mkdir(parents=True, exist_ok=True)
        # A unique staging folder never reuses or deletes an unrelated interrupted copy.
        staging = Path(tempfile.mkdtemp(prefix=f".chromascript-{identity['job_id']}-", dir=parent))
        entries: dict[str, Any] = {}
        for name, source in files.items():
            check_cancelled()
            if Path(name).is_absolute() or ".." in Path(name).parts or name == MANIFEST:
                raise ChromaError("export_path", "交付文件名无效。")
            destination = staging / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as src, destination.open("xb") as dst:
                while chunk := src.read(8 * 1024**2):
                    dst.write(chunk)
                    check_cancelled()
            entries[name] = {"bytes": destination.stat().st_size, "sha256": file_hash(destination)}
        (staging / MANIFEST).write_text(
            json.dumps({**identity, "files": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        check_cancelled()
        if _existing_bundle(target, identity):
            return target
        # Complete bundles are nonempty, so rename cannot replace another published bundle.
        staging.rename(target)
        return target
    except OSError as exc:
        raise ChromaError(
            "export_failed",
            "交付文件夹写入失败。请检查磁盘空间及目录权限，再继续任务。",
            destination=str(target),
            reason=str(exc),
        ) from exc
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
