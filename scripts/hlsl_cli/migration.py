"""Explicit fresh-start migration of only named legacy workspace artifacts."""

from contextlib import ExitStack
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import subprocess
import tempfile

from . import workspace as ws
from .build_support import _save


BEGIN = "# >>> codegraph: local index scope, not committed >>>"
END = "# <<< codegraph <<<"


@dataclass(frozen=True)
class MigrationPreview:
    """An inventory and its preflight verdict, not permission to delete."""

    text: str
    artifacts: tuple[Path, ...] = ()
    markers: tuple[tuple[ws.Worktree, Path, bytes], ...] = ()
    conflicts: tuple[str, ...] = ()
    locks: tuple[Path, ...] = ()


def _present(path):
    return path.exists() or path.is_symlink()


def _entries(path):
    """Inventory contents without following symlinks, including broken links."""
    if not _present(path):
        return []
    result = [path]
    if path.is_dir() and not path.is_symlink():
        for child in sorted(path.iterdir()):
            result.extend(_entries(child))
    return result


def _git(tree, *args, allow_status=()):
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(tree.path), *args],
            capture_output=True, check=False, text=True,
        )
    except OSError as error:
        raise ws.SelectionError(f"cannot inspect Git metadata for {tree.path}: {error}") from error
    if result.returncode not in (0, *allow_status):
        raise ws.SelectionError(
            f"git {' '.join(args)} failed for {tree.path}: "
            f"{result.stderr.strip() or result.returncode}; check Git metadata"
        )
    return result.stdout


def _ignore_state(tree, marker):
    """Only the legacy appended block can be removed from a tracked ignore."""
    if not marker.is_file() or marker.is_symlink():
        return None, f"{marker}: managed ignore marker is not a regular file"
    current = marker.read_bytes()
    lines = current.splitlines(keepends=True)
    begins = [i for i, line in enumerate(lines) if line.rstrip(b"\r\n") == BEGIN.encode()]
    ends = [i for i, line in enumerate(lines) if line.rstrip(b"\r\n") == END.encode()]
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        return None, f"{marker}: incomplete or repeated managed ignore markers"
    if begins[0] == 0 or lines[begins[0] - 1] != b"\n":
        return None, f"{marker}: changes outside managed block or block not appended by CodeGraph"
    body = b"".join(lines[begins[0] + 1:ends[0]])
    legacy_body = (
        b'# CodeGraph\'s built-in ignore list drops every directory called "target" (the\n'
        b'# Rust build dir) or "coverage", case-insensitively. Here that hides\n'
        b'# lib/Target and include/llvm/Target -- in llvm-project the DirectX and SPIR-V\n'
        b'# backends included -- along with their unittests and the coverage libraries.\n'
        b'# Only a root .gitignore negation overrides a built-in default. Managed by\n'
        b"# 'hlsl-codegraph'; 'hlsl-codegraph --restore-gitignore' takes it back out.\n"
        b'!**/Target/\n!**/Target/**\n!**/Coverage/\n!**/Coverage/**\n'
    )
    if body not in (legacy_body, b'!**/Target/\n'):
        return None, f"{marker}: unexpected content inside managed block"
    flags = _git(tree, "ls-files", "-v", "--", ".gitignore").strip()
    if not flags:
        return None, f"{marker}: not tracked; review before migration"
    index = _git(tree, "show", ":.gitignore").encode()
    head = _git(tree, "show", "HEAD:.gitignore").encode()
    start, end = begins[0], ends[0]
    stripped = b"".join(lines[:start] + lines[end + 1:])
    # The old task appends a newline first; remove only that one newline.
    if stripped.endswith(b"\n\n") and index.endswith(b"\n"):
        stripped = stripped[:-1]
    if stripped != index or index != head:
        return None, f"{marker}: changes outside managed block or staged changes"
    return stripped, None


def _locks(state):
    directory = state / "locks"
    if not directory.is_dir() or directory.is_symlink():
        return [], ([f"{directory}: unsafe lock directory"] if _present(directory) else [])
    locks = []
    conflicts = []
    for lock in sorted(directory.iterdir()):
        if lock.suffix != ".lock":
            continue
        if not lock.is_file() or lock.is_symlink():
            conflicts.append(f"{lock}: unsafe build lock")
        else:
            locks.append(lock)
    return locks, conflicts


def _active_locks(state):
    """Inspect existing build locks without changing or creating any files."""
    locks, conflicts = _locks(state)
    for lock in locks:
        try:
            fd = os.open(lock, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    conflicts.append(f"{lock}: active build lock")
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        except OSError as error:
            conflicts.append(f"{lock}: cannot inspect build lock: {error}")
    return conflicts


def _state_dirs(root):
    state = root / ".hlsl-dev"
    states = [state]
    override = os.getenv("HLSL_DEV_STATE")
    if override:
        other = Path(override).resolve()
        if other != state:
            states.append(other)
    return states


def _inventory(root, *, check_active=True):
    artifacts = []
    markers = []
    conflicts = []
    states = _state_dirs(root)
    for directory in states:
        if directory.is_symlink():
            conflicts.append(f"{directory}: state directory is a symlink")
            continue
        for name in ("pins", "settings.env", "toolchains"):
            path = directory / name
            if name == "toolchains" and (path / ".private-cli").is_file():
                continue  # This is a new CLI cache, never old workspace state.
            artifacts.extend(_entries(path))
            if name in ("pins", "toolchains") and _present(path) and (
                not path.is_dir() or path.is_symlink()
            ):
                conflicts.append(f"{path}: legacy directory is not a directory")
            if name == "settings.env" and _present(path) and (
                not path.is_file() or path.is_symlink()
            ):
                conflicts.append(f"{path}: legacy setting is not a regular file")
            if path.is_dir() and not path.is_symlink() and name == "pins":
                for entry in path.iterdir():
                    if not entry.name.endswith(".env") or not entry.is_file() or entry.is_symlink():
                        conflicts.append(f"{entry}: not a legacy pin; review before migration")
            if path.is_dir() and not path.is_symlink() and name == "toolchains":
                for entry in path.iterdir():
                    if entry.name not in ws.PLATFORMS or not entry.is_symlink():
                        conflicts.append(f"{entry}: not a legacy toolchain link; review before migration")
        # New selections/settings and locks are never migration targets.
        _, problems = _locks(directory)
        conflicts.extend(problems)
    trees = []
    for kind in ("llvm", "dxc", "offload", "golden"):
        for tree in ws.worktrees(root, kind):
            trees.append(tree)
            index_paths = (
                tree.path / ".codegraph",
                tree.path / "codegraph.json",
                *sorted(tree.path.glob(".codegraph-*")),
            )
            for path in index_paths:
                artifacts.extend(_entries(path))
                if _present(path) and _git(tree, "ls-files", "--", str(path.relative_to(tree.path))).strip():
                    conflicts.append(f"{path}: tracked CodeGraph path; review before migration")
            if _present(tree.path / ".codegraph/codegraph.lock"):
                conflicts.append(
                    f"{tree.path / '.codegraph/codegraph.lock'}: "
                    "index lock present (verify no active indexer)"
                )
            marker = tree.path / ".gitignore"
            if marker.is_file() and (BEGIN in marker.read_text() or END in marker.read_text()):
                artifacts.append(marker)
                stripped, conflict = _ignore_state(tree, marker)
                if conflict:
                    conflicts.append(conflict)
                else:
                    markers.append((tree, marker, stripped))
    if artifacts:
        if check_active:
            for directory in states:
                conflicts.extend(_active_locks(directory))
        for tree in trees:
            dirty = _git(tree, "status", "--porcelain=v1", "--untracked-files=all")
            for entry in dirty.splitlines():
                path = tree.path / entry[3:]
                relative = path.relative_to(tree.path)
                build_name = relative.parts[0] if relative.parts else ""
                if entry.startswith("?? ") and (
                    build_name in ("build", "build-d3d12", "build-container",
                                   "build-native-tools", "build-dist",
                                   os.getenv("HLSL_BUILD_DIR_NAME", "build"))
                    or build_name.startswith(("build.", "build-d3d12.", "build-dist."))
                ):
                    continue  # Preserved build outputs are not dirty source edits.
                if any(path == artifact or artifact in path.parents
                       for artifact in artifacts if artifact != tree.path / ".gitignore"):
                    continue  # Untracked CodeGraph outputs belong to the migration.
                if path == tree.path / ".gitignore" and any(
                    mark == path for _, mark, _ in markers
                ):
                    continue  # Its tracked content was already checked against HEAD.
                conflicts.append(f"{tree.path}: dirty worktree ({entry}); review Git status")
    return (tuple(dict.fromkeys(artifacts)), tuple(markers),
            tuple(dict.fromkeys(conflicts)), states)


def preview(root, *, check_active=True):
    """Inventory only explicitly named legacy state, never builds or archives."""
    root = root.resolve()
    artifacts, markers, conflicts, states = _inventory(root, check_active=check_active)
    locks = tuple(lock for state in states for lock in _locks(state)[0])
    lines = [f"workspace {root}", "migration preview (read-only; no migration performed)"]
    if artifacts:
        lines.append("legacy artifacts (exact paths; directories include their contents):")
        lines.extend(f"  {path}" for path in artifacts)
    else:
        lines.append("no legacy artifacts; fresh workspace is ready without migration")
    if conflicts:
        lines.append("conflicts to resolve before migration:")
        lines.extend(f"  {conflict}" for conflict in conflicts)
    else:
        lines.append("conflicts: none detected (execution will repeat preflight)")
    lines.append(
        "preserved (never migration targets): source checkouts, build trees, "
        "archives, new JSON selections and build-lock files"
    )
    return MigrationPreview("\n".join(lines) + "\n", artifacts, markers, conflicts, locks)


def _hold_locks(stack, plan):
    """Hold all existing lock inodes through cleanup; fail before any deletion."""
    for lock in plan.locks:
        try:
            fd = os.open(lock, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            if os.fstat(fd).st_ino != lock.stat().st_ino:
                raise ws.SelectionError(f"{lock}: lock changed during preflight")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ws.SelectionError(f"{lock}: active build lock; no artifacts removed") from error
        except OSError as error:
            raise ws.SelectionError(f"{lock}: cannot hold build lock: {error}") from error


def _restore_ignore(tree, marker, original):
    """Keep an interruption from leaving a truncated tracked ignore file."""
    _git(tree, "update-index", "--no-skip-worktree", ".gitignore")
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=marker.parent, prefix=".ignore-migrate-",
                                         delete=False) as output:
            temp = Path(output.name)
            os.fchmod(output.fileno(), marker.stat().st_mode & 0o777)
            output.write(original)
        os.replace(temp, marker)
    finally:
        if temp and temp.exists():
            temp.unlink()


def _remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        # Explicitly enumerated contents only, never follow a symlinked directory.
        for child in sorted(path.iterdir()):
            _remove(child)
        path.rmdir()


def execute(root):
    """Preflight every known checkout/lock before the first change; retry safely."""
    root = root.resolve()
    first = preview(root)
    if first.conflicts:
        raise ws.SelectionError("migration preflight failed; no artifacts removed:\n  "
                                + "\n  ".join(first.conflicts))
    if not first.artifacts:
        return MigrationPreview(f"workspace {root}\nfresh workspace is ready without migration\n")
    with ExitStack() as stack:
        _hold_locks(stack, first)
        current = preview(root, check_active=False)
        if (first.artifacts != current.artifacts or first.markers != current.markers
                or first.locks != current.locks or current.conflicts):
            raise ws.SelectionError("migration inputs changed during preflight; no artifacts removed")
        # Remove CodeGraph blocks first; on interruption a retry still recognizes
        # every remaining index/state path. The index is untouched by Git edits.
        for tree, marker, original in current.markers:
            _restore_ignore(tree, marker, original)
        # Remove only top-level named paths. Children were inventoried for review.
        targets = [path for path in current.artifacts if not any(
            parent in current.artifacts for parent in path.parents
        ) and path.name != ".gitignore"]
        for target in targets:
            _remove(target)
        # Completion is never recorded after partial cleanup. The stamp is not
        # required by fresh workspaces and is not needed for idempotent retry.
        _save(root / ".hlsl-dev/migration.json", {"completed": True})
    return MigrationPreview(f"workspace {root}\nmigration complete; builds and archives preserved\n")
