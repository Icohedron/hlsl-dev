"""Plan standalone offload builds and the LLVM distribution they consume."""

from dataclasses import dataclass
import os
from pathlib import Path

from . import cross
from . import native
from . import workspace as ws
from .build_support import (
    BuildError,
    _check_migration,
    _configured,
    _flag_hash,
    _link_database,
    _load,
    _run_command,
    _save,
    _selection_file,
    build_lock,
    expand_flags,
    migration_prerequisite,
    saved_selections,
)


@dataclass(frozen=True)
class DistributionPlan:
    """An LLVM install, including the exact source of its CMake cache."""

    root: Path
    llvm: ws.Worktree
    offload: ws.Worktree
    build: Path
    prefix: Path
    platform: str
    host_tools: cross.HostToolsPlan | None
    fingerprint: dict
    build_type: str
    configure_command: tuple[str, ...]
    build_command: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class OffloadPlan:
    """A standalone build and the prerequisites it needs before configuration."""

    root: Path
    tree: ws.Worktree
    build: Path
    platform: str
    selections: dict
    configure_command: tuple[str, ...] | None
    build_command: tuple[str, ...] | None
    distribution: DistributionPlan | None
    prerequisite: native.DxcPrerequisite | None
    migration: Path | None
    text: str


def _source(request, kind):
    root = request.root.resolve()
    spec = request.worktree or os.getenv("HLSL_WT")
    tree = ws.resolve(root, spec) if spec else ws.enclosing_worktree(
        request.cwd or Path.cwd()
    )
    if tree is None or tree.kind != kind:
        raise BuildError(f"select a {kind} worktree with --in")
    return root, tree


def _prefix(root, llvm, request, saved, platform):
    spec = (request.dist_prefix or os.getenv("HLSL_DIST_PREFIX")
            or saved.get("dist_prefix"))
    if spec:
        path = Path(spec)
        return (path if path.is_absolute() else root / path).resolve(), True
    return ws.distribution_prefix(llvm, platform), False


def _installed(prefix):
    return (prefix / "lib/cmake/llvm/LLVMConfig.cmake").is_file()


def _distribution_selection(platform):
    return "distribution" if platform == "native" else f"distribution.{platform}"


def _distribution(root, llvm, offload, prefix, request):
    """Describe an explicit refresh or an install missing from LLVM's prefix."""
    platform = ws.select_platform(request.platform)
    build = llvm.path / ("build-dist" if platform == "native" else
                         f"build-dist.{platform}")
    host_tools = (cross.host_tools_plan(root, llvm, native._jobs(request))
                  if platform != "native" else None)
    cache = build / "CMakeCache.txt"
    build_type = request.build_type
    if not build_type and cache.is_file():
        build_type = next((line.partition("=")[2] for line in cache.read_text().splitlines()
                           if line.startswith("CMAKE_BUILD_TYPE:")), None)
    build_type = build_type or "Release"
    flags = expand_flags(
        os.getenv("HLSL_CMAKE_FLAGS_LLVM_DIST"),
        {
            "HD_BUILD_TYPE": build_type,
            "HD_INSTALL_PREFIX": prefix,
            "HD_LLVM_SRC": llvm.path,
            "HD_OFFLOAD_SRC": offload.path,
            "HD_SEMI": ";",
        },
        "HLSL_CMAKE_FLAGS_LLVM_DIST",
    )
    if platform != "native":
        flags.extend(cross.flags(root, platform, "llvm", llvm=llvm))
    fingerprint = {
        "build_dir": str(build), "prefix": str(prefix),
        "offload": str(offload.path), "flags_hash": _flag_hash(flags),
    }
    record = _load(root, llvm, _distribution_selection(platform))
    if (_configured(build) or cache.is_file()) and not (
        record.get("configured") == fingerprint or native._retry_marker(record, build)
    ):
        raise BuildError(
            f"{build}: existing distribution build needs explicit revalidation; "
            "do not overwrite it with different LLVM/offload selections"
        )
    configure = ("cmake", "-S", str(llvm.path / "llvm"), "-B", str(build), *flags)
    jobs = native._jobs(request)
    command = (
        "cmake", "--build", str(build),
        *(("--parallel", str(jobs)) if jobs else ()),
        "--target", "install-distribution",
    )
    text = (
        f"distribution: install LLVM for {llvm.path} using {offload.path}\n"
        f"  build dir {build}\n  prefix {prefix}\n"
        + (host_tools.text if host_tools else "")
        + f"  cost: LLVM distribution build (large)\n"
        f"  configure: {' '.join(configure)}\n"
        f"  build: {' '.join(command)}\n"
    )
    return DistributionPlan(
        root, llvm, offload, build, prefix, platform, host_tools,
        fingerprint, build_type, configure, command, text
    )


def _distribution_plan(request):
    """Explicit refresh never silently switches to or overwrites another prefix."""
    root, llvm = _source(request, "llvm")
    platform = ws.select_platform(request.platform)
    toolchain = (cross.prerequisite(root, platform) if platform != "native"
                 else None)
    if request.reset or request.dxc or request.llvm:
        raise BuildError("distribution refresh only accepts LLVM/offload selections")
    saved = saved_selections(root, llvm, platform)
    offload = native._dependency(root, llvm, "offload", request.offload, saved)
    prefix, external = _prefix(root, llvm, request, saved, platform)
    if external:
        raise BuildError(
            f"{prefix}: external distribution is read-only; omit --dist-prefix "
            "and HLSL_DIST_PREFIX to refresh this LLVM worktree's own install"
        )
    distribution = _distribution(root, llvm, offload, prefix, request)
    migration = migration_prerequisite(root)
    text = (
        f"worktree {llvm.path} (llvm)\nplatform {platform}\n"
        + (f"blocked: {migration} needs explicit workspace migration\n"
           if migration else "")
        + (toolchain.text if toolchain else "") + distribution.text
    )
    return distribution, text


def plan_distribution(request):
    """Return a read-only preview of an explicit distribution refresh."""
    from .command import Plan

    _, text = _distribution_plan(request)
    return Plan(text)


def _install(distribution):
    """Serialize the prefix with readers and record only a successful install."""
    if distribution.host_tools:
        cross.refresh_host_tools(distribution.host_tools)
    env = (cross.cross_environment() if distribution.platform != "native" else None)
    with build_lock(distribution.root, distribution.prefix):
        with build_lock(distribution.root, distribution.build):
            current = _distribution(
                distribution.root, distribution.llvm, distribution.offload,
                distribution.prefix, _distribution_request(distribution)
            )
            if current.fingerprint != distribution.fingerprint:
                raise BuildError("distribution selection changed while waiting for lock")
            selection = _distribution_selection(distribution.platform)
            path = _selection_file(distribution.root, distribution.llvm, selection)
            native._begin_configure(
                distribution.root, distribution.llvm, distribution.build, selection
            )
            _run_command(distribution.configure_command, distribution.build, env=env)
            if not _configured(distribution.build):
                raise BuildError(f"cmake did not configure {distribution.build}")
            _save(path, {"configured": distribution.fingerprint, "choices": {}})
            _run_command(distribution.build_command, distribution.build, env=env)
            if not _installed(distribution.prefix):
                raise BuildError(
                    f"{distribution.prefix}: install-distribution did not produce "
                    "LLVMConfig.cmake"
                )


def _distribution_request(distribution):
    """Recheck flags without changing the selected prefix or source worktree."""
    from .command import Request

    return Request(
        "distribution refresh", distribution.root, platform=distribution.platform,
        build_type=distribution.build_type,
        jobs=(distribution.build_command[
            distribution.build_command.index("--parallel") + 1
        ] if "--parallel" in distribution.build_command else None),
    )


def refresh(request):
    """Refresh the selected LLVM install, even when it already exists."""
    distribution, text = _distribution_plan(request)
    _check_migration(distribution.root)
    if distribution.platform != "native":
        cross.fetch(distribution.root, distribution.platform)
    _install(distribution)
    from .command import Plan

    return Plan(text)


def plan(request):
    """Resolve one standalone worktree without changing build or selection state."""
    root, tree = _source(request, "offload")
    platform = ws.select_platform(request.platform)
    native._reject_cross_test_targets(request, platform)
    toolchain = (cross.prerequisite(root, platform, request.no_auto or
                                    os.getenv("HLSL_AUTO") == "0")
                 if platform != "native" else None)
    if request.offload:
        raise BuildError("--offload selects sources for LLVM; use --in for standalone")
    if request.reset and any((request.llvm, request.dist_prefix, request.dxc,
                              request.build_type)):
        raise BuildError("--reset cannot be combined with dependency/build selections")
    build = ws.build_directory(tree, platform, target=True, d3d12=request.d3d12,
                               root=root)
    record = _load(root, tree, platform, request.d3d12)
    saved = ({} if request.reset else
             saved_selections(root, tree, platform, request.d3d12))
    llvm = native._dependency(root, tree, "llvm", request.llvm, saved)
    golden = native._dependency(root, tree, "golden", None, saved)
    prefix, external = _prefix(root, llvm, request, saved, platform)
    installed = _installed(prefix)
    no_auto = request.no_auto or os.getenv("HLSL_AUTO") == "0"
    if not installed and external:
        raise BuildError(
            f"no LLVM distribution at {prefix} (--dist-prefix / HLSL_DIST_PREFIX); "
            "install it first or omit the external prefix"
        )
    if not installed and no_auto:
        raise BuildError(
            f"no LLVM distribution at {prefix} (--no-auto); "
            f"run 'hlsl distribution refresh --in {llvm.path}'"
        )
    distribution = (
        _distribution(root, llvm, tree, prefix, request) if not installed else None
    )
    dxc_bin, dxc_choice, missing_dxc = native._dxc_bin(
        root, tree, request.dxc, saved, no_auto, platform
    )
    dxc_plan = (native._plan_dxc(root, missing_dxc, native._jobs(request), platform)
                if missing_dxc else None)
    build_type = native._build_type(request, saved, build)
    flags = expand_flags(
        os.getenv("HLSL_CMAKE_FLAGS_OFFLOAD"),
        {
            "HD_BUILD_TYPE": build_type,
            "HD_OFFLOAD_SRC": tree.path,
            "HD_LLVM_SRC": llvm.path,
            "HD_LLVM_CMAKE_DIR": prefix / "lib/cmake/llvm",
            "HD_GOLDEN_DIR": golden.path,
            "HD_DXC_BIN_DIR": dxc_bin,
            "HD_INSTALL_PREFIX": build / "install",
            "HD_SEMI": ";",
        },
        "HLSL_CMAKE_FLAGS_OFFLOAD",
    )
    if toolchain:
        flags.extend(cross.flags(root, platform, "offload"))
        if cross.is_windows(platform):
            from .gpu import d3d12_flags

            flags.extend(d3d12_flags(root, request.d3d12))
    else:
        from .gpu import d3d12_flags

        flags.extend(d3d12_flags(root, request.d3d12))
    selections = {
        "build_dir": str(build), "llvm": str(llvm.path),
        "dist_prefix": str(prefix), "offload": str(tree.path),
        "golden": str(golden.path), "dxc": str(dxc_choice),
        "build_type": build_type, "flags_hash": _flag_hash(flags),
    }
    choices = {} if request.reset else dict(saved)
    if not request.reset:
        for key, value in (
            ("llvm", request.llvm or os.getenv("HLSL_LLVM")),
            ("dist_prefix", request.dist_prefix or os.getenv("HLSL_DIST_PREFIX")),
            ("golden", os.getenv("HLSL_GOLDEN")),
            ("dxc", request.dxc or os.getenv("HLSL_DXC")),
            ("build_type", request.build_type or os.getenv("HLSL_BUILD_TYPE")),
        ):
            if value:
                choices[key] = value
    needs_configure = (
        request.action == "configure" or not _configured(build)
        or (bool(record) and record.get("configured") != selections)
    )
    native._validate_build(
        request, tree, build, record, _configured(build), needs_configure,
        bool(request.llvm and request.dxc and request.platform),
    )
    configure = (
        ("cmake", "-S", str(tree.path), "-B", str(build), *flags)
        if needs_configure else None
    )
    command = native._build_command(build, request, native._jobs(request), platform)
    migration = migration_prerequisite(root)
    lines = [
        f"worktree {tree.path} (offload standalone)", f"platform {platform}",
        f"build dir {build}", f"build type {build_type}",
        f"llvm {llvm.path}", f"llvm dist {prefix} "
        f"({'external' if external else 'installed' if installed else 'missing'})",
        f"golden {golden.path}", f"dxc {dxc_bin}",
        f"cost: {'LLVM distribution (large) + ' if distribution else ''}"
        f"{'DXC prerequisite (large) + ' if dxc_plan else ''}"
        f"{'offload build' if command else 'configure only'}",
    ]
    if migration:
        lines.append(f"blocked: {migration} needs explicit workspace migration")
    if toolchain:
        lines.append(toolchain.text.rstrip())
    if distribution:
        lines.append("prerequisite: automatically provision missing LLVM distribution")
        lines.append(distribution.text.rstrip())
    if dxc_plan:
        lines.append(f"prerequisite: build dxc/dxv in {dxc_plan.build}")
        if dxc_plan.configure_command:
            lines.append(f"  configure: {' '.join(dxc_plan.configure_command)}")
        lines.append(f"  build: {' '.join(dxc_plan.build_command)}")
    if configure:
        lines.append(f"configure: {' '.join(configure)}")
        selection_path = _selection_file(root, tree, platform, request.d3d12)
        lines.append(f"write selections: {selection_path}")
        if platform == "native":
            lines.append(f"clangd link: {tree.path / 'compile_commands.json'} (if generated)")
    if command:
        lines.append(f"build: {' '.join(command)}")
    return OffloadPlan(
        root, tree, build, platform,
        {"configured": selections, "choices": choices},
        configure, command, distribution, dxc_plan, migration,
        "\n".join(lines) + "\n",
    )


def execute(request):
    """Install prerequisites, then replan under the prefix and build locks."""
    initial = plan(request)
    _check_migration(initial.root)
    if initial.platform != "native":
        cross.fetch(initial.root, initial.platform)
    if initial.distribution:
        _install(initial.distribution)
    if initial.prerequisite:
        native._execute_dxc(initial.root, initial.prerequisite, native._jobs(request))
    # Readers and distribution writers must acquire the prefix before the
    # build tree; replan under both locks before using either selected input.
    prefix = Path(initial.selections["configured"]["dist_prefix"])
    with build_lock(initial.root, prefix):
        with build_lock(initial.root, initial.build):
            current = plan(request)
            if (current.tree.path != initial.tree.path or current.build != initial.build
                    or current.selections != initial.selections):
                raise BuildError("offload selection changed while waiting for lock")
            if current.distribution or current.prerequisite:
                raise BuildError("offload prerequisite changed while waiting for lock")
            if not _installed(prefix):
                raise BuildError(f"{prefix}: LLVM distribution disappeared")
            if current.configure_command:
                path = _selection_file(current.root, current.tree, current.platform,
                                       request.d3d12)
                native._begin_configure(
                    current.root, current.tree, current.build, current.platform,
                    request.d3d12
                )
                _run_command(current.configure_command, current.build,
                             env=(cross.cross_environment()
                                  if current.platform != "native" else None))
                if not _configured(current.build):
                    raise BuildError(f"cmake did not configure {current.build}")
                _save(path, current.selections)
            if current.platform == "native":
                _link_database(current.tree, current.build)
            if current.build_command:
                _run_command(current.build_command, current.build,
                             env=(cross.cross_environment()
                                  if current.platform != "native" else None))
    return initial
