"""Private changed-lines CLI and Git-hook contracts in disposable repositories."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1]
PROFILE = Path(os.environ["DEVENV_ROOT"]) / ".devenv/profile"


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def invoke(root, *args, cwd=None, env=None):
    return subprocess.run(
        [sys.executable, str(root / "scripts/hlsl.py"), "format", *args],
        cwd=cwd or root, env={**os.environ, "HLSL_DEV_ROOT": str(root), **(env or {})},
        capture_output=True, text=True,
    )


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "devenv.nix").touch()
    (tmp_path / ".devenv").mkdir()
    (tmp_path / ".devenv/profile").symlink_to(PROFILE)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(SCRIPTS / "hlsl.py", scripts / "hlsl.py")
    shutil.copytree(SCRIPTS / "hlsl_cli", scripts / "hlsl_cli",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return tmp_path


def checkout(root, name="llvm-project", *, style=True):
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "llvm").mkdir()
    (repo / "llvm/CMakeLists.txt").touch()
    (repo / "clang").mkdir()
    git(repo, "config", "user.name", "Hook Test")
    git(repo, "config", "user.email", "hook@test")
    git(repo, "config", "commit.gpgsign", "false")
    if style:
        (repo / ".clang-format").write_text("BasedOnStyle: LLVM\n")
    (repo / "a.cpp").write_text("int badly ( int  x ) {return x;}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    return repo


def hook_path(repo):
    return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "hooks/pre-commit"


def clean_commit(repo, subject):
    return subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", subject],
        env={**os.environ, "PATH": f"{Path(shutil.which('git')).parent}:/usr/bin:/bin",
             "DEVENV_ROOT": "", "HLSL_DEV_ROOT": ""},
        capture_output=True, text=True,
    )


def test_staged_diff_fix_and_since_limit_changed_lines(workspace):
    repo = checkout(workspace)
    source = repo / "a.cpp"
    source.write_text(source.read_text() + "int   worse ( int  y ) {return y;}\n")
    git(repo, "add", "a.cpp")
    args = ("--in", str(repo))
    result = invoke(workspace, *args)
    assert result.returncode == 1 and "a.cpp" in result.stdout
    diff = invoke(workspace, *args, "--diff")
    assert diff.returncode == 1 and "+int worse(int y) { return y; }" in diff.stdout
    assert "-int badly" not in diff.stdout
    before = source.read_bytes()
    dry = invoke(workspace, *args, "--fix", "--dry-run")
    assert dry.returncode == 1 and source.read_bytes() == before
    fixed = invoke(workspace, *args, "--fix")
    assert fixed.returncode == 0, fixed.stderr
    assert "git add" in fixed.stdout
    assert "int worse(int y) { return y; }" in source.read_text()
    # A fix changes the working tree, not the staged contents.
    assert "worse" in git(repo, "show", ":a.cpp")
    assert "worse (" in git(repo, "show", ":a.cpp")
    # git clang-format compares its result with the working tree after --fix.
    assert invoke(workspace, *args).returncode == 0
    git(repo, "add", "a.cpp")
    assert invoke(workspace, *args, "--quiet").stdout == ""
    assert invoke(workspace, *args).returncode == 0
    assert "nothing to fix" in invoke(workspace, *args, "--fix").stdout
    git(repo, "commit", "-qm", "formatted")
    source.write_text(source.read_text() + "int   branch ( int  z ) {return z;}\n")
    git(repo, "add", "a.cpp")
    git(repo, "commit", "-qm", "branch change")
    assert invoke(workspace, *args).returncode == 0
    since = invoke(workspace, *args, "--since", "HEAD~1")
    assert since.returncode == 1 and "a.cpp" in since.stdout
    assert "branch" in invoke(workspace, *args, "--since", "HEAD~1", "--diff").stdout


def test_hook_warns_on_real_commit_outside_devenv_and_stays_quiet_when_clean(workspace):
    repo = checkout(workspace)
    installed = invoke(workspace, "--in", str(repo), "--install-hooks")
    assert installed.returncode == 0, installed.stderr
    path = hook_path(repo)
    assert path.is_file() and os.access(path, os.X_OK)
    subprocess.run(["sh", "-n", str(path)], check=True)
    assert str(workspace) not in path.read_text()
    assert str(PROFILE) not in path.read_text()
    assert invoke(workspace, "--in", str(repo), "--check-hooks").returncode == 0
    source = repo / "a.cpp"
    source.write_text(source.read_text() + "int   poor ( int  y ) {return y;}\n")
    git(repo, "add", "a.cpp")
    warning = clean_commit(repo, "poor")
    assert warning.returncode == 0, warning.stderr
    output = warning.stdout + warning.stderr
    assert "clang-format would change these staged files" in output
    assert "a.cpp" in output and "hlsl format --fix" in output
    assert "2" == git(repo, "rev-list", "--count", "HEAD")
    source.write_text(source.read_text() + "int fine(int x) { return x; }\n")
    git(repo, "add", "a.cpp")
    clean = clean_commit(repo, "fine")
    assert clean.returncode == 0, clean.stderr
    assert "clang-format" not in clean.stdout + clean.stderr
    assert git(repo, "status", "--porcelain") == ""
    assert "hooks/pre-commit" not in git(repo, "ls-files")


def test_shared_hook_dedup_install_check_remove_and_foreign_protection(workspace):
    repo = checkout(workspace)
    worktree = workspace / "llvm-project.topic"
    git(repo, "worktree", "add", "-qb", "topic", str(worktree))
    path = hook_path(repo)
    assert path == hook_path(worktree)
    assert invoke(workspace, "--check-hooks").returncode == 1
    dry = invoke(workspace, "--install-hooks", "--dry-run")
    assert dry.returncode == 0 and not path.exists()
    installed = invoke(workspace, "--install-hooks")
    assert installed.returncode == 0, installed.stderr
    assert installed.stdout.count("installed") == 1
    assert invoke(workspace, "--check-hooks").returncode == 0
    assert invoke(workspace, "--install-hooks").stdout == "nothing to do\n"
    path.write_text(path.read_text() + "# stale\n")
    assert invoke(workspace, "--check-hooks").returncode == 1
    assert invoke(workspace, "--install-hooks").returncode == 0
    assert "# stale" not in path.read_text()
    assert invoke(workspace, "--uninstall-hooks").returncode == 0
    assert not path.exists()
    path.write_text("#!/bin/sh\n# somebody else's hook\n# hlsl-dev clang-format hook\nexit 7\n")
    original = path.read_bytes()
    assert invoke(workspace, "--check-hooks").returncode == 0
    for operation in ("--install-hooks", "--uninstall-hooks"):
        result = invoke(workspace, operation)
        assert result.returncode == 2 and "foreign" in result.stdout
        assert path.read_bytes() == original
    path.unlink()
    path.symlink_to(workspace / "devenv.nix")
    result = invoke(workspace, "--install-hooks")
    assert result.returncode == 2 and path.is_symlink()
    assert (workspace / "devenv.nix").read_bytes() == b""


def test_hook_install_covers_each_recognized_clone(workspace):
    llvm = checkout(workspace)
    dxc = checkout(workspace, "DirectXShaderCompiler")
    (dxc / "cmake/caches").mkdir(parents=True)
    (dxc / "cmake/caches/PredefinedParams.cmake").touch()
    (dxc / "tools/clang").mkdir(parents=True)
    offload = checkout(workspace, "offload-test-suite")
    (offload / "tools/offloader").mkdir(parents=True)
    (offload / "lib/API").mkdir(parents=True)
    installed = invoke(workspace, "--install-hooks")
    assert installed.returncode == 0, installed.stderr
    assert installed.stdout.count("installed") == 3
    for repo in (llvm, dxc, offload):
        assert hook_path(repo).is_file()
    assert invoke(workspace, "--check-hooks").returncode == 0


def test_hook_from_external_worktree_finds_shared_toolchain(workspace):
    repo = checkout(workspace)
    external = workspace.parent / f"{workspace.name}.external"
    git(repo, "worktree", "add", "-qb", "external", str(external))
    assert invoke(workspace, "--install-hooks").returncode == 0
    assert hook_path(repo) == hook_path(external)
    external.joinpath("a.cpp").write_text("int  messy () {return 1;}\n")
    git(external, "add", "a.cpp")
    committed = clean_commit(external, "unformatted external")
    assert committed.returncode == 0, committed.stderr
    assert "clang-format would change these staged files" in (
        committed.stdout + committed.stderr
    )


def test_warning_hook_never_blocks_even_when_format_tools_are_missing(workspace):
    repo = checkout(workspace)
    assert invoke(workspace, "--in", str(repo), "--install-hooks").returncode == 0
    # The checkout is disposable; replace only its stand-in profile, not any
    # real profile or hook. Python and Git remain, git-clang-format is missing.
    profile = workspace / ".devenv/profile"
    profile.unlink()
    bin_dir = profile / "bin"
    bin_dir.mkdir(parents=True)
    for tool in ("python3", "git"):
        (bin_dir / tool).symlink_to(shutil.which(tool))
    repo.joinpath("a.cpp").write_text("int  broken () {return 0;}\n")
    git(repo, "add", "a.cpp")
    commit = clean_commit(repo, "tool missing")
    assert commit.returncode == 0, commit.stderr
    assert "git clang-format failed" in commit.stderr
    assert git(repo, "rev-list", "--count", "HEAD") == "2"


def test_missing_style_and_errors_do_not_hide_failures(workspace):
    repo = checkout(workspace, style=False)
    assert invoke(workspace, "--in", str(repo)).returncode == 0
    assert invoke(workspace, "--in", str(repo), "--install-hooks").returncode == 0
    assert not hook_path(repo).exists()
    assert invoke(workspace, "--in", str(repo), "--quiet").stdout == ""
    assert invoke(workspace, "--in", str(repo), "--since", "NOREF").returncode == 0
    assert invoke(workspace, "--in", str(repo), "--diff", "--fix").returncode == 2
    (repo / ".clang-format").write_text("BasedOnStyle: LLVM\n")
    (repo / "a.cpp").write_text("int  changed () {return 0;}\n")
    git(repo, "add", ".")
    invalid = invoke(workspace, "--in", str(repo), "--since", "NOREF")
    assert invalid.returncode != 0 and "NOREF" in invalid.stderr
    assert invoke(workspace, "--in", str(repo), "--check-hooks", "--fix").returncode != 0
    missing = invoke(workspace, cwd=workspace)
    assert missing.returncode != 0 and "git" in missing.stderr


@pytest.mark.parametrize("state_location", ["root", "override"])
def test_format_mutations_refuse_legacy_but_checks_remain_read_only(
        workspace, tmp_path, state_location):
    repo = checkout(workspace)
    source = repo / "a.cpp"
    source.write_text(source.read_text() + "int  poor ( int x ) {return x;}\n")
    git(repo, "add", "a.cpp")
    owned = invoke(workspace, "--in", str(repo), "--install-hooks")
    assert owned.returncode == 0, owned.stderr
    hook = hook_path(repo)
    original_hook = hook.read_bytes()
    other = tmp_path.parent / f"{tmp_path.name}-legacy-format-state"
    state = workspace / ".hlsl-dev" if state_location == "root" else other
    (state / "pins").mkdir(parents=True)
    (state / "pins/llvm-project.env").write_text("old\n")
    env = {"HLSL_DEV_STATE": str(other)}
    before = source.read_bytes()
    for args in ((), ("--diff",), ("--check-hooks",),
                 ("--fix", "--dry-run"), ("--uninstall-hooks", "--dry-run")):
        result = invoke(workspace, "--in", str(repo), *args, env=env)
        assert "migration" not in result.stderr, (args, result.stderr)
        assert source.read_bytes() == before and hook.read_bytes() == original_hook
    for args in (("--fix",), ("--install-hooks",), ("--uninstall-hooks",)):
        refused = invoke(workspace, "--in", str(repo), *args, env=env)
        assert refused.returncode != 0 and "hlsl workspace migrate --dry-run" in refused.stderr
        assert "hlsl-*" not in refused.stderr
        assert source.read_bytes() == before and hook.read_bytes() == original_hook
    assert not (other / "locks").exists()
