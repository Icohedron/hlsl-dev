"""Plan and run workspace submodule initialization and remote updates.

Git operations are scoped to the checkout containing the CLI source. Each
submodule is handled separately so an existing full clone never receives a
shallow fetch, including nested submodules.
"""

from dataclasses import dataclass
from pathlib import Path
import shlex
import subprocess
import sys

from .build_support import _check_migration, migration_prerequisite
from .workspace import SelectionError


DEPTH = "2"


@dataclass(frozen=True)
class Submodule:
    """One configured path and its current history state."""

    path: Path
    initialized: bool
    shallow: bool

    @property
    def depth(self):
        return not self.initialized or self.shallow


def _git(directory, *args, statuses=(0,), stream=False):
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=not stream,
            text=True,
            check=False,
        )
    except OSError as error:
        raise SelectionError(f"{directory}: cannot run git: {error}") from error
    if result.returncode not in statuses:
        reason = (result.stderr or "").strip() or f"exit {result.returncode}"
        raise SelectionError(
            f"{directory}: git {shlex.join(args)} failed: {reason}; "
            "check the submodule URL and Git metadata"
        )
    return result.stdout or ""


def _root(root):
    root = root.resolve()
    if not (root / ".gitmodules").is_file():
        raise SelectionError(f"{root}: no .gitmodules; select an HLSL workspace")
    top = _git(root, "rev-parse", "--show-toplevel").strip()
    if Path(top).resolve() != root:
        raise SelectionError(f"{root}: not the top level of this workspace")
    return root


def _modules(parent, root):
    config = parent / ".gitmodules"
    if not config.is_file():
        return []
    # -z separates records by NUL and key from value by newline, so paths
    # containing spaces are never split like shell words.
    output = _git(
        parent,
        "config",
        "-z",
        "-f",
        str(config),
        "--get-regexp",
        r"^submodule\..*\.path$",
        statuses=(0, 1),
    )
    modules = []
    for record in output.split("\0"):
        if not record:
            continue
        key, separator, name = record.partition("\n")
        if not separator or not name or "\n" in name:
            raise SelectionError(f"{config}: invalid submodule path entry {key!r}")
        path = Path(name)
        if path.is_absolute() or any(part in (".", "..") for part in path.parts):
            raise SelectionError(f"{config}: unsafe submodule path {name!r}")
        target = parent / path
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise SelectionError(
                f"{config}: submodule path escapes workspace: {name!r}"
            )
        if not key.startswith("submodule."):
            raise SelectionError(f"{config}: invalid submodule entry {key!r}")
        # .gitmodules can retain an entry after its gitlink is removed (DXC's
        # external/googletest does). Git rejects updates for such paths.
        entries = _git(parent, "ls-files", "--stage", "-z", "--", str(path))
        if not any(
            record.partition(" ")[0] == "160000"
            and record.partition("\t")[2] == str(path)
            for record in entries.split("\0") if record
        ):
            continue
        initialized = (target / ".git").exists()
        if initialized:
            checkout_root = Path(
                _git(target, "rev-parse", "--show-toplevel").strip()
            ).resolve()
            if checkout_root != target.resolve():
                raise SelectionError(f"{target}: not a standalone submodule checkout")
            superproject = _git(
                target, "rev-parse", "--show-superproject-working-tree"
            ).strip()
            if Path(superproject).resolve() != parent.resolve():
                raise SelectionError(f"{target}: belongs to a different superproject")
        shallow = (
            _git(target, "rev-parse", "--is-shallow-repository").strip() == "true"
            if initialized
            else False
        )
        modules.append(Submodule(path, initialized, shallow))
    return modules


def _command(module, *, remote):
    # A .gitmodules shallow=true recommendation can override the requested
    # depth to 1, or shallow-fetch a previously full checkout.
    args = [
        "git", "submodule", "update", "--init", "--checkout",
        "--no-recommend-shallow", "--progress",
    ]
    if remote:
        args.append("--remote")
    if module.depth:
        args += ["--depth", DEPTH]
    return (*args, "--", str(module.path))


def plan(root, *, remote):
    """Describe current submodules; nested ones appear after their parent exists."""
    root = _root(root)
    lines = [f"workspace {root}", "cost: Git network fetch (unless remotes are local)"]
    legacy = migration_prerequisite(root)
    if legacy:
        lines.append(f"blocked: {legacy} needs explicit workspace migration")
    found = False

    def visit(parent):
        nonlocal found
        for module in _modules(parent, root):
            found = True
            target = parent / module.path
            history = (
                "full history" if module.initialized and not module.shallow
                else "shallow" if module.initialized else "not initialized"
            )
            command = shlex.join(_command(module, remote=remote))
            lines.append(f"{target}: {history}; {command}")
            if module.initialized:
                visit(target)
            else:
                lines.append(f"{target}: nested modules inspected after initialization")

    visit(root)
    if not found:
        lines.append("no submodules configured")
    return "\n".join(lines) + "\n"


def execute(root, *, remote):
    """Update each initialized module without applying a depth to full clones."""
    root = _root(root)
    _check_migration(root)
    lines = [f"workspace {root}"]

    def visit(parent):
        for module in _modules(parent, root):
            target = parent / module.path
            command = _command(module, remote=remote)
            lines.append(f"{target}: {shlex.join(command)}")
            print(f"updating {target} (Git fetch may take a while)",
                  file=sys.stderr, flush=True)
            _git(parent, *command[1:], stream=True)
            visit(target)

    visit(root)
    if len(lines) == 1:
        lines.append("no submodules configured")
    return "\n".join(lines) + "\n"
