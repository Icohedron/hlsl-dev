"""Stopping builds never requires acquiring the lock held by a build."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


SCRIPTS = Path(__file__).resolve().parents[1]
CLI = SCRIPTS / "hlsl.py"


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "devenv.nix").touch()
    script = tmp_path / "scripts/hlsl.py"
    script.parent.mkdir()
    script.write_text(
        "import os, pathlib, subprocess, sys, time\n"
        "if '--child' in sys.argv:\n"
        "    child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(60)'])\n"
        "    pathlib.Path(os.environ['FAKE_CHILD_PID']).write_text(str(child.pid))\n"
        "    child.wait()\n"
        "else:\n"
        "    time.sleep(60)\n"
    )
    return tmp_path


def run_cli(workspace, *args):
    return subprocess.run(
        [sys.executable, str(CLI), "builds", "stop", *args],
        env={**os.environ, "HLSL_DEV_ROOT": str(workspace)},
        capture_output=True, text=True, timeout=8,
    )


def alive(pid):
    try:
        os.kill(pid, 0)
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][0] != "Z"
    except ProcessLookupError:
        return False


@pytest.mark.skipif(sys.platform != "linux", reason="requires /proc")
def test_stop_builds_covers_active_and_queued_without_touching_other_processes(
    workspace, tmp_path,
):
    child_file = tmp_path / "child.pid"
    env = {**os.environ, "HLSL_DEV_ROOT": str(workspace),
           "FAKE_CHILD_PID": str(child_file)}
    commands = (("build", "--active"), ("build", "--queued"),
                ("test", "clang-vk"), ("configure",),
                ("distribution", "install"), ("package", "full", "--child"),
                ("cross", "fetch", "linux-arm64"))
    processes = [
        subprocess.Popen(
            [sys.executable, "-B", str(workspace / "scripts/hlsl.py"), *args],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for args in commands
    ]
    unrelated = subprocess.Popen(
        [sys.executable, "-B", str(workspace / "scripts/hlsl.py"), "info"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    child = None
    try:
        for _ in range(100):
            if child_file.exists():
                child = int(child_file.read_text())
                break
            time.sleep(.02)
        assert child is not None

        preview = run_cli(workspace, "--dry-run")
        assert preview.returncode == 0, preview.stderr
        for proc in processes:
            assert str(proc.pid) in preview.stdout
            assert alive(proc.pid)
        assert alive(child)
        assert str(unrelated.pid) not in preview.stdout

        refused = run_cli(workspace)
        assert refused.returncode != 0
        assert "--yes" in refused.stderr
        assert all(alive(proc.pid) for proc in processes)

        stopped = run_cli(workspace, "--yes")
        assert stopped.returncode == 0, stopped.stderr
        for proc in processes:
            proc.wait(timeout=3)
        assert not alive(child)
        assert alive(unrelated.pid)
        assert "No running build-related commands" in run_cli(workspace, "--dry-run").stdout
    finally:
        for proc in [*processes, unrelated]:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=3)
        if child and alive(child):
            os.kill(child, signal.SIGKILL)


@pytest.mark.skipif(sys.platform != "linux", reason="requires /proc")
def test_stop_builds_does_not_signal_another_workspace(workspace, tmp_path):
    other = tmp_path / "other-workspace"
    other.mkdir()
    (other / "devenv.nix").touch()
    process = subprocess.Popen(
        [sys.executable, "-B", str(workspace / "scripts/hlsl.py"), "build"],
        env={**os.environ, "HLSL_DEV_ROOT": str(other)},
    )
    try:
        assert str(process.pid) not in run_cli(workspace, "--dry-run").stdout
        assert run_cli(workspace, "--yes").returncode == 0
        assert alive(process.pid)
    finally:
        process.terminate()
        process.wait(timeout=3)


def test_pid_reuse_is_not_signaled(monkeypatch):
    sys.path.insert(0, str(SCRIPTS))
    from hlsl_cli import stop_builds

    old = stop_builds.Process(12345, 1, 100, "S", ())
    new = stop_builds.Process(12345, 1, 101, "S", ())
    signals = []
    monkeypatch.setattr(stop_builds, "_read", lambda pid: new)
    monkeypatch.setattr(stop_builds.os, "kill", lambda pid, sig: signals.append(sig))
    stop_builds._signal(old, signal.SIGTERM)
    assert not signals


def test_stop_builds_help_and_conflicting_modes(workspace):
    assert "--dry-run" in run_cli(workspace, "--help").stdout
    result = run_cli(workspace, "--yes", "--dry-run")
    assert result.returncode != 0
