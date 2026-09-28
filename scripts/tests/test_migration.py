"""Confirmed migration exercises disposable Git worktrees, never the workspace."""

import fcntl
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_hlsl_cli import checkout, cli, files, git, native_inputs, workspace  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from hlsl_cli import migration  # noqa: E402
from hlsl_cli.command import Request, run  # noqa: E402


def fixture(root, tmp_path):
    llvm = checkout(root, "llvm-project", "llvm")
    (llvm / ".gitignore").write_text("original\n")
    git(llvm, "add", ".gitignore")
    git(llvm, "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-qm", "ignore")
    external = tmp_path.parent / f"{tmp_path.name}.external-migrate"
    git(llvm, "worktree", "add", "-qb", "migration", str(external))
    dxc = checkout(root, "DirectXShaderCompiler", "dxc")
    (external / ".gitignore").write_text(
        "original\n\n# >>> codegraph: local index scope, not committed >>>\n"
        "!**/Target/\n# <<< codegraph <<<\n"
    )
    git(external, "update-index", "--skip-worktree", ".gitignore")
    for tree in (llvm, external, dxc):
        (tree / "build").mkdir()
        (tree / "build/build.ninja").touch()
        (tree / "codegraph.json").write_text("{}")
    (llvm / "build-d3d12").mkdir()
    (llvm / "build-d3d12/build.ninja").touch()
    (external / ".codegraph").mkdir()
    (external / ".codegraph/index.db").write_text("index")
    state = root / ".hlsl-dev"
    (state / "pins").mkdir(parents=True)
    (state / "pins/llvm-project.env").write_text("old")
    (state / "settings.env").write_text("old")
    (state / "toolchains").mkdir()
    (state / "toolchains/linux-arm64").symlink_to("/absent/toolchain")
    (state / "locks").mkdir()
    lock = state / "locks/llvm-project%build.lock"
    lock.touch()
    archive = root / "preserved.zip"
    archive.write_bytes(b"archive")
    return llvm, external, dxc, state, lock, archive


def test_confirmed_migration_removes_only_legacy_and_retry_is_noop(workspace, tmp_path):
    llvm, external, dxc, state, lock, archive = fixture(workspace, tmp_path)
    plan = cli(workspace, "workspace", "migrate", "--dry-run")
    assert plan.returncode == 0, plan.stderr
    assert "conflicts: none detected" in plan.stdout
    done = cli(workspace, "workspace", "migrate", "--yes")
    assert done.returncode == 0, done.stderr
    assert "migration complete" in done.stdout
    for tree in (llvm, external, dxc):
        assert (tree / "build/build.ninja").exists()
        assert not (tree / "codegraph.json").exists()
        expected = {"?? build/"}
        if tree == llvm:
            assert (tree / "build-d3d12/build.ninja").exists()
            expected.add("?? build-d3d12/")
        assert set(git(tree, "status", "--porcelain").splitlines()) == expected
    assert not (external / ".codegraph").exists()
    assert (external / ".gitignore").read_text() == "original\n"
    assert git(external, "ls-files", "-v", ".gitignore").startswith("H ")
    for name in ("pins", "settings.env", "toolchains"):
        assert not (state / name).exists()
    assert lock.exists() and archive.read_bytes() == b"archive"
    assert (state / "migration.json").exists()
    before, other = files(workspace), files(external)
    again = cli(workspace, "workspace", "migrate", "--yes")
    assert again.returncode == 0 and "fresh workspace" in again.stdout
    assert files(workspace) == before and files(external) == other


@pytest.mark.parametrize("conflict", ["dirty", "marker", "active-lock", "tracked-index"])
def test_preflight_conflict_never_partially_deletes(workspace, tmp_path, conflict):
    llvm, external, dxc, state, lock, archive = fixture(workspace, tmp_path)
    if conflict == "dirty":
        (dxc / ".fixture").write_text("unrelated")
    elif conflict == "marker":
        (external / ".gitignore").write_text(
            "unrelated\n# >>> codegraph: local index scope, not committed >>>\n"
            "!**/Target/\n# <<< codegraph <<<\n"
        )
    elif conflict == "tracked-index":
        git(dxc, "add", "-f", "codegraph.json")
    before, other = files(workspace), files(external)
    with lock.open("r") as handle:
        if conflict == "active-lock":
            fcntl.flock(handle, fcntl.LOCK_EX)
        refused = cli(workspace, "workspace", "migrate", "--yes")
    assert refused.returncode != 0 and "preflight" in refused.stderr
    assert files(workspace) == before and files(external) == other
    assert not (state / "migration.json").exists()


def test_retry_after_interruption_only_cleans_remaining_artifacts(workspace, tmp_path,
                                                                  monkeypatch):
    llvm, external, _, state, _, _ = fixture(workspace, tmp_path)
    original = migration._remove
    interrupted = False

    def interrupt(path):
        nonlocal interrupted
        original(path)
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt()

    monkeypatch.setattr(migration, "_remove", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run(Request(action="workspace migrate", root=workspace))
    assert not (state / "migration.json").exists()
    assert (external / ".gitignore").read_text() == "original\n"
    assert (external / ".codegraph/index.db").exists()
    monkeypatch.setattr(migration, "_remove", original)
    done = run(Request(action="workspace migrate", root=workspace))
    assert "migration complete" in done.text
    assert not (external / ".codegraph").exists()
    assert (llvm / "build/build.ninja").exists()


def test_old_configured_build_still_needs_explicit_revalidation(workspace, tmp_path,
                                                                   monkeypatch):
    llvm, _, _, _, _, _ = fixture(workspace, tmp_path)
    offload, _, dxc = native_inputs(workspace)
    assert cli(workspace, "workspace", "migrate", "--yes").returncode == 0
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja")
    # Build remains present, but no new JSON fingerprint can validate its inputs.
    attempted = cli(workspace, "build", "clang", "--in", str(llvm),
                    "--offload", str(offload), "--dxc", str(dxc), "--no-auto")
    assert attempted.returncode != 0 and "hlsl configure" in attempted.stderr
    assert (llvm / "build/build.ninja").exists()
    assert not (workspace / ".hlsl-dev/selections").exists()


def test_fresh_workspace_no_confirmation_and_new_cache_preserved(workspace):
    assert cli(workspace, "workspace", "migrate", "--yes").returncode == 0
    assert not (workspace / ".hlsl-dev").exists()
    state = workspace / ".hlsl-dev/toolchains"
    state.mkdir(parents=True)
    (state / ".private-cli").touch()
    (state / "linux-arm64").symlink_to("/absent/new-toolchain")
    before = files(workspace)
    assert cli(workspace, "workspace", "migrate", "--yes").returncode == 0
    assert files(workspace) == before
