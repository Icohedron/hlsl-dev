"""Command requests, read-only inspection, and native build dispatch."""

from dataclasses import dataclass
import os
from pathlib import Path

from . import workspace as ws


@dataclass(frozen=True)
class Request:
    action: str
    root: Path
    worktree: str | None = None
    platform: str | None = None
    cwd: Path | None = None
    offload: str | None = None
    llvm: str | None = None
    dist_prefix: str | None = None
    dxc: str | None = None
    build_type: str | None = None
    targets: tuple[str, ...] = ()
    suites: tuple[str, ...] = ()
    suite: str | None = None
    filter: str | None = None
    test_path: str | None = None
    paths: tuple[str, ...] = ()
    lit_args: str | None = None
    lit_flags: tuple[str, ...] = ()
    jobs: str | None = None
    no_auto: bool = False
    reset: bool = False
    all: bool = False
    all_build_dirs: bool = False
    yes: bool = False
    dist: bool = False
    repository: str | None = None
    since: str | None = None
    diff: bool = False
    fix: bool = False
    hook_action: str | None = None
    quiet: bool = False
    out: str | None = None
    vk: str | None = None
    d3d12: str | None = None
    gpu_choice: str | None = None
    gpu_list: bool = False
    gpu_export: bool = False


@dataclass(frozen=True)
class Plan:
    text: str
    status: int = 0


def _label(name, value):
    return f"{name} {value}"


def _display(root, tree):
    if tree is None:
        return "not checked out (run 'hlsl setup')"
    try:
        return str(tree.path.relative_to(root))
    except ValueError:
        return str(tree.path)


def _legacy_pin(root, tree, platform):
    key = str(tree.path).removeprefix(str(root) + "/").replace("/", "%")
    pins = root / ".hlsl-dev/pins"
    paths = [pins / f"{key}.env"]
    if platform != "native":
        paths.append(pins / f"{key}@{platform}.env")
    return [path for path in paths if path.is_file()]


def _saved(root, tree, platform):
    from .native import saved_selections

    return saved_selections(root, tree, platform)


def _get_dependency(root, tree, kind, platform="native"):
    selection = (
        os.getenv(f"HLSL_{kind.upper()}")
        or _saved(root, tree, platform).get(kind)
    )
    if selection:
        return ws.resolve(root, selection, (kind,))
    return ws.dependency(root, tree, kind)


def _absolute_dependency(root, tree, kind, platform="native"):
    dependency = _get_dependency(root, tree, kind, platform)
    return dependency.path if dependency else "not checked out (run 'hlsl setup')"


def _prebuilt_dxc():
    directory = os.getenv("HLSL_DXC_PREBUILT_DIR")
    if not directory or not any(
        (Path(directory) / name).is_file() for name in ("dxc", "dxc.exe")
    ):
        return f"prebuilt dxc missing at {directory or 'HLSL_DXC_PREBUILT_DIR'}"
    return directory


def _dxc_directory(root, tree, platform):
    selection = os.getenv("HLSL_DXC") or _saved(root, tree, platform).get("dxc")
    if selection in ("nix", "prebuilt", "system"):
        return _prebuilt_dxc()
    if selection:
        directory = Path(selection)
        if not directory.is_dir():
            directory = root / selection
        if (directory / "dxc").is_file() or (directory / "dxc.exe").is_file():
            return str(directory.resolve())
    source = _get_dependency(root, tree, "dxc")
    if source:
        path = ws.build_directory(source, platform) / "bin"
        if (path / "dxc").is_file():
            return str(path)
        if selection:
            return f"{path} (not built)"
    if os.getenv("HLSL_DXC_PREBUILT_DIR"):
        return _prebuilt_dxc()
    if source:
        return f"{path} (not built)"
    return "not checked out"


def _inventory(root, cwd, platform):
    current = ws.enclosing_worktree(cwd)
    lines = ["worktree  branch  build  legacy pins"]
    missing = False
    for kind, label in ws.REPOSITORIES.items():
        lines.extend(("", label))
        trees = ws.worktrees(root, kind)
        if not trees:
            lines.append("  (not checked out)")
            missing = True
        for tree in trees:
            marker = "*" if current and tree.path == current.path else " "
            build = ""
            if kind != "golden":
                directory = ws.build_directory(tree, platform)
                built = (directory / "build.ninja").is_file() or (
                    directory / "Makefile"
                ).is_file()
                build = "built" if built else "not built"
                if kind == "llvm":
                    prefix = (
                        ws.distribution_prefix(tree, platform, root=root)
                        / "lib/cmake/llvm/LLVMConfig.cmake"
                    )
                    if prefix.is_file():
                        build += " + dist"
            pins = (
                "legacy pin (not imported)"
                if _legacy_pin(root, tree, platform)
                else ""
            )
            lines.append(
                f"{marker} {_display(root, tree)}  "
                f"{tree.branch or '(no branch)'}  {build}  {pins}".rstrip()
            )
    if current:
        lines.extend(("", "* current checkout; 'hlsl info' inspects it"))
    if missing:
        lines.extend(("", "Missing repositories: run 'hlsl setup' to clone them"))
    return Plan("\n".join(lines) + "\n")


def _info(root, request, platform):
    spec = request.worktree or os.getenv("HLSL_WT")
    if spec in (ws.REPOSITORIES[kind] for kind in ws.TARGET_KINDS):
        candidate = root / spec
        if not ws.kind_of(candidate):
            return Plan(
                f"workspace {root}\nworktree {candidate}\n"
                "status not checked out (run 'hlsl setup')\n"
            )
    if spec:
        tree = ws.resolve(root, spec)
    else:
        tree = ws.enclosing_worktree(request.cwd or Path.cwd())
        if not tree or tree.kind not in ws.TARGET_KINDS:
            raise ws.SelectionError(
                "not inside a buildable worktree; pass --in <worktree> "
                "(see 'hlsl list')"
            )
    directory = ws.build_directory(tree, platform, target=True)
    prefix = ws.distribution_prefix(tree, platform, root=root)
    lines = [
        _label("workspace", root),
        _label("worktree", f"{tree.path} ({ws.REPOSITORIES[tree.kind]})"),
        _label("branch", tree.branch or "(no branch)"),
        _label("platform", platform),
        _label("build dir", directory),
    ]
    cache = directory / "CMakeCache.txt"
    build_type = os.getenv("HLSL_BUILD_TYPE")
    if not build_type and cache.is_file():
        build_type = next(
            (
                line.partition("=")[2]
                for line in cache.read_text().splitlines()
                if line.startswith("CMAKE_BUILD_TYPE:")
            ),
            None,
        )
    lines.append(_label("build type", build_type or "RelWithDebInfo"))
    if tree.kind == "llvm":
        lines += [
            _label("dist prefix", os.getenv("HLSL_DIST_PREFIX") or prefix),
            _label("offload", _absolute_dependency(root, tree, "offload", platform)),
            _label("golden", _absolute_dependency(root, tree, "golden", platform)),
            _label("dxc", _dxc_directory(root, tree, platform)),
        ]
    elif tree.kind == "offload":
        llvm = _get_dependency(root, tree, "llvm")
        lines.append(
            _label("llvm", llvm.path if llvm else "not checked out (run 'hlsl setup')")
        )
        if llvm:
            llvm_dist = os.getenv("HLSL_DIST_PREFIX") or ws.distribution_prefix(
                llvm, platform, root=root
            )
            lines.append(_label("llvm dist", llvm_dist))
        lines += [
            _label("golden", _absolute_dependency(root, tree, "golden")),
            _label("dxc", _dxc_directory(root, tree, platform)),
        ]
    cdb = tree.path / "compile_commands.json"
    if cdb.is_symlink():
        lines.append(_label("clangd db", f"{cdb} -> {os.readlink(cdb)}"))
    elif cdb.is_file():
        lines.append(_label("clangd db", f"{cdb} (regular file; left alone)"))
    else:
        lines.append(_label("clangd db", "none yet (written by configure)"))
    pins = _legacy_pin(root, tree, platform)
    if pins:
        lines.append(
            _label("legacy pins", f"{', '.join(map(str, pins))} (not imported)")
        )
    return Plan("\n".join(map(str, lines)) + "\n")


def preview(request):
    """Inspect selection and build state without writing or importing old pins."""
    root = request.root.resolve()
    if request.action in ("setup", "workspace update"):
        from . import submodules

        return Plan(submodules.plan(root, remote=request.action == "workspace update"))
    if request.action == "format":
        from . import formatting

        return formatting.execute(request, preview=True)
    if request.action == "builds stop":
        from . import stop_builds

        return stop_builds.plan(root)
    if request.action == "workspace migrate":
        from .migration import preview as migration_preview

        return migration_preview(root)
    if request.action.startswith("gpu "):
        from . import gpu

        return gpu.shell_exports(request) if request.gpu_export else gpu.plan(request)
    if request.action == "tools explorer":
        from . import explorer

        return explorer.plan(request)
    if request.action.startswith("cross "):
        from . import cross

        if request.action == "cross list":
            return Plan(cross.inventory(root))
        return Plan(cross.toolchain_plan(root, request.platform,
                                        refresh=request.action == "cross refresh").text)
    platform = ws.select_platform(request.platform)
    if request.action == "list":
        return _inventory(root, request.cwd or Path.cwd(), platform)
    if request.action == "info":
        return _info(root, request, platform)
    if request.action in ("configure", "build"):
        from . import native

        return native.plan(request)
    if request.action in ("test", "lit"):
        from . import testing

        return testing.plan(request)
    if request.action == "distribution install":
        from . import offload

        return offload.plan_distribution(request)
    if request.action == "clean":
        from . import clean

        return clean.plan(request)
    if request.action == "trim":
        from . import trim

        return trim.preview(request)
    if request.action == "package repro":
        from . import repro

        return repro.preview(request)
    if request.action in ("package full", "package dxc", "package compiler",
                          "package precompiled"):
        from . import package

        return package.preview(request)
    raise ws.SelectionError(f"unknown private command '{request.action}'")


def run(request):
    """Use the same planner for inspection and execution, replanning under lock."""
    if request.action == "builds stop":
        from . import stop_builds

        return stop_builds.stop(request.root)
    if request.action.startswith("gpu "):
        from . import gpu

        if request.gpu_export:
            if request.action != "gpu vulkan":
                raise ws.SelectionError("--export is only available for gpu vulkan")
            return gpu.shell_exports(request)
        return gpu.execute(request)
    if request.action == "tools explorer":
        from . import explorer

        return explorer.execute(request)
    if request.action.startswith("cross ") and request.action != "cross list":
        from . import cross

        result = cross.fetch(request.root.resolve(), request.platform,
                             refresh=request.action == "cross refresh")
        return Plan(result.text)
    if request.action in ("configure", "build"):
        from . import native
        from .gpu import loader_scope
        from contextlib import nullcontext

        runs_tests = (request.action == "build"
                      and ws.select_platform(request.platform) == "native"
                      and any(target.startswith("check-hlsl")
                              for target in request.targets))
        with loader_scope(request.root, request.vk) if runs_tests else nullcontext():
            return native.execute(request)
    if request.action in ("test", "lit"):
        from . import testing

        return testing.execute(request)
    if request.action == "distribution install":
        from . import offload

        return offload.install_distribution(request)
    if request.action == "clean":
        from . import clean

        return clean.execute(request)
    if request.action == "trim":
        from . import trim

        return trim.execute(request)
    if request.action == "package repro":
        from . import repro

        return repro.execute(request)
    if request.action in ("package full", "package dxc", "package compiler",
                          "package precompiled"):
        from . import package

        return package.execute(request)
    if request.action == "workspace migrate":
        from . import migration

        return migration.execute(request.root)
    if request.action in ("setup", "workspace update"):
        from . import submodules

        return Plan(submodules.execute(
            request.root, remote=request.action == "workspace update"
        ))
    if request.action == "format":
        from . import formatting

        return formatting.execute(request)
    return preview(request)
