"""Conservative cleanup of disposable CMake trees in a checkout workspace."""

from contextlib import ExitStack
from dataclasses import dataclass
import os
from pathlib import Path
import shutil

from . import workspace as ws
from .build_support import BuildError, _check_migration, _key, build_lock


_MARKERS = ("CMakeCache.txt", "build.ninja", "Makefile", "CMakeFiles")
_EXCLUDED = ("build-dist", ".git", ".jj", ".devenv", ".direnv",
             ".hlsl-dev", ".sccache", ".codegraph")


@dataclass(frozen=True)
class CleanPlan:
    root: Path
    trees: tuple[ws.Worktree, ...]
    directories: tuple[Path, ...]
    owners: tuple[ws.Worktree, ...]
    probes: tuple[Path, ...]
    text: str


def _marker(directory):
    return any((directory / name).exists() for name in _MARKERS)


def _tracked(tree, directory):
    if not (tree.path / ".git").exists():
        return False
    relative = directory.relative_to(tree.path)
    return bool(ws.git_output(tree.path, "ls-files", "--", f"{relative}/"))


def _safe(directory, tree, *, distribution=False):
    """Reject source, symlinks and paths whose identity is not a build tree."""
    if directory.is_symlink() or not directory.is_dir():
        raise BuildError(f"refusing to clean non-directory or symlink {directory}")
    path = directory.resolve()
    if distribution:
        relative = path.relative_to(tree.path) if tree.path in path.parents else None
        parts = relative.parts if relative else ()
        if not (parts and parts[0] in (
            "build-dist", *(f"build-dist.{platform}" for platform in ws.PLATFORMS)
        ) and (len(parts) == 1 or parts[1:] == ("install",))):
            raise BuildError(f"refusing to clean external distribution {directory}")
    if path == tree.path or path in tree.path.parents:
        raise BuildError(f"refusing to clean source/workspace directory {directory}")
    if tree.path in path.parents and _tracked(tree, path):
        raise BuildError(f"refusing to clean Git-tracked source directory {directory}")
    # An external target must have been selected explicitly; caller enforces
    # that. Reject trees containing a Git checkout even without tracked files.
    if (directory / ".git").exists():
        raise BuildError(f"refusing to clean Git checkout {directory}")


def _link_database(tree):
    link = tree.path / "compile_commands.json"
    if link.exists() and not link.is_symlink():
        return
    candidates = []
    for directory in tree.path.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        if directory.name.startswith("build-dist") or directory.name.startswith("build."):
            continue
        database = directory / "compile_commands.json"
        if database.is_file() and _marker(directory) and not _tracked(tree, directory):
            candidates.append(database)
    if candidates:
        newest = max(candidates, key=lambda file: file.stat().st_mtime_ns)
        if link.is_symlink():
            link.unlink()
        link.symlink_to(os.path.relpath(newest, tree.path))
    elif link.is_symlink() and not link.exists():
        link.unlink()


def plan(request):
    root = request.root.resolve()
    if request.all and request.worktree:
        raise BuildError("--in cannot be combined with --all")
    if request.repository and not request.all:
        raise BuildError("repository selection requires --all")
    if request.all_build_dirs and request.platform not in (None, "all"):
        raise BuildError("--all-build-dirs covers every platform; drop --platform")
    if request.platform == "all":
        platforms = ("native", *ws.PLATFORMS)
    elif request.all_build_dirs:
        platforms = ("native", *ws.PLATFORMS)
    else:
        platforms = (ws.select_platform(request.platform),)
    if request.all:
        kind = request.repository
        if kind:
            kind = next((k for k in ws.TARGET_KINDS if kind in (k, ws.REPOSITORIES[k])), None)
            if kind is None:
                raise BuildError("unknown repository; expected llvm-project, "
                                 "DirectXShaderCompiler or offload-test-suite")
        trees = tuple(tree for name in ((kind,) if kind else ws.TARGET_KINDS)
                      for tree in ws.worktrees(root, name))
        if not trees:
            raise BuildError("no checkouts to clean; run 'hlsl setup'")
    else:
        spec = request.worktree or os.getenv("HLSL_WT")
        tree = ws.resolve(root, spec) if spec else ws.enclosing_worktree(request.cwd or Path.cwd())
        if tree is None or tree.kind not in ws.TARGET_KINDS:
            raise BuildError("select a buildable worktree with --in (see 'hlsl list')")
        trees = (tree,)

    dirs = {}
    probes = {}

    def collect(directory, tree, *, dist=False, explicit=False):
        if directory.is_symlink():
            raise BuildError(f"refusing to clean symlink {directory}")
        if not directory.is_dir():
            return
        if not dist:
            if directory.name.startswith("build-dist"):
                raise BuildError(f"refusing distribution tree without --dist: {directory}")
            resolved = directory.resolve()
            if resolved == tree.path or resolved in tree.path.parents:
                _safe(directory, tree)
            # Only an absolute, explicitly selected build directory may live
            # outside this checkout. A relative override cannot escape it.
            external = tree.path not in resolved.parents
            override = os.getenv("HLSL_BUILD_DIR")
            if external and (not explicit or not override or not Path(override).is_absolute()):
                raise BuildError(f"refusing external build directory {directory}")
        if _marker(directory):
            _safe(directory, tree, distribution=dist)
            dirs[directory] = tree
        else:
            lock = Path(os.getenv("HLSL_DEV_STATE") or root / ".hlsl-dev") / "locks" / f"{_key(root, directory)}.lock"
            if lock.exists():
                _safe(directory, tree, distribution=dist)
                probes[directory] = tree

    for tree in trees:
        if request.all_build_dirs:
            for directory in sorted(tree.path.iterdir()):
                if directory.is_symlink():
                    # Never traverse an alias to an arbitrary source directory.
                    continue
                if not directory.is_dir() or directory.name.startswith(_EXCLUDED):
                    continue
                if _tracked(tree, directory):
                    continue
                collect(directory, tree)
            if not request.all:
                collect(ws.build_directory(tree, target=True), tree, explicit=True)
        else:
            for platform in platforms:
                directory = ws.build_directory(tree, platform, target=not request.all)
                collect(directory, tree, explicit=not request.all)
        if request.dist and tree.kind == "llvm":
            for platform in platforms:
                build = tree.path / ("build-dist" + (f".{platform}" if platform != "native" else ""))
                prefix = build / "install"
                installed = (prefix / "lib/cmake/llvm/LLVMConfig.cmake").is_file()
                if build.is_dir() and (_marker(build) or installed):
                    _safe(build, tree, distribution=True)
                    dirs[build] = tree
                    if prefix.is_dir():
                        _safe(prefix, tree, distribution=True)
                        probes[prefix] = tree  # Offload readers lock the prefix.
                else:
                    collect(build, tree, dist=True)
                    collect(prefix, tree, dist=True)
    lines = [
        f"Would check build lock on {path} "
        + ("(distribution prefix; removed with its build tree)"
           if path.parent in dirs else "(no CMake marker; not removed)")
        for path in probes if path not in dirs
    ]
    lines.extend(f"Would remove {p}" for p in dirs)
    if not dirs and not probes:
        lines.append("Nothing to remove: no selected build trees")
    elif len(trees) > 1:
        lines.append(f"Would remove {len(dirs)} directories in {len(trees)} checkouts")
    return CleanPlan(root, trees, tuple(dirs), tuple(dirs.values()),
                     tuple(p for p in probes if p not in dirs), "\n".join(lines) + "\n")


def execute(request):
    """Lock every candidate before re-inspection; never start a partial sweep."""
    if request.all and not request.yes:
        raise BuildError(
            "clean --all requires --yes to remove build trees; use --dry-run to preview"
        )
    _check_migration(request.root.resolve())
    first = plan(request)
    locks = sorted(set((*first.directories, *first.probes)),
                   key=lambda path: str(Path(os.getenv("HLSL_DEV_STATE") or first.root / ".hlsl-dev") / "locks" / f"{_key(first.root, path)}.lock"))
    with ExitStack() as stack:
        for directory in locks:
            stack.enter_context(build_lock(first.root, directory, timeout=0))
        current = plan(request)
        if ((current.trees, current.directories, current.owners, current.probes) !=
                (first.trees, first.directories, first.owners, first.probes)):
            raise BuildError("cleanup selection changed while acquiring locks; no directories removed")
        _check_migration(current.root)
        for directory, tree in zip(current.directories, current.owners):
            # Check again after locking before any deletion.
            _safe(directory, tree, distribution=directory.name.startswith("build-dist"))
        for directory in current.directories:
            try:
                shutil.rmtree(directory)
            except OSError as error:
                raise BuildError(f"cannot remove {directory}: {error}") from error
        for tree in current.trees:
            try:
                _link_database(tree)
            except OSError as error:
                raise BuildError(f"cannot refresh clangd link in {tree.path}: {error}") from error
    if not current.directories:
        return ws_plan("Nothing to remove: no selected build trees\n")
    lines = [f"Removing {directory}" for directory in current.directories]
    if len(current.trees) > 1:
        lines.append(f"Removed {len(current.directories)} directories in {len(current.trees)} checkouts")
    return ws_plan("\n".join(lines) + "\n")


def ws_plan(text):
    from .command import Plan
    return Plan(text)
