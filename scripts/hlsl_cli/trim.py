"""Preview and remove graph-owned, unreachable ELF binaries from a Ninja tree."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import subprocess

from . import workspace as ws
from .build_support import BuildError, _check_migration, build_lock, migration_prerequisite
from .command import Plan


_NODE = re.compile(r'^"[^"\\]+" \[label="([^"\\]*)"(?:, [^]]*)?\]$')
_EDGE = re.compile(r'^"[^"\\]+" -> "[^"\\]+" \[label="[^"\\]*"(?:, [^]]*)?\]$')
_TARGET = re.compile(r"^[A-Za-z0-9_./+:-]+$")
_DEFAULTS = {"llvm": ("check-hlsl", "check-clang"), "offload": ("check-hlsl",)}


@dataclass(frozen=True)
class File:
    """An unlinkable path and its identity at the time of the preview."""

    path: Path
    identity: tuple[int, int, int, int]


def _identity(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _ninja(build, *args):
    try:
        result = subprocess.run(
            ["ninja", "-C", str(build), "-t", *args],
            capture_output=True, text=True, check=False, close_fds=True,
        )
    except OSError as error:
        raise BuildError(f"{build}: cannot inspect Ninja graph: {error}") from error
    if result.returncode:
        raise BuildError(
            f"{build}: Ninja graph inspection failed: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def _graph_nodes(build, target):
    output = _ninja(build, "graph", target)
    nodes = set()
    for line in output.splitlines():
        if "[label=" not in line or "shape=ellipse" in line:
            continue
        match = _NODE.fullmatch(line)
        if match:
            nodes.add(match.group(1))
        elif not _EDGE.fullmatch(line):
            raise BuildError(f"{build}: cannot safely parse Ninja graph label: {line}")
    if target not in nodes:
        raise BuildError(f"{build}: Ninja graph omitted target {target!r}")
    return nodes


def _outputs(build):
    outputs = set()
    for line in _ninja(build, "targets", "all").splitlines():
        name, separator, _rule = line.rpartition(": ")
        if not separator:
            raise BuildError(f"{build}: cannot safely parse Ninja target: {line}")
        outputs.add(name)
    return outputs


def _install_paths(build):
    """Keep the standard install tree and any in-tree CMake install prefix."""
    excluded = {build / "install"}
    cache = build / "CMakeCache.txt"
    if cache.is_file():
        for line in cache.read_text().splitlines():
            if line.startswith("CMAKE_INSTALL_PREFIX:PATH="):
                prefix = Path(line.partition("=")[2])
                if prefix.is_absolute() and prefix != build and build in prefix.parents:
                    excluded.add(prefix)
    return excluded


def _binary(path):
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
            return False
        with path.open("rb") as source:
            return source.read(4) == b"\x7fELF"
    except FileNotFoundError:
        return False


def _candidates(build, outputs, kept):
    excluded = _install_paths(build)
    binaries = []
    for directory, subdirs, files in os.walk(build, followlinks=False):
        parent = Path(directory)
        subdirs[:] = [
            name for name in subdirs
            if name != "CMakeFiles" and parent / name not in excluded
            and not (parent / name).is_symlink()
        ]
        for name in files:
            path = parent / name
            relative = path.relative_to(build).as_posix()
            # A graph-unowned file may be a hand-installed tool, not a build output.
            if relative in outputs and relative not in kept and _binary(path):
                binaries.append(File(path, _identity(path)))
    return tuple(sorted(binaries, key=lambda item: (-item.identity[2], str(item.path))))


def _aliases(build, binaries):
    """Identify only aliases of removed files, never unrelated broken links."""
    doomed = {item.path for item in binaries}
    aliases = []
    for directory in (build / "bin", build / "lib"):
        if not directory.is_dir() or directory.is_symlink():
            continue
        for link in directory.iterdir():
            if link.is_symlink() and link.resolve() in doomed:
                aliases.append(File(link, _identity(link)))
    return tuple(sorted(aliases, key=lambda item: str(item.path)))


def _plan(request):
    root = request.root.resolve()
    platform = ws.select_platform(request.platform)
    if platform.startswith("windows"):
        raise BuildError(f"{platform}: trim supports ELF build trees only")
    spec = request.worktree or os.getenv("HLSL_WT")
    tree = ws.resolve(root, spec) if spec else ws.enclosing_worktree(
        request.cwd or Path.cwd()
    )
    if tree is None or tree.kind not in ws.TARGET_KINDS:
        raise BuildError("select a buildable worktree with --in (see 'hlsl list')")
    build = ws.build_directory(tree, platform, target=True)
    if build.resolve() != build or build.is_symlink():
        raise BuildError(f"{build}: build directory must not contain symlinks")
    if not (build / "build.ninja").is_file() or (build / "build.ninja").is_symlink():
        raise BuildError(f"{build} is not a configured Ninja build directory")
    targets = request.targets or _DEFAULTS.get(tree.kind)
    if not targets:
        raise BuildError(f"{tree.kind} has no default keep targets; name targets")
    if any(not _TARGET.fullmatch(target) or target.startswith("-")
           or target.startswith("/") or ".." in Path(target).parts
           for target in targets):
        raise BuildError("invalid keep target; use Ninja target names without spaces")
    outputs = _outputs(build)
    for target in targets:
        if target not in outputs:
            raise BuildError(f"{build}: unknown keep target {target!r}; nothing removed")
    kept = set()
    for target in targets:
        kept.update(_graph_nodes(build, target))
    binaries = _candidates(build, outputs, kept)
    aliases = _aliases(build, binaries)
    return root, tree, build, platform, targets, binaries, aliases, _identity(build / "build.ninja")


def _report(plan, dry_run):
    root, tree, build, platform, targets, binaries, aliases, _graph_identity = plan
    total = sum(item.identity[2] for item in binaries)
    lines = [f"worktree {tree.path} ({tree.kind})", f"platform {platform}",
             f"build dir {build}", f"keep targets {' '.join(targets)}",
             "cost: Ninja graph inspection; no rebuild"]
    prerequisite = migration_prerequisite(root)
    if prerequisite:
        lines.append(f"blocked: {prerequisite} needs explicit workspace migration")
    lines.extend(f"  {item.identity[2]} bytes  {item.path.relative_to(build)}"
                 for item in binaries)
    lines.extend(f"  alias  {item.path.relative_to(build)}" for item in aliases)
    if not binaries:
        lines.append(f"nothing to trim in {build}")
    else:
        verb = "would remove" if dry_run else "removed"
        lines.append(f"{verb} {len(binaries)} binaries from {build} ({total} bytes)"
                 f" and {len(aliases)} aliases")
    return Plan("\n".join(lines) + "\n")


def preview(request):
    """Inspect the actual Ninja graph without creating state or build files."""
    return _report(_plan(request), True)


def execute(request):
    """Recheck graph and file identities under the build lock before unlinking."""
    initial = _plan(request)
    _check_migration(initial[0])
    with build_lock(initial[0], initial[2]):
        current = _plan(request)
        _check_migration(current[0])
        if current != initial:
            raise BuildError("Ninja graph or binaries changed while waiting; retry trim")
        for item in (*current[5], *current[6]):
            if _identity(item.path) != item.identity or (
                not item.path.is_symlink() and not _binary(item.path)
            ):
                raise BuildError(f"{item.path}: changed during trim; nothing removed")
        for item in (*current[5], *current[6]):
            item.path.unlink()
    return _report(initial, False)
