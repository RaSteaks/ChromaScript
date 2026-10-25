"""Commit notifications for connected clients; SQLite remains the source of truth."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any


class JobEvents:
    def __init__(self) -> None:
        self.listeners: set[asyncio.Queue[dict[str, str] | None]] = set()
        self.closed = False

    def close(self) -> None:
        # Wake idle SSE consumers before the HTTP server drains connections.
        self.closed = True
        for queue in self.listeners:
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)

    def publish(self, job: dict[str, Any]) -> None:
        signal = {"project_id": job["project_id"], "job_id": job["id"], "kind": job["kind"]}
        for queue in self.listeners:
            if queue.full():
                # A slow client gets one full reconciliation instead of losing
                # the final change or retaining an unbounded frame-event backlog.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
            else:
                queue.put_nowait(signal)

    async def stream(self) -> AsyncIterator[str]:
        queue: asyncio.Queue[dict[str, str] | None] = asyncio.Queue(maxsize=128)
        self.listeners.add(queue)
        try:
            # Subscribe before asking the client to reconcile. Changes committed
            # during its initial/reconnect read are then queued, never missed.
            yield "data: null\n\n"
            while not self.closed:
                try:
                    signal = await asyncio.wait_for(queue.get(), timeout=15)
                    if self.closed:
                        return
                    yield "data: " + json.dumps(signal) + "\n\n"
                except TimeoutError:
                    # Transport keepalive only: no job/list reads on either side.
                    yield ": keepalive\n\n"
        finally:
            self.listeners.discard(queue)
