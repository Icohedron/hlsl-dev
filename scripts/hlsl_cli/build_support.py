"""Shared build selections, CMake data expansion and process/lock safety."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time

from . import workspace as ws


_LOCKS = threading.local()
_PLACEHOLDER = re.compile(r"\$(?:\{(HD_[A-Z_]+)\}|(HD_[A-Z_]+))")
_VALID_TEMPLATE = re.compile(r"^[A-Za-z0-9_./:=,+${}%-]+$")


class BuildError(ws.SelectionError):
    """A build cannot safely proceed with the selected inputs."""


def _key(root, path):
    """Use the old Bash lock key so both task layers serialize the same tree."""
    value = str(path).rstrip("/")
    prefix = str(root).rstrip("/") + "/"
    return value.removeprefix(prefix).replace("/", "%")


def _selection_file(root, tree, platform, d3d12=None):
    suffix = "" if platform == "native" else f"@{platform}"
    if (tree.kind in ("llvm", "offload")
            and (platform == "native" or platform.startswith("windows-"))):
        from .gpu import d3d12 as d3d12_choice

        if d3d12_choice(root, d3d12) == "on":
            suffix += "@d3d12"
    return root / ".hlsl-dev/selections" / f"{_key(root, tree.path)}{suffix}.json"


def _load(root, tree, platform, d3d12=None):
    path = _selection_file(root, tree, platform, d3d12)
    if not path.is_file():
        return {}
    try:
        result = json.loads(path.read_text())
        if not isinstance(result, dict) or result.get("version") != 1:
            raise ValueError("unsupported version")
        choices = result.get("choices")
        if not isinstance(choices, dict) or not isinstance(
            result.get("configured"), dict
        ) or any(not isinstance(value, str) for value in choices.values()):
            raise ValueError("missing or malformed choices/configuration")
        if any(
            key in result and not isinstance(result[key], dict)
            for key in ("attempted", "reconfigure_failed")
        ):
            raise ValueError("malformed configure retry state")
        return result
    except (OSError, ValueError) as error:
        raise BuildError(
            f"invalid selections at {path}: {error}; reconfigure"
        ) from error


def saved_selections(root, tree, platform, d3d12=None):
    """Read this platform's choices, falling back to native per missing key."""
    selection = _load(root, tree, platform, d3d12).get("choices", {})
    if (tree.kind in ("llvm", "offload")
            and (platform == "native" or platform.startswith("windows-"))):
        from .gpu import d3d12 as d3d12_choice

        other = "off" if d3d12_choice(root, d3d12) == "on" else "on"
        fallback = _load(root, tree, platform, other).get("choices", {})
    else:
        fallback = {}
    if platform != "native":
        native = _load(root, tree, "native", d3d12).get("choices", {})
        return {**fallback, **native, **selection}
    return {**fallback, **selection}


def _save(path, selections):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, prefix=".selection-", delete=False
        ) as output:
            name = output.name
            json.dump({"version": 1, **selections}, output, indent=2, sort_keys=True)
            output.write("\n")
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


@contextmanager
def build_lock(root, directory, timeout=None):
    """Hold the Bash-compatible lock without exporting its FD to child tools."""
    path = Path(os.getenv("HLSL_DEV_STATE") or root / ".hlsl-dev") / "locks"
    path = path / f"{_key(root, directory)}.lock"
    held = getattr(_LOCKS, "held", None)
    if held is None:
        held = _LOCKS.held = {}
    if path in held:
        yield  # The outer invocation continues to hold this lock.
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o666)
    except OSError as error:
        raise BuildError(
            f"cannot open the build lock for {directory}: {error}"
        ) from error
    try:
        if timeout is None:
            try:
                timeout = float(os.getenv("HLSL_LOCK_TIMEOUT", "7200"))
            except ValueError as error:
                raise BuildError(
                    "HLSL_LOCK_TIMEOUT must be a number of seconds"
                ) from error
        deadline = time.monotonic() + max(0, timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise BuildError(
                        f"timed out waiting for the build lock on {directory}"
                    ) from error
                time.sleep(min(0.05, deadline - time.monotonic()))
        held[path] = fd
        try:
            yield
        finally:
            del held[path]
    except OSError as error:
        raise BuildError(f"build lock failed for {directory}: {error}") from error
    finally:
        os.close(fd)


def expand_flags(template, values, label):
    """Expand Nix flag data only; never interpret it as a shell program."""
    if not template:
        raise BuildError(f"{label} is missing; enter the devenv shell")
    tokens = template.split()
    if not tokens or any(not _VALID_TEMPLATE.fullmatch(token) for token in tokens):
        raise BuildError("invalid CMake flag template: shell syntax is not allowed")

    def replace(match):
        name = match.group(1) or match.group(2)
        if name not in values:
            raise BuildError(f"unknown CMake flag placeholder ${name}")
        value = str(values[name])
        if not value or any(c.isspace() for c in value):
            raise BuildError(f"{name} is empty or contains whitespace: {value!r}")
        return value

    expanded = []
    for token in tokens:
        flag = _PLACEHOLDER.sub(replace, token)
        if "$" in flag and "${HD_SEMI}" not in token:
            raise BuildError(f"invalid CMake flag template token: {token}")
        expanded.append(flag)
    return expanded


def _flag_hash(flags):
    return hashlib.sha256(json.dumps(flags).encode()).hexdigest()


def _configured(build):
    return (build / "build.ninja").is_file() or (build / "Makefile").is_file()


def _link_database(tree, build):
    database = build / "compile_commands.json"
    if not database.is_file():
        return
    link = tree.path / "compile_commands.json"
    if link.exists() and not link.is_symlink():
        return
    if link.is_symlink():
        link.unlink()
    link.symlink_to(os.path.relpath(database, tree.path))
    exclude_path = ws.git_output(tree.path, "rev-parse", "--git-path", "info/exclude")
    exclude = Path(exclude_path)
    if not exclude.is_absolute():
        exclude = tree.path / exclude
    # Only Git's local exclude is changed, never a tracked .gitignore.
    if exclude.is_file():
        content = exclude.read_text()
        if "/compile_commands.json" not in content.splitlines():
            with exclude.open("a") as output:
                output.write("\n/compile_commands.json\n")


def migration_prerequisite(root):
    """Return the first legacy artifact that blocks new mutating commands."""
    root = root.resolve()
    states = [root / ".hlsl-dev"]
    override = os.getenv("HLSL_DEV_STATE")
    if override:
        state = Path(override).resolve()
        if state != states[0]:
            states.append(state)
    legacy = None
    for state in states:
        legacy = next(
            (path for path in (state / "pins", state / "settings.env")
             if path.exists() or path.is_symlink()),
            None,
        )
        toolchains = state / "toolchains"
        if (legacy is None and (toolchains.exists() or toolchains.is_symlink())
                and not (toolchains / ".private-cli").is_file()):
            legacy = toolchains
        if legacy is not None:
            break
    if legacy is None:
        for kind in ("llvm", "dxc", "offload", "golden"):
            for tree in ws.worktrees(root, kind):
                marker = tree.path / ".gitignore"
                managed = marker.is_file() and (
                    "# >>> codegraph: local index scope, not committed >>>"
                    in marker.read_text()
                )
                if (
                    (tree.path / ".codegraph").exists()
                    or (tree.path / ".codegraph").is_symlink()
                    or (tree.path / "codegraph.json").exists()
                    or (tree.path / "codegraph.json").is_symlink()
                    or any(tree.path.glob(".codegraph-*"))
                    or managed
                ):
                    legacy = tree.path
                    break
            if legacy is not None:
                break
    return legacy


def _check_migration(root):
    legacy = migration_prerequisite(root)
    if legacy is not None:
        raise BuildError(
            f"{legacy}: old workspace state needs explicit migration before "
            "mutation; run 'hlsl workspace migrate --dry-run' to preview, "
            "then 'hlsl workspace migrate --yes' to confirm"
        )


def _run_command(argv, build, *, env=None):
    try:
        subprocess.run(argv, check=True, close_fds=True, env=env)
    except (OSError, subprocess.CalledProcessError) as error:
        raise BuildError(f"{build}: command failed: {error}") from error
