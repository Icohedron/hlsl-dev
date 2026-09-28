"""Workspace GPU choices and scoped Vulkan loader configuration."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import platform
import shlex

from . import workspace as ws
from .build_support import (
    BuildError, _check_migration, _configured, _save, build_lock,
)
from .command import Plan


_CHOICES = ("on", "off")


def _file(root):
    return root / ".hlsl-dev/gpu.json"


def _choices(root):
    path = _file(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
        if (not isinstance(data, dict) or data.get("version") != 1
                or any(key not in ("version", "vk", "d3d12") for key in data)
                or any(not isinstance(value, str) for key, value in data.items()
                       if key != "version")
                or data.get("d3d12", "on") not in _CHOICES):
            raise ValueError("invalid GPU settings")
        return {key: data[key] for key in ("vk", "d3d12") if key in data}
    except (OSError, ValueError) as error:
        raise BuildError(f"invalid GPU settings at {path}: {error}") from error


def driver(root, override=None):
    return override or os.getenv("HLSL_VK_DRIVER") or _choices(root).get("vk", "lavapipe")


def d3d12(root, override=None):
    value = (override or os.getenv("HLSL_D3D12") or _choices(root).get("d3d12")
             or os.getenv("HLSL_D3D12_DEFAULT", "on"))
    if value not in _CHOICES:
        raise BuildError(f"invalid D3D12 choice '{value}'; use on or off")
    return value


def d3d12_flags(root, override=None):
    disabled = "ON" if d3d12(root, override) == "off" else "OFF"
    return (f"-DCMAKE_DISABLE_FIND_PACKAGE_D3D12={disabled}",
            f"-DCMAKE_DISABLE_FIND_PACKAGE_D3D12_WSL={disabled}")


def manifest(root, override=None):
    selected = driver(root, override)
    if selected == "system":
        return None
    if selected in ("lavapipe", "lvp"):
        selected = "lvp"
    if Path(selected).is_absolute():
        return Path(selected)
    if not selected or selected in (".", "..") or not all(
        char.isalnum() or char in "_-" for char in selected
    ):
        raise BuildError(f"invalid Vulkan driver '{selected}'; use a name or absolute manifest")
    directory = os.getenv("HLSL_VK_ICD_DIR")
    return Path(directory or "") / f"{selected}_icd.{platform.machine()}.json"


def _required_manifest(root, override=None):
    path = manifest(root, override)
    if path is not None and (not path.is_absolute() or not path.is_file()):
        raise BuildError(
            f"Vulkan driver '{driver(root, override)}' has no manifest at {path}; "
            "use 'hlsl gpu vulkan list' or choose system"
        )
    return path


def loader_changes(root, override=None):
    # An explicit loader selection wins even over the workspace's selection.
    if override is None and not os.getenv("HLSL_VK_DRIVER") and (
        os.getenv("VK_DRIVER_FILES") or os.getenv("VK_ICD_FILENAMES")
    ):
        return {}
    path = _required_manifest(root, override)
    value = str(path) if path else None
    return {"VK_DRIVER_FILES": value, "VK_ICD_FILENAMES": value}


@contextmanager
def loader_scope(root, override=None):
    changes = loader_changes(root, override)
    previous = {key: os.environ.get(key) for key in changes}
    try:
        for key, value in changes.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _tree(request):
    spec = request.worktree or os.getenv("HLSL_WT")
    if spec:
        tree = ws.resolve(request.root, spec)
    else:
        tree = ws.enclosing_worktree(request.cwd or Path.cwd())
    if tree and tree.kind in ("llvm", "offload"):
        return tree
    if spec:
        raise BuildError("D3D12 suites require an LLVM or offload worktree")
    return None


def _available():
    return os.name == "nt" or Path("/usr/lib/wsl/lib/libd3d12.so").exists()


def plan(request):
    """Report GPU settings without enumerating devices or writing state."""
    root = request.root.resolve()
    if request.action == "gpu vulkan":
        if request.gpu_choice and request.gpu_list:
            raise BuildError("choose a driver or list, not both")
        if request.gpu_list:
            directory = Path(os.getenv("HLSL_VK_ICD_DIR") or "/nonexistent")
            names = sorted(path.name for path in directory.glob(
                f"*_icd.{platform.machine()}.json") if path.is_file())
            return Plan("system (loader discovery)\nlavapipe (lvp)\n" +
                        "".join(f"{name}\n" for name in names) +
                        "absolute paths to ICD manifests are also accepted\n")
        selected = driver(root, request.gpu_choice)
        path = manifest(root, request.gpu_choice)
        state = "loader discovery" if path is None else (
            str(path) if path.is_file() and path.is_absolute() else f"{path} (missing)")
        return Plan(f"driver {selected}\nmanifest {state}\nsettings {_file(root)}\n")
    if request.action == "gpu d3d12":
        if request.gpu_choice and request.gpu_choice not in _CHOICES:
            raise BuildError("D3D12 accepts on or off")
        tree = _tree(request)
        lines = [f"setting {d3d12(root, request.gpu_choice)}",
                 f"available {'yes' if _available() else 'no'}", f"settings {_file(root)}"]
        if tree:
            build = ws.build_directory(tree, target=True, d3d12=request.gpu_choice,
                                       root=root)
            test_root = (build / "tools/OffloadTest/test" if tree.kind == "llvm"
                         else build / "test")
            state = ("unconfigured" if not _configured(build) else
                     "d3d12 suites configured" if (test_root / "d3d12").is_dir()
                     else "no d3d12 suites")
            lines.extend((f"worktree {tree.path}", f"build tree {build} ({state})"))
        return Plan("\n".join(lines) + "\n")
    raise BuildError(f"unknown GPU command '{request.action}'")


def execute(request):
    if not request.gpu_choice:
        return plan(request)
    root = request.root.resolve()
    if request.action == "gpu vulkan":
        _required_manifest(root, request.gpu_choice)
    elif request.action == "gpu d3d12":
        if request.gpu_choice not in _CHOICES:
            raise BuildError("D3D12 accepts on or off")
        forced = os.getenv("HLSL_D3D12")
        if forced and forced != request.gpu_choice:
            raise BuildError(
                f"HLSL_D3D12={forced} overrides saved D3D12 choices; "
                "unset it or use --d3d12 for a single command"
            )
    _check_migration(root)
    # Switching the preferred mode never configures or alters either build.
    # Validate --in before saving so an invalid checkout cannot change state.
    with build_lock(root, root / "gpu"):
        if request.action == "gpu d3d12":
            _tree(request)
        choices = _choices(root)
        key = "vk" if request.action == "gpu vulkan" else "d3d12"
        if choices.get(key) != request.gpu_choice:
            _save(_file(root), {**choices, key: request.gpu_choice})
    return plan(request)


def shell_exports(request):
    """Emit quoted Bash exports for eval, without reading or writing checkouts."""
    if request.gpu_choice or request.gpu_list or request.worktree:
        raise BuildError("gpu vulkan --export does not accept a driver, list or --in")
    path = _required_manifest(request.root.resolve())
    if path is None:
        return Plan("unset VK_DRIVER_FILES VK_ICD_FILENAMES\n")
    quoted = shlex.quote(str(path))
    return Plan(f"export VK_DRIVER_FILES={quoted}\n"
                f"export VK_ICD_FILENAMES={quoted}\n")
