"""Append verified frame records without rewriting a growing sequence every frame."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .store import canonical


class FrameManifest:
    """Recovery reads the last snapshot plus an append-only, private journal."""

    def __init__(self, path: Path, metadata: dict[str, Any] | None = None) -> None:
        self.path = path
        self.journal = path.with_suffix(".jsonl")
        self.metadata = metadata or {}
        saved = json.loads(path.read_text()) if path.exists() else {}
        self.frames: dict[str, Any] = saved.get("frames", {})
        if self.journal.exists():
            with self.journal.open("r+b") as stream:
                while line := stream.readline():
                    if not line.endswith(b"\n"):
                        # A killed writer may leave only its last record incomplete.
                        # Discard that tail before appending; the frame will be rendered again.
                        stream.seek(-len(line), 1)
                        stream.truncate()
                        break
                    record = json.loads(line)
                    self.frames[str(record["frame"])] = record["entry"]

    def record(self, frame: int, entry: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open("ab") as stream:
            stream.write((canonical({"frame": frame, "entry": entry}) + "\n").encode())
        # Publish to the in-memory index only after the record has been flushed.
        self.frames[str(frame)] = entry

    def finish(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_suffix(".partial")
        partial.write_text(json.dumps(
            {**self.metadata, "frames": self.frames}, ensure_ascii=False, indent=2, allow_nan=False,
        ))
        partial.replace(self.path)
        # If interrupted after replace, replaying the journal is idempotent.
        # Only the final ordinary JSON manifest is included in a published delivery.
        self.journal.unlink(missing_ok=True)
