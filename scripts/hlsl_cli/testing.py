"""Plan and run native offload suites and checkout-local llvm-lit paths."""

from contextlib import nullcontext
from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import shlex
import subprocess

from . import native
from . import workspace as ws
from .build_support import BuildError, _check_migration, _load, build_lock
from .command import Plan


SUITES = (
    "d3d12", "vk", "mtl", "warp-d3d12", "clang-d3d12", "clang-vk",
    "clang-mtl", "clang-warp-d3d12", "unit",
)
_SOURCE = re.compile(r'^config\.offloadtest_src_root = path\(r"(.*)"\)$')


@dataclass(frozen=True)
class TestPlan:
    """A build plan followed, for an explicit selection, by lit."""

    build_request: object
    build_plan: object
    lit_command: tuple[str, ...] | None
    text: str


def _selection(request, kinds):
    root = request.root.resolve()
    platform = ws.select_platform(request.platform)
    if platform != "native":
        raise BuildError(f"cannot run tests for {platform}: use native binaries")
    spec = request.worktree or os.getenv("HLSL_WT")
    tree = ws.resolve(root, spec) if spec else ws.enclosing_worktree(
        request.cwd or Path.cwd()
    )
    if tree is None or tree.kind not in kinds:
        raise BuildError(f"select an {' or '.join(kinds)} worktree with --in")
    return root, tree, ws.build_directory(tree, "native", target=True,
                                          d3d12=request.d3d12, root=root)


def _lit_args(request):
    try:
        return (*shlex.split(request.lit_args or "-v"), *request.lit_flags)
    except ValueError as error:
        raise BuildError(f"invalid --lit-args: {error}") from error


def _test_root(tree, build):
    integrated = build / "tools/OffloadTest/test"
    return integrated if tree.kind == "llvm" else build / "test"


def _suite_source(suite_directory, default):
    site = suite_directory / "lit.site.cfg.py"
    if site.is_file():
        for line in reversed(site.read_text().splitlines()):
            match = _SOURCE.fullmatch(line)
            if match:
                return Path(match.group(1))
    return default


def _selected_command(request, tree, build, offload):
    root = _test_root(tree, build)
    suite = root / request.suite if request.suite else root
    command = (str(build / "bin/llvm-lit"), *_lit_args(request))
    if request.filter is not None:
        return (*command, "--filter", request.filter, str(suite))
    if request.test_path is not None:
        path = Path(request.test_path)
        if path.is_absolute():
            # Lit maps configured build paths back to the source suite.
            source = _suite_source(suite, offload) / "test" / request.suite
            try:
                path = path.relative_to(source)
            except ValueError:
                return (*command, str(path))
        return (*command, str(suite / path))
    return (*command, str(suite))


def _test_plan(request):
    root, tree, build = _selection(request, ("llvm", "offload"))
    if request.suite not in (None, *SUITES):
        raise BuildError(f"unknown suite '{request.suite}'; expected: {', '.join(SUITES)}")
    if request.test_path is not None and request.filter is not None:
        raise BuildError("choose PATH or --filter REGEX, not both")
    if (request.test_path is not None or request.filter is not None) and not request.suite:
        raise BuildError("PATH and --filter need a suite: hlsl test SUITE PATH")
    selected = (request.test_path is not None or request.filter is not None
                or bool(request.lit_flags) or request.lit_args is not None)
    target = (
        "hlsl-test-depends" if selected
        else "check-hlsl" + (f"-{request.suite}" if request.suite else "")
    )
    build_request = replace(request, action="build", targets=(target,))
    build_plan = native.plan(build_request)
    # A configured site's source is authoritative when it differs from the
    # currently selected source; use the selected source for unbuilt previews.
    lit_command = (
        _selected_command(request, tree, build,
                          Path(build_plan.selections["configured"]["offload"]))
        if selected else None
    )
    text = build_plan.text
    if lit_command:
        text += f"lit: {shlex.join(lit_command)}\n"
    return TestPlan(build_request, build_plan, lit_command, text)


def _lit_plan(request):
    root, tree, build = _selection(request, ("llvm", "offload", "dxc"))
    if not request.paths:
        raise BuildError("lit requires at least one test path")
    command = (str(build / "bin/llvm-lit"), *_lit_args(request), *request.paths)
    return root, tree, build, command


def plan(request):
    """Preview the same build and test commands without invoking either."""
    if request.action == "test":
        return Plan(_test_plan(request).text)
    root, tree, build, command = _lit_plan(request)
    return Plan(f"worktree {tree.path} ({tree.kind})\nplatform native\n"
                f"build dir {build}\nlit: {shlex.join(command)}\n")


def _require_lit(build):
    lit = build / "bin/llvm-lit"
    if not lit.is_file() or not os.access(lit, os.X_OK):
        raise BuildError(f"{lit} not found; build it first")


def _run_lit(root, tree, build, command, request):
    """Hold the build and, for standalone tests, its installed distribution."""
    # Reject missing tools before build_lock creates a lock file.
    _require_lit(build)
    prefix = None
    if tree.kind == "offload":
        selected = _load(root, tree, "native", request.d3d12).get("configured", {})
        if selected.get("build_dir") == str(build) and selected.get("dist_prefix"):
            prefix = Path(selected["dist_prefix"])
    with build_lock(root, prefix) if prefix else nullcontext():
        with build_lock(root, build):
            _, current_tree, current_build = _selection(request, (tree.kind,))
            if current_tree.path != tree.path or current_build != build:
                raise BuildError("test selection changed while waiting for lock")
            _require_lit(build)
            try:
                return subprocess.run(command, check=False, close_fds=True).returncode
            except OSError as error:
                raise BuildError(f"{build}: cannot run llvm-lit: {error}") from error


def execute(request):
    """Build prerequisites via native's locked planner, then run selected tests."""
    from .gpu import loader_scope

    with loader_scope(request.root, request.vk):
        return _execute_with_loader(request)


def _execute_with_loader(request):
    if request.action == "lit":
        root, tree, build, command = _lit_plan(request)
        _check_migration(root)
        status = _run_lit(root, tree, build, command, request)
        return Plan(plan(request).text, status)

    initial = _test_plan(request)
    _check_migration(initial.build_plan.root)
    try:
        native.execute(initial.build_request)
    except BuildError as error:
        if isinstance(error.__cause__, subprocess.CalledProcessError):
            return Plan(initial.text, error.__cause__.returncode)
        raise
    if initial.lit_command is None:
        return Plan(initial.text)
    current = _test_plan(request)
    if (current.build_plan.tree.path != initial.build_plan.tree.path
            or current.build_plan.build != initial.build_plan.build):
        raise BuildError("test selection changed during build")
    suite = _test_root(current.build_plan.tree, current.build_plan.build)
    if request.suite:
        suite /= request.suite
    if not suite.is_dir():
        raise BuildError(f"suite '{request.suite}' is not configured in {current.build_plan.build}")
    status = _run_lit(
        current.build_plan.root, current.build_plan.tree, current.build_plan.build,
        current.lit_command, request,
    )
    return Plan(current.text, status)
