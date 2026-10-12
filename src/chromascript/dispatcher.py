"""One bounded worker per workspace; independent processes outlive MCP connections."""

from __future__ import annotations

import fcntl
import subprocess
import sys
import threading
from typing import Any

import psutil

from .store import Store, now


class Dispatcher:
    def __init__(self, store: Store):
        self.store = store
        self.stop_event = threading.Event()
        self.children: dict[str, subprocess.Popen[Any]] = {}
        self.lock_file = (store.root / "dispatcher.lock").open("a")
        fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.thread = threading.Thread(
            target=self.loop, daemon=True, name="chromascript-dispatcher"
        )

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        # Do not kill independent workers on a transport/server shutdown.
        self.stop_event.set()
        self.thread.join(timeout=3)
        self.lock_file.close()

    def loop(self) -> None:
        while not self.stop_event.wait(0.3):
            try:
                self.tick()
            except Exception as exc:
                print(f"Dispatcher: {exc}", file=sys.stderr, flush=True)

    def tick(self) -> None:
        for id, child in list(self.children.items()):
            if child.poll() is not None:
                del self.children[id]
        running = False
        for job in self.store.list("job"):
            if job["status"] != "running":
                continue
            alive = False
            try:
                p = psutil.Process(job["pid"])
                alive = (
                    p.is_running()
                    and p.status() != psutil.STATUS_ZOMBIE
                    and abs(p.create_time() - (job["process_started"] or 0)) < 0.1
                )
            except (psutil.NoSuchProcess, TypeError, psutil.AccessDenied):
                pass
            if alive:
                running = True
            else:
                self.store.update(
                    "job",
                    job["id"],
                    lambda j: {
                        **j,
                        "status": "interrupted",
                        "stage": "interrupted",
                        "updated_at": now(),
                    },
                )
        if running:
            return
        queued = [j for j in reversed(self.store.list("job")) if j["status"] == "queued"]
        if not queued:
            return
        job = queued[0]
        path = self.store.file(f"jobs/{job['id']}/worker.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as log:
            child = subprocess.Popen(
                [sys.executable, "-m", "chromascript.worker", str(self.store.root), job["id"]],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        self.children[job["id"]] = child
        born = psutil.Process(child.pid).create_time()

        # Only attach PID to a still-queued job. Cancellation racing dispatch also
        # terminates the new process before it can perform substantial work.
        def claim(j: dict[str, Any]) -> dict[str, Any]:
            if j["status"] != "queued":
                child.terminate()
                return j
            return {
                **j,
                "status": "running",
                "stage": "starting",
                "pid": child.pid,
                "process_started": born,
                "updated_at": now(),
            }

        self.store.update("job", job["id"], claim)
