"""Portable, revision-owned animation modules; never resolve user paths on disk."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .store import canonical

MAX_FILES = 128
MAX_SOURCE_LENGTH = 2_000_000
SOURCE_PATH = re.compile(r"(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.(?:ts|tsx|js|jsx)\Z")


def validate_sources(code: str, files: dict[str, str]) -> dict[str, str]:
    """Keep paths portable and unambiguous on case-insensitive export volumes."""
    if len(files) > MAX_FILES or len(code) + sum(map(len, files.values())) > MAX_SOURCE_LENGTH:
        raise ValueError("最多支持 128 个分章文件，全部源码合计最多 2000000 字符。")
    seen = {"animation.ts", "animation.tsx"}
    directories: dict[str, str] = {}
    for path, source in files.items():
        if len(path) > 180 or not SOURCE_PATH.fullmatch(path):
            raise ValueError(f"无效分章路径 {path!r}；请使用相对路径及 ts/tsx/js/jsx 扩展名。")
        if path.lower() in seen:
            raise ValueError(f"分章路径重复或与入口冲突：{path}")
        seen.add(path.lower())
        # Compare every directory prefix: distinct filenames can still alias a
        # directory on case-insensitive volumes and invalidate the export manifest.
        parts = path.split("/")
        for depth in range(1, len(parts)):
            directory = "/".join(parts[:depth])
            if directories.setdefault(directory.lower(), directory) != directory:
                raise ValueError(f"分章目录大小写不一致：{directory}")
        if not source.strip() or len(source) > 200_000:
            raise ValueError(f"分章 {path} 不能为空，最多 200000 字符。")
    return files


def source_hash(code: str, files: dict[str, str]) -> str:
    # Preserve single-file attestations; any chapter change invalidates the full graph.
    source = canonical({"code": code, "files": files}) if files else code
    return hashlib.sha256(source.encode()).hexdigest()


def write_sources(directory: Path, engine: str, code: str, files: dict[str, str]) -> list[Path]:
    """Export a self-contained tree, separate from masters and renderer work files."""
    validate_sources(code, files)
    root = directory / "sources" if files else directory
    entry = "Animation.tsx" if engine == "remotion-sdr-v1" else "Animation.ts"
    paths = []
    for name, contents in {entry: code, **files}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        paths.append(path)
    return paths
