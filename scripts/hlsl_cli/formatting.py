"""Changed-line formatting and ownership-safe, warning-only Git hooks."""

import os
from pathlib import Path
import re
import subprocess

from . import workspace as ws
from .build_support import _check_migration
from .command import Plan


MARKER = "# hlsl-dev clang-format hook"
HOOK = f"""#!/bin/sh
{MARKER}
# This hook is a warning, never a commit gate. No machine-specific paths.
root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
for start in "$root" "$(cd "$(dirname "$0")" && pwd -P)"; do
    root=$start
    while [ "$root" != / ]; do
        if [ -f "$root/devenv.nix" ] && [ -f "$root/scripts/hlsl.py" ]; then
            break
        fi
        root=$(dirname "$root")
    done
    [ "$root" = / ] || break
done
[ "$root" != / ] || exit 0
export HLSL_DEV_ROOT="$root"
export PATH="$root/.devenv/profile/bin:$PATH"
python3 -B "$root/scripts/hlsl.py" format --quiet || :
exit 0
"""


def _git(directory, *args):
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise ws.SelectionError(f"cannot run git in {directory}: {error}") from error
    if result.returncode:
        raise ws.SelectionError(
            f"git {' '.join(args)} failed in {directory}: "
            f"{result.stderr.strip() or result.stdout.strip() or f'exit {result.returncode}'}"
        )
    return result.stdout.strip()


def _checkout(request):
    """Formatting accepts any Git checkout, not just recognized HLSL sources."""
    if request.worktree:
        candidate = Path(request.worktree)
        if not candidate.is_dir():
            candidate = request.root / request.worktree
        if not candidate.is_dir():
            candidate = ws.resolve(request.root, request.worktree).path
    else:
        candidate = request.cwd or Path.cwd()
    return Path(_git(candidate, "rev-parse", "--show-toplevel"))


def _hook_paths(request):
    if request.worktree:
        trees = [_checkout(request)]
    else:
        trees = [
            tree.path
            for kind in ws.TARGET_KINDS
            for tree in ws.worktrees(request.root, kind)
        ]
    seen = set()
    for tree in trees:
        if not (tree / ".clang-format").is_file():
            continue
        common = Path(
            _git(tree, "rev-parse", "--path-format=absolute", "--git-common-dir")
        )
        if common not in seen:
            seen.add(common)
            yield tree.name, common / "hooks/pre-commit"


def _state(path):
    """Distinguish a missing, owned, or foreign hook without following symlinks."""
    if path.is_symlink():
        return "foreign"
    try:
        contents = path.read_text()
    except FileNotFoundError:
        return "missing"
    except (OSError, UnicodeError):
        return "foreign"
    if contents.splitlines()[:2] != ["#!/bin/sh", MARKER]:
        return "foreign"
    return "current" if contents == HOOK and os.access(path, os.X_OK) else "stale"


def _hooks(request, *, preview):
    action = request.hook_action
    paths = list(_hook_paths(request))  # Discover all targets before any writes.
    output = []
    stale = blocked = 0
    for name, path in paths:
        state = _state(path)
        if state == "foreign":
            if action != "check":
                blocked += 1
                output.append(
                    f"{name}: foreign pre-commit hook at {path}; left untouched"
                )
            continue
        if action == "check":
            if state != "current":
                stale += 1
                output.append(f"{name}: {state} pre-commit hook at {path}")
        elif action == "install" and state != "current":
            output.append(f"{'would install' if preview else 'installed'} {path}")
            if not preview:
                path.parent.mkdir(parents=True, exist_ok=True)
                if state == "missing":
                    # O_EXCL prevents replacing a newly created foreign hook.
                    try:
                        with path.open("x") as hook:
                            hook.write(HOOK)
                    except FileExistsError as error:
                        raise ws.SelectionError(
                            f"hook appeared at {path}; left untouched"
                        ) from error
                else:
                    # A stale owned hook may be replaced; never follow a symlink.
                    if _state(path) != "stale":
                        raise ws.SelectionError(f"hook changed at {path}; left untouched")
                    path.write_text(HOOK)
                path.chmod(0o755)
        elif action == "remove" and state in ("current", "stale"):
            output.append(f"{'would remove' if preview else 'removed'} {path}")
            if not preview:
                if _state(path) not in ("current", "stale"):
                    raise ws.SelectionError(f"hook changed at {path}; left untouched")
                path.unlink()
    if not request.quiet and not output:
        output.append("nothing to do")
    return Plan(
        "\n".join(output) + ("\n" if output else ""),
        1 if stale else 2 if blocked else 0,
    )


def _clang_format(directory, *args):
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "clang-format", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise ws.SelectionError(f"cannot run git clang-format: {error}") from error
    # git clang-format uses exit 1 for both an emitted patch and an applied
    # change. Only these known outputs are success; an actual error still fails.
    changed = result.stdout.startswith(
        "diff --git " if args and args[0] == "--diff" else "changed files:\n"
    )
    if result.returncode and not (
        result.returncode == 1 and changed and not result.stderr.strip()
    ):
        raise ws.SelectionError(
            "git clang-format failed: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
        )
    return result.stdout


def _format(request, *, preview):
    directory = _checkout(request)
    if not (directory / ".clang-format").is_file():
        return Plan(
            "" if request.quiet else f"{directory} has no .clang-format; nothing to check\n"
        )
    scope = (request.since,) if request.since else ()
    # Dry-run of --fix reports the same changed-line patch but cannot edit files.
    if request.fix and not preview:
        result = _clang_format(directory, *scope)
        if result.startswith(
            ("no modified files to format", "clang-format did not modify any files")
        ):
            return Plan("" if request.quiet else "clang-format: nothing to fix\n")
        return Plan(
            result
            + "\nThe working tree was updated; 'git add' the files you want in this commit.\n"
        )
    output = _clang_format(directory, "--diff", *scope)
    if not output.strip() or output.startswith(
        ("no modified files to format", "clang-format did not modify any files")
    ):
        return Plan("" if request.quiet else "clang-format: nothing to fix\n")
    if request.diff or request.fix:
        return Plan(output, 1)
    files = sorted(set(re.findall(r"^\+\+\+ b/(.+)$", output, re.MULTILINE)))
    if not files:
        raise ws.SelectionError("git clang-format returned an unrecognized diff")
    return Plan(
        (
            "clang-format would change these files:\n"
            if request.since
            else "clang-format would change these staged files:\n"
        )
        + "".join(f"    {name}\n" for name in files)
        + "\n    hlsl format --diff    to see it\n"
        + "    hlsl format --fix     to apply it\n",
        1,
    )


def execute(request, *, preview=False):
    if not preview and (request.fix or request.hook_action in ("install", "remove")):
        _check_migration(request.root.resolve())
    try:
        if request.hook_action:
            if request.since or request.diff or request.fix:
                raise ws.SelectionError(
                    "hook management cannot be combined with formatting options"
                )
            return _hooks(request, preview=preview)
        return _format(request, preview=preview)
    except OSError as error:
        raise ws.SelectionError(f"cannot manage formatting: {error}") from error
