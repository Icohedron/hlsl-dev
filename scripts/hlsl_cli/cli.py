"""Argument parsing and output for the checkout-run HLSL command layer."""

import argparse
import sys
from pathlib import Path

from .command import Request, preview, run
from .workspace import SelectionError, workspace_root


def parser():
    commands = argparse.ArgumentParser(
        prog="hlsl",
        description=(
            "Checkout-run HLSL workspace commands."
        ),
        epilog="Example: python3 scripts/hlsl.py info --in llvm-project",
    )
    actions = commands.add_subparsers(dest="action", metavar="command")
    listing = actions.add_parser(
        "list",
        help="List initialized worktrees and build state",
        epilog="Example: python3 scripts/hlsl.py list --platform windows-x64",
    )
    listing.add_argument(
        "--platform",
        metavar="PLATFORM",
        help="Inspect this platform's build trees (default: native)",
    )
    info = actions.add_parser(
        "info",
        help="Inspect a checkout without changing it",
        epilog="Example: python3 scripts/hlsl.py info --in llvm-project",
    )
    info.add_argument(
        "--in",
        dest="worktree",
        metavar="WORKTREE",
        help="Path, name, suffix or branch (default: current checkout)",
    )
    info.add_argument(
        "--platform",
        metavar="PLATFORM",
        help="Inspect this platform's build tree (default: native)",
    )
    setup = actions.add_parser(
        "setup",
        help="Initialize workspace submodules with shallow history",
        description="Initialize all submodules (including nested ones) at depth 2. "
        "Existing full clones keep their complete history.",
        epilog="Example: python3 scripts/hlsl.py setup --dry-run",
    )
    setup.add_argument(
        "--dry-run", action="store_true", help="Preview without fetching"
    )
    workspace = actions.add_parser("workspace", help="Manage workspace submodules")
    workspace_actions = workspace.add_subparsers(dest="workspace_action")
    update = workspace_actions.add_parser(
        "update",
        help="Update submodules to their remote branches without truncating history",
        description="Advance all submodules (including nested ones) to their remote "
        "branches. Fetch full history for existing full clones; only missing or "
        "shallow clones use depth 2.",
        epilog=(
            "Example: python3 scripts/hlsl.py workspace update --dry-run. "
            "For full history, use git -C <repo> fetch --unshallow. "
            "For other branches, use git -C <repo> fetch origin <refspec>."
        ),
    )
    update.add_argument(
        "--dry-run", action="store_true", help="Preview without fetching"
    )
    migrate = workspace_actions.add_parser(
        "migrate",
        help="Preview or explicitly remove legacy state and CodeGraph indexes",
        description=(
            "Preflight all known worktrees, managed ignore blocks and locks before "
            "removing named legacy artifacts. Preserves sources, builds and archives."
        ),
        epilog=("Preview first: python3 scripts/hlsl.py workspace migrate --dry-run; "
                "then confirm: python3 scripts/hlsl.py workspace migrate --yes"),
    )
    migration_mode = migrate.add_mutually_exclusive_group(required=True)
    migration_mode.add_argument("--dry-run", action="store_true",
                                help="Inspect only; do not change the workspace")
    migration_mode.add_argument("--yes", action="store_true",
                                help="Confirm cleanup after a full preflight")
    formatting = actions.add_parser(
        "format",
        help="Check or fix changed-line formatting; manage a warning-only hook",
        epilog="Example: hlsl format --since main --diff; hlsl format --install-hooks",
    )
    formatting.add_argument("--in", dest="worktree", metavar="WORKTREE")
    formatting.add_argument("--since", metavar="COMMIT")
    mode = formatting.add_mutually_exclusive_group()
    mode.add_argument("--diff", action="store_true")
    mode.add_argument("--fix", action="store_true")
    hooks = formatting.add_mutually_exclusive_group()
    hooks.add_argument(
        "--install-hooks", dest="hook_action", action="store_const", const="install"
    )
    hooks.add_argument(
        "--check-hooks", dest="hook_action", action="store_const", const="check"
    )
    hooks.add_argument(
        "--uninstall-hooks", dest="hook_action", action="store_const", const="remove"
    )
    formatting.add_argument("--quiet", action="store_true")
    formatting.add_argument("--dry-run", action="store_true")
    tools = actions.add_parser("tools", help="Developer tools")
    tool_actions = tools.add_subparsers(dest="tool_action")
    explorer = tool_actions.add_parser(
        "explorer",
        help="Launch Compiler Explorer in the foreground with local compilers",
        description=(
            "Generate Compiler Explorer's HLSL local config for existing native "
            "Clang, Clang-DXC and DXC binaries, then start the service explicitly. "
            "Never builds a compiler."
        ),
        epilog=(
            "Example: hlsl tools explorer --llvm llvm-project --dxc "
            "DirectXShaderCompiler --dry-run"
        ),
    )
    explorer.add_argument("--in", dest="worktree", metavar="WORKTREE")
    explorer.add_argument("--llvm", metavar="WORKTREE")
    explorer.add_argument("--dxc", metavar="WORKTREE|DIRECTORY|nix")
    explorer.add_argument("--platform", metavar="PLATFORM")
    explorer.add_argument(
        "--dry-run", action="store_true", help="Preview config and service without writes"
    )
    cross = actions.add_parser("cross", help="List or prepare Linux/Windows cross toolchains")
    cross_actions = cross.add_subparsers(dest="cross_action")
    cross_actions.add_parser("list", help="List target platforms and toolchains")
    for name in ("fetch", "refresh"):
        selected = cross_actions.add_parser(name, help=f"{name.title()} a cross toolchain")
        selected.add_argument("platform", metavar="PLATFORM")
        selected.add_argument("--dry-run", action="store_true")
    for action, help_text in (
        ("configure", "Configure LLVM/DXC/standalone offload"),
        ("build", "Configure and build native LLVM/DXC/standalone offload"),
    ):
        command = actions.add_parser(
            action,
            help=help_text,
            description=f"{help_text}. Use --dry-run for a read-only plan.",
            epilog=(
                "Examples: hlsl configure --in llvm-project --dxc nix; "
                "hlsl configure --in DirectXShaderCompiler --build-type Debug; "
                "hlsl build dxc dxv --in DirectXShaderCompiler --dry-run"
            ),
        )
        if action == "build":
            command.add_argument("targets", nargs="*", metavar="TARGET")
        else:
            command.add_argument(
                "--reset", action="store_true", help="Clear saved dependency choices"
            )
        command.add_argument("--in", dest="worktree", metavar="WORKTREE")
        command.add_argument("--platform", metavar="PLATFORM")
        command.add_argument("--offload", metavar="WORKTREE")
        command.add_argument("--llvm", metavar="WORKTREE")
        command.add_argument("--dist-prefix", metavar="PREFIX")
        command.add_argument("--dxc", metavar="WORKTREE|DIRECTORY|nix")
        command.add_argument("--build-type", metavar="TYPE")
        command.add_argument("--jobs", metavar="N")
        command.add_argument("--no-auto", action="store_true")
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--d3d12", choices=("on", "off"),
                             help="D3D12 configuration for this call only")
        if action == "build":
            command.add_argument("--vulkan-driver", dest="vk", metavar="DRIVER",
                                 help="Use a Vulkan ICD for this call only")
    testing = actions.add_parser(
        "test", help="Build and run offload suites or a selected lit test",
        description=("No suite runs the full HLSL umbrella (potentially expensive). "
                     "PATH selects a test in SUITE; --filter selects by regex. "
                     "Use -- to forward subsequent arguments to llvm-lit."),
        epilog=("Examples: hlsl test clang-vk Feature/HLSLLib/log2.32.test; "
                "hlsl test clang-vk --filter 'log2.*' -- --time-tests"),
    )
    testing.add_argument("suite", nargs="?", metavar="SUITE")
    testing.add_argument("test_path", nargs="?", metavar="PATH")
    testing.add_argument("--filter", metavar="REGEX")
    lit = actions.add_parser(
        "lit", help="Run the selected worktree's llvm-lit on arbitrary paths",
        description="Run arbitrary LLVM lit paths. Use -- to forward lit flags.",
        epilog="Example: hlsl lit clang/test/CodeGenHLSL -- --time-tests",
    )
    for command in (testing, lit):
        command.add_argument("--in", dest="worktree", metavar="WORKTREE")
        command.add_argument("--platform", metavar="PLATFORM")
        command.add_argument("--lit-args", metavar="FLAGS",
                             help="Legacy alias for lit flags (prefer -- FLAGS)")
        command.add_argument("--dry-run", action="store_true")
    testing.add_argument("--llvm", metavar="WORKTREE")
    testing.add_argument("--dist-prefix", metavar="PREFIX")
    testing.add_argument("--dxc", metavar="WORKTREE|DIRECTORY|nix")
    testing.add_argument("--jobs", metavar="N")
    testing.add_argument("--no-auto", action="store_true")
    lit.add_argument("paths", nargs="+", metavar="PATH")
    gpu = actions.add_parser("gpu", help="Inspect or select GPU behavior")
    gpu_actions = gpu.add_subparsers(dest="gpu_action")
    vulkan = gpu_actions.add_parser("vulkan", help="Vulkan ICD (default: lavapipe)")
    vulkan.add_argument("--export", dest="gpu_export", action="store_true",
                        help="Print quoted loader exports for eval in a shell")
    vulkan_actions = vulkan.add_subparsers(dest="gpu_mode")
    vulkan_actions.add_parser("status", help="Show the selected driver")
    vulkan_actions.add_parser("list", help="List available drivers")
    use = vulkan_actions.add_parser("use", help="Save the selected driver")
    use.add_argument("gpu_choice", metavar="DRIVER")
    d3d12 = gpu_actions.add_parser("d3d12", help="D3D12 build configuration")
    d3d12.add_argument("gpu_choice", choices=("status", "on", "off"))
    d3d12.add_argument("--in", dest="worktree", metavar="WORKTREE")
    for command in (testing, lit):
        command.add_argument("--vulkan-driver", dest="vk", metavar="DRIVER",
                             help="Use a Vulkan ICD for this call only")
    for command in (testing, *()):
        command.add_argument("--d3d12", choices=("on", "off"),
                             help="D3D12 configuration for this call only")
    distribution = actions.add_parser(
        "distribution", help="Manage a selected LLVM worktree's distribution"
    )
    distribution_actions = distribution.add_subparsers(dest="distribution_action")
    refresh = distribution_actions.add_parser(
        "refresh", help="Explicitly rebuild and install an LLVM distribution",
        description="Refresh even an installed distribution; use --dry-run first.",
    )
    refresh.add_argument("--in", dest="worktree", metavar="LLVM_WORKTREE")
    refresh.add_argument("--offload", metavar="WORKTREE")
    refresh.add_argument("--build-type", metavar="TYPE")
    refresh.add_argument("--platform", metavar="PLATFORM")
    refresh.add_argument("--jobs", metavar="N")
    refresh.add_argument("--dry-run", action="store_true")
    clean = actions.add_parser(
        "clean", help="Remove disposable build trees (preview with --dry-run)",
        epilog="Example: hlsl clean --all --all-build-dirs --dist --dry-run",
    )
    clean.add_argument("repository", nargs="?", metavar="REPOSITORY")
    clean.add_argument("--in", dest="worktree", metavar="WORKTREE")
    clean.add_argument("--platform", metavar="PLATFORM")
    clean.add_argument("--dist", action="store_true")
    clean.add_argument("--all", action="store_true")
    clean.add_argument("--all-build-dirs", action="store_true")
    clean.add_argument("--yes", action="store_true",
                       help="Confirm removal across all selected worktrees")
    clean.add_argument("--dry-run", action="store_true")
    trim = actions.add_parser(
        "trim",
        help="Preview or remove unused ELF build binaries from the Ninja graph",
        description=("Keep the requested Ninja targets and their dependencies. "
                     "Defaults: LLVM keeps check-hlsl and check-clang; "
                     "offload keeps check-hlsl. No default for DXC."),
        epilog="Examples: hlsl trim --in llvm-project --dry-run; hlsl trim clang",
    )
    trim.add_argument("targets", nargs="*", metavar="TARGET")
    trim.add_argument("--in", dest="worktree", metavar="WORKTREE")
    trim.add_argument("--platform", metavar="PLATFORM")
    trim.add_argument("--dry-run", action="store_true")
    packages = actions.add_parser("package", help="Create portable archives")
    package_actions = packages.add_subparsers(dest="package_action")
    full = package_actions.add_parser(
        "full", help="Archive LLVM tools, configured suites and golden images",
        description=("In an offload worktree, merge its installed suite/tools "
                     "with the selected LLVM distribution's compiler and "
                     "resource headers. Windows execution is unverified until "
                     "the archive is run on a target."),
        epilog=("Examples: hlsl package full --in llvm-project --dry-run; "
                "hlsl package full --in offload-test-suite "
                "--platform windows-x64 --no-auto"),
    )
    full.add_argument("--in", dest="worktree", metavar="WORKTREE")
    full.add_argument("--platform", metavar="PLATFORM")
    full.add_argument("--llvm", metavar="WORKTREE")
    full.add_argument("--offload", metavar="WORKTREE")
    full.add_argument("--dist-prefix", metavar="PREFIX")
    full.add_argument("--dxc", metavar="WORKTREE|DIRECTORY|nix")
    full.add_argument("--out", metavar="ARCHIVE")
    full.add_argument("--jobs", metavar="N")
    full.add_argument("--no-auto", action="store_true")
    full.add_argument("--dry-run", action="store_true")
    dxc_package = package_actions.add_parser(
        "dxc", help="Archive a curated DXC compiler and validator prefix",
        epilog="Example: hlsl package dxc --in DirectXShaderCompiler --dry-run",
    )
    dxc_package.add_argument("--in", dest="worktree", metavar="DXC_WORKTREE")
    dxc_package.add_argument("--platform", metavar="PLATFORM")
    dxc_package.add_argument("--out", metavar="ARCHIVE")
    dxc_package.add_argument("--jobs", metavar="N")
    dxc_package.add_argument("--no-auto", action="store_true")
    dxc_package.add_argument("--dry-run", action="store_true")
    compiler = package_actions.add_parser(
        "compiler", help="Archive LLVM compiler tools, resource headers and lit only",
        epilog="Example: hlsl package compiler --in llvm-project --dry-run",
    )
    compiler.add_argument("--in", dest="worktree", metavar="LLVM_WORKTREE")
    compiler.add_argument("--platform", metavar="PLATFORM")
    compiler.add_argument("--out", metavar="ARCHIVE")
    compiler.add_argument("--jobs", metavar="N")
    compiler.add_argument("--no-auto", action="store_true")
    compiler.add_argument("--dry-run", action="store_true")
    precompiled = package_actions.add_parser(
        "precompiled", help="Archive offload suites with shaders compiled on this host",
        description=("Stage selected configured suites and compile their shaders with "
                     "native host tools. The target needs Python and a GPU driver, "
                     "but no compiler or DXC. Windows execution is unverified."),
        epilog=("Example: hlsl package precompiled clang-vk --in offload-test-suite "
                "--out /tmp/precompiled.tar.gz --dry-run"),
    )
    precompiled.add_argument("suites", nargs="*", metavar="SUITE")
    for flag in ("--in", "--platform", "--llvm", "--offload", "--dxc",
                 "--dist-prefix", "--out", "--jobs"):
        precompiled.add_argument(flag, dest="worktree" if flag == "--in" else None)
    precompiled.add_argument("--no-auto", action="store_true")
    precompiled.add_argument("--dry-run", action="store_true")
    repro = package_actions.add_parser(
        "repro", help="Archive named tests with Python-free sh/cmd runners",
        description=("Precompile every selected test on the host and package its "
                     "objects, data, runtime and provenance. Unsupported lit "
                     "constructs refuse the whole archive, never skip tests."),
        epilog=("Example: hlsl package repro Feature/HLSLLib/log2.32.test "
                "--suite clang-vk --in offload-test-suite --dry-run"),
    )
    repro.add_argument("paths", nargs="+", metavar="TEST")
    repro.add_argument("--suite", metavar="SUITE")
    for flag in ("--in", "--platform", "--llvm", "--offload", "--dxc",
                 "--dist-prefix", "--out", "--jobs"):
        repro.add_argument(flag, dest="worktree" if flag == "--in" else None)
    repro.add_argument("--no-auto", action="store_true")
    repro.add_argument("--dry-run", action="store_true")
    return commands, {"test": testing, "lit": lit}


def main(argv=None):
    commands, testing_parsers = parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    lit_flags = ()
    if argv and argv[0] in testing_parsers:
        # The separator belongs to lit, not argparse. parse_intermixed_args
        # keeps workspace options usable before or after positional paths.
        if "--" in argv:
            separator = argv.index("--")
            argv, lit_flags = argv[:separator], tuple(argv[separator + 1:])
        args = testing_parsers[argv[0]].parse_intermixed_args(argv[1:])
        args.action = argv[0]
    else:
        args = commands.parse_args(argv)
    if args.action is None:
        commands.print_help()
        return 0
    if (args.action == "workspace" and args.workspace_action is None
            or args.action == "distribution" and args.distribution_action is None):
        commands.print_help()
        return 0
    if args.action == "cross" and args.cross_action is None:
        commands.print_help()
        return 0
    if args.action == "tools" and args.tool_action is None:
        commands.print_help()
        return 0
    if args.action == "gpu" and args.gpu_action is None:
        commands.print_help()
        return 0
    if args.action == "gpu" and args.gpu_action == "vulkan":
        if args.gpu_export == (args.gpu_mode is not None):
            commands.error("gpu vulkan requires status, list, use DRIVER, or --export")
    if args.action == "package" and args.package_action is None:
        commands.print_help()
        return 0
    try:
        action = args.action
        if action == "workspace":
            action = f"workspace {args.workspace_action}"
        elif action == "distribution":
            action = f"distribution {args.distribution_action}"
        elif action == "tools":
            action = f"tools {args.tool_action}"
        elif action == "cross":
            action = f"cross {args.cross_action}"
        elif action == "gpu":
            action = f"gpu {args.gpu_action}"
        elif action == "package":
            action = f"package {args.package_action}"
        # Git-wide operations must never follow a stale DEVENV_ROOT/HLSL_DEV_ROOT
        # into a different checkout from the one providing this CLI source.
        root = (
            Path(__file__).resolve().parents[2]
            if action in ("setup", "workspace update")
            else workspace_root()
        )
        request = Request(
            action=action,
            root=root,
            worktree=getattr(args, "worktree", None),
            platform=getattr(args, "platform", None),
            offload=getattr(args, "offload", None),
            llvm=getattr(args, "llvm", None),
            dist_prefix=getattr(args, "dist_prefix", None),
            dxc=getattr(args, "dxc", None),
            build_type=getattr(args, "build_type", None),
            targets=tuple(getattr(args, "targets", ())),
            suites=tuple(getattr(args, "suites", ())),
            suite=getattr(args, "suite", None),
            filter=getattr(args, "filter", None),
            test_path=getattr(args, "test_path", None),
            paths=tuple(getattr(args, "paths", ())),
            lit_args=getattr(args, "lit_args", None),
            lit_flags=lit_flags,
            jobs=getattr(args, "jobs", None),
            no_auto=getattr(args, "no_auto", False),
            reset=getattr(args, "reset", False),
            all=getattr(args, "all", False),
            all_build_dirs=getattr(args, "all_build_dirs", False),
            yes=getattr(args, "yes", False),
            dist=getattr(args, "dist", False),
            repository=getattr(args, "repository", None),
            since=getattr(args, "since", None),
            diff=getattr(args, "diff", False),
            fix=getattr(args, "fix", False),
            hook_action=getattr(args, "hook_action", None),
            quiet=getattr(args, "quiet", False),
            out=getattr(args, "out", None),
            vk=getattr(args, "vk", None),
            d3d12=getattr(args, "d3d12", None),
            gpu_choice=(None if action == "gpu d3d12" and args.gpu_choice == "status"
                        else getattr(args, "gpu_choice", None)),
            gpu_list=getattr(args, "gpu_mode", None) == "list",
            gpu_export=getattr(args, "gpu_export", False),
        )
        if args.action == "workspace" and args.workspace_action == "migrate":
            request = Request(action="workspace migrate", root=request.root)
        report = preview(request) if getattr(args, "dry_run", False) else run(request)
    except SelectionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(report.text, end="")
    return getattr(report, "status", 0)
