"""SQLite state and immutable snapshots survive transport and daemon restarts."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import httpx

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
        self.on_job_change: Callable[[dict[str, Any]], None] | None = None
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(kind,id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS requests (key TEXT PRIMARY KEY, hash TEXT NOT NULL, job_id TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS job_notifications (job_id TEXT PRIMARY KEY, token TEXT NOT NULL)"
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
            notification = self._queue_job_change(connection, id) if kind == "job" else None
        if kind == "job":
            assert notification is not None
            self.notify_job(value, notification)
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
            notification = self._queue_job_change(connection, id) if kind == "job" else None
        if kind == "job":
            assert notification is not None
            self.notify_job(value, notification)
        return value

    def _queue_job_change(self, connection: sqlite3.Connection, job_id: str) -> str:
        # The outbox commits with the job, so a worker exit or failed HTTP hop
        # cannot lose its terminal update. Coalesce progress to one row per job.
        token = identifier()
        connection.execute(
            "INSERT INTO job_notifications VALUES (?,?) "
            "ON CONFLICT(job_id) DO UPDATE SET token=excluded.token",
            (job_id, token),
        )
        return token

    def flush_job_notifications(self) -> None:
        # Only the owning daemon drains the outbox. Its existing dispatcher tick
        # retries durable signals without polling browser job lists or workers.
        if self.on_job_change is None:
            return
        with self.connect() as connection:
            pending = connection.execute(
                "SELECT r.data, n.token FROM job_notifications n "
                "JOIN records r ON r.kind='job' AND r.id=n.job_id LIMIT 128"
            ).fetchall()
        for data, token in pending:
            self.notify_job(json.loads(data), token)

    def notify_job(self, job: dict[str, Any], notification: str) -> None:
        # Notify only after COMMIT: a browser responding to the signal must see
        # the corresponding durable state. In-daemon writes need no HTTP hop.
        if self.on_job_change:
            self.on_job_change(job)
            self._ack_job_change(job["id"], notification)
            return
        try:
            info = json.loads((self.root / "server.json").read_text())
        except (OSError, ValueError):
            return
        if info.get("workspace") != str(self.root) or not info.get("job_events"):
            return
        # Independent render workers signal the owning daemon over authenticated
        # loopback HTTP. Failures leave the committed outbox entry for the daemon
        # to publish even after this worker exits; rendering never waits for retry.
        try:
            response = httpx.post(
                info["url"] + "/api/job-events",
                json={"job_id": job["id"]},
                headers={"Authorization": "Bearer " + info["token"]},
                timeout=1,
                trust_env=False,
            )
            response.raise_for_status()
            self._ack_job_change(job["id"], notification)
        except (httpx.HTTPError, KeyError) as exc:
            logging.getLogger(__name__).warning("Job notification unavailable: %s", type(exc).__name__)

    def _ack_job_change(self, job_id: str, token: str) -> None:
        # A newer commit can race delivery. Acknowledge only the version sent,
        # never delete the replacement notification for the same job.
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM job_notifications WHERE job_id=? AND token=?", (job_id, token)
            )

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
            notification = self._queue_job_change(connection, job["id"])
        self.notify_job(job, notification)
        return job

    def file(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ChromaError(
                "path_outside_workspace", "File must be inside the configured workspace"
            )
        return path

    def revise_scene(
        self, scene_id: str, expected: str, revision: dict[str, Any], *, motion: bool = False
    ) -> None:
        """Commit history and current pointer together; rejected edits leave no phantom revision."""
        # React source has its own namespace; it must never be read as WGSL/ACEScg.
        kind, history = ("motion_scene", "motion_revision") if motion else ("scene", "revision")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM records WHERE kind=? AND id=?", (kind, scene_id)
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
                "INSERT INTO records VALUES (?,?,?)", (history, revision["id"], canonical(revision))
            )
            scene.update(
                revision_id=revision["id"], name=revision["scene"]["name"], updated_at=now()
            )
            connection.execute(
                "UPDATE records SET data=? WHERE kind=? AND id=?",
                (canonical(scene), kind, scene_id),
            )
