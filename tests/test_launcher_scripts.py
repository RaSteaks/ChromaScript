"""Exercise launcher boundaries without downloading packages or touching real workspaces."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Fake only external processes so the real shell scripts still handle quoting,
# installation order, early failure, and preservation of an existing environment.
TOOL = r'''
import json
import os
import shutil
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["TOOL_LOG"], "a") as log:
    log.write(json.dumps([name, args, os.getcwd()]) + "\n")
if name == "python3.12" and args[:2] == ["-m", "venv"]:
    target = Path(args[2]) / "bin"
    target.mkdir(parents=True)
    shutil.copy2(sys.argv[0], target / "python")
elif name == "python" and args[:3] == ["-m", "pip", "install"]:
    if os.environ.get("FAIL_PIP"):
        sys.exit(17)
    if "-e" in args:
        Path(".venv/bin/chromascript").touch()
elif name == "npm" and args[-2:] == ["run", "build"]:
    Path("frontend/dist").mkdir(parents=True, exist_ok=True)
    Path("frontend/dist/index.html").write_text("ready")
elif name == "python" and args[:2] == ["-m", "chromascript.cli"]:
    sys.exit(int(os.environ.get("SERVER_EXIT", "0")))
'''


@pytest.fixture
def checkout(tmp_path: Path):
    root = tmp_path / "项目 with spaces"
    root.mkdir()
    for name in ("install.sh", "start.sh"):
        shutil.copy2(ROOT / name, root / name)
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("python3.12", "node", "npm", "ffmpeg", "ffprobe"):
        tool = tools / name
        tool.write_text(f"#!{sys.executable}\n{TOOL}")
        tool.chmod(0o755)
    # No host package managers can be reached by the fixture's restricted PATH.
    (tools / "dirname").symlink_to("/usr/bin/dirname")
    env = {**os.environ, "PATH": str(tools), "TOOL_LOG": str(tmp_path / "tools.jsonl")}
    return root, env


def run_script(checkout, name, *args, **overrides):
    root, env = checkout
    return subprocess.run(
        ["/bin/bash", str(root / name), *args],
        cwd=root.parent,
        env={**env, **overrides},
        capture_output=True,
        text=True,
        timeout=15,
    )


def calls(checkout):
    log = Path(checkout[1]["TOOL_LOG"])
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_install_repeat_preserves_data_and_builds_from_checkout(checkout):
    root, _ = checkout
    workspace = root / "workspace"
    workspace.mkdir()
    sentinel = workspace / "keep.txt"
    sentinel.write_text("existing data")
    for _ in range(2):
        result = run_script(checkout, "install.sh")
        assert result.returncode == 0, result.stderr
    assert sentinel.read_text() == "existing data"
    entries = calls(checkout)
    assert sum(args[:2] == ["-m", "venv"] for _, args, _ in entries) == 1
    installs = [args for _, args, _ in entries if args[:2] == ["-m", "pip"]]
    assert installs == [
        ["-m", "pip", "install", "-r", "requirements.lock"],
        ["-m", "pip", "install", "-e", ".", "--no-deps"],
    ] * 2
    assert all(Path(cwd) == root for name, _, cwd in entries if name == "npm")
    assert (root / "frontend/dist/index.html").is_file()


def test_install_failure_stops_before_frontend_and_can_retry(checkout):
    failed = run_script(checkout, "install.sh", FAIL_PIP="1")
    assert failed.returncode == 17
    assert "安装未完成" in failed.stderr
    assert not any(name == "npm" for name, _, _ in calls(checkout))
    assert run_script(checkout, "install.sh").returncode == 0


def test_install_refuses_foreign_environment(checkout):
    root, _ = checkout
    (root / ".venv").mkdir()
    sentinel = root / ".venv/keep.txt"
    sentinel.write_text("do not delete")
    result = run_script(checkout, "install.sh")
    assert result.returncode == 1
    assert "请先将其移走" in result.stderr
    assert sentinel.read_text() == "do not delete"


def test_missing_dependency_does_not_create_environment(checkout):
    root, env = checkout
    (Path(env["PATH"]) / "ffprobe").unlink()
    result = run_script(checkout, "install.sh")
    assert result.returncode == 1
    assert "缺少 ffprobe" in result.stderr
    assert not (root / ".venv").exists()


def test_start_paths_and_exit_status_without_installing(checkout):
    root, env = checkout
    assert run_script(checkout, "install.sh").returncode == 0
    Path(env["TOOL_LOG"]).write_text("")
    assert run_script(checkout, "start.sh").returncode == 0
    default = calls(checkout)[0][1]
    assert default == ["-m", "chromascript.cli", "--workspace", str(root / "workspace"), "serve"]
    result = run_script(
        checkout, "start.sh", "--port", "8766", "--workspace", "data space",
        "--export-root", "exports space", SERVER_EXIT="23",
    )
    assert result.returncode == 23
    entries = calls(checkout)
    assert len(entries) == 2
    assert entries[-1][1] == [
        "-m", "chromascript.cli", "--workspace", "data space", "serve",
        "--port", "8766", "--export-root", "exports space",
    ]
    assert Path(entries[-1][2]) == root.parent


@pytest.mark.parametrize("args", [("--workspace",), ("--workspace=",), ("-w", "--port")])
def test_start_rejects_missing_workspace(checkout, args):
    result = run_script(checkout, "start.sh", *args)
    assert result.returncode == 2
    assert "需要一个工作区路径" in result.stderr
    assert not calls(checkout)


def test_start_requires_install_but_help_is_available(checkout):
    assert run_script(checkout, "start.sh", "--help").returncode == 0
    result = run_script(checkout, "start.sh")
    assert result.returncode == 1
    assert "安装不完整" in result.stderr
    assert not calls(checkout)
