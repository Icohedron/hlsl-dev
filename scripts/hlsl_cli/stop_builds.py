"""Stop workspace commands that can build, without waiting for build locks."""

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import sys
import time

from .command import Plan
from .workspace import SelectionError


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    start: int
    state: str
    argv: tuple[str, ...]


def _read(pid):
    """Return a process identity (PID plus kernel start tick), or None on exit."""
    try:
        directory = Path(f"/proc/{pid}")
        if directory.stat().st_uid != os.geteuid():
            return None
        stat = (directory / "stat").read_text().rsplit(") ", 1)[1].split()
        argv = tuple(os.fsdecode(arg) for arg in
                     (directory / "cmdline").read_bytes().split(b"\0") if arg)
        return Process(pid, int(stat[1]), int(stat[19]), stat[0], argv)
    except (OSError, ValueError, IndexError):
        return None


def _command_args(process, root):
    """Return arguments passed to this workspace's checkout-run CLI."""
    argv = process.argv
    if not argv or "python" not in Path(argv[0]).name:
        return ()
    args = list(argv[1:])
    while args and args[0] in ("-B", "-u", "-E", "-I", "-s", "-S"):
        args.pop(0)
    if len(args) < 2 or Path(args[0]).resolve() != root / "scripts/hlsl.py":
        return ()
    return tuple(args[1:])


def _build_process(process, root):
    """Select checkout-run commands that can launch build subprocesses."""
    args = _command_args(process, root)
    if not args:
        return False
    action = args[0]
    if not (action in ("build", "configure", "test", "package")
            or (action == "distribution" and args[1:2] == ("install",))
            or (action == "cross" and args[1:2] in (("fetch",), ("refresh",)))):
        return False
    # The checkout-run script can be used with HLSL_DEV_ROOT pointing elsewhere.
    # Match the actual workspace chosen by that process, not just its script path.
    try:
        environ = (Path(f"/proc/{process.pid}") / "environ").read_bytes()
    except OSError:
        return False
    values = dict(item.split(b"=", 1) for item in environ.split(b"\0")
                  if b"=" in item)
    for key in (b"HLSL_DEV_ROOT", b"DEVENV_ROOT"):
        if key in values:
            candidate = Path(os.fsdecode(values[key]))
            if (candidate / "devenv.nix").is_file():
                return candidate.resolve() == root
    return Path(__file__).resolve().parents[2] == root


def _snapshot(root):
    if sys.platform != "linux" or not Path("/proc").is_dir():
        raise SelectionError("hlsl builds stop needs Linux /proc")
    processes = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdecimal():
            process = _read(int(entry.name))
            if process and process.state != "Z":
                processes[process.pid] = process
    builds = [process for process in processes.values()
              if _build_process(process, root)]
    return processes, builds


def _descendants(process, children):
    for child in children.get(process.pid, ()):
        # A reused parent PID must not add an unrelated younger process tree.
        if child.start >= process.start:
            yield from _descendants(child, children)
            yield child


def _alive(process):
    current = _read(process.pid)
    return (current is not None and current.start == process.start
            and current.state != "Z")


def _signal(process, sig):
    if not _alive(process):
        return
    try:
        os.kill(process.pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError as error:
        raise SelectionError(f"cannot signal build process {process.pid}: {error}") from error


def plan(root):
    """Show build-capable commands, including those queued on a lock."""
    _, builds = _snapshot(root.resolve())
    if not builds:
        return Plan("No running build-related commands in this workspace.\n")
    lines = ["Build-related commands in this workspace (running or waiting on locks):"]
    lines += [f"  {process.pid}: hlsl {' '.join(_command_args(process, root.resolve()))}"
              for process in sorted(builds, key=lambda p: p.start)]
    lines.append("To stop these commands and their children: hlsl builds stop --yes")
    return Plan("\n".join(lines) + "\n")


def stop(root):
    """Terminate queued build-capable commands, then their active children."""
    root = root.resolve()
    processes, builds = _snapshot(root)
    if not builds:
        return Plan("No running build-related commands in this workspace.\n")
    children = {}
    for process in processes.values():
        children.setdefault(process.parent, []).append(process)
    # Newer commands tend to be queued behind older lock owners. Stop them
    # first so releasing an owner's lock cannot start another queued build.
    ordered = []
    seen = set()
    for build in sorted(builds, key=lambda p: p.start, reverse=True):
        for process in (*_descendants(build, children), build):
            if process.pid not in seen:
                seen.add(process.pid)
                ordered.append(process)
    for process in ordered:
        _signal(process, signal.SIGTERM)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and any(map(_alive, ordered)):
        time.sleep(.05)
    for process in ordered:
        _signal(process, signal.SIGKILL)
    remaining = [str(p.pid) for p in ordered if _alive(p)]
    if remaining:
        raise SelectionError(f"build processes did not exit: {', '.join(remaining)}")
    return Plan(f"Stopped {len(builds)} build-related command(s).\n")
