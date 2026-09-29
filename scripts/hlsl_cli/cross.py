"""Cross toolchains and host generators for the private command layer."""

from dataclasses import dataclass
import os
from pathlib import Path

from . import workspace as ws
from .build_support import (BuildError, _check_migration, _configured, _flag_hash,
                            _load, _run_command, _save, _selection_file,
                            build_lock, expand_flags)


TRIPLES = {"linux-arm64": "aarch64-unknown-linux-gnu",
           "linux-x64": "x86_64-unknown-linux-gnu",
           "windows-x64": "x86_64-pc-windows-msvc",
           "windows-arm64": "aarch64-pc-windows-msvc"}
HOST_TARGETS = ("llvm-min-tblgen", "llvm-tblgen", "clang-tblgen")


@dataclass(frozen=True)
class ToolchainPlan:
    root: Path
    platform: str
    file: Path
    command: tuple[str, ...] | None
    text: str


def require_cross(platform):
    if platform not in TRIPLES:
        raise BuildError(f"cross builds only support: {', '.join(TRIPLES)}")


def is_windows(platform):
    return platform.startswith("windows-")


def msvc_license_accepted():
    """Only an explicit developer choice permits realizing the licensed SDK."""
    return os.getenv("HLSL_MSVC_LICENSE") == "accepted"


def _licence_error(platform):
    return (f"{platform} needs Microsoft's SDK licence. Read "
            "https://visualstudio.microsoft.com/license-terms/mt644918/ "
            "and explicitly set HLSL_MSVC_LICENSE=accepted for this command. "
            f"Preview with 'hlsl cross fetch {platform} --dry-run'. "
            "No SDK has been fetched.")


def toolchain_file(root, platform):
    directory = os.getenv("HLSL_CROSS_TOOLCHAINS")
    if directory:
        path = Path(directory)
        if not path.is_absolute():
            raise BuildError("HLSL_CROSS_TOOLCHAINS must be an absolute path")
    else:
        path = root / ".hlsl-dev/toolchains"
    return path / platform / "toolchain.cmake"


def toolchain_plan(root, platform, *, refresh=False):
    platform = ws.select_platform(platform)
    require_cross(platform)
    file = toolchain_file(root, platform)
    needed = refresh or not file.is_file()
    nixpkgs = os.getenv("HLSL_NIXPKGS_PATH")
    command = None
    if needed:
        # Dry runs never fetch nixpkgs or realize a toolchain. Its pinned path
        # must be available only when the command actually executes.
        command = ("nix-build", str(root / "scripts/cross/toolchains.nix"),
                   "--argstr", "nixpkgs", nixpkgs or "<HLSL_NIXPKGS_PATH>",
                   "--argstr", "platform", platform,
                   *(("--arg", "acceptMsvcLicense", "true")
                     if is_windows(platform) and msvc_license_accepted() else ()),
                   "-o", str(file.parent))
    licence = (f"MSVC licence: {'accepted' if msvc_license_accepted() else 'not accepted'}\n"
               if is_windows(platform) else "")
    blocked = (f"blocked: {_licence_error(platform)}\n"
               if needed and is_windows(platform) and not msvc_license_accepted() else "")
    gpu = ("target APIs: D3D12 (Windows SDK), Vulkan "
           "(vulkan-1.lib from llvm-dlltool; target supplies vulkan-1.dll)\n"
           if is_windows(platform) else "")
    return ToolchainPlan(root, platform, file, command,
                         f"platform {platform} ({TRIPLES[platform]})\n"
                         f"toolchain {file} ({'refresh' if refresh else 'missing' if needed else 'ready'})\n"
                         + licence + gpu + blocked
                         + (f"fetch: {' '.join(command)}\n" if command else ""))


def fetch(root, platform, *, refresh=False):
    """Refresh through a temporary GC root; never unlink a working toolchain."""
    _check_migration(root)
    initial = toolchain_plan(root, platform, refresh=refresh)
    if initial.command and is_windows(platform) and not msvc_license_accepted():
        raise BuildError(_licence_error(platform))
    with build_lock(root, initial.file.parent):
        current = toolchain_plan(root, platform, refresh=refresh)
        if current.command is None:
            return current
        if is_windows(platform) and not msvc_license_accepted():
            raise BuildError(_licence_error(platform))
        nixpkgs = os.getenv("HLSL_NIXPKGS_PATH")
        if not nixpkgs or not Path(nixpkgs).is_dir():
            raise BuildError("HLSL_NIXPKGS_PATH must name the pinned nixpkgs directory")
        directory = current.file.parent
        directory.parent.mkdir(parents=True, exist_ok=True)
        pending = directory.parent / f".{platform}.pending-{os.getpid()}"
        command = (*current.command[:-1], str(pending))
        try:
            _run_command(command, directory)
            if not (pending / "toolchain.cmake").is_file():
                raise BuildError(f"{pending}: nix-build did not produce toolchain.cmake")
            if directory.exists() and not directory.is_symlink():
                raise BuildError(f"{directory}: refusing to replace a non-symlink toolchain")
            os.replace(pending, directory)
            # Distinguish new private toolchain roots from legacy Bash state.
            (directory.parent / ".private-cli").touch()
        finally:
            if pending.is_symlink():
                pending.unlink()
        return current


def prerequisite(root, platform, no_auto=False):
    """Inspect a target toolchain, refusing implicit fetches in no-auto mode."""
    plan = toolchain_plan(root, platform)
    if plan.command and no_auto:
        raise BuildError(f"{plan.file}: toolchain missing (--no-auto); "
                         f"run 'hlsl cross fetch {platform}' first")
    return plan


def inventory(root, platform=None):
    if platform:
        return toolchain_plan(root, platform).text
    lines = ["platform  triple  toolchain"]
    for target, triple in TRIPLES.items():
        if target == ws.host_platform():
            continue
        state = ("ready" if toolchain_file(root, target).is_file()
                 else "needs licence" if is_windows(target) and not msvc_license_accepted()
                 else "not built")
        lines.append(f"{target}  {triple}  {state}")
    return "\n".join(lines) + "\n"


def cross_environment():
    """Strip native search prefixes inherited from devenv before CMake runs."""
    env = os.environ.copy()
    for name in ("C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "CPATH",
                 "LIBRARY_PATH", "NIXPKGS_CMAKE_PREFIX_PATH",
                 "CMAKE_PREFIX_PATH", "CMAKE_INCLUDE_PATH", "CMAKE_LIBRARY_PATH"):
        env.pop(name, None)
    return env


def flags(root, platform, kind, *, llvm=None):
    require_cross(platform)
    values = {"HD_TOOLCHAIN_FILE": toolchain_file(root, platform),
              "HD_TARGET_TRIPLE": TRIPLES[platform], "HD_SEMI": ";"}
    if kind == "llvm":
        if llvm is None:
            raise BuildError("cross LLVM flags require an LLVM worktree")
        values["HD_NATIVE_TOOL_DIR"] = llvm.path / "build-native-tools/bin"
    result = []
    for label in ("HLSL_CMAKE_FLAGS_CROSS",
                  "HLSL_CMAKE_FLAGS_CROSS_WINDOWS" if is_windows(platform)
                  else "HLSL_CMAKE_FLAGS_CROSS_LINUX",
                  *(("HLSL_CMAKE_FLAGS_CROSS_LLVM",) if kind == "llvm" else ()),
                  *(("HLSL_CMAKE_FLAGS_CROSS_LLVM_WINDOWS",)
                    if kind == "llvm" and is_windows(platform) else ()),
                  *(("HLSL_CMAKE_FLAGS_CROSS_DXC",) if kind == "dxc" else ())):
        result.extend(expand_flags(os.getenv(label), values, label))
    if kind == "dxc" and is_windows(platform):
        dia = os.getenv("HLSL_DIA_SDK")
        if not dia or not Path(dia).is_dir():
            raise BuildError(f"{platform} DXC needs Microsoft's DIA SDK from "
                             "Visual Studio (not in nixpkgs); copy it yourself "
                             "and set HLSL_DIA_SDK to its directory, or pass "
                             "--dxc a prebuilt Windows DXC directory")
        values["HD_DIA_SDK"] = Path(dia).resolve()
        label = "HLSL_CMAKE_FLAGS_CROSS_DXC_DIA"
        result.extend(expand_flags(os.getenv(label), values, label))
    return result


@dataclass(frozen=True)
class HostToolsPlan:
    root: Path
    tree: ws.Worktree
    build: Path
    fingerprint: dict
    configure_command: tuple[str, ...] | None
    build_command: tuple[str, ...]
    text: str


def host_tools_plan(root, tree, jobs=None):
    """Always build targets incrementally: existing generators may be stale."""
    build = tree.path / "build-native-tools"
    flags = expand_flags(os.getenv("HLSL_CMAKE_FLAGS_NATIVE_TOOLS"),
                         {"HD_LLVM_SRC": tree.path,
                          "HD_INSTALL_PREFIX": build / "install", "HD_SEMI": ";"},
                         "HLSL_CMAKE_FLAGS_NATIVE_TOOLS")
    fingerprint = {"build_dir": str(build), "flags_hash": _flag_hash(flags)}
    record = _load(root, tree, "native-tools")
    if (_configured(build) or (build / "CMakeCache.txt").is_file()) and not (
            record.get("configured") == fingerprint or
            any(record.get(key, {}).get("build_dir") == str(build)
                for key in ("attempted", "reconfigure_failed"))):
        raise BuildError(f"{build}: preserved host tools need explicit revalidation")
    configure = None
    if not _configured(build) or record.get("configured") != fingerprint:
        configure = ("cmake", "-S", str(tree.path / "llvm"), "-B", str(build), *flags)
    command = ("cmake", "--build", str(build),
               *(("--parallel", str(jobs)) if jobs else ()), "--target", *HOST_TARGETS)
    return HostToolsPlan(root, tree, build, fingerprint, configure, command,
                         f"host tools {build}/bin (incremental refresh)\n"
                         + (f"  configure: {' '.join(configure)}\n" if configure else "")
                         + f"  build: {' '.join(command)}\n")


def require_host_tools(plan, no_auto=False):
    """An existing native generator set can be used without implicit builds."""
    if no_auto and (plan.configure_command or any(
            not (plan.build / "bin" / name).is_file()
            for name in ("llvm-tblgen", "clang-tblgen"))):
        raise BuildError(f"{plan.build}: host tools missing or stale (--no-auto); "
                         "build them before the cross build")


def refresh_host_tools(plan):
    from . import native

    with build_lock(plan.root, plan.build):
        current = host_tools_plan(plan.root, plan.tree,
                                  plan.build_command[plan.build_command.index("--parallel") + 1]
                                  if "--parallel" in plan.build_command else None)
        if current.fingerprint != plan.fingerprint or current.build != plan.build:
            raise BuildError("host tools changed while waiting for lock")
        if current.configure_command:
            path = _selection_file(current.root, current.tree, "native-tools")
            native._begin_configure(current.root, current.tree, current.build,
                                    "native-tools")
            _run_command(current.configure_command, current.build)
            if not _configured(current.build):
                raise BuildError(f"cmake did not configure {current.build}")
            _save(path, {"configured": current.fingerprint, "choices": {}})
        _run_command(current.build_command, current.build)
        for target in ("llvm-tblgen", "clang-tblgen"):
            if not (current.build / "bin" / target).is_file():
                raise BuildError(f"{current.build}: missing host {target}")
