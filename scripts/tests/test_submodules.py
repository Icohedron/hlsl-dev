"""Exercise setup and update with local-only Git remotes and nested submodules."""

import os
from pathlib import Path
import select
import shutil
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1]


def git(directory, *args):
    result = subprocess.run(
        ["git", "-C", str(directory), *args], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def commit(directory, name):
    path = directory / "history.txt"
    with path.open("a") as output:
        output.write(name + "\n")
    git(directory, "add", "history.txt")
    git(directory, "commit", "-qm", name)
    return git(directory, "rev-parse", "HEAD")


def init(directory):
    directory.mkdir(parents=True)
    git(directory, "init", "-qb", "main")
    git(directory, "config", "user.name", "Fixture")
    git(directory, "config", "user.email", "fixture@example.org")
    return directory


@pytest.fixture
def repository(tmp_path, monkeypatch):
    # All subprocess Git operations, including recursive submodule fetches,
    # reject https/ssh; every remote is a file:// URL under this temp dir.
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    nested = init(tmp_path / "nested-remote")
    for i in range(4):
        commit(nested, f"nested {i}")
    outer = init(tmp_path / "outer-remote")
    for i in range(4):
        commit(outer, f"outer {i}")
    git(outer, "submodule", "add", nested.as_uri(), "child module")
    git(outer, "config", "-f", ".gitmodules", "submodule.child module.shallow", "true")
    git(outer, "add", ".gitmodules")
    git(outer, "commit", "-qm", "add child")

    workspace = init(tmp_path / "workspace")
    (workspace / "devenv.nix").touch()
    git(workspace, "submodule", "add", outer.as_uri(), "sub module")
    git(
        workspace, "config", "-f", ".gitmodules",
        "submodule.sub module.shallow", "true",
    )
    git(workspace, "add", ".gitmodules")
    git(workspace, "commit", "-qm", "add parent")
    git(workspace, "submodule", "deinit", "-f", "--all")
    # submodule add populated full-history caches; force setup to fetch fresh
    # clones so the depth assertions exercise its initialization path.
    shutil.rmtree(workspace / ".git/modules")

    # Checkout-run CLI: mutating Git commands bind to *this* copy of source,
    # even if the ambient developer environment names another workspace.
    scripts = workspace / "scripts"
    shutil.copytree(
        SCRIPTS / "hlsl_cli", scripts / "hlsl_cli",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copyfile(SCRIPTS / "hlsl.py", scripts / "hlsl.py")
    return workspace, outer, nested


def cli(workspace, *args, cwd=None, **env):
    return subprocess.run(
        [sys.executable, str(workspace / "scripts/hlsl.py"), *args],
        cwd=cwd or workspace,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )


def successful(workspace, *args, **kwargs):
    result = cli(workspace, *args, **kwargs)
    assert result.returncode == 0, result.stderr
    return result.stdout


def history(path):
    return git(path, "rev-list", "--count", "HEAD")


def test_setup_initializes_recursive_shallow_and_preview_is_read_only(repository):
    workspace, _, _ = repository
    outer = workspace / "sub module"
    nested = outer / "child module"
    before = git(workspace, "status", "--porcelain")
    plan = successful(workspace, "setup", "--dry-run")
    assert "--depth 2" in plan and "nested modules inspected" in plan
    assert not (outer / ".git").exists()
    assert before == git(workspace, "status", "--porcelain")
    output = successful(workspace, "setup")
    assert str(nested) in output
    assert git(outer, "rev-parse", "--is-shallow-repository") == "true"
    assert git(nested, "rev-parse", "--is-shallow-repository") == "true"
    assert history(outer) == history(nested) == "2"
    assert not (workspace / ".hlsl-dev").exists()


def test_setup_skips_stale_nested_gitmodules_entry(repository):
    workspace, outer_remote, _ = repository
    # DXC has a .gitmodules entry for googletest without a matching gitlink.
    git(outer_remote, "rm", "--cached", "--", "child module")
    git(outer_remote, "commit", "-qm", "remove gitlink but retain config")
    git(
        workspace, "update-index", "--cacheinfo", "160000",
        git(outer_remote, "rev-parse", "HEAD"), "sub module",
    )
    git(workspace, "commit", "-qm", "pin parent with stale nested config")
    assert "child module" in (outer_remote / ".gitmodules").read_text()
    assert not git(outer_remote, "ls-files", "--stage", "--", "child module")

    output = successful(workspace, "setup")
    assert "child module" not in output
    assert (workspace / "sub module/.git").is_file()
    assert not (workspace / "sub module/child module/.git").exists()
    plan = successful(workspace, "setup", "--dry-run")
    assert "child module" not in plan
    update = successful(workspace, "workspace", "update")
    assert "child module" not in update


def test_setup_reports_activity_before_git_finishes(repository, tmp_path):
    workspace, _, _ = repository
    gate = tmp_path / "release-git"
    shim = tmp_path / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-C" ] && [ "$3" = "submodule" ] '
        '&& [ "$4" = "update" ]; then\n'
        '  echo "Git fetch started" >&2\n'
        '  while [ ! -e "$HLSL_TEST_GATE" ]; do sleep 0.05; done\n'
        "fi\n"
        f'exec "{shutil.which("git")}" "$@"\n'
    )
    shim.chmod(0o755)
    process = subprocess.Popen(
        [sys.executable, str(workspace / "scripts/hlsl.py"), "setup"],
        cwd=workspace,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
             "HLSL_TEST_GATE": str(gate)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        ready, _, _ = select.select([process.stderr], [], [], 2)
        assert ready, "setup printed no activity before Git finished"
        assert "sub module" in process.stderr.readline()
        assert "Git fetch started" in process.stderr.readline()
    finally:
        gate.touch()
        stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stderr
    assert "child module" in stdout


def test_update_preserves_full_parent_and_nested_history(repository):
    workspace, outer_remote, nested_remote = repository
    outer = workspace / "sub module"
    nested = outer / "child module"
    successful(workspace, "setup")
    git(outer, "fetch", "--unshallow")
    git(nested, "fetch", "--unshallow")
    old_outer_count, old_nested_count = int(history(outer)), int(history(nested))
    assert old_outer_count >= 5 and old_nested_count >= 4
    outer_sha = commit(outer_remote, "new parent")
    nested_sha = commit(nested_remote, "new child")
    # No refspec or history-wrapper actions are required to advance remotes.
    before = git(outer, "rev-parse", "HEAD")
    plan = successful(workspace, "workspace", "update", "--dry-run")
    assert "full history" in plan and "--no-recommend-shallow" in plan
    assert "--depth" not in plan
    assert git(outer, "rev-parse", "HEAD") == before
    successful(workspace, "workspace", "update")
    assert git(outer, "rev-parse", "HEAD") == outer_sha
    assert git(nested, "rev-parse", "HEAD") == nested_sha
    assert git(outer, "rev-parse", "--is-shallow-repository") == "false"
    assert git(nested, "rev-parse", "--is-shallow-repository") == "false"
    assert int(history(outer)) == old_outer_count + 1
    assert int(history(nested)) == old_nested_count + 1


def test_setup_does_not_shallow_existing_full_clones(repository):
    workspace, _, _ = repository
    outer = workspace / "sub module"
    nested = outer / "child module"
    successful(workspace, "setup")
    for path in (outer, nested):
        git(path, "fetch", "--unshallow")
    before = (history(outer), history(nested))
    plan = successful(workspace, "setup", "--dry-run")
    assert plan.count("--no-recommend-shallow") == 2
    assert "--depth" not in plan
    successful(workspace, "setup")
    assert (history(outer), history(nested)) == before
    for path in (outer, nested):
        assert git(path, "rev-parse", "--is-shallow-repository") == "false"


def test_update_initializes_missing_nested_modules(repository):
    workspace, outer_remote, nested_remote = repository
    outer_sha = commit(outer_remote, "latest parent")
    nested_sha = commit(nested_remote, "latest child")
    successful(workspace, "workspace", "update")
    outer = workspace / "sub module"
    nested = outer / "child module"
    assert git(outer, "rev-parse", "HEAD") == outer_sha
    assert git(nested, "rev-parse", "HEAD") == nested_sha
    for path in (outer, nested):
        assert git(path, "rev-parse", "--is-shallow-repository") == "true"
        assert history(path) == "2"


def test_update_keeps_shallow_clones_shallow(repository):
    workspace, outer_remote, nested_remote = repository
    successful(workspace, "setup")
    commit(outer_remote, "new parent")
    commit(nested_remote, "new child")
    plan = successful(workspace, "workspace", "update", "--dry-run")
    assert plan.count("--depth 2") == 2
    successful(workspace, "workspace", "update")
    for path in (workspace / "sub module", workspace / "sub module/child module"):
        assert git(path, "rev-parse", "--is-shallow-repository") == "true"
        assert history(path) == "2"


def test_bad_remote_fails_clearly_without_touching_other_workspace(
    repository, tmp_path
):
    workspace, _, _ = repository
    other = init(tmp_path / "other-workspace")
    (other / ".gitmodules").write_text("bad config\n")
    # Git scope is the CLI's source tree, never DEVENV_ROOT/HLSL_DEV_ROOT/cwd.
    bad = workspace / "missing-remote"
    git(
        workspace, "config", "-f", ".gitmodules",
        "submodule.sub module.url", bad.as_uri(),
    )
    git(workspace, "submodule", "sync")
    result = cli(
        workspace, "setup", cwd=other, HLSL_DEV_ROOT=str(other), DEVENV_ROOT=str(other)
    )
    assert result.returncode != 0
    assert str(workspace) in result.stderr
    assert "submodule URL" in result.stderr
    assert not (other / "sub module").exists()


def test_malformed_workspace_and_migration_refuse_before_fetch(repository):
    workspace, _, _ = repository
    (workspace / ".hlsl-dev/pins").mkdir(parents=True)
    plan = successful(
        workspace, "workspace", "update", "--dry-run", HLSL_PLATFORM="invalid"
    )
    assert "blocked:" in plan and "migration" in plan
    result = cli(workspace, "setup")
    assert result.returncode != 0 and "migration" in result.stderr
    assert not (workspace / "sub module/.git").exists()
    (workspace / ".hlsl-dev/pins").rmdir()
    (workspace / ".gitmodules").rename(workspace / "missing.gitmodules")
    result = cli(workspace, "workspace", "update")
    assert result.returncode != 0 and "no .gitmodules" in result.stderr
    assert not (workspace / "sub module/.git").exists()


def test_cli_help_and_module_interface(repository):
    workspace, _, _ = repository
    for args in (
        ("setup", "--help"),
        ("workspace", "--help"),
        ("workspace", "update", "--help"),
    ):
        assert "usage:" in successful(workspace, *args)
    help_text = successful(workspace, "workspace", "update", "--help")
    normalized_help = " ".join(help_text.split())
    assert "git -C <repo> fetch --unshallow" in normalized_help
    assert "git -C <repo> fetch origin <refspec>" in normalized_help
    sys.path.insert(0, str(workspace / "scripts"))
    try:
        from hlsl_cli.command import Request, preview, run

        request = Request("setup", workspace)
        assert "--depth 2" in preview(request).text
        assert "child module" in run(request).text
    finally:
        sys.path.remove(str(workspace / "scripts"))
