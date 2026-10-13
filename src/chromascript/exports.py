"""Publish verified delivery bundles without replacing existing user files."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from .errors import ChromaError
from .images import file_hash

MANIFEST = "delivery.json"


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
