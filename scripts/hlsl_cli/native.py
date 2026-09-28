"""Plan and execute native LLVM/DXC configure/build in a selected worktree."""

from contextlib import nullcontext
from dataclasses import dataclass
import os
from pathlib import Path

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
    migration_prerequisite,
    expand_flags,
    saved_selections,
)


@dataclass(frozen=True)
class NativePlan:
    """Native commands resolved for a single worktree and build tree."""

    root: Path
    tree: ws.Worktree
    build: Path
    platform: str
    selections: dict
    configure_command: tuple[str, ...] | None
    build_command: tuple[str, ...] | None
    prerequisite: "DxcPrerequisite | None"
    host_tools: object | None
    migration: Path | None
    text: str


@dataclass(frozen=True)
class DxcPrerequisite:
    """DXC configure/build work that must precede LLVM configuration."""

    tree: ws.Worktree
    build: Path
    fingerprint: dict
    configure_command: tuple[str, ...] | None
    build_command: tuple[str, ...]
    platform: str = "native"


def _dependency(root, tree, kind, explicit, saved):
    selection = explicit or os.getenv(f"HLSL_{kind.upper()}") or saved.get(kind)
    if selection:
        return ws.resolve(root, selection, (kind,))
    dependency = ws.dependency(root, tree, kind)
    if dependency is None:
        raise BuildError(
            f"{tree.path}: {kind} is not checked out; run 'hlsl setup'"
        )
    return dependency


def _has_dxc(directory):
    return all(
        (directory / name).is_file() or (directory / f"{name}.exe").is_file()
        for name in ("dxc", "dxv")
    )


def _dxc_bin(root, tree, selection, saved, no_auto, platform="native"):
    """Return binary directory, saved choice and missing source (if any)."""
    if platform.startswith("windows-"):
        return _windows_dxc_bin(root, tree, selection, saved, no_auto, platform)
    spec = selection or os.getenv("HLSL_DXC") or saved.get("dxc")
    prebuilt = os.getenv("HLSL_DXC_PREBUILT_DIR")
    if spec in ("nix", "prebuilt", "system"):
        if prebuilt and _has_dxc(Path(prebuilt)):
            return Path(prebuilt).resolve(), spec, None
        raise BuildError("prebuilt dxc/dxv is missing; enter the devenv shell")
    if spec:
        directory = Path(spec)
        if not directory.is_dir():
            directory = root / spec
        if _has_dxc(directory):
            return directory.resolve(), spec, None
        if directory.is_dir() and any(
            (directory / name).is_file() for name in ("dxc", "dxv")
        ):
            raise BuildError(f"{directory}: both dxc and dxv are required")
        source = ws.resolve(root, spec, ("dxc",))
    else:
        source = ws.dependency(root, tree, "dxc")
    if source:
        directory = ws.build_directory(source) / "bin"
        if _has_dxc(directory):
            record = _load(root, source, "native")
            failed_here = _retry_marker(record, directory.parent)
            validated_here = record.get("configured", {}).get("build_dir") == str(
                directory.parent
            )
            if validated_here or failed_here:
                changed = _plan_dxc(root, source, None).configure_command
                if not changed:
                    return directory, str(source.path), None
                if no_auto:
                    raise BuildError(
                        f"{directory}: DXC configuration needs refresh "
                        "(--no-auto); reconfigure"
                    )
                return directory, str(source.path), source
            if spec:
                raise BuildError(
                    f"{directory}: preserved DXC build needs explicit "
                    "revalidation; use --dxc nix or a standalone DXC directory"
                )
    if not spec and prebuilt and _has_dxc(Path(prebuilt)):
        return Path(prebuilt).resolve(), "nix", None
    if source and _has_dxc(directory):
        raise BuildError(
            f"{directory}: preserved DXC build needs explicit revalidation; "
            "pass --dxc to a standalone directory or enter the devenv shell"
        )
    if not source:
        raise BuildError("no dxc/dxv available; enter the devenv shell or pass --dxc")
    if no_auto:
        raise BuildError(
            f"dxc/dxv is missing at {directory} (--no-auto); "
            "omit --no-auto to build it, or pass --dxc nix"
        )
    return directory, str(source.path), source


def _windows_dxc_bin(root, tree, selection, saved, no_auto, platform):
    """Windows targets must never link to a host dxc/dxv by accident."""
    spec = selection or os.getenv("HLSL_DXC") or saved.get("dxc")
    if spec in ("nix", "prebuilt", "system"):
        raise BuildError(f"{spec} DXC is for this host, not {platform}; "
                         "pass a Windows DXC directory or a DXC worktree")
    source = None
    if spec:
        directory = Path(spec)
        if not directory.is_dir():
            directory = root / spec
        if _has_windows_dxc(directory):
            return directory.resolve(), spec, None
        source = ws.resolve(root, spec, ("dxc",))
    else:
        source = ws.dependency(root, tree, "dxc")
    if source is None:
        raise BuildError(f"no {platform} dxc/dxv available; pass --dxc a "
                         "Windows DXC directory or check out DXC")
    directory = ws.build_directory(source, platform) / "bin"
    built = _has_windows_dxc(directory)
    dxc_plan = _plan_dxc(root, source, None, platform)
    if built and not dxc_plan.configure_command:
        return directory, str(source.path), None
    if no_auto:
        raise BuildError(f"{directory}: Windows DXC missing or stale (--no-auto); "
                         "build it first or pass --dxc a Windows DXC directory")
    # Check for DIA before proposing an implicit DXC build, never fetch it.
    from . import cross

    cross.flags(root, platform, "dxc")
    return directory, str(source.path), source


def _has_windows_dxc(directory):
    return all((directory / name).is_file() for name in
               ("dxc.exe", "dxv.exe", "dxcompiler.dll", "dxil.dll"))


def _dxc_flags(source, build_type):
    return expand_flags(
        os.getenv("HLSL_CMAKE_FLAGS_DXC"),
        {
            "HD_BUILD_TYPE": build_type,
            "HD_DXC_SRC": source.path,
            "HD_SEMI": ";",
        },
        "HLSL_CMAKE_FLAGS_DXC",
    )


def _build_type(request, saved, build):
    selected = request.build_type if request else None
    value = selected or os.getenv("HLSL_BUILD_TYPE") or saved.get("build_type")
    if not value and (build / "CMakeCache.txt").is_file():
        for line in (build / "CMakeCache.txt").read_text().splitlines():
            if line.startswith("CMAKE_BUILD_TYPE:"):
                value = line.partition("=")[2]
                break
    return value or "RelWithDebInfo"


def _jobs(request):
    jobs = request.jobs or os.getenv("HLSL_JOBS")
    if jobs and (not jobs.isdecimal() or int(jobs) < 1):
        raise BuildError("--jobs must be a positive integer")
    return jobs


def _reject_cross_test_targets(request, platform):
    if request.action != "build" or platform == "native":
        return
    if any(target.startswith("check-") or target in ("check", "test")
           for target in request.targets):
        target_host = "Windows" if platform.startswith("windows-") else "the target"
        raise BuildError(f"{platform} test targets cannot run on this host; "
                         f"build tools here and run tests on {target_host}")


def _build_command(build, request, jobs, platform="native"):
    if request.action != "build":
        return None
    _reject_cross_test_targets(request, platform)
    return (
        "cmake", "--build", str(build),
        *(("--parallel", str(jobs)) if jobs else ()),
        *(("--target", *request.targets) if request.targets else ()),
    )


def _validate_build(request, tree, build, record, configured, needs_configure,
                    explicit_revalidation):
    old_tree = (
        (configured or (build / "CMakeCache.txt").is_file())
        and not record.get("configured")
        and not _retry_marker(record, build)
    )
    if old_tree and request.action == "configure" and not explicit_revalidation:
        platform = request.platform or "native"
        if tree.kind == "llvm":
            requirement = f"--offload, --dxc and --platform {platform}"
        elif tree.kind == "offload":
            requirement = f"--llvm, --dxc and --platform {platform}"
        else:
            requirement = f"--platform {platform} and --build-type TYPE"
        raise BuildError(
            f"{build}: preserved build needs explicit {requirement} "
            "to revalidate it before use"
        )
    validated = record.get("configured", {}).get("build_dir") == str(build)
    if request.action == "build" and (
        old_tree or (configured and record.get("configured") and not validated)
    ):
        raise BuildError(
            f"{build}: existing build has no new selections; run 'hlsl configure "
            f"--in {tree.path}' explicitly before building"
        )
    if request.action == "build" and record.get(
        "reconfigure_failed", {}
    ).get("build_dir") == str(build):
        raise BuildError(
            f"{build}: previous configure failed; run 'hlsl configure "
            f"--in {tree.path}' before building"
        )
    if request.action == "build" and needs_configure and (
        request.no_auto or os.getenv("HLSL_AUTO") == "0"
    ):
        raise BuildError(
            f"{build}: configure required (--no-auto); "
            f"run 'hlsl configure --in {tree.path}'"
        )


def _plan_dxc(root, source, jobs, platform="native"):
    """Describe the missing DXC prerequisite without creating its build tree."""
    from . import cross

    build = ws.build_directory(source, platform)
    record = _load(root, source, platform)
    validated = record.get("configured", {})
    retriable = _retry_marker(record, build)
    if (
        _configured(build) or (build / "CMakeCache.txt").is_file()
    ) and ((not validated and not retriable) or (
        validated and validated.get("build_dir") != str(build)
    )):
        raise BuildError(
            f"{build}: existing DXC build needs explicit revalidation; "
            "use --dxc nix or revalidate the DXC checkout with "
            f"'hlsl configure --in <DXC worktree> --platform {platform} "
            "--build-type <TYPE>' before building it"
        )
    flags = _dxc_flags(source, _build_type(None, record.get("choices", {}), build))
    if platform != "native":
        flags.extend(cross.flags(root, platform, "dxc"))
    fingerprint = {"build_dir": str(build), "flags_hash": _flag_hash(flags)}
    configure = (
        ("cmake", "-S", str(source.path), "-B", str(build), *flags)
        if not _configured(build) or record.get("configured") != fingerprint
        else None
    )
    command = (
        "cmake", "--build", str(build),
        *(("--parallel", str(jobs)) if jobs else ()),
        "--target", "dxc", "dxv",
    )
    return DxcPrerequisite(source, build, fingerprint, configure, command, platform)


def _retry_marker(record, build):
    return any(
        record.get(key, {}).get("build_dir") == str(build)
        for key in ("attempted", "reconfigure_failed")
    )


def _plan_selected_dxc(request, root, tree, platform):
    from . import cross

    toolchain = (cross.prerequisite(root, platform, request.no_auto or
                                   os.getenv("HLSL_AUTO") == "0")
                 if platform != "native" else None)
    if request.offload or request.dxc:
        raise BuildError("DXC does not use --offload or --dxc; omit these selections")
    if request.reset and request.build_type:
        raise BuildError("--reset cannot be combined with --build-type")
    build = ws.build_directory(tree, platform, target=True)
    record = _load(root, tree, platform)
    saved = {} if request.reset else saved_selections(root, tree, platform)
    build_type = _build_type(request, saved, build)
    flags = _dxc_flags(tree, build_type)
    if toolchain:
        flags.extend(cross.flags(root, platform, "dxc"))
    selections = {
        "build_dir": str(build),
        "build_type": build_type,
        "flags_hash": _flag_hash(flags),
    }
    choices = {} if request.reset else dict(saved)
    if not request.reset and (request.build_type or os.getenv("HLSL_BUILD_TYPE")):
        choices["build_type"] = request.build_type or os.getenv("HLSL_BUILD_TYPE")
    configured = _configured(build)
    changed = bool(record) and record.get("configured") != selections
    needs_configure = request.action == "configure" or not configured or changed
    _validate_build(
        request, tree, build, record, configured, needs_configure,
        bool(request.platform and request.build_type),
    )
    configure_command = (
        ("cmake", "-S", str(tree.path), "-B", str(build), *flags)
        if needs_configure else None
    )
    build_command = _build_command(build, request, _jobs(request), platform)
    lines = [
        f"worktree {tree.path} (dxc)", f"platform {platform}",
        f"build dir {build}", f"build type {build_type}",
        f"cost: {'DXC build (large)' if build_command else 'configure only'}",
    ]
    migration = migration_prerequisite(root)
    if migration:
        lines.append(f"blocked: {migration} needs explicit workspace migration")
    if toolchain:
        lines.append(toolchain.text.rstrip())
    if configure_command:
        lines.append(f"configure: {' '.join(configure_command)}")
        if request.reset:
            lines.append("reset: clear saved build choices")
        lines.append(f"write selections: {_selection_file(root, tree, platform)}")
        lines.append(
            f"clangd link: {tree.path / 'compile_commands.json'} (if generated)"
        )
    if build_command:
        lines.append(f"build: {' '.join(build_command)}")
    return NativePlan(
        root, tree, build, platform,
        {"configured": selections, "choices": choices},
        configure_command, build_command, None, None, migration,
        "\n".join(lines) + "\n",
    )


def plan(request):
    """Resolve native inputs and costs without changing checkout or build state."""
    root = request.root.resolve()
    platform = ws.select_platform(request.platform)
    # Cross builds cannot execute check/CTest targets on this host. Refuse
    # before resolving target toolchains, DXC or LLVM distributions.
    _reject_cross_test_targets(request, platform)
    from . import cross

    toolchain = (cross.prerequisite(root, platform, request.no_auto or
                                   os.getenv("HLSL_AUTO") == "0")
                 if platform != "native" else None)
    spec = request.worktree or os.getenv("HLSL_WT")
    tree = (
        ws.resolve(root, spec)
        if spec else ws.enclosing_worktree(request.cwd or Path.cwd())
    )
    if tree is None or tree.kind not in ("llvm", "dxc", "offload"):
        raise BuildError("select an LLVM, DXC or offload worktree with --in")
    if tree.kind == "offload":
        from . import offload

        return offload.plan(request)
    if request.llvm or request.dist_prefix:
        raise BuildError("--llvm/--dist-prefix require a standalone offload worktree")
    if tree.kind == "dxc":
        return _plan_selected_dxc(request, root, tree, platform)
    if request.reset and any((request.offload, request.dxc, request.build_type)):
        raise BuildError("--reset cannot be combined with dependency/build selections")
    build = ws.build_directory(tree, platform, target=True, d3d12=request.d3d12,
                               root=root)
    record = _load(root, tree, platform, request.d3d12)
    configured = _configured(build)
    saved = ({} if request.reset else
             saved_selections(root, tree, platform, request.d3d12))
    offload = _dependency(root, tree, "offload", request.offload, saved)
    golden = _dependency(root, tree, "golden", None, saved)
    dxc_bin, dxc_choice, prerequisite = _dxc_bin(
        root, tree, request.dxc, saved,
        request.no_auto or os.getenv("HLSL_AUTO") == "0", platform
    )
    build_type = _build_type(request, saved, build)
    selections = {
        "build_dir": str(build),
        "offload": str(offload.path),
        "golden": str(golden.path),
        "dxc": str(dxc_choice),
        "build_type": build_type,
    }
    flags = expand_flags(
        os.getenv("HLSL_CMAKE_FLAGS_LLVM"),
        {
            "HD_BUILD_TYPE": build_type,
            "HD_INSTALL_PREFIX": build / "install",
            "HD_LLVM_SRC": tree.path,
            "HD_OFFLOAD_SRC": offload.path,
            "HD_GOLDEN_DIR": golden.path,
            "HD_DXC_BIN_DIR": dxc_bin,
            "HD_SEMI": ";",
        },
        "HLSL_CMAKE_FLAGS_LLVM",
    )
    if toolchain:
        flags.extend(cross.flags(root, platform, "llvm", llvm=tree))
        if cross.is_windows(platform):
            from .gpu import d3d12_flags

            flags.extend(d3d12_flags(root, request.d3d12))
    else:
        from .gpu import d3d12_flags

        flags.extend(d3d12_flags(root, request.d3d12))
    selections["flags_hash"] = _flag_hash(flags)
    choices = {} if request.reset else dict(saved)
    if not request.reset:
        for key, value in (
            ("offload", request.offload or os.getenv("HLSL_OFFLOAD")),
            ("golden", os.getenv("HLSL_GOLDEN")),
            ("dxc", request.dxc or os.getenv("HLSL_DXC")),
            ("build_type", request.build_type or os.getenv("HLSL_BUILD_TYPE")),
        ):
            if value:
                choices[key] = value
    changed = bool(record) and record.get("configured") != selections
    needs_configure = request.action == "configure" or not configured or changed
    _validate_build(
        request, tree, build, record, configured, needs_configure,
        bool(request.offload and request.dxc and request.platform),
    )
    configure_command = (
        ("cmake", "-S", str(tree.path / "llvm"), "-B", str(build), *flags)
        if needs_configure
        else None
    )
    jobs = _jobs(request)
    build_command = _build_command(build, request, jobs, platform)
    host_tools = cross.host_tools_plan(root, tree, jobs) if toolchain else None
    if host_tools:
        cross.require_host_tools(host_tools, request.no_auto or
                                 os.getenv("HLSL_AUTO") == "0")
    dxc_plan = (_plan_dxc(root, prerequisite, jobs, platform)
                if prerequisite else None)
    cost = "LLVM build (large)" if build_command else "configure only"
    if prerequisite:
        cost = f"DXC prerequisite (large) + {cost}"
    lines = [
        f"worktree {tree.path} (llvm)", f"platform {platform}",
        f"build dir {build}", f"build type {build_type}",
        f"offload {offload.path}", f"golden {golden.path}",
        f"dxc {dxc_bin}", f"cost: {cost}",
    ]
    migration = migration_prerequisite(root)
    if migration:
        lines.append(f"blocked: {migration} needs explicit workspace migration")
    if toolchain:
        lines.append(toolchain.text.rstrip())
    if host_tools:
        lines.append(host_tools.text.rstrip())
    if dxc_plan:
        lines.append(f"prerequisite: build dxc/dxv in {dxc_plan.build}")
        if dxc_plan.configure_command:
            lines.append(
                f"  configure: {' '.join(dxc_plan.configure_command)}"
            )
        lines.append(f"  build: {' '.join(dxc_plan.build_command)}")
    if configure_command:
        prefix = "configure (conditional):" if dxc_plan else "configure:"
        lines.append(f"{prefix} {' '.join(configure_command)}")
        if request.reset:
            lines.append("reset: clear saved dependency choices")
        selection_path = _selection_file(root, tree, platform, request.d3d12)
        lines.append(f"write selections: {selection_path}")
        if platform == "native":
            lines.append(
                f"clangd link: {tree.path / 'compile_commands.json'} (if generated)"
            )
    if build_command:
        lines.append(f"build: {' '.join(build_command)}")
    return NativePlan(
        root, tree, build, platform,
        {"configured": selections, "choices": choices},
        configure_command, build_command, dxc_plan, host_tools, migration,
        "\n".join(lines) + "\n",
    )


def _begin_configure(root, tree, build, platform, d3d12=None):
    """Invalidate old validation or mark a brand-new build safe to retry."""
    path = _selection_file(root, tree, platform, d3d12)
    old = _load(root, tree, platform, d3d12)
    if old.get("configured"):
        # A failed reconfigure invalidates the cache, but only a successful
        # configure is allowed to replace the developer's saved choices.
        _save(path, {**old, "configured": {},
                     "reconfigure_failed": {"build_dir": str(build)}})
    elif not old and not (_configured(build) or (build / "CMakeCache.txt").is_file()):
        _save(path, {"configured": {}, "choices": {},
                     "attempted": {"build_dir": str(build)}})


def _execute_dxc(root, prerequisite, jobs):
    with build_lock(root, prerequisite.build):
        current = _plan_dxc(root, prerequisite.tree, jobs, prerequisite.platform)
        if current.build != prerequisite.build:
            raise BuildError("DXC build directory changed while waiting; retry")
        ready = (_has_windows_dxc(current.build / "bin")
                 if current.platform.startswith("windows-")
                 else _has_dxc(current.build / "bin"))
        if ready and not current.configure_command:
            return
        if current.configure_command:
            path = _selection_file(root, current.tree, current.platform)
            _begin_configure(root, current.tree, current.build, current.platform)
            from . import cross

            _run_command(current.configure_command, current.build,
                         env=(cross.cross_environment()
                              if current.platform != "native" else None))
            if not _configured(current.build):
                raise BuildError(f"cmake did not configure {current.build}")
            _save(path, {"configured": current.fingerprint, "choices": {}})
            _link_database(current.tree, current.build)
        _run_command(current.build_command, current.build,
                     env=(cross.cross_environment()
                          if current.platform != "native" else None))
        ready = (_has_windows_dxc(current.build / "bin")
                 if current.platform.startswith("windows-")
                 else _has_dxc(current.build / "bin"))
        if not ready:
            raise BuildError(f"{current.build}: DXC build did not produce "
                             "dxc, dxv and required Windows DLLs")


def execute(request):
    """Replan under each build lock and never execute with a stale lock target."""
    initial = plan(request)
    _check_migration(initial.root)
    if initial.tree.kind == "offload":
        from . import offload

        return offload.execute(request)
    if initial.platform != "native":
        from . import cross

        cross.fetch(initial.root, initial.platform)
        if initial.host_tools and not (request.no_auto or os.getenv("HLSL_AUTO") == "0"):
            cross.refresh_host_tools(initial.host_tools)
    if initial.prerequisite:
        try:
            _execute_dxc(
                initial.root, initial.prerequisite,
                request.jobs or os.getenv("HLSL_JOBS"),
            )
        except OSError as error:
            raise BuildError(
                f"{initial.prerequisite.build}: command failed: {error}"
            ) from error
    installs = initial.tree.kind == "llvm" and any(
        target == "install" or target.startswith("install-")
        for target in request.targets
    )
    prefix = initial.build / "install"
    with build_lock(initial.root, prefix) if installs else nullcontext():
        with build_lock(initial.root, initial.build):
            current = plan(request)
            if (current.tree.path != initial.tree.path or current.build != initial.build
                    or current.selections != initial.selections):
                raise BuildError(
                    "build selection changed while waiting for the lock; retry"
                )
            if current.prerequisite:
                raise BuildError("DXC prerequisite changed while waiting; retry")
            try:
                if current.configure_command:
                    path = _selection_file(current.root, current.tree, current.platform,
                                           request.d3d12)
                    _begin_configure(current.root, current.tree, current.build,
                                     current.platform, request.d3d12)
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
            except OSError as error:
                raise BuildError(f"{current.build}: command failed: {error}") from error
    return initial
