"""Build lock waits report progress without changing lock behavior."""

import io
import os
from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hlsl_cli import build_support  # noqa: E402


class Output(io.StringIO):
    def __init__(self, tty):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


def fake_contention(monkeypatch, release_at=None, interrupt_at=None):
    clock = [0.0]
    fds = []
    def advance(delay):
        clock[0] += delay

    monkeypatch.setattr(build_support.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(build_support.time, "sleep", advance)

    def flock(fd, _flags):
        fds.append(fd)
        if interrupt_at is not None and clock[0] >= interrupt_at:
            raise KeyboardInterrupt
        if release_at is None or clock[0] < release_at:
            raise BlockingIOError

    monkeypatch.setattr(build_support.fcntl, "flock", flock)
    return fds


@pytest.mark.parametrize("tty", (True, False))
def test_contended_lock_reports_wait_then_acquisition(tmp_path, monkeypatch, tty):
    output = Output(tty)
    monkeypatch.setattr(sys, "stderr", output)
    fake_contention(monkeypatch, release_at=.65)
    with build_support.build_lock(tmp_path, tmp_path / "llvm-project/build", timeout=2):
        pass
    result = output.getvalue()
    assert "Waiting for lock" in result
    assert "Acquired lock" in result
    assert ("\r" in result) == tty
    if not tty:
        assert result.count("Waiting for lock") == 1


def test_uncontended_or_immediate_timeout_stays_quiet(tmp_path, monkeypatch):
    output = Output(True)
    monkeypatch.setattr(sys, "stderr", output)
    with build_support.build_lock(tmp_path, tmp_path / "build"):
        with build_support.build_lock(tmp_path, tmp_path / "build"):
            pass
    assert output.getvalue() == ""
    fake_contention(monkeypatch)
    with pytest.raises(build_support.BuildError, match="timed out"):
        with build_support.build_lock(tmp_path, tmp_path / "build", timeout=0):
            pass
    assert output.getvalue() == ""


@pytest.mark.parametrize("interrupt", (False, True))
def test_spinner_clears_on_timeout_or_interrupt(tmp_path, monkeypatch, interrupt):
    output = Output(True)
    monkeypatch.setattr(sys, "stderr", output)
    fds = fake_contention(monkeypatch, interrupt_at=.4 if interrupt else None)
    error = KeyboardInterrupt if interrupt else build_support.BuildError
    with pytest.raises(error):
        with build_support.build_lock(tmp_path, tmp_path / "build", timeout=.5):
            pass
    result = output.getvalue()
    assert "Waiting for lock" in result
    assert "Acquired lock" not in result
    assert result.endswith("\r")  # Cleared before the error is reported.
    with pytest.raises(OSError):
        os.fstat(fds[0])
