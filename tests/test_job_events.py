"""Notifications follow commits, coalesce slow clients, and never change job state."""

import asyncio
import json

import httpx
import pytest

from chromascript.dispatcher import Dispatcher
from chromascript.job_events import JobEvents
from chromascript.store import Store


def test_slow_client_reconciles_and_disconnect_removes_subscription():
    async def check():
        events = JobEvents()
        stream = events.stream()
        assert await anext(stream) == "data: null\n\n"
        for _ in range(129):
            events.publish({"id": "job", "project_id": "project", "kind": "motion"})
        assert await anext(stream) == "data: null\n\n"
        await stream.aclose()
        assert not events.listeners

    asyncio.run(check())


def test_commit_signals_are_readable_and_failed_updates_emit_nothing(tmp_path):
    store = Store(tmp_path)
    observed = []

    def committed(job):
        # A separate connection must already see exactly what the notification names.
        assert Store(tmp_path).get("job", job["id"]) == job
        observed.append(job)

    store.on_job_change = committed
    job = {"id": "job", "project_id": "project", "kind": "motion", "status": "queued"}
    store.create_job("key", "hash", job, {})
    store.create_job("key", "hash", job, {})
    assert observed == [job]

    def invalid(_):
        raise ValueError("rejected")

    with pytest.raises(ValueError, match="rejected"):
        store.update("job", "job", invalid)
    assert observed == [job]
    store.update("job", "job", lambda old: {**old, "status": "completed"})
    assert len(observed) == 2
    assert observed[-1]["status"] == "completed"


def test_shutdown_wakes_an_idle_event_stream():
    async def check():
        events = JobEvents()
        stream = events.stream()
        await anext(stream)
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        events.close()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(pending, timeout=1)
        assert not events.listeners

    asyncio.run(check())


@pytest.mark.parametrize("write", ["put", "update", "create_job"])
def test_failed_worker_notification_recovers_without_sse_reconnect(tmp_path, monkeypatch, write):
    async def check():
        daemon = Store(tmp_path)
        events = JobEvents()
        daemon.on_job_change = events.publish
        job = {"id": "job", "project_id": "project", "kind": "motion", "status": "running"}
        if write == "update":
            daemon.put("job", "job", job)
        stream = events.stream()
        assert await anext(stream) == "data: null\n\n"
        # Fail the worker-to-daemon hop while the browser stream stays healthy.
        # Recovery must survive discarding the worker's entire Store instance.
        (tmp_path / "server.json").write_text(json.dumps({
            "workspace": str(tmp_path.resolve()), "job_events": True,
            "url": "http://127.0.0.1:8765", "token": "test-token",
        }))

        def unavailable(*args, **kwargs):
            raise httpx.ConnectTimeout("Temporary notification failure")

        monkeypatch.setattr("chromascript.store.httpx.post", unavailable)
        worker = Store(tmp_path)
        completed = {**job, "status": "completed"}
        if write == "create_job":
            worker.create_job("key", "hash", completed, {})
        elif write == "put":
            worker.put("job", "job", completed)
        else:
            worker.update("job", "job", lambda old: {**old, "status": "completed"})
        del worker
        assert all(queue.empty() for queue in events.listeners)

        dispatcher = Dispatcher(daemon)
        try:
            dispatcher.tick()
            signal = await asyncio.wait_for(anext(stream), timeout=1)
            assert json.loads(signal.removeprefix("data: "))["job_id"] == "job"
            assert daemon.get("job", "job")["status"] == "completed"
            dispatcher.tick()
            assert all(queue.empty() for queue in events.listeners)
        finally:
            # This focused test invokes ticks directly, never starting workers.
            dispatcher.lock_file.close()
            await stream.aclose()

    asyncio.run(check())


def test_acknowledgement_cannot_remove_a_newer_commit(tmp_path):
    worker = Store(tmp_path)
    job = {"id": "job", "project_id": "project", "kind": "motion", "status": "running"}
    worker.put("job", "job", job)
    daemon = Store(tmp_path)
    observed = []

    def publish(current):
        observed.append(current["status"])
        if current["status"] == "running":
            # A second process commits while the first notification is being
            # delivered. Acknowledging the old token must leave this one queued.
            worker.update("job", "job", lambda old: {**old, "status": "completed"})

    daemon.on_job_change = publish
    daemon.flush_job_notifications()
    daemon.flush_job_notifications()
    daemon.flush_job_notifications()
    assert observed == ["running", "completed"]


def test_failed_transaction_does_not_leave_a_notification(tmp_path, monkeypatch):
    store = Store(tmp_path)
    job = {"id": "job", "project_id": "project", "kind": "motion", "status": "queued"}
    enqueue = store._queue_job_change

    def fail_before_commit(connection, job_id):
        enqueue(connection, job_id)
        raise ValueError("Transaction rejected")

    monkeypatch.setattr(store, "_queue_job_change", fail_before_commit)
    with pytest.raises(ValueError):
        # Both job and notification have been inserted, but neither may survive
        # a failure before the shared transaction commits.
        store.create_job("key", "hash", job, {})
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM job_notifications").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
