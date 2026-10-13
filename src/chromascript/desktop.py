"""Local desktop actions; paths are data and native dialogs never execute shell text."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .errors import ChromaError
from .exports import export_directory

_picker_lock = threading.Lock()
FOLDER_DIALOG = r"""
ObjC.import("AppKit");
function run(argv) {
    var app = $.NSApplication.sharedApplication;
    app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);
    var panel = $.NSOpenPanel.openPanel;
    panel.setTitle("选择导出文件夹");
    panel.setPrompt("选择文件夹");
    panel.setCanChooseFiles(false);
    panel.setCanChooseDirectories(true);
    panel.setAllowsMultipleSelection(false);
    panel.setCanCreateDirectories(true);
    if (argv.length) panel.setDirectoryURL($.NSURL.fileURLWithPath(argv[0]));
    app.activateIgnoringOtherApps(true);
    if (Number(panel.runModal) !== Number($.NSModalResponseOK)) {
        return JSON.stringify({cancelled: true, directory: null});
    }
    return JSON.stringify({cancelled: false, directory: ObjC.unwrap(panel.URL.path)});
}
"""


def directory_picker_available() -> bool:
    return sys.platform == "darwin" and Path("/usr/bin/osascript").is_file()


def choose_directory(initial: Path) -> dict[str, Any]:
    if not directory_picker_available():
        raise ChromaError(
            "folder_picker_unavailable", "当前系统不支持文件夹选择器，请在导出位置中直接输入路径。"
        )
    if not _picker_lock.acquire(blocking=False):
        raise ChromaError("folder_picker_busy", "文件夹选择器已打开，请先完成当前选择。")
    try:
        # The initial location is advisory: unfinished/nonexistent edits must not
        # prevent opening the picker. Native cancellation leaves the draft intact.
        try:
            initial = initial.expanduser().resolve()
            while not initial.is_dir() and initial != initial.parent:
                initial = initial.parent
        except (OSError, ValueError, RuntimeError):
            initial = Path.home()
        result = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", FOLDER_DIALOG, str(initial)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode:
            raise ChromaError(
                "folder_picker_failed",
                "无法打开系统文件夹选择器，请重试或直接输入路径。",
                reason=result.stderr[-2000:],
            )
        reply = json.loads(result.stdout)
        if reply.get("cancelled"):
            return {"cancelled": True, "directory": None}
        # Validate the actual chosen folder, rather than inferring filesystem
        # permission from the dialog or storing a permanent directory grant.
        return {"cancelled": False, "directory": str(export_directory(reply["directory"]))}
    except subprocess.TimeoutExpired as exc:
        raise ChromaError("folder_picker_timeout", "选择文件夹超时，请重新打开选择器。") from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ChromaError(
            "folder_picker_failed", "文件夹选择未完成，请重试或直接输入路径。"
        ) from exc
    finally:
        _picker_lock.release()


def open_directory(value: str) -> dict[str, str]:
    try:
        path = Path(value).expanduser()
        if not path.is_absolute() or "\x00" in value:
            raise ValueError("请提供导出文件夹的绝对路径。")
        path = path.resolve()
        if not path.is_dir():
            raise ValueError("导出文件夹不存在，可能已被移动或删除。")
        if sys.platform == "darwin":
            command = ["/usr/bin/open", str(path)]
        else:
            opener = shutil.which("xdg-open")
            if not opener:
                raise ChromaError(
                    "folder_open_unavailable",
                    "当前系统无法自动打开文件夹，请复制导出路径后手动打开。",
                )
            command = [opener, str(path)]
        # No shell interpolation: spaces, quotes and non-ASCII folder names keep
        # their literal meaning, including paths returned by another transport.
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ChromaError(
                "folder_open_failed",
                "无法打开导出文件夹，请复制路径后手动打开。",
                reason=result.stderr[-2000:],
            )
        return {"directory": str(path)}
    except subprocess.TimeoutExpired as exc:
        raise ChromaError("folder_open_timeout", "打开文件夹超时，请重试。") from exc
    except (OSError, ValueError, RuntimeError) as exc:
        raise ChromaError("folder_open_failed", str(exc)) from exc
