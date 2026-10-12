"""SQLite state and immutable snapshots survive transport and daemon restarts."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .errors import ChromaError


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identifier() -> str:
    return uuid4().hex


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.sqlite3"
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(kind,id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS requests (key TEXT PRIMARY KEY, hash TEXT NOT NULL, job_id TEXT NOT NULL)"
            )

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def get(self, kind: str, id: str) -> dict[str, Any]:
        with self.connect() as connection:
            record = connection.execute(
                "SELECT data FROM records WHERE kind=? AND id=?", (kind, id)
            ).fetchone()
        if not record:
            raise ChromaError("not_found", f"{kind} {id} does not exist")
        return json.loads(record[0])

    def put(self, kind: str, id: str, value: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO records VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
                (kind, id, canonical(value)),
            )
        return value

    def update(
        self, kind: str, id: str, change: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute(
                "SELECT data FROM records WHERE kind=? AND id=?", (kind, id)
            ).fetchone()
            if not record:
                raise ChromaError("not_found", f"{kind} {id} does not exist")
            value = change(json.loads(record[0]))
            connection.execute(
                "UPDATE records SET data=? WHERE kind=? AND id=?", (canonical(value), kind, id)
            )
            return value

    def list(self, kind: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT data FROM records WHERE kind=? ORDER BY rowid DESC", (kind,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def create_job(
        self, key: str, snapshot_hash: str, job: dict[str, Any], snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        # A transaction makes idempotency robust even when two clients submit at once.
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT hash,job_id FROM requests WHERE key=?", (key,)
            ).fetchone()
            if previous:
                if previous[0] != snapshot_hash:
                    raise ChromaError(
                        "idempotency_conflict", "This key already names different render settings"
                    )
                return json.loads(
                    connection.execute(
                        "SELECT data FROM records WHERE kind='job' AND id=?", (previous[1],)
                    ).fetchone()[0]
                )
            connection.execute(
                "INSERT INTO records VALUES ('snapshot',?,?)", (job["id"], canonical(snapshot))
            )
            connection.execute(
                "INSERT INTO records VALUES ('job',?,?)", (job["id"], canonical(job))
            )
            connection.execute(
                "INSERT INTO requests VALUES (?,?,?)", (key, snapshot_hash, job["id"])
            )
        return job

    def file(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ChromaError(
                "path_outside_workspace", "File must be inside the configured workspace"
            )
        return path

    def revise_scene(self, scene_id: str, expected: str, revision: dict[str, Any]) -> None:
        """Commit history and current pointer together; rejected edits leave no phantom revision."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM records WHERE kind='scene' AND id=?", (scene_id,)
            ).fetchone()
            if not row:
                raise ChromaError("not_found", "Scene does not exist")
            scene = json.loads(row[0])
            if scene["revision_id"] != expected:
                raise ChromaError(
                    "revision_conflict",
                    "Read the latest scene before editing",
                    current_revision_id=scene["revision_id"],
                )
            connection.execute(
                "INSERT INTO records VALUES ('revision',?,?)", (revision["id"], canonical(revision))
            )
            scene.update(
                revision_id=revision["id"], name=revision["scene"]["name"], updated_at=now()
            )
            connection.execute(
                "UPDATE records SET data=? WHERE kind='scene' AND id=?",
                (canonical(scene), scene_id),
            )
