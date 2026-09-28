"""Plan and assemble full, compiler-only, and curated DXC archives."""

from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import zipfile

from . import workspace as ws
from .build_support import (
    BuildError,
    _check_migration,
    build_lock,
    migration_prerequisite,
    saved_selections,
)
from .command import Plan, Request
from .lit_relocation import relocate_lit


@dataclass(frozen=True)
class PackagePlan:
    """Read-only selection of installed artifacts and an archive destination."""

    root: Path
    tree: ws.Worktree
    llvm: ws.Worktree
    offload: ws.Worktree
    golden: ws.Worktree
    build: Path
    distribution: Path | None
    archive: Path
    platform: str
    install_targets: tuple[str, ...]
    needs_install: bool
    text: str


@dataclass(frozen=True)
class CompilerPlan:
    """Read-only LLVM compiler selection, independent of offload worktrees."""

    root: Path
    tree: ws.Worktree
    build: Path
    archive: Path
    platform: str
    install_targets: tuple[str, ...]
    needs_install: bool
    text: str


def _source(request):
    root = request.root.resolve()
    spec = request.worktree or os.getenv("HLSL_WT")
    tree = ws.resolve(root, spec) if spec else ws.enclosing_worktree(
        request.cwd or Path.cwd()
    )
    if tree is None or tree.kind not in ("llvm", "offload"):
        raise BuildError("package full needs an LLVM or offload worktree; pass --in")
    return root, tree


def _archive_path(request, build, platform, stem="hlsl"):
    if platform == "native":
        label = f"{os.uname().machine}-{os.uname().sysname.lower()}"
    else:
        label = platform
    suffix = ".zip" if _is_windows(platform) else ".tar.gz"
    default = build / f"{stem}-{label}{suffix}"
    archive = Path(request.out).absolute() if request.out else default.absolute()
    if not str(archive).endswith(suffix):
        raise BuildError(f"{archive}: {platform} packages require {suffix}")
    return archive


def _is_windows(platform):
    return platform.startswith("windows-") or (platform == "native" and os.name == "nt")


def _installed(plan):
    prefix = plan.build / "install"
    if not (
        (prefix / "bin/offloader").is_file()
        or (prefix / "bin/offloader.exe").is_file()
    ) or not (prefix / "share/hlsl-test-suite/test/lit.cfg.py").is_file():
        return False
    if not (prefix / "share/hlsl-test-suite/golden-images").is_dir():
        return False
    # A standalone build does not install Clang's tools or resource headers.
    # They must come from the *selected* distribution, even if a stale
    # compiler happens to be present in the offload install prefix.
    compiler = plan.distribution or prefix
    return any(
        (compiler / "bin" / name).is_file()
        for name in ("clang-dxc", "clang-dxc.exe")
    ) and (compiler / "lib/clang").is_dir()


def _plan(request):
    root, tree = _source(request)
    platform = ws.select_platform(request.platform)
    build = ws.build_directory(tree, platform, target=True)
    saved = saved_selections(root, tree, platform)
    if tree.kind == "llvm":
        if request.llvm or request.dist_prefix:
            raise BuildError(
                "LLVM packages use their own worktree; omit --llvm/--dist-prefix"
            )
        llvm = tree
        offload_spec = (
            request.offload or os.getenv("HLSL_OFFLOAD") or saved.get("offload")
        )
        offload = (
            ws.resolve(root, offload_spec, ("offload",))
            if offload_spec else ws.dependency(root, tree, "offload")
        )
        distribution = None
        targets = (
            "install-distribution", "install-offload-tools",
            "install-offload-test-suite",
        )
    else:
        if request.offload:
            raise BuildError("standalone offload packages use --in; omit --offload")
        offload = tree
        llvm_spec = request.llvm or os.getenv("HLSL_LLVM") or saved.get("llvm")
        llvm = (
            ws.resolve(root, llvm_spec, ("llvm",))
            if llvm_spec else ws.dependency(root, tree, "llvm")
        )
        prefix = (
            request.dist_prefix or os.getenv("HLSL_DIST_PREFIX")
            or saved.get("dist_prefix")
        )
        if prefix:
            external = Path(prefix)
            distribution = (
                external if external.is_absolute() else root / external
            ).resolve()
        else:
            distribution = (ws.distribution_prefix(llvm, platform, root=root)
                            if llvm else None)
        targets = ("install-offload-tools", "install-offload-test-suite")
    golden_spec = os.getenv("HLSL_GOLDEN") or saved.get("golden")
    golden = (
        ws.resolve(root, golden_spec, ("golden",))
        if golden_spec else ws.dependency(root, tree, "golden")
    )
    if not (llvm and offload and golden):
        raise BuildError(
            f"{tree.path}: missing LLVM, offload or golden images; run 'hlsl setup'"
        )
    archive = _archive_path(request, build, platform)
    tentative = PackagePlan(
        root, tree, llvm, offload, golden, build, distribution, archive,
        platform, targets, False, "",
    )
    installed = _installed(tentative)
    if not installed and (request.no_auto or os.getenv("HLSL_AUTO") == "0"):
        raise BuildError(
            f"{build}: missing full install or LLVM distribution (--no-auto); "
            f"install {', '.join(targets)}"
            + (f" and check {distribution}/bin and lib/clang" if distribution else "")
        )
    if not installed and platform != "native":
        raise BuildError(
            f"{build}: cross install or LLVM distribution missing; "
            f"build {', '.join(targets)} first"
            + (f" and check {distribution}/bin and lib/clang" if distribution else "")
        )
    site_root = build / ("tools/OffloadTest/test" if tree.kind == "llvm" else "test")
    sites = sorted(
        site.parent.name for site in site_root.glob("*/lit.site.cfg.py")
        if site.parent.name != "Unit" and not site.parent.name.endswith("-lavapipe")
    )
    cost = "LLVM/offload install (large)" if not installed else "stage and archive"
    lines = [
        f"worktree {tree.path} ({tree.kind})", f"platform {platform}",
        f"llvm {llvm.path}", f"offload {offload.path}", f"golden {golden.path}",
        f"install {build / 'install'}", f"archive {archive}",
        f"configured suites {site_root}: {', '.join(sites) if sites else '(none yet)'}",
        "contents: compiler, suites, golden images, lit, PyYAML, provenance",
        f"cost: {cost}",
    ]
    if distribution:
        lines.append(f"LLVM distribution {distribution} (bin and lib/clang merged)")
    if not installed:
        lines.append(f"prerequisite: build {' '.join(targets)}")
    migration = migration_prerequisite(root)
    if migration:
        lines.append(f"blocked: {migration} needs explicit workspace migration")
    if platform.startswith("windows-"):
        lines.append("Windows execution unverified; symlinks will be materialized")
    return PackagePlan(
        root, tree, llvm, offload, golden, build, distribution, archive,
        platform, targets, not installed, "\n".join(lines) + "\n",
    )


def _compiler_installed(build):
    prefix = build / "install"
    return (prefix / "lib/clang").is_dir() and all(
        any((prefix / "bin" / (name + ext)).is_file() for ext in ("", ".exe"))
        for name in ("clang", "clang-dxc")
    )


def _compiler_plan(request):
    root = request.root.resolve()
    spec = request.worktree or os.getenv("HLSL_WT")
    tree = ws.resolve(root, spec) if spec else ws.enclosing_worktree(
        request.cwd or Path.cwd()
    )
    if tree is None or tree.kind != "llvm":
        raise BuildError("package compiler needs an LLVM worktree; pass --in")
    platform = ws.select_platform(request.platform)
    build = ws.build_directory(tree, platform, target=True)
    archive = _archive_path(request, build, platform, stem="hlsl-llvm")
    targets = ("install-distribution",)
    installed = _compiler_installed(build)
    if not installed and (request.no_auto or os.getenv("HLSL_AUTO") == "0"):
        raise BuildError(
            f"{build}: missing compiler install (--no-auto); "
            "install install-distribution"
        )
    lines = [
        f"worktree {tree.path} (llvm)", f"platform {platform}",
        f"install {build / 'install'}", f"archive {archive}",
        "contents: LLVM compiler, resource headers, lit, PyYAML; no offload suite",
        f"cost: {'LLVM install (large)' if not installed else 'stage and archive'}",
    ]
    if not installed:
        lines.append("prerequisite: build install-distribution")
    migration = migration_prerequisite(root)
    if migration:
        lines.append(f"blocked: {migration} needs explicit workspace migration")
    if platform.startswith("windows-"):
        lines.append("Windows execution unverified; symlinks will be materialized")
    return CompilerPlan(
        root, tree, build, archive, platform, targets, not installed,
        "\n".join(lines) + "\n",
    )


def preview(request):
    """Describe package inputs and work without creating staging, state or locks."""
    if request.action == "package precompiled":
        return Plan(_precompiled_plan(request).text)
    if request.action == "package dxc":
        return Plan(_plan_dxc(request).text)
    if request.action == "package compiler":
        return Plan(_compiler_plan(request).text)
    return Plan(_plan(request).text)


def _copy_tree(source, destination):
    if not source.is_dir():
        raise BuildError(
            f"{source}: missing installed files; install the package targets"
        )
    shutil.copytree(
        source, destination, dirs_exist_ok=True, symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def _link_target(stage, path):
    try:
        target = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise BuildError(f"{path}: dangling or cyclic symlink: {error}") from error
    if not target.is_relative_to(stage.resolve()):
        raise BuildError(f"{path}: symlink escapes the staged prefix")
    return target


def _materialize_links(stage):
    # A symlinked directory may contain another symlink; bound the passes so
    # cyclic directory aliases cannot grow the staged tree without limit.
    for _ in range(8):
        links = sorted(
            (path for path in stage.rglob("*") if path.is_symlink()),
            key=lambda path: len(path.parts),
        )
        if not links:
            return
        for path in links:
            if not path.is_symlink():
                continue
            target = _link_target(stage, path)
            if target.is_dir() and path.parent.is_relative_to(target):
                raise BuildError(f"{path}: directory symlink would copy itself")
            path.unlink()
            if target.is_dir():
                # Keep inner symlinks as links until the next validation pass.
                shutil.copytree(target, path, symlinks=True)
            else:
                shutil.copy2(target, path)
    raise BuildError(f"{stage}: too many levels of directory symlinks")


def _reject_escaping_links(stage):
    for path in stage.rglob("*"):
        if path.is_symlink():
            _link_target(stage, path)


def _bundle_python(stage, llvm, location="share/hlsl-test-suite"):
    lit_source = llvm.path / "llvm/utils/lit/lit"
    if not lit_source.is_dir():
        raise BuildError(f"{lit_source}: bundled lit sources missing")
    specification = importlib.util.find_spec("yaml")
    if not specification or not specification.origin:
        raise BuildError("PyYAML is missing on the host; install it before packaging")
    yaml_source = Path(specification.origin).parent
    deps = stage / location / "python/yaml"
    deps.mkdir(parents=True, exist_ok=True)
    for source in yaml_source.glob("*.py"):
        shutil.copy2(source, deps / source.name)
    if not (deps / "__init__.py").is_file():
        raise BuildError(f"{yaml_source}: PyYAML Python sources missing")
    lit_root = stage / location / "lit"
    _copy_tree(lit_source, lit_root / "lit")
    (lit_root / "lit.py").write_text(
        "#!/usr/bin/env python3\nimport os\nimport sys\n"
        "here = os.path.dirname(os.path.abspath(__file__))\n"
        "sys.path.insert(0, here)\n"
        "sys.path.append(os.path.join(os.path.dirname(here), 'python'))\n"
        "from lit.main import main\n"
        "if __name__ == '__main__':\n    main()\n"
    )
    bin_dir = stage / "bin"
    bin_dir.mkdir(exist_ok=True)
    launcher = bin_dir / "lit"
    launcher.write_text(
        '#!/bin/sh\nhere=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        f'exec "${{PYTHON:-python3}}" "$here/../{location}/lit/lit.py" "$@"\n'
    )
    launcher.chmod(0o755)
    (bin_dir / "lit.cmd").write_bytes(
        ('@echo off\r\npython "%~dp0..\\' + location.replace('/', '\\')
         + '\\lit\\lit.py" %*\r\n').encode()
    )


def _revision(tree):
    sha = ws.git_output(tree.path, "rev-parse", "HEAD") or "unknown"
    status = ws.git_output(tree.path, "status", "--porcelain")
    return {
        "branch": tree.branch, "revision": sha,
        "dirty": bool(status) if sha != "unknown" else None,
    }


def _sites(plan, stage, selected=None):
    test_root = plan.build / (
        "tools/OffloadTest/test" if plan.tree.kind == "llvm" else "test"
    )
    if not test_root.is_dir():
        raise BuildError(
            f"{test_root}: no configured suites; configure the offload suite"
        )
    mappings = {
        plan.build / "bin": "bin",
        plan.offload.path: "share/hlsl-test-suite",
        plan.golden.path: "share/hlsl-test-suite/golden-images",
    }
    if plan.distribution:
        mappings[plan.distribution / "bin"] = "bin"
        mappings[plan.distribution / "lib/clang"] = "lib/clang"
    names = []
    for site in sorted(test_root.glob("*/lit.site.cfg.py")):
        name = site.parent.name
        if name == "Unit" or name.endswith("-lavapipe") or (
            selected is not None and name not in selected
        ):
            continue
        # CMake's configured DXC directory may point to a host tool even for
        # cross builds. It is a sibling archive, not silently merged here.
        text = site.read_text()
        match = re.search(
            r'^config\.offloadtest_dxc_dir\s*=\s*r["\']([^"\']*)["\']',
            text, re.MULTILINE,
        )
        if match and match.group(1):
            directory = Path(match.group(1))
            if not directory.is_absolute():
                directory = site.parent / directory
            mappings[directory] = "dxc/bin"
        destination = stage / "test" / name / "lit.site.cfg.py"
        rewritten = relocate_lit(text, site, destination, stage, mappings)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rewritten)
        names.append(name)
    if not names:
        raise BuildError(f"{test_root}: no configured, runnable lit suites")
    return names


def _stage(plan, stage, selected=None):
    _copy_tree(plan.build / "install", stage)
    if plan.distribution:
        # Install targets in a standalone offload tree provide the suite and
        # its runtime tools, not Clang, lit tools or resource headers. Merge
        # only the runtime parts of the LLVM distribution, with its compiler
        # and matching headers taking precedence over stale installed copies.
        prefix = plan.distribution
        if not any(
            (prefix / "bin" / name).is_file()
            for name in ("clang-dxc", "clang-dxc.exe")
        ):
            raise BuildError(f"{prefix}: missing clang-dxc in LLVM distribution")
        _copy_tree(prefix / "bin", stage / "bin")
        _copy_tree(prefix / "lib/clang", stage / "lib/clang")
    _bundle_python(stage, plan.llvm)
    names = _sites(plan, stage, selected)
    golden = stage / "share/hlsl-test-suite/golden-images"
    if not golden.is_dir():
        raise BuildError(
            f"{golden}: install-offload-test-suite did not include golden images"
        )
    if plan.platform.startswith("windows-"):
        _materialize_links(stage)
    else:
        _reject_escaping_links(stage)
    provenance = {
        "platform": plan.platform,
        "Windows execution unverified": plan.platform.startswith("windows-"),
        "suites": names,
        "sources": {
            kind: _revision(tree) for kind, tree in (
                ("llvm-project", plan.llvm),
                ("offload-test-suite", plan.offload),
                ("offload-golden-images", plan.golden),
            )
        },
        "target_python": "Python 3 with optional psutil (per-test timeouts)",
        "dxc": "separate archive; set HLSL_DXC_DIR to its bin directory",
    }
    (stage / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    status = (
        "Windows execution unverified; run the packaged suites on a Windows target."
        if plan.platform.startswith("windows-")
        else "Execution unverified; packaging does not run the suites."
    )
    (stage / "README.txt").write_text(
        "HLSL offload test package\n"
        f"Platform: {plan.platform}\n{status}\n"
        "Unpack this archive on the target. Python 3 is required for lit; "
        "DXC ships separately.\n"
        "Set HLSL_DXC_DIR to the target DXC bin directory, then run "
        "bin/lit (or bin/lit.cmd) on test/<suite>.\n"
        "See provenance.json for source revisions and configured suites.\n"
    )
    return names


def _archive(stage, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=".hlsl-package-", dir=target.parent, delete=False
    ) as temporary:
        pending = Path(temporary.name)
    try:
        if target.suffix == ".zip":
            with zipfile.ZipFile(
                pending, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False
            ) as output:
                for path in sorted(stage.rglob("*")):
                    if path.is_file():
                        output.write(path, path.relative_to(stage).as_posix())
        else:
            with tarfile.open(pending, "w:gz") as output:
                for path in sorted(stage.iterdir()):
                    output.add(path, path.name, recursive=True)
        os.replace(pending, target)
    finally:
        pending.unlink(missing_ok=True)


def _remove_staged_path(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _compiler_stage(plan, stage):
    _copy_tree(plan.build / "install", stage)
    # An install prefix is cumulative. Prune only the snapshot, never the
    # shared install that a subsequent full package or test still needs.
    _remove_staged_path(stage / "share/hlsl-test-suite")
    _remove_staged_path(stage / "bin/D3D12")
    for name in ("offloader", "api-query", "imgdiff", "clang++", "clang-cl", "clang-cpp"):
        for extension in ("", ".exe"):
            _remove_staged_path(stage / "bin" / (name + extension))
    _bundle_python(stage, plan.tree, location="share/llvm")
    if plan.platform.startswith("windows-"):
        _materialize_links(stage)
    else:
        _reject_escaping_links(stage)
    (stage / "provenance.json").write_text(json.dumps({
        "platform": plan.platform,
        "Windows execution unverified": plan.platform.startswith("windows-"),
        "contents": "LLVM compiler and lit (no offload test suite)",
        "sources": {"llvm-project": _revision(plan.tree)},
        "target_python": "Python 3 for lit",
    }, indent=2) + "\n")


def _execute_compiler(request):
    initial = _compiler_plan(request)
    _check_migration(initial.root)
    if initial.needs_install and initial.platform != "native":
        raise BuildError(
            f"{initial.build}: cross install missing; build install-distribution first"
        )
    if initial.needs_install:
        from . import native

        native.execute(Request(
            "build", initial.root, worktree=str(initial.tree.path),
            platform=initial.platform, targets=initial.install_targets,
            jobs=request.jobs, no_auto=request.no_auto,
        ))
    with build_lock(initial.root, initial.build / "install"):
        with build_lock(initial.root, initial.build):
            return _stage_compiler_under_lock(request, initial)


def _stage_compiler_under_lock(request, initial):
    current = _compiler_plan(request)
    if current.needs_install or current != initial and (
        current.tree != initial.tree or current.build != initial.build
        or current.archive != initial.archive
    ):
        raise BuildError("package inputs changed while waiting for build lock")
    with tempfile.TemporaryDirectory(prefix="hlsl-package-") as scratch:
        stage = Path(scratch) / "stage"
        stage.mkdir()
        _compiler_stage(current, stage)
        _archive(stage, current.archive)
    return Plan(current.text + f"packaged compiler and lit in {current.archive}\n")


def execute(request):
    """Install missing inputs, then stage a locked snapshot and archive it."""
    if request.action == "package precompiled":
        return _execute_precompiled(request)
    if request.action == "package dxc":
        return _execute_dxc(request)
    if request.action == "package compiler":
        return _execute_compiler(request)
    initial = _plan(request)
    _check_migration(initial.root)
    if initial.needs_install:
        from . import native

        native.execute(Request(
            "build", initial.root, worktree=str(initial.tree.path),
            offload=str(initial.offload.path) if initial.tree.kind == "llvm" else None,
            llvm=str(initial.llvm.path) if initial.tree.kind == "offload" else None,
            platform=initial.platform, targets=initial.install_targets,
            jobs=request.jobs, no_auto=request.no_auto, dxc=request.dxc,
            dist_prefix=request.dist_prefix,
        ))
    distribution_lock = build_lock(
        initial.root, initial.distribution or initial.build / "install"
    )
    # Distribution writers acquire the prefix before the build tree. Readers
    # must use the same order or a concurrent install can deadlock packaging.
    with distribution_lock:
        with build_lock(initial.root, initial.build):
            current = _plan(request)
            if current.needs_install or (
                current.tree != initial.tree
                or current.build != initial.build
                or current.archive != initial.archive
                or current.distribution != initial.distribution
                or current.offload != initial.offload
                or current.golden != initial.golden
            ):
                raise BuildError("package inputs changed while waiting for build lock")
            with tempfile.TemporaryDirectory(prefix="hlsl-package-") as scratch:
                stage = Path(scratch) / "stage"
                stage.mkdir()
                names = _stage(current, stage)
                _archive(stage, current.archive)
    return Plan(current.text + f"packaged {', '.join(names)} in {current.archive}\n")


@dataclass(frozen=True)
class DxcPackagePlan:
    """Selection of a DXC build tree and the standalone archive it supplies."""

    root: Path
    tree: ws.Worktree
    build: Path
    archive: Path
    platform: str
    targets: tuple[str, ...]
    needs_build: bool
    text: str


def _dxc_files(platform):
    if _is_windows(platform):
        return (
            ("dxc.exe", "dxv.exe"),
            ("dxcompiler.dll", "dxil.dll", "dxc.pdb", "dxv.pdb",
             "dxcompiler.pdb", "dxil.pdb"),
            ("dxcompiler.lib", "dxil.lib"),
        )
    return (
        ("dxc", "dxv"), (),
        ("libdxcompiler.so", "libdxcompiler.dylib", "libdxil.so",
         "libdxil.dylib"),
    )


def _plan_dxc(request):
    root = request.root.resolve()
    spec = request.worktree or os.getenv("HLSL_WT")
    tree = ws.resolve(root, spec, ("dxc",)) if spec else ws.enclosing_worktree(
        request.cwd or Path.cwd()
    )
    if tree is None or tree.kind != "dxc":
        raise BuildError("package dxc needs a DXC worktree; pass --in")
    platform = ws.select_platform(request.platform)
    build = ws.build_directory(tree, platform, target=True)
    archive = _archive_path(request, build, platform, stem="hlsl-dxc")
    required, _, _ = _dxc_files(platform)
    missing = tuple(name for name in required if not (build / "bin" / name).is_file())
    if missing and (request.no_auto or os.getenv("HLSL_AUTO") == "0"):
        raise BuildError(
            f"{build}: missing {', '.join(missing)} (--no-auto); "
            f"build {' '.join(required)} first"
        )
    if missing and platform != "native":
        raise BuildError(
            f"{build}: cross DXC missing {', '.join(missing)}; "
            "build the target binaries first"
        )
    targets = ("dxc", "dxv", "dxcompiler") + (("dxildll",) if _is_windows(platform) else ())
    lines = [
        f"worktree {tree.path} (dxc)", f"platform {platform}",
        f"source {build / 'bin'} and {build / 'lib'}", f"archive {archive}",
        "contents: curated bin/ and lib/ only (no HLSL headers)",
        f"cost: {'DXC build' if missing else 'stage and archive'}",
    ]
    if missing:
        lines.append(f"prerequisite: build {' '.join(targets)}")
    migration = migration_prerequisite(root)
    if migration:
        lines.append(f"blocked: {migration} needs explicit workspace migration")
    if _is_windows(platform):
        lines.append("Windows execution unverified; binaries have not been target-tested")
    return DxcPackagePlan(
        root, tree, build, archive, platform, targets, bool(missing),
        "\n".join(lines) + "\n",
    )


def _copy_dxc_file(source, destination, build):
    """Dereference versioned DXC links without importing unrelated host files."""
    try:
        actual = source.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise BuildError(f"{source}: dangling or cyclic symlink: {error}") from error
    if not actual.is_relative_to(build.resolve()) or not actual.is_file():
        raise BuildError(f"{source}: symlink escapes the DXC build or is not a file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(actual, destination)


def _stage_dxc(plan, stage):
    required, optional_bin, optional_lib = _dxc_files(plan.platform)
    copied = []
    missing = []
    for directory, names in (("bin", required + optional_bin), ("lib", optional_lib)):
        for name in names:
            source = plan.build / directory / name
            if not source.exists() and not source.is_symlink():
                missing.append(f"{directory}/{name}")
                if name in required:
                    raise BuildError(f"{source}: required DXC binary is missing")
                continue
            destination = stage / directory / name
            _copy_dxc_file(source, destination, plan.build)
            copied.append(f"{directory}/{name}")
    provenance = {
        "platform": plan.platform,
        "Windows execution unverified": _is_windows(plan.platform),
        "sources": {"DirectXShaderCompiler": _revision(plan.tree)},
    }
    (stage / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    return copied, missing


def _execute_dxc(request):
    initial = _plan_dxc(request)
    _check_migration(initial.root)
    if initial.needs_build:
        from . import native

        native.execute(Request(
            "build", initial.root, worktree=str(initial.tree.path),
            platform=initial.platform, targets=initial.targets,
            jobs=request.jobs, no_auto=request.no_auto,
        ))
    with build_lock(initial.root, initial.build):
        current = _plan_dxc(request)
        if current.needs_build or (
            current.tree != initial.tree or current.build != initial.build
            or current.archive != initial.archive
        ):
            raise BuildError("DXC package inputs changed while waiting for build lock")
        with tempfile.TemporaryDirectory(prefix="hlsl-dxc-package-") as scratch:
            stage = Path(scratch) / "stage"
            stage.mkdir()
            copied, missing = _stage_dxc(current, stage)
            _archive(stage, current.archive)
    result = current.text + f"packaged {', '.join(copied)} in {current.archive}\n"
    if missing:
        result += f"optional files not in this build: {', '.join(missing)}\n"
    if _is_windows(current.platform):
        if "bin/dxcompiler.dll" in missing:
            result += "warning: dxcompiler.dll missing; dxc/dxv cannot load it\n"
        if "bin/dxil.dll" in missing:
            result += "warning: dxil.dll missing; dxv cannot sign D3D12 shaders\n"
    return Plan(result)


@dataclass(frozen=True)
class PrecompiledPlan:
    full: PackagePlan
    suites: tuple[str, ...]
    native_bin: Path
    dxc_bin: Path | None
    archive: Path
    text: str


def _native_dxc(request, root, tree):
    selection = request.dxc or os.getenv("HLSL_DXC")
    if selection:
        if selection in ("nix", "prebuilt", "system"):
            location = os.getenv("HLSL_DXC_PREBUILT_DIR")
            if not location:
                return None
            return Path(location).resolve()
        path = Path(selection)
        if not path.is_absolute():
            path = root / path
        if (path / "dxc").is_file():
            return path.resolve()
        candidate = ws.resolve(root, selection, ("dxc",))
    else:
        candidate = ws.dependency(root, tree, "dxc")
    if candidate:
        directory = ws.build_directory(candidate, "native") / "bin"
        if (directory / "dxc").is_file():
            return directory
    return None


def _precompiled_plan(request):
    from .precompiled import suite_tools

    full = _plan(request)
    sites = full.build / (
        "tools/OffloadTest/test" if full.tree.kind == "llvm" else "test"
    )
    available = tuple(sorted(
        site.parent.name for site in sites.glob("*/lit.site.cfg.py")
        if site.parent.name != "Unit" and not site.parent.name.endswith("-lavapipe")
    ))
    suites = tuple(dict.fromkeys(request.suites or available))
    if not suites or any(suite not in available for suite in suites):
        raise BuildError(f"unknown or absent precompiled suite: {suites}; "
                         f"configured: {available or '(none)'}")
    native_prefix = ws.distribution_prefix(full.llvm, "native", root=full.root)
    native_bin = native_prefix / "bin"
    if full.tree.kind == "llvm" and full.platform == "native" and not native_bin.is_dir():
        native_bin = full.build / "install/bin"
    dxc_bin = _native_dxc(request, full.root, full.tree)
    # A dry run must report prerequisites, not require a native distribution
    # that can be installed by the build task later.
    for suite in suites:
        if not suite.startswith("clang-") and not dxc_bin:
            raise BuildError(f"{suite} requires native DXC; pass --dxc <bin directory>")
    archive = _archive_path(request, full.build, full.platform, "hlsl-precompiled")
    lines = [f"worktree {full.tree.path}", f"platform {full.platform}",
             f"suites {', '.join(suites)}", f"native tools {native_bin}",
             f"native DXC {dxc_bin or '(not selected)'}", f"archive {archive}",
             "contents: runtime, tests, objects, compile verdicts, lit; no compiler or DXC",
             f"cost: {'install, compile and archive' if full.needs_install else 'compile and archive'}"]
    if full.needs_install:
        lines.append(f"prerequisite: build {' '.join(full.install_targets)}")
    if full.platform.startswith("windows-"):
        lines.append("Windows execution unverified; symlinks will be materialized")
    return PrecompiledPlan(full, suites, native_bin, dxc_bin, archive,
                           "\n".join(lines) + "\n")


def _precompiled_site(stage, name):
    site = stage / "test" / name / "lit.site.cfg.py"
    # Append after the installed lit.cfg.py has registered the ordinary
    # compiler substitutions. Their regexes can have ToolSubst guards; remove
    # them by name and put exact compiler replacements in their place.
    site.write_text(site.read_text() + '''
# Compiler-free package: replay the host compiler's recorded verdict.
_hlsl_replay = ('"' + _hlsl_sys.executable + '" "' +
                _hlsl_os.path.join(_hlsl_pkg, 'bin', 'precompiled-cc.py') + '"')
config.substitutions = [(key, value) for key, value in config.substitutions
                        if 'dxc_target' not in key]
config.substitutions.extend([
    (r'%dxc_target_lib\\b', _hlsl_replay),
    (r'%dxc_target\\b', _hlsl_replay),
])
''')


def _stage_precompiled(plan, stage):
    from .precompiled import compile_suite, suite_tools

    full = plan.full
    _stage(full, stage, set(plan.suites))
    for child in (stage / "test").iterdir():
        if child.is_dir() and child.name not in plan.suites:
            shutil.rmtree(child)
    # The target package must not ship even symlink aliases of the compiler.
    for directory in (stage / "lib/clang", stage / "include", stage / "dxc"):
        _remove_staged_path(directory)
    for tool in ("clang", "clang++", "clang-cl", "clang-cpp", "clang-dxc",
                 "clang-tidy", "run-clang-tidy"):
        for suffix in ("", ".exe"):
            _remove_staged_path(stage / "bin" / (tool + suffix))
    for tool in (stage / "bin").glob("clang-[0-9]*"):
        _remove_staged_path(tool)
    shutil.copy2(Path(__file__).with_name("precompiled_cc.py"),
                 stage / "bin/precompiled-cc.py")
    tests = stage / "share/hlsl-test-suite/test"
    summaries = []
    for suite in plan.suites:
        compiler, flags, features = suite_tools(suite, plan.native_bin, plan.dxc_bin)
        split_file = plan.native_bin / "split-file"
        if not split_file.is_file():
            raise BuildError(f"native split-file missing: {split_file}")
        report = compile_suite(tests, stage / "test" / suite, compiler,
                               split_file, flags, features)
        _precompiled_site(stage, suite)
        failures = sum(bool(record["status"]) for record in report["objects"].values())
        summaries.append(f"{suite}: {len(report['compiled'])} compiled, "
                         f"{len(report['skipped'])} skipped, {failures} rejected")
    (stage / "PRECOMPILED.md").write_text(
        "# Precompiled offload tests\n\n"
        "Run `bin/lit -v test/<suite>` (Windows: `bin\\lit.cmd -v test\\<suite>`).\n"
        "The target needs Python and a GPU driver, not a compiler or DXC.\n"
        "Objects and compiler exit verdicts were produced on the packaging host.\n"
        "Each `test/<suite>/precompiled.json` lists compiled tests, skipped tests\n"
        "and their reasons. Missing records fail on the target, not silently pass.\n\n"
        + "\n".join(summaries) + "\n"
        + ("Windows execution unverified.\n" if full.platform.startswith("windows-") else "")
    )
    provenance = json.loads((stage / "provenance.json").read_text())
    provenance["contents"] = "precompiled tests and runtime; no compiler or DXC"
    provenance["suites"] = list(plan.suites)
    provenance["dxc"] = "not needed on target; compile verdicts replayed"
    (stage / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    (stage / "README.txt").write_text(
        "Compiler-free offload package. See PRECOMPILED.md for instructions.\n"
        + ("Windows execution unverified.\n" if full.platform.startswith("windows-") else "")
    )
    if full.platform.startswith("windows-"):
        _materialize_links(stage)
    else:
        _reject_escaping_links(stage)
    return summaries


def _execute_precompiled(request):
    initial = _precompiled_plan(request)
    full = initial.full
    _check_migration(full.root)
    if full.needs_install:
        from . import native

        native.execute(Request(
            "build", full.root, worktree=str(full.tree.path),
            offload=str(full.offload.path) if full.tree.kind == "llvm" else None,
            llvm=str(full.llvm.path) if full.tree.kind == "offload" else None,
            platform=full.platform, targets=full.install_targets,
            jobs=request.jobs, no_auto=request.no_auto, dxc=request.dxc,
            dist_prefix=request.dist_prefix,
        ))
    # Lock source distributions first, then target build. The native compiler
    # is read while compiling even when packaging a different target platform.
    locks = sorted({str(path) for path in (
        initial.native_bin.parent,
        full.distribution or full.build / "install",
    )})
    from contextlib import ExitStack

    with ExitStack() as held:
        for path in locks:
            held.enter_context(build_lock(full.root, Path(path)))
        held.enter_context(build_lock(full.root, full.build))
        current = _precompiled_plan(request)
        if current.full.needs_install or (current.full.tree != full.tree
                                          or current.archive != initial.archive
                                          or current.suites != initial.suites
                                          or current.native_bin != initial.native_bin):
            raise BuildError("precompiled package inputs changed while waiting for lock")
        with tempfile.TemporaryDirectory(prefix="hlsl-precompiled-") as scratch:
            stage = Path(scratch) / "stage"
            stage.mkdir()
            summaries = _stage_precompiled(current, stage)
            _archive(stage, current.archive)
    return Plan(current.text + "\n".join(summaries) +
                f"\npackaged precompiled suites in {current.archive}\n")
