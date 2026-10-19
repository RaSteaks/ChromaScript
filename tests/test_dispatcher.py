"""Deterministic worker/dispatcher races, without starting processes or browsers."""

import psutil
import pytest

from chromascript.dispatcher import Dispatcher
from chromascript.store import Store


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "running"])
def test_dispatcher_preserves_concurrent_transition(tmp_path, monkeypatch, status):
    store = Store(tmp_path)
    original = {
        "id": "job",
        "status": "running",
        "stage": "rendering",
        "pid": 123456,
        "process_started": 1,
    }
    store.put("job", "job", original)
    current = {**original, "status": status, "stage": status, "progress": 1}
    if status == "running":
        current["process_started"] = 2  # Same PID can refer to a different worker attempt.

    def exited(pid):
        store.put("job", "job", current)
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr("chromascript.dispatcher.psutil.Process", exited)
    dispatcher = Dispatcher.__new__(Dispatcher)
    dispatcher.store, dispatcher.children = store, {}
    dispatcher.tick()
    assert store.get("job", "job") == current


def test_dispatcher_interrupts_genuinely_dead_worker(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.put(
        "job",
        "job",
        {
            "id": "job",
            "status": "running",
            "stage": "rendering",
            "pid": 123456,
            "process_started": 1,
        },
    )

    def exited(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr("chromascript.dispatcher.psutil.Process", exited)
    dispatcher = Dispatcher.__new__(Dispatcher)
    dispatcher.store, dispatcher.children = store, {}
    dispatcher.tick()
    result = store.get("job", "job")
    assert result["status"] == result["stage"] == "interrupted"
