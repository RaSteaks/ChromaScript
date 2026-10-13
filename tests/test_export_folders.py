"""Native folder actions preserve cancellation, concurrency and literal paths."""

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from chromascript import desktop, runtime
from chromascript.errors import ChromaError
from chromascript.operations import REGISTRY, execute
from chromascript.service import Service


def test_picker_returns_selected_folder_without_persisting_grants(tmp_path, monkeypatch):
    service = Service(tmp_path / "workspace")
    chosen = tmp_path / "选择 ' $() 文件夹"
    chosen.mkdir()
    calls = []
    monkeypatch.setattr(desktop, "directory_picker_available", lambda: True)

    def pick(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"cancelled": False, "directory": str(chosen)}),
            stderr="",
        )

    monkeypatch.setattr(desktop.subprocess, "run", pick)
    before = service.store.list("project")
    reply = execute(
        service, "choose_export_directory", {"initial_directory": str(tmp_path / "not-created")}
    )
    assert reply == {"cancelled": False, "directory": str(chosen)}
    assert calls[0][0][-1] == str(tmp_path)
    assert calls[0][1]["timeout"] == 300 and "shell" not in calls[0][1]
    assert service.store.list("project") == before
    assert not service.store.list("export_root")


def test_picker_cancellation_and_failure_leave_destination_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "directory_picker_available", lambda: True)
    monkeypatch.setattr(
        desktop.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout='{"cancelled":true,"directory":null}', stderr=""
        ),
    )
    assert desktop.choose_directory(tmp_path)["cancelled"]

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("osascript", 300)

    monkeypatch.setattr(desktop.subprocess, "run", timeout)
    with pytest.raises(ChromaError, match="超时"):
        desktop.choose_directory(tmp_path)
    assert not desktop._picker_lock.locked()


def test_native_dialog_does_not_hold_the_shared_operation_lock(tmp_path, monkeypatch):
    service = Service(tmp_path)
    started, finished = threading.Event(), threading.Event()

    def pick(initial):
        started.set()
        assert finished.wait(5)
        return {"cancelled": True, "directory": None}

    monkeypatch.setattr("chromascript.service.choose_directory", pick)
    with ThreadPoolExecutor(max_workers=2) as pool:
        selection = pool.submit(execute, service, "choose_export_directory", {})
        assert started.wait(2)
        try:
            assert (
                pool.submit(execute, service, "list_projects", {}).result(timeout=1)["total"] == 0
            )
        finally:
            finished.set()
        assert selection.result()["cancelled"]
    assert not REGISTRY["choose_export_directory"].serialized


def test_open_folder_uses_literal_arguments_and_validates_existence(tmp_path, monkeypatch):
    directory = tmp_path / "folder ' $(literal)"
    directory.mkdir()
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    calls = []

    def opened(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(desktop.subprocess, "run", opened)
    reply = Service(tmp_path / "workspace").open_export_directory(str(directory))
    assert reply["directory"] == str(directory)
    assert calls == [["/usr/bin/open", str(directory)]]
    with pytest.raises(ChromaError, match="不存在"):
        desktop.open_directory(str(tmp_path / "missing"))
    assert len(calls) == 1


def test_managed_artifact_can_export_outside_workspace_without_overwrite(tmp_path):
    service = Service(tmp_path / "workspace")
    source = service.store.file("frames/source.bin")
    source.parent.mkdir()
    source.write_bytes(b"original contents")
    service.store.put("artifact", "a", {"relative_path": "frames/source.bin"})
    target = tmp_path / "outside" / "copied.bin"
    result = service.export_artifact("a", str(target))
    assert result["directory"] == str(target.parent)
    assert target.read_bytes() == source.read_bytes()
    target.write_bytes(b"user edit")
    with pytest.raises(ChromaError):
        service.export_artifact("a", str(target))
    assert target.read_bytes() == b"user edit"


def test_model_and_cli_proxy_allow_user_paced_folder_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runtime, "ensure_daemon", lambda root: {"url": "http://127.0.0.1:1", "token": "test"}
    )
    seen = []

    def post(*args, **kwargs):
        seen.append(kwargs["timeout"])
        return SimpleNamespace(is_success=True, json=lambda: {"cancelled": True})

    monkeypatch.setattr(runtime.httpx, "post", post)
    runtime.call(tmp_path, "chromascript.choose_export_directory", {})
    runtime.call(tmp_path, "list_projects", {})
    assert seen == [330, 60]
