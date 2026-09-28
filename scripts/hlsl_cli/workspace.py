"""Read-only discovery of the workspace and its source worktrees."""

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess


REPOSITORIES = {
    "llvm": "llvm-project",
    "dxc": "DirectXShaderCompiler",
    "offload": "offload-test-suite",
    "golden": "offload-golden-images",
}
TARGET_KINDS = ("llvm", "dxc", "offload")
PLATFORMS = ("linux-arm64", "linux-x64", "windows-x64", "windows-arm64")


class SelectionError(ValueError):
    """An explicit worktree/platform selection cannot be inspected."""


@dataclass(frozen=True)
class Worktree:
    path: Path
    kind: str
    branch: str


def workspace_root():
    """Use the developer root, or the checkout that owns this Python source."""
    for value in (os.getenv("HLSL_DEV_ROOT"), os.getenv("DEVENV_ROOT")):
        if value and (Path(value) / "devenv.nix").is_file():
            return Path(value).resolve()
    return Path(__file__).resolve().parents[2]


def kind_of(path):
    """Recognize an initialized checkout by its contents, not its directory name."""
    if (path / "cmake/caches/PredefinedParams.cmake").is_file() and (
        path / "tools/clang"
    ).is_dir():
        return "dxc"
    if (path / "tools/offloader").is_dir() and (path / "lib/API").is_dir():
        return "offload"
    if (path / "llvm/CMakeLists.txt").is_file() and (path / "clang").is_dir():
        return "llvm"
    if (
        (path / "hlsl").is_dir()
        and (path / "README.md").is_file()
        and not (path / "CMakeLists.txt").is_file()
    ):
        return "golden"
    return None


def git_output(path, *args, allow_status=()):
    """Report broken Git metadata rather than silently omitting worktrees."""
    if not (path / ".git").exists():
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise SelectionError(f"cannot run git for {path}: {error}") from error
    if result.returncode not in (0, *allow_status):
        reason = result.stderr.strip() or f"exit {result.returncode}"
        raise SelectionError(
            f"git {' '.join(args)} failed for {path}: {reason}; "
            "check this checkout's Git metadata"
        )
    return result.stdout.strip()


def branch_of(path):
    if not (path / ".git").exists():
        return ""
    branch = git_output(
        path, "symbolic-ref", "-q", "--short", "HEAD", allow_status=(1,)
    )
    if branch:
        return branch
    git_output(path, "rev-parse", "--verify", "HEAD")
    return "(detached)"


def worktrees(root, kind):
    """List submodule, registered worktrees (including external), then clones."""
    base = root / REPOSITORIES[kind]
    candidates = [base]
    candidates += [
        Path(line.removeprefix("worktree "))
        for line in git_output(base, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]
    candidates += sorted(root.glob(f"{base.name}*"))
    found = []
    seen = set()
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        path = candidate.resolve()
        if path in seen or kind_of(path) != kind:
            continue
        seen.add(path)
        found.append(Worktree(path, kind, branch_of(path)))
    return found


def enclosing_worktree(directory):
    for path in (directory.resolve(), *directory.resolve().parents):
        kind = kind_of(path)
        if kind:
            return Worktree(path, kind, branch_of(path))
    return None


def resolve(root, spec, kinds=TARGET_KINDS):
    """Resolve a path, directory name, suffix or branch without reading pins."""
    for candidate in (Path(spec), root / spec):
        if candidate.is_dir():
            path = candidate.resolve()
            kind = kind_of(path)
            if kind not in kinds:
                raise SelectionError(f"{path} is not an inspectable worktree")
            return Worktree(path, kind, branch_of(path))
    for kind in kinds:
        for tree in worktrees(root, kind):
            if spec in (
                tree.path.name,
                f"{REPOSITORIES[kind]}.{spec}",
                tree.branch,
            ):
                return tree
    raise SelectionError(f"no worktree matches '{spec}'; run 'hlsl list'")


def dependency(root, from_tree, kind):
    """Match the source branch, then fall back to the initialized submodule."""
    trees = worktrees(root, kind)
    if from_tree.branch and from_tree.branch != "(detached)":
        for tree in trees:
            if tree.branch == from_tree.branch:
                return tree
    return trees[0] if trees else None


def distribution_prefix(tree, platform="native", *, d3d12=None, root=None):
    """Installed LLVM distribution in the selected integrated build tree."""
    return build_directory(tree, platform, target=True, d3d12=d3d12,
                           root=root) / "install"


def build_directory(tree, platform="native", *, target=False, d3d12=None, root=None):
    override = os.getenv("HLSL_BUILD_DIR") if target else None
    if override:
        path = Path(override)
        return path if path.is_absolute() else tree.path / path
    name = os.getenv("HLSL_BUILD_DIR_NAME")
    if not name:
        name = "build"
        if (tree.kind in ("llvm", "offload")
                and (platform == "native" or platform.startswith("windows-"))):
            from .gpu import d3d12 as d3d12_choice

            if d3d12_choice(root or workspace_root(), d3d12) == "on":
                name = "build-d3d12"
    return tree.path / (name + (f".{platform}" if platform != "native" else ""))


def host_platform():
    override = os.getenv("HLSL_HOST_PLATFORM")
    if override:
        return override
    machine = os.uname().machine
    if machine in ("x86_64", "amd64"):
        return "linux-x64"
    if machine in ("aarch64", "arm64"):
        return "linux-arm64"
    return None


def select_platform(requested=None):
    platform = requested or os.getenv("HLSL_PLATFORM") or "native"
    if platform not in ("native", *PLATFORMS):
        raise SelectionError(
            f"unknown platform '{platform}'; use native or: {', '.join(PLATFORMS)}"
        )
    if platform != "native" and platform == host_platform():
        raise SelectionError(f"{platform} is this machine; use native instead")
    return platform
