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
from .command import Request


OFFLOAD_INSTALL_TARGETS = ("install-offload-tools", "install-offload-test-suite")
# A standalone offload build needs LLVM's CMake exports and static libraries;
# installed Clang also needs the two dylibs when linked against them.
LLVM_INSTALL_COMPONENTS = frozenset((
    "clang", "clang-resource-headers", "hlsl-resource-headers", "FileCheck",
    "split-file", "obj2yaml", "not", "llvm-headers", "LLVMSupport",
    "LLVMObject", "cmake-exports",
))


@dataclass(frozen=True)
class InstallPlan:
    """Install targets from a previously validated, configured build tree."""

    root: Path
    tree: ws.Worktree
    build: Path
    prefix: Path
    platform: str
    configured: dict
    command: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class DistributionPlan:
    """An LLVM install, including the exact source of its CMake cache."""

    root: Path
    llvm: ws.Worktree
    build: Path
    prefix: Path
    platform: str
    request: Request
    selections: dict
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
    allowed = (kind,) if kind else ("llvm", "offload")
    if tree is None or tree.kind not in allowed:
        raise BuildError(f"select a {kind or 'llvm or offload'} worktree with --in")
    return root, tree


def _prefix(root, llvm, request, saved, platform):
    spec = (request.dist_prefix or os.getenv("HLSL_DIST_PREFIX")
            or saved.get("dist_prefix"))
    if spec:
        path = Path(spec)
        return (path if path.is_absolute() else root / path).resolve(), True
    return ws.distribution_prefix(llvm, platform, d3d12=request.d3d12,
                                  root=root), False


def _installed(prefix):
    return (prefix / "lib/cmake/llvm/LLVMConfig.cmake").is_file()


def _distribution(root, llvm, prefix, request):
    """Install from the same LLVM build used for integrated tests and packages."""
    build_request = Request(
        "build", root, worktree=str(llvm.path), platform=request.platform,
        d3d12=request.d3d12, offload=request.offload, dxc=request.dxc,
        build_type=(request.build_type if request.action == "distribution install"
                    else None),
        jobs=request.jobs, targets=("install-distribution",),
    )
    integrated = native.plan(build_request)
    if integrated.build / "install" != prefix:
        raise BuildError(f"{prefix}: selected LLVM build installs to "
                         f"{integrated.build / 'install'}")
    text = (f"distribution: install LLVM from {integrated.build}\n"
            f"  prefix {prefix}\n" + integrated.text)
    return DistributionPlan(root, llvm, integrated.build, prefix,
                            integrated.platform, build_request,
                            integrated.selections, text)


def _install_plan(request):
    """Read the selected CMake tree; never resolve or change its inputs."""
    root, tree = _source(request, None)
    platform = ws.select_platform(request.platform)
    build = ws.build_directory(tree, platform, target=True, d3d12=request.d3d12,
                               root=root)
    prefix = build / "install"
    if build.is_symlink() or prefix.is_symlink():
        raise BuildError(f"refusing to install through a symlink: {build}")
    if not _configured(build):
        raise BuildError(f"{build}: no configured build; run 'hlsl configure "
                         f"--in {tree.path}' first")
    configured = _load(root, tree, platform, request.d3d12).get("configured", {})
    if configured.get("build_dir") != str(build):
        raise BuildError(f"{build}: build needs explicit revalidation; run "
                         f"'hlsl configure --in {tree.path}' first")
    cache = build / "CMakeCache.txt"
    if not cache.is_file():
        raise BuildError(f"{build}: CMakeCache.txt is missing; reconfigure first")
    keys = {"CMAKE_INSTALL_PREFIX", "CMAKE_HOME_DIRECTORY",
            "LLVM_DISTRIBUTION_COMPONENTS", "LLVM_LINK_LLVM_DYLIB"}
    values = {}
    for line in cache.read_text().splitlines():
        key, separator, value = line.partition("=")
        name = key.partition(":")[0]
        if separator and name in keys:
            values[name] = value
    installed_to = values.get("CMAKE_INSTALL_PREFIX")
    source = values.get("CMAKE_HOME_DIRECTORY")
    expected_source = tree.path / "llvm" if tree.kind == "llvm" else tree.path
    if source != str(expected_source):
        raise BuildError(f"{build}: CMake source does not match {expected_source}; "
                         "reconfigure before installing")
    if installed_to != str(prefix):
        raise BuildError(f"{build}: configured install prefix is "
                         f"{installed_to or '(missing)'}, not {prefix}; "
                         "reconfigure before installing")
    if tree.kind == "llvm":
        configured_components = set(values.get(
            "LLVM_DISTRIBUTION_COMPONENTS", ""
        ).split(";"))
        required = set(LLVM_INSTALL_COMPONENTS)
        if values.get("LLVM_LINK_LLVM_DYLIB") == "ON":
            required.update(("LLVM", "clang-cpp"))
        missing = sorted(required - configured_components)
        if missing:
            raise BuildError(
                f"{build}: configured LLVM distribution lacks "
                f"{', '.join(missing)}; run 'hlsl configure --in {tree.path}' "
                "to include standalone LLVM components before installing"
            )
    jobs = native._jobs(request)
    targets = (("install-distribution",) if tree.kind == "llvm"
               else OFFLOAD_INSTALL_TARGETS)
    command = ("cmake", "--build", str(build),
               *(("--parallel", jobs) if jobs else ()), "--target", *targets)
    migration = migration_prerequisite(root)
    text = (f"worktree {tree.path} ({tree.kind})\nplatform {platform}\n"
            f"build dir {build}\ninstall {prefix}\n"
            + (f"blocked: {migration} needs explicit workspace migration\n"
               if migration else "")
            + f"build: {' '.join(command)}\n"
            "no CLI configure or dependency provisioning\n")
    return InstallPlan(root, tree, build, prefix, platform, configured, command, text)


def plan_distribution(request):
    """Preview install targets without configuring or selecting dependencies."""
    from .command import Plan

    return Plan(_install_plan(request).text)


def _install(distribution):
    """Serialize the prefix with readers while building the integrated tree."""
    with build_lock(distribution.root, distribution.prefix):
        current = _distribution(distribution.root, distribution.llvm,
                                distribution.prefix, distribution.request)
        if (current.selections != distribution.selections
                or current.build != distribution.build):
            raise BuildError("distribution selection changed while waiting for lock")
        native.execute(distribution.request)
        if not _installed(distribution.prefix):
            raise BuildError(f"{distribution.prefix}: install-distribution did not "
                             "produce LLVMConfig.cmake")


def install_distribution(request):
    """Build only install targets, holding the prefix before the build lock."""
    selected = _install_plan(request)
    _check_migration(selected.root)
    with build_lock(selected.root, selected.prefix):
        with build_lock(selected.root, selected.build):
            current = _install_plan(request)
            if current != selected:
                raise BuildError("distribution build selection changed while waiting "
                                 "for locks; retry")
            env = (cross.cross_environment() if current.platform != "native" else None)
            _run_command(current.command, current.build, env=env)
            if current.tree.kind == "llvm":
                if not _installed(current.prefix):
                    raise BuildError(f"{current.prefix}: install-distribution did not "
                                     "produce LLVMConfig.cmake")
            elif not ((current.prefix / "bin/offloader").is_file()
                      or (current.prefix / "bin/offloader.exe").is_file()) or not (
                          current.prefix / "share/hlsl-test-suite/test/lit.cfg.py"
                      ).is_file():
                raise BuildError(f"{current.prefix}: offload install did not produce "
                                 "tools and suite")
    from .command import Plan

    return Plan(selected.text)


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
            f"run 'hlsl distribution install --in {llvm.path}'"
        )
    distribution = _distribution(root, llvm, prefix, request) if not installed else None
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

            # Windows cross toolchains provide D3D12 regardless of whether
            # this host can run it.
            flags.extend(d3d12_flags(root, "on"))
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
