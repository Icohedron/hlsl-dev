"""Exercise the private Python inspector in disposable HLSL workspaces."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1]
CLI = SCRIPTS / "hlsl.py"
sys.path.insert(0, str(SCRIPTS))

from hlsl_cli.command import Request, preview  # noqa: E402


def git(directory, *args):
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def checkout(root, name, kind):
    path = root / name
    path.mkdir()
    if kind == "llvm":
        (path / "llvm").mkdir()
        (path / "llvm/CMakeLists.txt").touch()
        (path / "clang").mkdir()
        (path / "clang/.fixture").touch()
    elif kind == "dxc":
        (path / "cmake/caches").mkdir(parents=True)
        (path / "cmake/caches/PredefinedParams.cmake").touch()
        (path / "tools/clang").mkdir(parents=True)
    elif kind == "offload":
        (path / "tools/offloader").mkdir(parents=True)
        (path / "lib/API").mkdir(parents=True)
    elif kind == "golden":
        (path / "hlsl").mkdir()
        (path / "README.md").touch()
    (path / ".fixture").touch()
    git(path, "init", "-q")
    git(path, "add", ".")
    git(
        path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "init",
    )
    return path


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    (tmp_path / "devenv.nix").touch()
    monkeypatch.setenv("HLSL_DEV_ROOT", str(tmp_path))
    monkeypatch.delenv("HLSL_WT", raising=False)
    monkeypatch.delenv("HLSL_PLATFORM", raising=False)
    monkeypatch.delenv("HLSL_BUILD_DIR", raising=False)
    monkeypatch.delenv("HLSL_BUILD_DIR_NAME", raising=False)
    monkeypatch.delenv("HLSL_D3D12", raising=False)
    monkeypatch.setenv("HLSL_D3D12_DEFAULT", "off")
    return tmp_path


def cli(workspace, *args, cwd=None):
    env = os.environ.copy()
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=cwd or workspace,
        env=env,
        text=True,
        capture_output=True,
    )


def files(root):
    """Include bytes and symlink targets, not just a count of paths."""
    return {
        str(p.relative_to(root)): (
            ("link", os.readlink(p))
            if p.is_symlink()
            else ("file", p.read_bytes())
            if p.is_file()
            else ("dir",)
        )
        for p in root.rglob("*")
    }


def test_help_and_empty_list_without_checkout(workspace):
    for args in ((), ("--help",), ("list", "--help"), ("info", "--help")):
        result = cli(workspace, *args)
        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout
        assert (args[0] if args and args[0] != "--help" else "list") in result.stdout
    result = cli(workspace, "list")
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("(not checked out)") == 4
    assert "hlsl setup" in result.stdout
    assert not (workspace / ".hlsl-dev").exists()
    for name in ("llvm-project", "offload-test-suite", "DirectXShaderCompiler"):
        info = cli(workspace, "info", "--in", name)
        assert info.returncode == 0, info.stderr
        assert "not checked out" in info.stdout
        assert "hlsl setup" in info.stdout
    assert not (workspace / ".hlsl-dev").exists()


@pytest.mark.parametrize("group", [
    ("distribution",), ("workspace",), ("tools",),
    ("cross",), ("gpu",), ("gpu", "vulkan"), ("package",),
])
def test_incomplete_command_prints_its_own_help(workspace, group):
    incomplete = cli(workspace, *group)
    expected = cli(workspace, *group, "--help")
    assert incomplete.returncode == 0, incomplete.stderr
    assert incomplete.stdout == expected.stdout
    assert incomplete.stdout.startswith(f"usage: hlsl {' '.join(group)} ")
    assert not (workspace / ".hlsl-dev").exists()


@pytest.mark.parametrize("args", [
    ("configure",), ("build",), ("package", "full"),
    ("package", "precompiled"), ("package", "repro"),
])
def test_build_and_package_help_separates_worktree_options(workspace, args):
    result = cli(workspace, *args, "--help")
    assert result.returncode == 0, result.stderr
    text = result.stdout
    assert "LLVM only (--in LLVM_WORKTREE):" in text
    assert "Standalone offload only (--in OFFLOAD_WORKTREE):" in text
    llvm_options = text.split("LLVM only (--in LLVM_WORKTREE):", 1)[1].split(
        "Standalone offload only (--in OFFLOAD_WORKTREE):", 1
    )[0]
    offload_options = text.split("Standalone offload only (--in OFFLOAD_WORKTREE):", 1)[1]
    assert "--offload WORKTREE" in llvm_options
    assert "--llvm WORKTREE" not in llvm_options
    assert "--llvm WORKTREE" in offload_options
    assert "--dist-prefix PREFIX" in offload_options
    assert "--offload WORKTREE" not in offload_options
    if args[0] in ("configure", "build"):
        assert "All builds (LLVM, DXC, standalone offload):" in text
        assert "LLVM and standalone offload only:" in text
        dxc_options = text.split("LLVM and standalone offload only:", 1)[1].split(
            "LLVM only (--in LLVM_WORKTREE):", 1
        )[0]
        assert "--dxc WORKTREE|DIRECTORY|nix" in dxc_options
    else:
        assert "LLVM and standalone offload packages:" in text


def test_test_and_clean_help_separates_worktree_options(workspace):
    test_help = cli(workspace, "test", "--help")
    assert test_help.returncode == 0, test_help.stderr
    assert "LLVM and standalone offload:" in test_help.stdout
    assert "Standalone offload only (--in OFFLOAD_WORKTREE):" in test_help.stdout
    assert "--llvm WORKTREE" in test_help.stdout.split(
        "Standalone offload only (--in OFFLOAD_WORKTREE):", 1
    )[1]
    clean_help = cli(workspace, "clean", "--help")
    assert clean_help.returncode == 0, clean_help.stderr
    assert "LLVM, DXC and standalone offload:" in clean_help.stdout
    hlsl_options = clean_help.stdout.split(
        "LLVM and standalone offload build trees:", 1
    )[1].split("LLVM build trees", 1)[0]
    assert "--d3d12 {on,off}" in hlsl_options
    assert "--dist" in clean_help.stdout.split(
        "LLVM build trees (--in LLVM_WORKTREE or --all):", 1
    )[1]
    lit_help = cli(workspace, "lit", "--help")
    assert lit_help.returncode == 0 and "LLVM, DXC and standalone offload:" in lit_help.stdout


def test_direct_source_run_does_not_write_bytecode(workspace):
    source = workspace / "scripts"
    shutil.copytree(
        SCRIPTS / "hlsl_cli", source / "hlsl_cli",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copyfile(CLI, source / "hlsl.py")
    result = subprocess.run(
        [sys.executable, str(source / "hlsl.py"), "list"],
        cwd=workspace,
        env={
            **os.environ,
            "HLSL_DEV_ROOT": str(workspace),
            "PYTHONDONTWRITEBYTECODE": "0",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not list(source.rglob("*.pyc"))
    assert not list(source.rglob("__pycache__"))


def test_partial_checkout_and_missing_dependencies_are_reported(workspace):
    llvm = checkout(workspace, "llvm-project", "llvm")
    (llvm / "build").mkdir()
    (llvm / "build/build.ninja").touch()
    result = cli(workspace, "list", cwd=llvm)
    assert result.returncode == 0, result.stderr
    assert "* llvm-project" in result.stdout
    assert "built" in result.stdout
    assert result.stdout.count("(not checked out)") == 3
    info = cli(workspace, "info", "--in", "llvm-project")
    assert info.returncode == 0, info.stderr
    assert str(llvm) in info.stdout
    assert "not checked out" in info.stdout
    assert "clangd db" in info.stdout
    assert "none yet" in info.stdout


def test_info_uses_prebuilt_dxc_when_checkout_is_not_built(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    checkout(workspace, "DirectXShaderCompiler", "dxc")
    prebuilt = workspace / "prebuilt"
    prebuilt.mkdir()
    (prebuilt / "dxc").touch()
    monkeypatch.setenv("HLSL_DXC_PREBUILT_DIR", str(prebuilt))
    result = cli(workspace, "info", "--in", str(llvm))
    assert result.returncode == 0, result.stderr
    assert f"dxc {prebuilt}" in result.stdout
    (prebuilt / "dxc").unlink()
    unavailable = cli(workspace, "info", "--in", str(llvm))
    assert unavailable.returncode == 0, unavailable.stderr
    assert f"dxc {prebuilt}" not in unavailable.stdout
    assert "prebuilt dxc missing" in unavailable.stdout


def test_git_errors_are_reported_not_hidden(workspace, monkeypatch):
    checkout(workspace, "llvm-project", "llvm")
    fake = workspace / "fake-bin"
    fake.mkdir()
    tool = fake / "git"
    tool.write_text("#!/bin/sh\necho broken git >&2\nexit 3\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    result = cli(workspace, "list")
    assert result.returncode != 0
    assert "broken git" in result.stderr
    assert "llvm-project" in result.stderr
    assert "(not checked out)" not in result.stdout


def test_host_platform_override_is_honored(workspace, monkeypatch):
    monkeypatch.setenv("HLSL_HOST_PLATFORM", "linux-arm64")
    refused = cli(workspace, "list", "--platform", "linux-arm64")
    assert refused.returncode != 0
    assert "this machine" in refused.stderr
    allowed = cli(workspace, "list", "--platform", "linux-x64")
    assert allowed.returncode == 0, allowed.stderr


def test_current_and_explicit_worktree_resolution_without_writes(workspace):
    llvm = checkout(workspace, "llvm-project", "llvm")
    feature = workspace / "llvm-project.feature"
    git(llvm, "worktree", "add", "-qb", "feature", str(feature))
    offload = checkout(workspace, "offload-test-suite", "offload")
    git(offload, "checkout", "-qb", "feature")
    checkout(workspace, "DirectXShaderCompiler", "dxc")
    checkout(workspace, "offload-golden-images", "golden")
    (offload / "src").mkdir()
    (offload / "compile_commands.json").symlink_to("build/compile_commands.json")
    state = workspace / ".hlsl-dev/pins"
    state.mkdir(parents=True)
    (state / "offload-test-suite.env").write_text("LLVM=./llvm-project\n")
    before = files(workspace)

    current = cli(workspace, "info", cwd=offload / "src")
    assert current.returncode == 0, current.stderr
    assert f"llvm {feature}" in current.stdout
    assert "legacy pins" in current.stdout
    assert "-> build/compile_commands.json" in current.stdout
    explicit = cli(workspace, "info", "--in=feature")
    assert explicit.returncode == 0, explicit.stderr
    assert f"worktree {feature}" in explicit.stdout
    assert cli(workspace, "info", "--in", str(feature)).returncode == 0
    assert files(workspace) == before


def test_external_worktree_and_platform_are_read_only(workspace, tmp_path):
    llvm = checkout(workspace, "llvm-project", "llvm")
    external = tmp_path.parent / f"{tmp_path.name}.external"
    git(llvm, "worktree", "add", "-qb", "external", str(external))
    (external / "build.windows-x64").mkdir()
    (external / "build.windows-x64/build.ninja").touch()
    before = files(workspace)
    external_before = files(external)
    result = cli(workspace, "list", "--platform", "windows-x64", cwd=external)
    assert result.returncode == 0, result.stderr
    assert f"* {external}" in result.stdout
    assert "built" in result.stdout
    info = cli(workspace, "info", "--platform", "windows-x64", cwd=external)
    assert info.returncode == 0, info.stderr
    assert f"build dir {external / 'build.windows-x64'}" in info.stdout
    assert files(workspace) == before
    assert files(external) == external_before
    assert not (workspace / ".hlsl-dev").exists()


def test_public_preview_and_errors(workspace, monkeypatch):
    missing_info = preview(Request("info", workspace, worktree="llvm-project"))
    assert "not checked out" in missing_info.text
    llvm = checkout(workspace, "llvm-project", "llvm")
    monkeypatch.chdir(workspace)
    report = preview(Request("list", workspace))
    assert "llvm-project" in report.text
    report = preview(Request("info", workspace, worktree="llvm-project"))
    assert str(llvm) in report.text
    assert "build" in report.text
    missing = cli(workspace, "info")
    assert missing.returncode != 0
    assert "--in" in missing.stderr
    bad = cli(workspace, "info", "--in", "no-such-tree")
    assert bad.returncode != 0
    assert "no-such-tree" in bad.stderr
    assert "hlsl list" in bad.stderr
    assert not (workspace / ".hlsl-dev").exists()


def native_inputs(root):
    offload = checkout(root, "offload-test-suite", "offload")
    golden = checkout(root, "offload-golden-images", "golden")
    dxc = root / "dxc-bin"
    dxc.mkdir()
    (dxc / "dxc").touch()
    (dxc / "dxv").touch()
    return offload, golden, dxc


def fake_cmake(root, *, fail=False):
    tool = root / "fake-bin/cmake"
    tool.parent.mkdir(exist_ok=True)
    tool.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "with open(os.environ['CMAKE_LOG'], 'a') as f:\n"
        "    f.write(repr(sys.argv[1:]) + '\\n')\n"
        "if '-B' in sys.argv:\n"
        "    build = pathlib.Path(sys.argv[sys.argv.index('-B') + 1])\n"
        "    build.mkdir(parents=True, exist_ok=True)\n"
        "    (build / 'build.ninja').touch()\n"
        "    (build / 'compile_commands.json').write_text('[]')\n"
        "    for arg in sys.argv:\n"
        "        if arg.startswith('-DCMAKE_INSTALL_PREFIX='):\n"
        "            (build / '.prefix').write_text(arg.split('=', 1)[1])\n"
        "    if (build / '.prefix').is_file():\n"
        "        (build / 'CMakeCache.txt').write_text(\n"
        "            'CMAKE_HOME_DIRECTORY:INTERNAL=' + sys.argv[sys.argv.index('-S') + 1] + '\\n'\n"
        "            + 'CMAKE_INSTALL_PREFIX:PATH=' + (build / '.prefix').read_text() + '\\n'\n"
        "            + 'LLVM_DISTRIBUTION_COMPONENTS:STRING=clang;clang-resource-headers;'\n"
        "              'hlsl-resource-headers;FileCheck;split-file;obj2yaml;not;'\n"
        "              'llvm-headers;LLVMSupport;LLVMObject;cmake-exports;LLVM;clang-cpp\\n'\n"
        "            + 'LLVM_LINK_LLVM_DYLIB:BOOL=ON\\n')\n"
        "if '--build' in sys.argv:\n"
        "    build = pathlib.Path(sys.argv[sys.argv.index('--build') + 1])\n"
        "    if 'install-distribution' in sys.argv and (build / '.prefix').is_file():\n"
        "        prefix = pathlib.Path((build / '.prefix').read_text())\n"
        "        (prefix / 'lib/cmake/llvm').mkdir(parents=True, exist_ok=True)\n"
        "        (prefix / 'lib/cmake/llvm/LLVMConfig.cmake').touch()\n"
        "    if 'install-offload-tools' in sys.argv and (build / '.prefix').is_file():\n"
        "        prefix = pathlib.Path((build / '.prefix').read_text())\n"
        "        (prefix / 'bin').mkdir(parents=True, exist_ok=True)\n"
        "        (prefix / 'bin/offloader').touch()\n"
        "    if 'install-offload-test-suite' in sys.argv and (build / '.prefix').is_file():\n"
        "        prefix = pathlib.Path((build / '.prefix').read_text())\n"
        "        test = prefix / 'share/hlsl-test-suite/test'\n"
        "        test.mkdir(parents=True, exist_ok=True)\n"
        "        (test / 'lit.cfg.py').touch()\n"
        "    if build.parent.name == 'DirectXShaderCompiler':\n"
        "        (build / 'bin').mkdir(exist_ok=True)\n"
        "        (build / 'bin/dxc').touch()\n"
        "        (build / 'bin/dxv').touch()\n"
        f"sys.exit({2 if fail else 0})\n"
    )
    tool.chmod(0o755)
    return tool.parent


def test_native_llvm_preview_and_configure_build(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    offload, golden, dxc = native_inputs(workspace)
    tool_dir = fake_cmake(workspace)
    log = workspace / "cmake.log"
    monkeypatch.setenv("PATH", f"{tool_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("CMAKE_LOG", str(log))
    monkeypatch.setenv(
        "HLSL_CMAKE_FLAGS_LLVM",
        '-G Ninja -DCMAKE_BUILD_TYPE=$HD_BUILD_TYPE '
        '-DLLVM_EXTERNAL_OFFLOADTEST_SOURCE_DIR=$HD_OFFLOAD_SRC '
        '-DGOLDENIMAGE_DIR=$HD_GOLDEN_DIR -DDXC_DIR=$HD_DXC_BIN_DIR '
        '-DLLVM_ENABLE_PROJECTS=clang${HD_SEMI}lld -C $HD_LLVM_SRC/cache.cmake',
    )
    before = files(workspace)
    plan = cli(workspace, "configure", "--in", str(llvm), "--dxc", str(dxc),
               "--offload", str(offload), "--dry-run")
    assert plan.returncode == 0, plan.stderr
    assert "cmake" in plan.stdout and str(golden) in plan.stdout
    assert not log.exists()
    assert files(workspace) == before
    configured = cli(workspace, "configure", "--in", str(llvm), "--dxc", str(dxc),
                     "--offload", str(offload))
    assert configured.returncode == 0, configured.stderr
    assert (llvm / "compile_commands.json").is_symlink()
    assert os.readlink(llvm / "compile_commands.json") == "build/compile_commands.json"
    assert (workspace / ".hlsl-dev/selections").is_dir()
    built = cli(workspace, "build", "clang", "llvm-dis", "--in", str(llvm),
                "--jobs", "2")
    assert built.returncode == 0, built.stderr
    calls = log.read_text().splitlines()
    assert len(calls) == 2
    assert "clang;lld" in calls[0] and str(offload) in calls[0]
    assert "--target', 'clang', 'llvm-dis'" in calls[1]
    assert "--parallel', '2'" in calls[1]
    assert str(dxc) in cli(workspace, "info", "--in", str(llvm)).stdout


def test_invalid_flags_and_no_auto_do_not_mutate(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja -DSECRET=$(touch hacked)")
    bad = cli(workspace, "configure", "--in", str(llvm), "--dry-run")
    assert bad.returncode != 0
    assert not (workspace / "hacked").exists()
    assert not (workspace / ".hlsl-dev").exists()
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja -DVALUE=$HD_UNKNOWN")
    assert cli(workspace, "configure", "--in", str(llvm)).returncode != 0
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja")
    refused = cli(workspace, "build", "clang", "--in", str(llvm), "--no-auto")
    assert refused.returncode != 0
    assert "configure" in refused.stderr
    assert not (llvm / "build").exists()


def test_configure_failure_does_not_save_selection_or_link(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    fake = fake_cmake(workspace, fail=True)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    monkeypatch.setenv("CMAKE_LOG", str(workspace / "cmake.log"))
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja")
    result = cli(workspace, "configure", "--in", str(llvm))
    assert result.returncode != 0
    import json

    saved = json.loads((workspace / ".hlsl-dev/selections/llvm-project.json").read_text())
    assert saved["configured"] == {}
    assert saved["choices"] == {}
    assert saved["attempted"]["build_dir"] == str(llvm / "build")
    assert not (llvm / "compile_commands.json").exists()


def test_existing_legacy_tree_requires_explicit_configure(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    (llvm / "build").mkdir()
    (llvm / "build/build.ninja").touch()
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja")
    result = cli(workspace, "build", "clang", "--in", str(llvm))
    assert result.returncode != 0
    assert "configure" in result.stderr
    assert not (workspace / ".hlsl-dev").exists()


def test_build_auto_configures_then_reset_clears_explicit_choices(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    other = checkout(workspace, "offload-test-suite.other", "offload")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja -DOFFLOAD=$HD_OFFLOAD_SRC")
    monkeypatch.setenv("CMAKE_LOG", str(workspace / "cmake.log"))
    monkeypatch.setenv("PATH", f"{fake_cmake(workspace)}:{os.environ['PATH']}")
    first = cli(workspace, "build", "clang", "--in", str(llvm), "--offload", str(other))
    assert first.returncode == 0, first.stderr
    assert str(other) in cli(workspace, "info", "--in", str(llvm)).stdout
    reset = cli(workspace, "configure", "--reset", "--in", str(llvm))
    assert reset.returncode == 0, reset.stderr
    assert str(workspace / "offload-test-suite") in cli(
        workspace, "info", "--in", str(llvm)
    ).stdout
    assert cli(workspace, "build", "clang", "--in", str(llvm)).returncode == 0
    assert len((workspace / "cmake.log").read_text().splitlines()) == 4


def test_old_state_refuses_mutation_but_allows_preview(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja")
    pins = workspace / ".hlsl-dev/pins"
    pins.mkdir(parents=True)
    (pins / "llvm-project.env").write_text("OFFLOAD=old\n")
    before = files(workspace)
    planned = cli(workspace, "build", "--in", str(llvm), "--dry-run")
    assert planned.returncode == 0
    assert "blocked:" in planned.stdout and "migration" in planned.stdout
    assert files(workspace) == before
    refused = cli(workspace, "configure", "--in", str(llvm))
    assert refused.returncode != 0
    assert "migration" in refused.stderr
    assert files(workspace) == before


def test_lock_contention_and_reentry_use_legacy_key(workspace, monkeypatch):
    from hlsl_cli.native import build_lock

    tree = workspace / "llvm-project"
    lock = workspace / ".hlsl-dev/locks/llvm-project%build.lock"
    monkeypatch.setenv("HLSL_LOCK_TIMEOUT", "0")
    with build_lock(workspace, tree / "build"):
        assert lock.is_file()
        with build_lock(workspace, tree / "build"):
            pass
        other = subprocess.run(
            [sys.executable, "-c", "import fcntl, os, sys; "
             "fd=os.open(sys.argv[1], os.O_RDWR); "
             "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)" , str(lock)],
            capture_output=True, text=True,
        )
        assert other.returncode != 0
    # A different process can acquire it once the outer invocation releases.
    with build_lock(workspace, tree / "build", timeout=0):
        pass
    with build_lock(workspace, tree / "build"):
        contender = subprocess.run(
            [sys.executable, "-c",
             "from hlsl_cli.native import build_lock; import pathlib, sys; "
             "root=pathlib.Path(sys.argv[1]); "
             "build_lock(root, root / 'llvm-project/build', timeout=0).__enter__()",
             str(workspace)],
            env={**os.environ, "PYTHONPATH": str(SCRIPTS)},
            capture_output=True, text=True,
        )
        assert contender.returncode != 0
        assert "timed out" in contender.stderr


def test_child_does_not_inherit_build_lock(workspace, monkeypatch):
    from hlsl_cli.native import build_lock

    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    tool = fake_cmake(workspace) / "cmake"
    script = tool.read_text()
    script = script.replace(
        "sys.exit(0)",
        "import subprocess\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
        "start_new_session=True)\n"
        "pathlib.Path(os.environ['CHILD_PID']).write_text(str(child.pid))\n"
        "sys.exit(0)",
    )
    tool.write_text(script)
    monkeypatch.setenv("PATH", f"{tool.parent}:{os.environ['PATH']}")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja")
    monkeypatch.setenv("CMAKE_LOG", str(workspace / "cmake.log"))
    monkeypatch.setenv("CHILD_PID", str(workspace / "child.pid"))
    result = cli(workspace, "configure", "--in", str(llvm))
    assert result.returncode == 0, result.stderr
    pid = int((workspace / "child.pid").read_text())
    try:
        os.kill(pid, 0)  # Child is still alive after CMake exits.
        with build_lock(workspace, llvm / "build", timeout=0):
            pass
    finally:
        # This fixture's detached process does not need to finish sleeping.
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass


def test_module_preview_matches_cli_without_writes(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja")
    request = Request("build", workspace, worktree=str(llvm), targets=("clang",))
    before = files(workspace)
    plan = preview(request)
    result = cli(workspace, "build", "clang", "--in", str(llvm), "--dry-run")
    assert result.returncode == 0, result.stderr
    assert result.stdout == plan.text
    assert files(workspace) == before


def test_existing_cache_and_unvalidated_build_dir_refuse_reuse(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    offload, _, dxc = native_inputs(workspace)
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja")
    (llvm / "build").mkdir()
    (llvm / "build/CMakeCache.txt").write_text("CMAKE_BUILD_TYPE:STRING=Debug\n")
    old = cli(workspace, "build", "clang", "--in", str(llvm))
    assert old.returncode != 0 and "configure" in old.stderr
    monkeypatch.setenv("CMAKE_LOG", str(workspace / "cmake.log"))
    monkeypatch.setenv("PATH", f"{fake_cmake(workspace)}:{os.environ['PATH']}")
    assert cli(workspace, "configure", "--in", str(llvm)).returncode != 0
    assert cli(workspace, "configure", "--in", str(llvm), "--offload",
               str(offload), "--dxc", str(dxc), "--platform", "native").returncode == 0
    (llvm / "other").mkdir()
    (llvm / "other/build.ninja").touch()
    monkeypatch.setenv("HLSL_BUILD_DIR", "other")
    other = cli(workspace, "build", "clang", "--in", str(llvm))
    assert other.returncode != 0 and "configure" in other.stderr
    assert len((workspace / "cmake.log").read_text().splitlines()) == 1


def test_invalid_saved_json_is_reported_without_mutation(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja")
    selections = workspace / ".hlsl-dev/selections"
    selections.mkdir(parents=True)
    (selections / "llvm-project.json").write_text('{"version": 1, "choices": []}')
    before = files(workspace)
    result = cli(workspace, "build", "--in", str(llvm), "--dry-run")
    assert result.returncode != 0
    assert "invalid selections" in result.stderr
    assert files(workspace) == before


def test_unbuilt_selected_dxc_is_conditional_in_preview(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    dxc = checkout(workspace, "DirectXShaderCompiler", "dxc")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja -DDXC=$HD_DXC_BIN_DIR")
    before = files(workspace)
    previewed = cli(workspace, "build", "clang", "--in", str(llvm),
                    "--dxc", str(dxc), "--dry-run")
    assert previewed.returncode == 0, previewed.stderr
    assert "prerequisite:" in previewed.stdout
    assert "configure (conditional):" in previewed.stdout
    assert files(workspace) == before
    no_auto = cli(workspace, "build", "clang", "--in", str(llvm),
                  "--dxc", str(dxc), "--no-auto")
    assert no_auto.returncode != 0
    assert "--no-auto" in no_auto.stderr
    assert files(workspace) == before
    monkeypatch.setenv("CMAKE_LOG", str(workspace / "cmake.log"))
    monkeypatch.setenv("PATH", f"{fake_cmake(workspace)}:{os.environ['PATH']}")
    built = cli(workspace, "build", "clang", "--in", str(llvm),
                "--dxc", str(dxc))
    assert built.returncode == 0, built.stderr
    assert (dxc / "build/bin/dxc").is_file()
    assert (dxc / "build/bin/dxv").is_file()
    assert (dxc / "compile_commands.json").is_symlink()
    assert len((workspace / "cmake.log").read_text().splitlines()) == 4


def test_codegraph_artifacts_alone_guard_mutation(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja")
    (llvm / ".gitignore").write_text(
        "# >>> codegraph: local index scope, not committed >>>\n"
    )
    before = files(workspace)
    plan = cli(workspace, "configure", "--in", str(llvm), "--dry-run")
    assert plan.returncode == 0, plan.stderr
    result = cli(workspace, "configure", "--in", str(llvm))
    assert result.returncode != 0
    assert ".gitignore" not in result.stdout
    assert "migration" in result.stderr
    assert files(workspace) == before


def test_failed_reconfigure_invalidates_previous_validation(workspace, monkeypatch):
    import json

    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    tool = fake_cmake(workspace) / "cmake"
    monkeypatch.setenv("PATH", f"{tool.parent}:{os.environ['PATH']}")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja -DREV=one")
    monkeypatch.setenv("CMAKE_LOG", str(workspace / "cmake.log"))
    assert cli(workspace, "build", "clang", "--in", str(llvm)).returncode == 0
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja -DREV=two")
    tool.write_text(tool.read_text().replace("sys.exit(0)", "sys.exit(2)"))
    failed = cli(workspace, "configure", "--in", str(llvm))
    assert failed.returncode != 0
    saved = json.loads((workspace / ".hlsl-dev/selections/llvm-project.json").read_text())
    assert saved["configured"] == {}
    # Even when the previous flags are restored, the old build is unsafe.
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja -DREV=one")
    refused = cli(workspace, "build", "clang", "--in", str(llvm))
    assert refused.returncode != 0 and "configure" in refused.stderr
    assert len((workspace / "cmake.log").read_text().splitlines()) == 3
    tool.write_text(tool.read_text().replace("sys.exit(2)", "sys.exit(0)"))
    retry = cli(workspace, "configure", "--in", str(llvm))
    assert retry.returncode == 0, retry.stderr
    assert cli(workspace, "build", "clang", "--in", str(llvm)).returncode == 0


def test_replanned_lock_target_change_fails_closed(workspace, monkeypatch):
    from dataclasses import replace
    from hlsl_cli import native

    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja")
    request = Request("build", workspace, worktree=str(llvm))
    original = native.plan(request)
    changes = iter((original, replace(original, build=llvm / "other")))
    monkeypatch.setattr(native, "plan", lambda _: next(changes))
    with pytest.raises(native.BuildError, match="selection changed"):
        native.execute(request)
    assert not (llvm / "other").exists()
    assert not (llvm / "build").exists()


def test_unopenable_lock_fails_without_running_cmake(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja")
    (workspace / ".hlsl-dev").mkdir()
    (workspace / ".hlsl-dev/locks").touch()  # Cannot mkdir a regular file.
    result = cli(workspace, "build", "clang", "--in", str(llvm))
    assert result.returncode != 0 and "build lock" in result.stderr
    assert not (llvm / "build").exists()


def test_migration_preview_is_private_read_only_and_fresh_is_ready(workspace):
    from hlsl_cli.migration import preview as migration_preview

    before = files(workspace)
    plan = migration_preview(workspace)
    result = cli(workspace, "workspace", "migrate", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert result.stdout == plan.text
    assert "fresh workspace is ready without migration" in result.stdout
    refused = cli(workspace, "workspace", "migrate")
    assert refused.returncode != 0 and "--yes" in refused.stderr
    confirmed = cli(workspace, "workspace", "migrate", "--yes")
    assert confirmed.returncode == 0 and "fresh workspace" in confirmed.stdout
    assert cli(workspace, "workspace", "migrate", "--help").returncode == 0
    assert cli(workspace, "workspace").returncode == 0
    assert files(workspace) == before
    assert not (workspace / ".hlsl-dev").exists()


def test_migration_preview_enumerates_multi_worktree_artifacts(workspace, tmp_path):
    import fcntl

    llvm = checkout(workspace, "llvm-project", "llvm")
    (llvm / ".gitignore").write_text("original\n")
    git(llvm, "add", ".gitignore")
    git(llvm, "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-qm", "ignore")
    external = tmp_path.parent / f"{tmp_path.name}.llvm-external"
    git(llvm, "worktree", "add", "-qb", "external", str(external))
    dxc = checkout(workspace, "DirectXShaderCompiler", "dxc")
    offload = checkout(workspace, "offload-test-suite", "offload")
    checkout(workspace, "offload-golden-images", "golden")
    index = external / ".codegraph"
    index.mkdir()
    (index / "codegraph.db").write_bytes(b"index")
    (index / "codegraph.db-wal").touch()
    (dxc / ".codegraph-old").mkdir()
    (dxc / ".codegraph-old/db").touch()
    (offload / "codegraph.json").write_text("{}")
    (llvm / "codegraph.json").write_text("{}")
    (external / ".gitignore").write_text(
        "original\n\n# >>> codegraph: local index scope, not committed >>>\n"
        "!**/Target/\n# <<< codegraph <<<\n"
    )
    git(external, "update-index", "--skip-worktree", ".gitignore")
    (dxc / ".fixture").write_text("dirty")
    state = workspace / ".hlsl-dev"
    (state / "pins").mkdir(parents=True)
    (state / "pins/llvm-project.env").write_text("LLVM=old\n")
    (state / "settings.env").write_text("MSVC_LICENSE=accepted\n")
    (state / "toolchains").mkdir()
    (state / "toolchains/windows-x64").symlink_to("/absent/toolchain")
    (state / "locks").mkdir()
    lock = state / "locks/llvm-project%build.lock"
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        for tree in (llvm, external, dxc, offload):
            (tree / "build").mkdir()
            (tree / "build/build.ninja").touch()
        (workspace / "hlsl-linux-arm64.tar.gz").touch()
        before = files(workspace)
        external_before = files(external)
        result = cli(workspace, "workspace", "migrate", "--dry-run")
        assert result.returncode == 0, result.stderr
        for path in (
            state / "pins/llvm-project.env", state / "settings.env",
            state / "toolchains/windows-x64", index / "codegraph.db",
            dxc / ".codegraph-old/db", offload / "codegraph.json",
            external / ".gitignore", llvm / "codegraph.json",
        ):
            assert f"  {path}\n" in result.stdout
        assert "active build lock" in result.stdout
        assert "dirty worktree" in result.stdout
        assert "skip-worktree set" not in result.stdout  # Normal legacy state.
        assert "preserved (never migration targets)" in result.stdout
        assert "build/build.ninja" not in result.stdout
        assert "hlsl-linux-arm64.tar.gz" not in result.stdout
        assert f"  {lock}\n" not in result.stdout
        assert files(workspace) == before
        assert files(external) == external_before


def test_migration_preview_flags_unowned_ignore_edits_and_incomplete_markers(workspace):
    llvm = checkout(workspace, "llvm-project", "llvm")
    (llvm / ".gitignore").write_text("original\n")
    git(llvm, "add", ".gitignore")
    git(llvm, "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-qm", "ignore")
    marker = llvm / ".gitignore"
    marker.write_text(
        "unrelated edit\n# >>> codegraph: local index scope, not committed >>>\n"
        "!**/Target/\n# <<< codegraph <<<\n"
    )
    git(llvm, "update-index", "--skip-worktree", ".gitignore")
    before = files(workspace)
    result = cli(workspace, "workspace", "migrate", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "changes outside managed block" in result.stdout
    assert files(workspace) == before
    marker.write_text("# <<< codegraph <<<\n")
    result = cli(workspace, "workspace", "migrate", "--dry-run")
    assert "incomplete or repeated managed ignore markers" in result.stdout
    assert f"  {marker}\n" in result.stdout


def test_real_checkout_native_plan_is_read_only_when_available(monkeypatch):
    root = SCRIPTS.parent
    if not os.getenv("HLSL_CMAKE_FLAGS_LLVM") or not all(
        (root / name).is_dir()
        for name in ("llvm-project/llvm", "offload-test-suite", "offload-golden-images")
    ):
        pytest.skip("real checkout or devenv CMake templates unavailable")
    llvm = root / "llvm-project"
    link = llvm / "compile_commands.json"
    old_link = os.readlink(link) if link.is_symlink() else None
    state = root / ".hlsl-dev"
    old_state = state.exists()
    result = cli(root, "configure", "--in", str(llvm), "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "cost:" in result.stdout and "configure:" in result.stdout
    assert state.exists() == old_state
    assert (os.readlink(link) if link.is_symlink() else None) == old_link


def test_old_dxc_build_is_not_reused_as_a_prerequisite(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    dxc = checkout(workspace, "DirectXShaderCompiler", "dxc")
    (dxc / "build/bin").mkdir(parents=True)
    (dxc / "build/build.ninja").touch()
    (dxc / "build/bin/dxc").touch()
    (dxc / "build/bin/dxv").touch()
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja")
    before = files(workspace)
    selected = cli(workspace, "build", "--in", str(llvm), "--dxc", str(dxc))
    assert selected.returncode != 0
    assert "revalidation" in selected.stderr
    assert files(workspace) == before
    fallback = cli(workspace, "build", "--in", str(llvm), "--dry-run")
    assert fallback.returncode == 0, fallback.stderr
    assert os.environ["HLSL_DXC_PREBUILT_DIR"] in fallback.stdout


def test_flag_change_requires_reconfigure_and_no_auto_refuses(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    monkeypatch.setenv("CMAKE_LOG", str(workspace / "cmake.log"))
    monkeypatch.setenv("PATH", f"{fake_cmake(workspace)}:{os.environ['PATH']}")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja -DMODE=one")
    assert cli(workspace, "configure", "--in", str(llvm)).returncode == 0
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja -DMODE=two")
    before = files(workspace)
    refused = cli(workspace, "build", "clang", "--in", str(llvm), "--no-auto")
    assert refused.returncode != 0 and "configure required" in refused.stderr
    assert files(workspace) == before
    built = cli(workspace, "build", "clang", "--in", str(llvm))
    assert built.returncode == 0, built.stderr
    assert len((workspace / "cmake.log").read_text().splitlines()) == 3


def test_failed_first_configure_retries_auto_without_old_tree_guard(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    tool = fake_cmake(workspace, fail=True) / "cmake"
    monkeypatch.setenv("PATH", f"{tool.parent}:{os.environ['PATH']}")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja")
    monkeypatch.setenv("CMAKE_LOG", str(workspace / "cmake.log"))
    failed = cli(workspace, "build", "clang", "--in", str(llvm))
    assert failed.returncode != 0
    (llvm / "build/CMakeCache.txt").write_text("CMAKE_BUILD_TYPE:STRING=Debug\n")
    tool.write_text(tool.read_text().replace("sys.exit(2)", "sys.exit(0)"))
    retry = cli(workspace, "build", "clang", "--in", str(llvm))
    assert retry.returncode == 0, retry.stderr
    assert len((workspace / "cmake.log").read_text().splitlines()) == 3


def test_dxc_flag_change_rebuilds_prerequisite(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    dxc = checkout(workspace, "DirectXShaderCompiler", "dxc")
    monkeypatch.setenv("PATH", f"{fake_cmake(workspace)}:{os.environ['PATH']}")
    monkeypatch.setenv("CMAKE_LOG", str(workspace / "cmake.log"))
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_DXC", "-G Ninja -DREV=one")
    assert cli(workspace, "build", "clang", "--in", str(llvm),
               "--dxc", str(dxc)).returncode == 0
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_DXC", "-G Ninja -DREV=two")
    before = files(workspace)
    refused = cli(workspace, "build", "clang", "--in", str(llvm),
                  "--dxc", str(dxc), "--no-auto")
    assert refused.returncode != 0 and "DXC configuration needs refresh" in refused.stderr
    assert files(workspace) == before
    updated = cli(workspace, "build", "clang", "--in", str(llvm),
                  "--dxc", str(dxc))
    assert updated.returncode == 0, updated.stderr
    assert len((workspace / "cmake.log").read_text().splitlines()) == 7


def test_failed_first_dxc_configure_retries_auto(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    dxc = checkout(workspace, "DirectXShaderCompiler", "dxc")
    tool = fake_cmake(workspace, fail=True) / "cmake"
    monkeypatch.setenv("PATH", f"{tool.parent}:{os.environ['PATH']}")
    monkeypatch.setenv("CMAKE_LOG", str(workspace / "cmake.log"))
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_DXC", "-G Ninja")
    first = cli(workspace, "build", "clang", "--in", str(llvm),
                "--dxc", str(dxc))
    assert first.returncode != 0
    assert not (llvm / "build").exists()
    tool.write_text(tool.read_text().replace("sys.exit(2)", "sys.exit(0)"))
    retry = cli(workspace, "build", "clang", "--in", str(llvm),
                "--dxc", str(dxc))
    assert retry.returncode == 0, retry.stderr
    assert len((workspace / "cmake.log").read_text().splitlines()) == 5


def test_failed_dxc_reconfigure_can_retry_automatically(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    dxc = checkout(workspace, "DirectXShaderCompiler", "dxc")
    tool = fake_cmake(workspace) / "cmake"
    monkeypatch.setenv("PATH", f"{tool.parent}:{os.environ['PATH']}")
    monkeypatch.setenv("CMAKE_LOG", str(workspace / "cmake.log"))
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_DXC", "-G Ninja -DREV=one")
    assert cli(workspace, "build", "clang", "--in", str(llvm),
               "--dxc", str(dxc)).returncode == 0
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_DXC", "-G Ninja -DREV=two")
    tool.write_text(tool.read_text().replace("sys.exit(0)", "sys.exit(2)"))
    failed = cli(workspace, "build", "clang", "--in", str(llvm),
                 "--dxc", str(dxc))
    assert failed.returncode != 0
    tool.write_text(tool.read_text().replace("sys.exit(2)", "sys.exit(0)"))
    retry = cli(workspace, "build", "clang", "--in", str(llvm),
                "--dxc", str(dxc))
    assert retry.returncode == 0, retry.stderr
    assert len((workspace / "cmake.log").read_text().splitlines()) == 8


def test_native_dxc_cli_plan_configure_and_multi_target_build(workspace, monkeypatch):
    import ast
    import json

    dxc = checkout(workspace, "DirectXShaderCompiler", "dxc")
    tool_dir = fake_cmake(workspace)
    log = workspace / "cmake.log"
    monkeypatch.setenv("PATH", f"{tool_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("CMAKE_LOG", str(log))
    monkeypatch.setenv(
        "HLSL_CMAKE_FLAGS_DXC",
        "-G Ninja -DCMAKE_BUILD_TYPE=$HD_BUILD_TYPE "
        "-C $HD_DXC_SRC/cmake/caches/PredefinedParams.cmake",
    )
    request = Request("build", workspace, cwd=dxc, targets=("dxc", "dxv"), jobs="2")
    before = files(workspace)
    planned = preview(request)
    cli_plan = cli(workspace, "build", "dxc", "dxv", "--jobs", "2", "--dry-run", cwd=dxc)
    assert cli_plan.returncode == 0, cli_plan.stderr
    assert cli_plan.stdout == planned.text
    assert "offload" not in planned.text and "prerequisite:" not in planned.text
    assert files(workspace) == before and not log.exists()
    result = cli(workspace, "build", "dxc", "dxv", "--jobs", "2", cwd=dxc)
    assert result.returncode == 0, result.stderr
    assert (dxc / "compile_commands.json").is_symlink()
    calls = [ast.literal_eval(line) for line in log.read_text().splitlines()]
    assert len(calls) == 2
    assert calls[0][:4] == ["-S", str(dxc), "-B", str(dxc / "build")]
    assert "-DCMAKE_BUILD_TYPE=RelWithDebInfo" in calls[0]
    assert str(dxc / "cmake/caches/PredefinedParams.cmake") in calls[0]
    assert calls[1][-5:] == ["--parallel", "2", "--target", "dxc", "dxv"]
    saved = json.loads((workspace / ".hlsl-dev/selections/DirectXShaderCompiler.json").read_text())
    assert saved["configured"]["build_type"] == "RelWithDebInfo"
    assert saved["configured"]["build_dir"] == str(dxc / "build")
    assert cli(workspace, "build", "--in", str(dxc)).returncode == 0
    assert len(log.read_text().splitlines()) == 3  # No redundant configure.


def test_native_dxc_explicit_configure_choices_reset_and_flags(workspace, monkeypatch):
    import json

    dxc = checkout(workspace, "DirectXShaderCompiler", "dxc")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_DXC", "-G Ninja -DTYPE=$HD_BUILD_TYPE")
    monkeypatch.setenv("CMAKE_LOG", str(workspace / "cmake.log"))
    monkeypatch.setenv("PATH", f"{fake_cmake(workspace)}:{os.environ['PATH']}")
    assert cli(workspace, "configure", "--in", str(dxc), "--build-type", "Debug").returncode == 0
    selection = workspace / ".hlsl-dev/selections/DirectXShaderCompiler.json"
    assert json.loads(selection.read_text())["choices"] == {"build_type": "Debug"}
    assert cli(workspace, "build", "dxc", "--in", str(dxc)).returncode == 0
    assert len((workspace / "cmake.log").read_text().splitlines()) == 2
    reset = cli(workspace, "configure", "--reset", "--in", str(dxc))
    assert reset.returncode == 0, reset.stderr
    assert json.loads(selection.read_text())["choices"] == {}
    assert "-DTYPE=RelWithDebInfo" in (workspace / "cmake.log").read_text()
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_DXC", "-G Ninja -DTYPE=changed")
    before = files(workspace)
    refused = cli(workspace, "build", "--in", str(dxc), "--no-auto")
    assert refused.returncode != 0 and "configure required" in refused.stderr
    assert files(workspace) == before
    assert cli(workspace, "build", "--in", str(dxc)).returncode == 0
    assert len((workspace / "cmake.log").read_text().splitlines()) == 5


def test_native_dxc_rejects_invalid_selections_and_preserved_tree(workspace, monkeypatch):
    dxc = checkout(workspace, "DirectXShaderCompiler", "dxc")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_DXC", "-G Ninja")
    invalid_jobs = cli(workspace, "build", "--in", str(dxc), "--jobs", "0")
    assert invalid_jobs.returncode != 0 and "--jobs" in invalid_jobs.stderr
    (dxc / "build").mkdir()
    (dxc / "build/CMakeCache.txt").write_text("CMAKE_BUILD_TYPE:STRING=Debug\n")
    before = files(workspace)
    for args, error in (
        (("build", "--in", str(dxc)), "configure"),
        (("configure", "--in", str(dxc)), "--build-type"),
        (("configure", "--in", str(dxc), "--offload", "x"), "does not use"),
        (("configure", "--in", str(dxc), "--dxc", "nix"), "does not use"),
        (("configure", "--in", str(dxc), "--reset", "--build-type", "Debug"), "--reset"),
    ):
        result = cli(workspace, *args)
        assert result.returncode != 0 and error in result.stderr
        assert files(workspace) == before
    monkeypatch.setenv("PATH", f"{fake_cmake(workspace)}:{os.environ['PATH']}")
    monkeypatch.setenv("CMAKE_LOG", str(workspace / "cmake.log"))
    configured = cli(workspace, "configure", "--in", str(dxc), "--platform", "native",
                     "--build-type", "Debug")
    assert configured.returncode == 0, configured.stderr
    assert cli(workspace, "build", "dxc", "--in", str(dxc)).returncode == 0
    assert len((workspace / "cmake.log").read_text().splitlines()) == 2


def test_native_dxc_lock_and_failed_reconfigure_fail_closed(workspace, monkeypatch):
    import json
    from hlsl_cli.native import build_lock

    dxc = checkout(workspace, "DirectXShaderCompiler", "dxc")
    tool = fake_cmake(workspace) / "cmake"
    monkeypatch.setenv("PATH", f"{tool.parent}:{os.environ['PATH']}")
    monkeypatch.setenv("CMAKE_LOG", str(workspace / "cmake.log"))
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_DXC", "-G Ninja -DREV=one")
    monkeypatch.setenv("HLSL_LOCK_TIMEOUT", "0")
    with build_lock(workspace, dxc / "build"):
        locked = cli(workspace, "build", "dxc", "--in", str(dxc))
        assert locked.returncode != 0 and "timed out" in locked.stderr
        assert not (dxc / "build").exists()
    assert cli(workspace, "build", "dxc", "--in", str(dxc)).returncode == 0
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_DXC", "-G Ninja -DREV=two")
    tool.write_text(tool.read_text().replace("sys.exit(0)", "sys.exit(2)"))
    failed = cli(workspace, "configure", "--in", str(dxc))
    assert failed.returncode != 0
    selection = workspace / ".hlsl-dev/selections/DirectXShaderCompiler.json"
    assert json.loads(selection.read_text())["configured"] == {}
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_DXC", "-G Ninja -DREV=one")
    refused = cli(workspace, "build", "dxc", "--in", str(dxc))
    assert refused.returncode != 0 and "configure" in refused.stderr
    tool.write_text(tool.read_text().replace("sys.exit(2)", "sys.exit(0)"))
    assert cli(workspace, "configure", "--in", str(dxc)).returncode == 0
    assert cli(workspace, "build", "dxc", "--in", str(dxc)).returncode == 0


def test_native_dxc_invalid_template_and_migration_are_read_only(workspace, monkeypatch):
    dxc = checkout(workspace, "DirectXShaderCompiler", "dxc")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_DXC", "-G Ninja -DVALUE=$HD_UNKNOWN")
    before = files(workspace)
    bad = cli(workspace, "build", "--in", str(dxc), "--dry-run")
    assert bad.returncode != 0 and "placeholder" in bad.stderr
    assert files(workspace) == before
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_DXC", "-G Ninja")
    pins = workspace / ".hlsl-dev/pins"
    pins.mkdir(parents=True)
    (pins / "DirectXShaderCompiler.env").write_text("BUILD_TYPE=Debug\n")
    before = files(workspace)
    plan = cli(workspace, "configure", "--in", str(dxc), "--dry-run")
    assert plan.returncode == 0 and "blocked:" in plan.stdout
    assert files(workspace) == before
    refused = cli(workspace, "configure", "--in", str(dxc))
    assert refused.returncode != 0 and "migration" in refused.stderr
    assert files(workspace) == before


def test_real_checkout_dxc_preview_when_available(monkeypatch):
    root = Path(os.getenv("DEVENV_ROOT", ""))
    dxc = root / "DirectXShaderCompiler"
    if not os.getenv("HLSL_CMAKE_FLAGS_DXC") or not (
        dxc / "cmake/caches/PredefinedParams.cmake"
    ).is_file():
        pytest.skip("real DXC checkout or devenv CMake flags unavailable")
    monkeypatch.setenv("HLSL_DEV_ROOT", str(root))
    link = dxc / "compile_commands.json"
    old_link = os.readlink(link) if link.is_symlink() else None
    state = root / ".hlsl-dev"
    old_state = state.exists()
    for args in (("configure",), ("build", "dxc", "dxv")):
        result = cli(root, *args, "--in", str(dxc), "--dry-run")
        assert result.returncode == 0, result.stderr
        assert "cost:" in result.stdout and str(dxc / "build") in result.stdout
    assert state.exists() == old_state
    assert (os.readlink(link) if link.is_symlink() else None) == old_link


def test_clean_preview_and_atomic_workspace_sweep(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    feature = workspace / "llvm-project.feature"
    git(llvm, "worktree", "add", "-qb", "feature", str(feature))
    dxc = checkout(workspace, "DirectXShaderCompiler", "dxc")
    offload = checkout(workspace, "offload-test-suite", "offload")
    checkout(workspace, "offload-golden-images", "golden")
    for tree in (llvm, feature, dxc, offload):
        (tree / "build").mkdir()
        (tree / "build/CMakeCache.txt").touch()
    for name in ("build-container", "build-d3d12", "build.windows-x64",
                 "build-native-tools"):
        (llvm / name).mkdir()
        (llvm / name / "build.ninja").touch()
    (llvm / "buildbot-notes").mkdir()
    (llvm / "buildbot-notes/keep").touch()
    (llvm / "build-dist/install/lib/cmake/llvm").mkdir(parents=True)
    (llvm / "build-dist/install/lib/cmake/llvm/LLVMConfig.cmake").touch()
    (llvm / "build/compile_commands.json").touch()
    (llvm / "build-container/compile_commands.json").touch()
    (llvm / "compile_commands.json").symlink_to("build/compile_commands.json")
    before = files(workspace)
    previewed = cli(workspace, "clean", "--all", "--all-build-dirs", "--dist", "--dry-run")
    assert previewed.returncode == 0, previewed.stderr
    assert "Would remove" in previewed.stdout
    assert files(workspace) == before
    # Every candidate lock is acquired before a single tree can be removed.
    from hlsl_cli.build_support import build_lock

    monkeypatch.setenv("HLSL_LOCK_TIMEOUT", "0")
    with build_lock(workspace, dxc / "build"):
        blocked = cli(workspace, "clean", "--all", "--all-build-dirs", "--dist", "--yes")
        assert blocked.returncode != 0
        assert str(dxc / "build") in blocked.stderr
        assert not any(line.startswith("Removing") for line in blocked.stdout.splitlines())
        for tree in (llvm, feature, dxc, offload):
            assert (tree / "build").is_dir()
    removed = cli(workspace, "clean", "--all", "--all-build-dirs", "--dist", "--yes")
    assert removed.returncode == 0, removed.stderr
    for tree in (llvm, feature, dxc, offload):
        assert not (tree / "build").exists()
    assert not (llvm / "build-dist").exists()
    assert not (llvm / "build-container").exists()
    assert not (llvm / "build-d3d12").exists()
    assert (llvm / "buildbot-notes/keep").is_file()
    assert not (llvm / "compile_commands.json").is_symlink()
    assert "checkouts" in removed.stdout


@pytest.mark.parametrize("kind, name", (("llvm", "llvm-project"),
                                       ("offload", "offload-test-suite")))
@pytest.mark.parametrize("mode, selected", (("on", "build-d3d12"),
                                           ("off", "build")))
def test_clean_d3d12_selects_only_requested_mode(workspace, kind, name, mode,
                                                 selected):
    tree = checkout(workspace, name, kind)
    for directory in ("build", "build-d3d12"):
        build = tree / directory
        build.mkdir()
        (build / "build.ninja").touch()
    other = "build" if selected == "build-d3d12" else "build-d3d12"
    before = files(workspace)
    command = ("clean", "--in", str(tree), "--d3d12", mode)
    dry = cli(workspace, *command, "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert f"Would remove {tree / selected}\n" in dry.stdout
    assert f"Would remove {tree / other}\n" not in dry.stdout
    assert files(workspace) == before
    cleaned = cli(workspace, *command)
    assert cleaned.returncode == 0, cleaned.stderr
    assert not (tree / selected).exists() and (tree / other / "build.ninja").is_file()


def test_clean_d3d12_all_scopes_worktrees_and_rejects_all_build_dirs(workspace):
    llvm = checkout(workspace, "llvm-project", "llvm")
    offload = checkout(workspace, "offload-test-suite", "offload")
    for tree in (llvm, offload):
        for name in ("build", "build-d3d12"):
            (tree / name).mkdir()
            (tree / name / "build.ninja").touch()
    before = files(workspace)
    refused = cli(workspace, "clean", "--in", str(llvm), "--all-build-dirs",
                  "--d3d12", "on", "--dry-run")
    assert refused.returncode != 0 and "--all-build-dirs" in refused.stderr
    preview = cli(workspace, "clean", "--all", "--d3d12", "on", "--dry-run")
    assert preview.returncode == 0, preview.stderr
    for tree in (llvm, offload):
        assert f"Would remove {tree / 'build-d3d12'}\n" in preview.stdout
        assert f"Would remove {tree / 'build'}\n" not in preview.stdout
    assert files(workspace) == before
    result = cli(workspace, "clean", "--all", "--d3d12", "on", "--yes")
    assert result.returncode == 0, result.stderr
    for tree in (llvm, offload):
        assert not (tree / "build-d3d12").exists()
        assert (tree / "build/build.ninja").is_file()


def test_clean_d3d12_windows_cross_keeps_other_mode(workspace):
    llvm = checkout(workspace, "llvm-project", "llvm")
    for name in ("build.windows-x64", "build-d3d12.windows-x64"):
        (llvm / name).mkdir()
        (llvm / name / "build.ninja").touch()
    command = ("clean", "--in", str(llvm), "--platform", "windows-x64",
               "--d3d12", "on")
    dry = cli(workspace, *command, "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert f"Would remove {llvm / 'build-d3d12.windows-x64'}\n" in dry.stdout
    assert f"Would remove {llvm / 'build.windows-x64'}\n" not in dry.stdout
    result = cli(workspace, *command)
    assert result.returncode == 0, result.stderr
    assert (llvm / "build.windows-x64/build.ninja").is_file()
    assert not (llvm / "build-d3d12.windows-x64").exists()


def test_clean_scopes_links_and_tracked_source(workspace):
    llvm = checkout(workspace, "llvm-project", "llvm")
    dxc = checkout(workspace, "DirectXShaderCompiler", "dxc")
    for name in ("build", "build-container", "build.windows-x64"):
        (llvm / name).mkdir()
        (llvm / name / "CMakeCache.txt").touch()
    (llvm / "build/compile_commands.json").touch()
    (llvm / "build-container/compile_commands.json").touch()
    (llvm / "compile_commands.json").symlink_to("build/compile_commands.json")
    (dxc / "build").mkdir()
    (dxc / "build/build.ninja").touch()
    (llvm / "clang/CMakeCache.txt").touch()  # Tracked source, never a build.
    native = cli(workspace, "clean", "--in", str(llvm))
    assert native.returncode == 0, native.stderr
    assert (llvm / "compile_commands.json").is_symlink()
    assert os.readlink(llvm / "compile_commands.json") == "build-container/compile_commands.json"
    assert (llvm / "build-container").exists()
    assert (llvm / "build.windows-x64").exists()
    cross = cli(workspace, "clean", "--in", str(llvm), "--platform", "windows-x64")
    assert cross.returncode == 0, cross.stderr
    assert not (llvm / "build.windows-x64").exists()
    scoped = cli(workspace, "clean", "--all", "llvm", "--yes")
    assert scoped.returncode == 0, scoped.stderr
    assert (dxc / "build").exists()
    broad = cli(workspace, "clean", "--in", str(llvm), "--all-build-dirs")
    assert broad.returncode == 0, broad.stderr
    assert (llvm / "clang/CMakeCache.txt").exists()
    assert not (llvm / "build-container").exists()
    assert not (llvm / "compile_commands.json").exists()
    assert cli(workspace, "clean", "--in", str(llvm), "--all-build-dirs",
               "--platform", "windows-x64").returncode != 0
    assert cli(workspace, "clean", "--all", "--in", str(llvm)).returncode != 0
    assert cli(workspace, "clean", "llvm").returncode != 0


def test_clean_refuses_source_symlink_and_unopenable_lock(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    (llvm / "build").mkdir()
    (llvm / "build/CMakeCache.txt").touch()
    outside = workspace / "outside"
    outside.mkdir()
    (outside / "build.ninja").touch()
    (llvm / "linked-build").symlink_to(outside, target_is_directory=True)
    original = files(workspace)
    monkeypatch.setenv("HLSL_BUILD_DIR", str(llvm))
    result = cli(workspace, "clean", "--in", str(llvm))
    assert result.returncode != 0 and "refusing" in result.stderr
    assert files(workspace) == original
    monkeypatch.setenv("HLSL_BUILD_DIR", "linked-build")
    result = cli(workspace, "clean", "--in", str(llvm))
    assert result.returncode != 0 and "symlink" in result.stderr
    assert files(workspace) == original
    monkeypatch.delenv("HLSL_BUILD_DIR")
    locks = workspace / ".hlsl-dev/locks"
    locks.mkdir(parents=True)
    (locks / "llvm-project%build.lock").mkdir()
    result = cli(workspace, "clean", "--in", str(llvm))
    assert result.returncode != 0 and "lock" in result.stderr
    assert (llvm / "build").exists()


def test_clean_locks_distribution_prefix_even_when_nested(workspace, monkeypatch):
    from hlsl_cli.build_support import build_lock

    llvm = checkout(workspace, "llvm-project", "llvm")
    prefix = llvm / "build-dist/install"
    (prefix / "lib/cmake/llvm").mkdir(parents=True)
    (prefix / "lib/cmake/llvm/LLVMConfig.cmake").touch()
    (llvm / "build").mkdir()
    (llvm / "build/CMakeCache.txt").touch()
    monkeypatch.setenv("HLSL_LOCK_TIMEOUT", "0")
    with build_lock(workspace, prefix):
        blocked = cli(workspace, "clean", "--in", str(llvm), "--dist")
        assert blocked.returncode != 0 and str(prefix) in blocked.stderr
        assert (llvm / "build").exists()
        assert prefix.exists()
    result = cli(workspace, "clean", "--in", str(llvm), "--dist")
    assert result.returncode == 0, result.stderr
    assert not prefix.exists()
    assert not (llvm / "build").exists()


def test_clean_markerless_probe_and_external_override(workspace, monkeypatch):
    from hlsl_cli.build_support import build_lock

    llvm = checkout(workspace, "llvm-project", "llvm")
    checkout(workspace, "DirectXShaderCompiler", "dxc")
    external = workspace / "external-build"
    external.mkdir()
    (external / "CMakeCache.txt").touch()
    (llvm / "custom-dir").mkdir()
    (llvm / "custom-dir/CMakeCache.txt").touch()
    monkeypatch.setenv("HLSL_BUILD_DIR", str(external))
    assert cli(workspace, "clean", "--in", str(llvm), "--all-build-dirs",
               "--dry-run").returncode == 0
    assert external.exists()
    result = cli(workspace, "clean", "--in", str(llvm), "--all-build-dirs")
    assert result.returncode == 0, result.stderr
    assert not external.exists() and not (llvm / "custom-dir").exists()
    # An all-worktree sweep does not inherit the single-worktree override.
    external.mkdir()
    (external / "CMakeCache.txt").touch()
    assert cli(workspace, "clean", "--all", "--all-build-dirs", "--yes").returncode == 0
    assert external.exists()
    monkeypatch.setenv("HLSL_BUILD_DIR", "../external-build")
    blocked = cli(workspace, "clean", "--in", str(llvm))
    assert blocked.returncode != 0 and external.exists()
    monkeypatch.delenv("HLSL_BUILD_DIR")
    pending = llvm / "build-pending"
    pending.mkdir()
    with build_lock(workspace, pending):
        result = cli(workspace, "clean", "--in", str(llvm), "--all-build-dirs",
                     "--dry-run")
        assert "Would check build lock on" in result.stdout
        assert f"Would remove {pending}" not in result.stdout
        result = cli(workspace, "clean", "--in", str(llvm), "--all-build-dirs")
        assert result.returncode != 0 and pending.exists()
    result = cli(workspace, "clean", "--in", str(llvm), "--all-build-dirs")
    assert result.returncode == 0 and pending.exists()  # Unrecognized tree stays.


def test_clean_replans_after_lock_before_removal(workspace, monkeypatch):
    from contextlib import contextmanager
    from hlsl_cli import clean

    llvm = checkout(workspace, "llvm-project", "llvm")
    (llvm / "build").mkdir()
    (llvm / "build/build.ninja").touch()
    real_lock = clean.build_lock

    @contextmanager
    def changed(root, directory, timeout=None):
        with real_lock(root, directory, timeout=timeout):
            # Simulate a new configured tree appearing during lock acquisition.
            (llvm / "build-container").mkdir(exist_ok=True)
            (llvm / "build-container/build.ninja").touch()
            yield

    monkeypatch.setattr(clean, "build_lock", changed)
    request = Request("clean", workspace, worktree=str(llvm), all_build_dirs=True)
    with pytest.raises(clean.BuildError, match="selection changed"):
        clean.execute(request)
    assert (llvm / "build").exists() and (llvm / "build-container").exists()


def test_clean_preserves_tracked_distribution_and_unmarked_notes(workspace):
    llvm = checkout(workspace, "llvm-project", "llvm")
    (llvm / "build-dist").mkdir()
    (llvm / "build-dist/README").touch()
    git(llvm, "add", "build-dist/README")
    (llvm / "build-dist/CMakeCache.txt").touch()
    result = cli(workspace, "clean", "--in", str(llvm), "--dist")
    assert result.returncode != 0 and "Git-tracked" in result.stderr
    assert (llvm / "build-dist/README").exists()
    git(llvm, "rm", "--cached", "build-dist/README")
    (llvm / "build-dist/CMakeCache.txt").unlink()
    result = cli(workspace, "clean", "--in", str(llvm), "--dist")
    assert result.returncode == 0 and (llvm / "build-dist/README").exists()


def elf(path, *, executable=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x7fELF\x02\x01\x01\x00fixture\n")
    path.chmod(0o755 if executable else 0o644)


@pytest.fixture
def ninja_trim(workspace):
    """A real, disposable Ninja graph with files representing built outputs."""
    llvm = checkout(workspace, "llvm-project", "llvm")
    build = llvm / "build"
    build.mkdir()
    (build / "build.ninja").write_text(
        "rule link\n"
        "  command = touch $out\n"
        "build bin/keeper: link\n"
        "build bin/dropper: link\n"
        "build lib/libDropped.so.1: link\n"
        "build unittests/KeptTests: link\n"
        "build check-hlsl: phony bin/keeper\n"
        "build check-clang: phony unittests/KeptTests\n"
        "build check-other: phony lib/libDropped.so.1\n"
    )
    subprocess.run(["ninja", "-C", str(build), "-t", "graph", "check-hlsl"],
                   check=True, capture_output=True)
    for name in ("bin/keeper", "bin/dropper", "lib/libDropped.so.1",
                 "unittests/KeptTests", "install/bin/dropper",
                 "CMakeFiles/dropper"):
        elf(build / name)
    (build / "lib/libDropped.so").symlink_to("libDropped.so.1")
    (build / "bin/keeper-alias").symlink_to("keeper")
    (build / "bin/unrelated-broken").symlink_to("nonesuch")
    (build / "bin/llvm-lit").write_text("#!/bin/sh\necho lit\n")
    (build / "bin/llvm-lit").chmod(0o755)
    elf(build / "bin/untracked")
    elf(build / "lib/nonexecutable", executable=False)
    return llvm, build


def test_trim_preview_and_execution_preserve_unrelated_artifacts(ninja_trim, workspace):
    llvm, build = ninja_trim
    before = files(llvm)
    dry = cli(workspace, "trim", "--in", str(llvm), "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert "would remove 2 binaries" in dry.stdout
    assert "bin/dropper" in dry.stdout and "lib/libDropped.so.1" in dry.stdout
    assert "alias  lib/libDropped.so" in dry.stdout
    assert files(llvm) == before
    assert not (workspace / ".hlsl-dev").exists()
    done = cli(workspace, "trim", "--in", str(llvm))
    assert done.returncode == 0, done.stderr
    assert "removed 2 binaries" in done.stdout
    assert not (build / "bin/dropper").exists()
    assert not (build / "lib/libDropped.so.1").exists()
    assert not (build / "lib/libDropped.so").is_symlink()
    for name in ("bin/keeper", "unittests/KeptTests", "install/bin/dropper",
                 "CMakeFiles/dropper", "bin/llvm-lit", "bin/untracked",
                 "lib/nonexecutable"):
        assert (build / name).exists(), name
    assert (build / "bin/keeper-alias").is_symlink()
    assert (build / "bin/unrelated-broken").is_symlink()
    again = cli(workspace, "trim", "--in", str(llvm))
    assert again.returncode == 0 and "nothing to trim" in again.stdout


def test_trim_named_targets_replace_defaults(ninja_trim, workspace):
    llvm, build = ninja_trim
    chosen = cli(workspace, "trim", "check-other", "--in", str(llvm),
                 "--dry-run")
    assert chosen.returncode == 0, chosen.stderr
    assert "bin/keeper" in chosen.stdout
    assert "unittests/KeptTests" in chosen.stdout
    assert "lib/libDropped.so.1" not in chosen.stdout
    assert (build / "bin/keeper").exists()
    both = cli(workspace, "trim", "check-hlsl", "check-other",
               "--in", str(llvm), "--dry-run")
    assert both.returncode == 0, both.stderr
    assert "bin/keeper" not in both.stdout
    assert "unittests/KeptTests" in both.stdout


@pytest.mark.parametrize("targets", [("nonesuch",), ("check-hlsl", "nonesuch"),
                                    ("--bad",), ("../outside",)])
def test_trim_invalid_targets_never_delete(ninja_trim, workspace, targets):
    llvm, build = ninja_trim
    before = files(llvm)
    result = cli(workspace, "trim", *targets, "--in", str(llvm))
    assert result.returncode != 0
    assert "target" in result.stderr or "unrecognized arguments" in result.stderr
    assert files(llvm) == before
    assert not (workspace / ".hlsl-dev").exists()


def test_trim_missing_graph_and_migration_are_safe(ninja_trim, workspace):
    llvm, build = ninja_trim
    (build / "build.ninja").unlink()
    refused = cli(workspace, "trim", "--in", str(llvm))
    assert refused.returncode != 0 and "not a configured Ninja" in refused.stderr
    (build / "build.ninja").write_text("build check-hlsl: phony\n")
    before = files(llvm)
    refused = cli(workspace, "trim", "--in", str(llvm))
    assert refused.returncode != 0 and "check-clang" in refused.stderr
    assert files(llvm) == before
    (workspace / ".hlsl-dev/pins").mkdir(parents=True)
    (workspace / ".hlsl-dev/pins/legacy.env").touch()
    preview = cli(workspace, "trim", "check-hlsl", "--in", str(llvm),
                  "--dry-run")
    assert preview.returncode == 0 and "blocked:" in preview.stdout
    run = cli(workspace, "trim", "check-hlsl", "--in", str(llvm))
    assert run.returncode != 0 and "migration" in run.stderr


def test_trim_lock_contention_has_no_deletions(ninja_trim, workspace, monkeypatch):
    from hlsl_cli.native import build_lock

    llvm, build = ninja_trim
    monkeypatch.setenv("HLSL_LOCK_TIMEOUT", "0")
    with build_lock(workspace, build):
        result = cli(workspace, "trim", "--in", str(llvm))
        assert result.returncode != 0
        assert "build lock" in result.stderr
        assert (build / "bin/dropper").exists()
    assert cli(workspace, "trim", "--in", str(llvm)).returncode == 0


def test_trim_rechecks_graph_under_lock(ninja_trim, workspace, monkeypatch):
    from contextlib import contextmanager
    from hlsl_cli import trim

    llvm, build = ninja_trim
    original = trim.build_lock

    @contextmanager
    def changed_graph(root, directory):
        with original(root, directory):
            with (build / "build.ninja").open("a") as output:
                output.write("build new-target: phony bin/dropper\n")
            yield

    monkeypatch.setattr(trim, "build_lock", changed_graph)
    with pytest.raises(ValueError, match="changed while waiting"):
        trim.execute(Request("trim", workspace, worktree=str(llvm)))
    assert (build / "bin/dropper").exists()


def test_trim_offload_defaults_and_dxc_requires_targets(workspace):
    offload = checkout(workspace, "offload-test-suite", "offload")
    dxc = checkout(workspace, "DirectXShaderCompiler", "dxc")
    for tree in (offload, dxc):
        build = tree / "build"
        build.mkdir()
        (build / "build.ninja").write_text(
            "rule link\n  command = touch $out\n"
            "build bin/kept: link\nbuild bin/unused: link\n"
            "build check-hlsl: phony bin/kept\n"
        )
        elf(build / "bin/kept")
        elf(build / "bin/unused")
    preview = cli(workspace, "trim", "--in", str(offload), "--dry-run")
    assert preview.returncode == 0 and "would remove 1 binaries" in preview.stdout
    refused = cli(workspace, "trim", "--in", str(dxc))
    assert refused.returncode != 0 and "no default keep targets" in refused.stderr
    named = cli(workspace, "trim", "check-hlsl", "--in", str(dxc))
    assert named.returncode == 0 and not (dxc / "build/bin/unused").exists()
    assert (offload / "build/bin/unused").exists()


def test_trim_does_not_follow_symlinked_directory(ninja_trim, workspace):
    llvm, build = ninja_trim
    other = workspace / "elsewhere"
    other.mkdir()
    elf(other / "dropper")
    (build / "outside").symlink_to(other, target_is_directory=True)
    with (build / "build.ninja").open("a") as output:
        output.write("build outside/dropper: phony\n")
    result = cli(workspace, "trim", "--in", str(llvm))
    assert result.returncode == 0, result.stderr
    assert (other / "dropper").exists()
    assert (build / "outside").is_symlink()


def standalone_flags(monkeypatch):
    monkeypatch.setenv(
        "HLSL_CMAKE_FLAGS_LLVM",
        "-G Ninja -DCMAKE_BUILD_TYPE=$HD_BUILD_TYPE "
        "-DCMAKE_INSTALL_PREFIX=$HD_INSTALL_PREFIX "
        "-DDXC_DIR=$HD_DXC_BIN_DIR "
        "-C $HD_OFFLOAD_SRC/cmake/caches/StandaloneDistribution.cmake",
    )
    monkeypatch.setenv(
        "HLSL_CMAKE_FLAGS_OFFLOAD",
        "-G Ninja -DCMAKE_PREFIX_PATH=$HD_LLVM_CMAKE_DIR "
        "-DLLVM_MAIN_SRC_DIR=$HD_LLVM_SRC/llvm "
        "-DGOLDENIMAGE_DIR=$HD_GOLDEN_DIR -DDXC_DIR=$HD_DXC_BIN_DIR "
        "-DCMAKE_INSTALL_PREFIX=$HD_INSTALL_PREFIX",
    )


def test_standalone_auto_installs_selected_distribution_and_builds(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    alt_llvm = checkout(workspace, "llvm-project.other", "llvm")
    offload, golden, dxc = native_inputs(workspace)
    alt_offload = checkout(workspace, "offload-test-suite.other", "offload")
    tools = fake_cmake(workspace)
    log = workspace / "cmake.log"
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setenv("CMAKE_LOG", str(log))
    standalone_flags(monkeypatch)
    request = Request("build", workspace, worktree=str(alt_offload),
                      llvm=str(alt_llvm), dxc=str(dxc), targets=("check-hlsl",))
    before = files(workspace)
    plan = preview(request)
    result = cli(workspace, "build", "check-hlsl", "--in", str(alt_offload),
                 "--llvm", str(alt_llvm), "--dxc", str(dxc), "--dry-run")
    assert result.returncode == 0, result.stderr
    assert plan.text == result.stdout
    assert "missing" in plan.text and "automatically provision" in plan.text
    assert str(offload / "cmake/caches/StandaloneDistribution.cmake") in plan.text
    assert str(alt_llvm / "build/install/lib/cmake/llvm") in plan.text
    assert str(golden) in plan.text and str(dxc) in plan.text
    assert not log.exists() and files(workspace) == before

    built = cli(workspace, "build", "check-hlsl", "--in", str(alt_offload),
                "--llvm", str(alt_llvm), "--dxc", str(dxc))
    assert built.returncode == 0, built.stderr
    assert (alt_llvm / "build/install/lib/cmake/llvm/LLVMConfig.cmake").is_file()
    assert (alt_offload / "compile_commands.json").is_symlink()
    calls = log.read_text().splitlines()
    assert len(calls) == 4
    assert "install-distribution" in calls[1] and "--target', 'check-hlsl'" in calls[3]
    assert str(alt_llvm) in calls[0] and str(offload) in calls[0]
    assert str(alt_llvm / "build/install/lib/cmake/llvm") in calls[2]
    assert not (llvm / "build").exists() and not (offload / "build").exists()
    repeat = cli(workspace, "build", "check-hlsl", "--in", str(alt_offload))
    assert repeat.returncode == 0, repeat.stderr
    assert "installed" in repeat.stdout
    assert len(log.read_text().splitlines()) == 5  # No stale auto refresh.


def test_standalone_external_prefix_and_no_auto_are_safe(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    offload, _, dxc = native_inputs(workspace)
    standalone_flags(monkeypatch)
    before = files(workspace)
    refused = cli(workspace, "build", "--in", str(offload), "--llvm", str(llvm),
                  "--dxc", str(dxc), "--no-auto", "--dry-run")
    assert refused.returncode != 0 and "--no-auto" in refused.stderr
    external = workspace / "external"
    external.mkdir()
    missing = cli(workspace, "configure", "--in", str(offload), "--dxc", str(dxc),
                  "--dist-prefix", str(external), "--dry-run")
    assert missing.returncode != 0 and "external prefix" in missing.stderr
    assert files(workspace) == {**before, "external": ("dir",)}
    config = external / "lib/cmake/llvm/LLVMConfig.cmake"
    config.parent.mkdir(parents=True)
    config.touch()
    planned = cli(workspace, "configure", "--in", str(offload), "--dxc", str(dxc),
                  "--dist-prefix", str(external), "--dry-run")
    assert planned.returncode == 0, planned.stderr
    assert f"llvm dist {external} (external)" in planned.stdout
    assert "install-distribution" not in planned.stdout
    assert not (llvm / "build").exists()
    tools = fake_cmake(workspace)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setenv("CMAKE_LOG", str(workspace / "cmake.log"))
    assert cli(workspace, "configure", "--in", str(offload), "--dxc",
               str(dxc), "--dist-prefix", str(external)).returncode == 0
    assert config.is_file() and not (llvm / "build").exists()
    assert str(external) in cli(workspace, "build", "--in", str(offload),
                               "--dry-run").stdout


def test_devenv_llvm_distribution_components_cover_standalone_build():
    native = os.getenv("HLSL_CMAKE_FLAGS_LLVM", "")
    windows = os.getenv("HLSL_CMAKE_FLAGS_CROSS_LLVM_WINDOWS", "")
    if not native or not windows:
        pytest.skip("enter the updated devenv shell for CMake flag templates")
    required = ("clang", "hlsl-resource-headers", "llvm-headers", "LLVMSupport",
                "LLVMObject", "LLVMOption", "cmake-exports")
    for name in required:
        assert name in native and name in windows
    assert "LLVMMC${HD_SEMI}LLVMOption" in native
    assert "LLVMMC${HD_SEMI}LLVMOption" in windows
    assert "LLVM${HD_SEMI}clang-cpp" in native
    assert "LLVM${HD_SEMI}clang-cpp" not in windows


def test_distribution_install_requires_configured_llvm_build(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    offload, _, dxc = native_inputs(workspace)
    standalone_flags(monkeypatch)
    tools = fake_cmake(workspace)
    log = workspace / "cmake.log"
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setenv("CMAKE_LOG", str(log))
    command = ("distribution", "install", "--in", str(llvm))
    before = files(workspace)
    for extra in (("--dry-run",), ()):
        refused = cli(workspace, *command, *extra)
        assert refused.returncode != 0 and "hlsl configure" in refused.stderr
    assert not log.exists() and files(workspace) == before

    configured = cli(workspace, "configure", "--in", str(llvm), "--dxc", str(dxc))
    assert configured.returncode == 0, configured.stderr
    assert len(log.read_text().splitlines()) == 1
    cache = llvm / "build/CMakeCache.txt"
    cache.write_text(cache.read_text().replace(
        "LLVM_DISTRIBUTION_COMPONENTS:STRING=",
        "LLVM_DISTRIBUTION_COMPONENTS:UNINITIALIZED=",
    ).replace("LLVM_LINK_LLVM_DYLIB:BOOL=",
              "LLVM_LINK_LLVM_DYLIB:INTERNAL="))
    dry = cli(workspace, *command, "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert "--target install-distribution" in dry.stdout
    assert "configure:" not in dry.stdout and "prerequisite:" not in dry.stdout
    assert len(log.read_text().splitlines()) == 1
    first = cli(workspace, *command)
    assert first.returncode == 0, first.stderr
    assert (llvm / "build/install/lib/cmake/llvm/LLVMConfig.cmake").is_file()
    assert len(log.read_text().splitlines()) == 2
    second = cli(workspace, *command)
    assert second.returncode == 0, second.stderr
    assert len(log.read_text().splitlines()) == 3  # Reinstall, never reconfigure.
    assert cli(workspace, "build", "--in", str(offload), "--dxc", str(dxc),
               "--no-auto", "--dry-run").returncode != 0  # Offload still needs configure.
    monkeypatch.setenv("HLSL_DIST_PREFIX", str(workspace / "external"))
    assert cli(workspace, *command, "--dry-run").returncode == 0
    assert len(log.read_text().splitlines()) == 3


def test_distribution_install_never_changes_configured_selections(workspace,
                                                                    monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    standalone_flags(monkeypatch)
    tools = fake_cmake(workspace)
    log = workspace / "cmake.log"
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setenv("CMAKE_LOG", str(log))
    assert cli(workspace, "configure", "--in", str(llvm)).returncode == 0
    before = (workspace / ".hlsl-dev/selections/llvm-project.json").read_bytes()
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja -DCHANGED=ON")
    monkeypatch.setenv("HLSL_OFFLOAD", "missing-worktree")
    monkeypatch.setenv("HLSL_DXC", "missing-dxc")
    result = cli(workspace, "distribution", "install", "--in", str(llvm),
                 "--jobs", "2")
    assert result.returncode == 0, result.stderr
    assert "--parallel 2 --target install-distribution" in result.stdout
    assert len(log.read_text().splitlines()) == 2  # Configure once, install once.
    assert (workspace / ".hlsl-dev/selections/llvm-project.json").read_bytes() == before
    assert not (workspace / "missing-worktree").exists()


def test_distribution_install_rejects_wrong_cache_and_unvalidated_build(workspace,
                                                                           monkeypatch):
    import json

    llvm = checkout(workspace, "llvm-project", "llvm")
    native_inputs(workspace)
    standalone_flags(monkeypatch)
    tools = fake_cmake(workspace)
    log = workspace / "cmake.log"
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setenv("CMAKE_LOG", str(log))
    assert cli(workspace, "configure", "--in", str(llvm)).returncode == 0
    cache = llvm / "build/CMakeCache.txt"
    original = cache.read_text()
    for old, new, expected in (
        (str(llvm / "build/install"), str(workspace / "other-install"), "prefix"),
        (str(llvm / "llvm"), str(workspace / "other-source"), "source"),
    ):
        cache.write_text(original.replace(old, new))
        for extra in (("--dry-run",), ()):
            refused = cli(workspace, "distribution", "install", "--in", str(llvm),
                          *extra)
            assert refused.returncode != 0 and expected in refused.stderr
        assert len(log.read_text().splitlines()) == 1
    cache.write_text(original +
                     "LLVM_DISTRIBUTION_COMPONENTS:STRING=clang;hlsl-resource-headers;"
                     "FileCheck;split-file;obj2yaml;not\n"
                     "LLVM_LINK_LLVM_DYLIB:BOOL=ON\n")
    for extra in (("--dry-run",), ()):
        missing = cli(workspace, "distribution", "install", "--in", str(llvm),
                      *extra)
        assert missing.returncode != 0 and "cmake-exports" in missing.stderr
    assert len(log.read_text().splitlines()) == 1
    cache.write_text(original)
    selection = workspace / ".hlsl-dev/selections/llvm-project.json"
    saved = json.loads(selection.read_text())
    saved["configured"] = {}
    selection.write_text(json.dumps(saved))
    refused = cli(workspace, "distribution", "install", "--in", str(llvm),
                  "--dry-run")
    assert refused.returncode != 0 and "revalidation" in refused.stderr
    assert len(log.read_text().splitlines()) == 1


def test_offload_distribution_install_installs_own_prefix(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    offload, _, dxc = native_inputs(workspace)
    standalone_flags(monkeypatch)
    tools = fake_cmake(workspace)
    log = workspace / "cmake.log"
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setenv("CMAKE_LOG", str(log))
    command = ("distribution", "install", "--in", str(offload))
    before = files(workspace)
    denied = cli(workspace, *command, "--dry-run")
    assert denied.returncode != 0 and "hlsl configure" in denied.stderr
    assert files(workspace) == before and not log.exists()

    llvm_config = cli(workspace, "configure", "--in", str(llvm), "--dxc", str(dxc))
    assert llvm_config.returncode == 0, llvm_config.stderr
    assert cli(workspace, "distribution", "install", "--in", str(llvm)).returncode == 0
    offload_config = cli(workspace, "configure", "--in", str(offload),
                         "--llvm", str(llvm), "--dxc", str(dxc))
    assert offload_config.returncode == 0, offload_config.stderr
    assert len(log.read_text().splitlines()) == 3
    dry = cli(workspace, *command, "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert f"install {offload / 'build/install'}" in dry.stdout
    assert "install-distribution" not in dry.stdout
    assert "--target install-offload-tools install-offload-test-suite" in dry.stdout
    assert "configure:" not in dry.stdout and "prerequisite:" not in dry.stdout
    assert len(log.read_text().splitlines()) == 3

    installed = cli(workspace, *command)
    assert installed.returncode == 0, installed.stderr
    assert (llvm / "build/install/lib/cmake/llvm/LLVMConfig.cmake").is_file()
    assert (offload / "build/install/bin/offloader").is_file()
    assert (offload / "build/install/share/hlsl-test-suite/test/lit.cfg.py").is_file()
    assert len(log.read_text().splitlines()) == 4
    repeated = cli(workspace, *command)
    assert repeated.returncode == 0, repeated.stderr
    assert len(log.read_text().splitlines()) == 5  # Incremental reinstall of offload only.


def test_distribution_install_help_rejects_dependency_overrides(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    offload, _, dxc = native_inputs(workspace)
    standalone_flags(monkeypatch)
    help_text = cli(workspace, "distribution", "install", "--help").stdout
    index = cli(workspace, "distribution", "--help")
    assert index.returncode == 0 and "{install}" in index.stdout
    old = cli(workspace, "distribution", "refresh", "--in", str(llvm),
              "--dry-run")
    assert old.returncode != 0 and "invalid choice" in old.stderr
    assert "already configured" in help_text
    assert "Does not change selections, invoke configure, or provision dependencies" in help_text
    for flag in ("--llvm", "--offload", "--dxc", "--dist-prefix", "--build-type",
                 "--no-auto"):
        assert flag not in help_text
        refused = cli(workspace, "distribution", "install", "--in", str(llvm),
                      flag, "ignored" if flag != "--no-auto" else "--dry-run")
        assert refused.returncode != 0 and "unrecognized arguments" in refused.stderr
    assert cli(workspace, "distribution", "install", "--in", str(offload),
               "--dry-run").returncode != 0  # Both kinds require an existing build.


def test_distribution_reuses_each_d3d12_build_and_clean_locks_prefix(workspace,
                                                                       monkeypatch):
    from hlsl_cli.build_support import build_lock

    llvm = checkout(workspace, "llvm-project", "llvm")
    offload, _, dxc = native_inputs(workspace)
    standalone_flags(monkeypatch)
    monkeypatch.setenv("HLSL_DXC", str(dxc))
    tools = fake_cmake(workspace)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setenv("CMAKE_LOG", str(workspace / "cmake.log"))

    for mode, name in (("off", "build"), ("on", "build-d3d12")):
        prefix = llvm / name / "install"
        configured = cli(workspace, "configure", "--in", str(llvm),
                         "--dxc", str(dxc), "--d3d12", mode)
        assert configured.returncode == 0, configured.stderr
        dry = cli(workspace, "distribution", "install", "--in", str(llvm),
                  "--d3d12", mode, "--dry-run")
        assert dry.returncode == 0, dry.stderr
        assert f"install {prefix}" in dry.stdout
        assert not prefix.exists()
        refreshed = cli(workspace, "distribution", "install", "--in", str(llvm),
                        "--d3d12", mode)
        assert refreshed.returncode == 0, refreshed.stderr
        assert (prefix / "lib/cmake/llvm/LLVMConfig.cmake").is_file()
        plan = cli(workspace, "build", "check-hlsl", "--in", str(offload),
                   "--d3d12", mode, "--dxc", str(dxc), "--dry-run")
        assert plan.returncode == 0, plan.stderr
        assert f"llvm dist {prefix} (installed)" in plan.stdout
        assert "install-distribution" not in plan.stdout

    assert (workspace / ".hlsl-dev/selections/llvm-project.json").is_file()
    assert (workspace / ".hlsl-dev/selections/llvm-project@d3d12.json").is_file()
    monkeypatch.setenv("HLSL_LOCK_TIMEOUT", "0")
    with build_lock(workspace, llvm / "build/install"):
        blocked = cli(workspace, "clean", "--in", str(llvm))
        assert blocked.returncode != 0 and "build/install" in blocked.stderr
        assert (llvm / "build").exists()
    cleaned = cli(workspace, "clean", "--in", str(llvm))
    assert cleaned.returncode == 0, cleaned.stderr
    assert not (llvm / "build").exists()
    assert (llvm / "build-d3d12/install").exists()


def test_standalone_pairs_matching_branches_without_crossing_sources(workspace, monkeypatch):
    base_llvm = checkout(workspace, "llvm-project", "llvm")
    llvm = workspace / "llvm-project.feature"
    git(base_llvm, "worktree", "add", "-qb", "feature", str(llvm))
    base_offload, base_golden, dxc = native_inputs(workspace)
    for base, dirs in ((base_offload, ("tools/offloader", "lib/API")),
                       (base_golden, ("hlsl",))):
        for directory in dirs:
            (base / directory / ".fixture").touch()
        git(base, "add", ".")
        git(base, "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-qm", "track checkout directories")
    offload = workspace / "offload-test-suite.feature"
    git(base_offload, "worktree", "add", "-qb", "feature", str(offload))
    golden = workspace / "offload-golden-images.feature"
    git(workspace / "offload-golden-images", "worktree", "add", "-qb",
        "feature", str(golden))
    standalone_flags(monkeypatch)
    before = files(workspace)
    result = cli(workspace, "configure", "--in", str(offload), "--dxc", str(dxc),
                 "--dry-run")
    assert result.returncode == 0, result.stderr
    assert f"llvm {llvm}" in result.stdout
    assert f"golden {golden}" in result.stdout
    assert str(offload / "cmake/caches/StandaloneDistribution.cmake") in result.stdout
    assert str(base_llvm / "build") not in result.stdout
    assert files(workspace) == before


def fake_testing_tools(workspace):
    """Fake cmake and lit: no compiler, GPU suite or driver is invoked."""
    tool = fake_cmake(workspace) / "cmake"
    tool.write_text(tool.read_text().replace(
        "sys.exit(0)",
        "if '--build' in sys.argv:\n"
        "    build = pathlib.Path(sys.argv[sys.argv.index('--build') + 1])\n"
        "    (build / 'bin').mkdir(exist_ok=True)\n"
        "    suite = (build / 'tools/OffloadTest/test/clang-vk' if "
        "build.parent.name.startswith('llvm-project') else "
        "build / 'test/clang-vk')\n"
        "    suite.mkdir(parents=True, exist_ok=True)\n"
        "    lit = build / 'bin/llvm-lit'\n"
        "    lit.write_text('#!/usr/bin/env python3\\n'\n"
        "                   'import os, sys\\n'\n"
        "                   'with open(os.environ[\\\"LIT_LOG\\\"], \\\"a\\\") as f:\\n'\n"
        "                   '    f.write(repr(sys.argv[1:]) + \\\"\\\\n\\\")\\n'\n"
        "                   'sys.exit(int(os.getenv(\\\"LIT_STATUS\\\", \\\"0\\\")))\\n')\n"
        "    lit.chmod(0o755)\n"
        "sys.exit(0)",
    ))
    return tool.parent


@pytest.fixture
def test_workspace(workspace, monkeypatch):
    llvm = checkout(workspace, "llvm-project", "llvm")
    offload, _, dxc = native_inputs(workspace)
    tools = fake_testing_tools(workspace)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setenv("CMAKE_LOG", str(workspace / "cmake.log"))
    monkeypatch.setenv("LIT_LOG", str(workspace / "lit.log"))
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja -DOFFLOAD=$HD_OFFLOAD_SRC")
    icds = workspace / "fake-icds"
    icds.mkdir()
    (icds / f"lvp_icd.{os.uname().machine}.json").write_text("{}")
    monkeypatch.setenv("HLSL_VK_ICD_DIR", str(icds))
    return llvm, offload, dxc


def test_private_test_suite_build_and_help(workspace, test_workspace):
    llvm, _, dxc = test_workspace
    help_text = cli(workspace, "test", "--help")
    assert help_text.returncode == 0
    assert "--filter REGEX" in help_text.stdout
    assert "PATH" in help_text.stdout and " -- " in help_text.stdout
    lit_help = cli(workspace, "lit", "--help")
    assert lit_help.returncode == 0 and " -- " in lit_help.stdout
    before = files(workspace)
    dry = cli(workspace, "test", "clang-vk", "--in", str(llvm),
              "--dxc", str(dxc), "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert "check-hlsl-clang-vk" in dry.stdout
    assert files(workspace) == before
    run = cli(workspace, "test", "clang-vk", "--in", str(llvm), "--dxc", str(dxc))
    assert run.returncode == 0, run.stderr
    assert "check-hlsl-clang-vk" in (workspace / "cmake.log").read_text()
    assert not (workspace / "lit.log").exists()
    umbrella = cli(workspace, "test", "--in", str(llvm))
    assert umbrella.returncode == 0, umbrella.stderr
    assert "'--target', 'check-hlsl'" in (workspace / "cmake.log").read_text()


def test_private_test_path_filter_flags_and_lit_paths(workspace, test_workspace):
    llvm, offload, dxc = test_workspace
    path = offload / "test/clang-vk/Feature/Example.test"
    path.parent.mkdir(parents=True)
    path.touch()
    suite = llvm / "build/tools/OffloadTest/test/clang-vk"
    suite.mkdir(parents=True)
    (suite / "lit.site.cfg.py").write_text(
        f'config.offloadtest_src_root = path(r"{offload}")\n'
    )
    before = files(workspace)
    dry = cli(workspace, "test", "clang-vk", "--in", str(llvm),
              "Feature/Example.test", "--dxc", str(dxc), "--dry-run",
              "--", "--time-tests", "--filter=forwarded")
    assert dry.returncode == 0, dry.stderr
    assert "hlsl-test-depends" in dry.stdout
    assert "--time-tests --filter=forwarded" in dry.stdout
    assert str(suite / "Feature/Example.test") in dry.stdout
    assert files(workspace) == before
    result = cli(workspace, "test", "clang-vk", "Feature/Example.test",
                 "--in", str(llvm), "--dxc", str(dxc), "--",
                 "--time-tests", "--filter=forwarded")
    assert result.returncode == 0, result.stderr
    assert "'-v', '--time-tests', '--filter=forwarded'" in (
        workspace / "lit.log").read_text()
    assert str(suite / "Feature/Example.test") in (workspace / "lit.log").read_text()
    # An absent path is still a path; an existing path is still a regex when explicit.
    absent = cli(workspace, "test", "clang-vk", "log2.*", "--in", str(llvm),
                 "--dry-run")
    assert absent.returncode == 0, absent.stderr
    assert str(suite / "log2.*") in absent.stdout
    assert "--filter" not in absent.stdout
    regex = cli(workspace, "test", "clang-vk", "--filter", "Feature/Example.test",
                "--in", str(llvm), "--", "--time-tests")
    assert regex.returncode == 0, regex.stderr
    assert "'--filter', 'Feature/Example.test'" in (
        workspace / "lit.log").read_text()
    assert "'--time-tests'" in (workspace / "lit.log").read_text()
    lit = cli(workspace, "lit", "clang/test/CodeGenHLSL", "--in", str(llvm),
              "llvm/test/Transforms", "--", "--time-tests", "--filter=raw",
              cwd=llvm)
    assert lit.returncode == 0, lit.stderr
    assert "'-v', '--time-tests', '--filter=raw', 'clang/test/CodeGenHLSL', " \
           "'llvm/test/Transforms'" in (workspace / "lit.log").read_text()


def test_private_tests_no_auto_cross_and_invalid_suite_are_safe(workspace, test_workspace):
    llvm, _, _ = test_workspace
    before = files(workspace)
    no_auto = cli(workspace, "test", "clang-vk", "--in", str(llvm), "--no-auto")
    assert no_auto.returncode != 0 and "--no-auto" in no_auto.stderr
    invalid = cli(workspace, "test", "not-a-suite", "--in", str(llvm))
    assert invalid.returncode != 0 and "unknown suite" in invalid.stderr
    conflicting = cli(workspace, "test", "clang-vk", "Feature/foo.test",
                      "--filter", "foo", "--in", str(llvm))
    assert conflicting.returncode != 0 and "PATH or --filter" in conflicting.stderr
    missing_suite = cli(workspace, "test", "--filter", "foo", "--in", str(llvm))
    assert missing_suite.returncode != 0 and "need a suite" in missing_suite.stderr
    for action in (("test", "clang-vk"), ("lit", "clang/test/CodeGenHLSL")):
        cross = cli(workspace, *action, "--in", str(llvm),
                    "--platform", "windows-x64", "--", "--time-tests")
        assert cross.returncode != 0 and "native" in cross.stderr
    missing = cli(workspace, "lit", "clang/test", "--in", str(llvm))
    assert missing.returncode != 0 and "llvm-lit" in missing.stderr
    assert files(workspace) == before


def test_private_lit_and_filtered_test_propagate_exit_status(workspace, test_workspace,
                                                              monkeypatch):
    llvm, _, _ = test_workspace
    monkeypatch.setenv("LIT_STATUS", "7")
    built = cli(workspace, "test", "clang-vk", "--in", str(llvm))
    assert built.returncode == 0, built.stderr
    lit = cli(workspace, "lit", "clang/test", "--in", str(llvm))
    assert lit.returncode == 7, lit.stderr
    filtered = cli(workspace, "test", "clang-vk", "--filter", "log2.*",
                   "--in", str(llvm))
    assert filtered.returncode == 7, filtered.stderr
    forwarded = cli(workspace, "test", "clang-vk", "--in", str(llvm),
                    "--", "--time-tests")
    assert forwarded.returncode == 7, forwarded.stderr
    umbrella = cli(workspace, "test", "--in", str(llvm), "--", "--time-tests")
    assert umbrella.returncode == 7, umbrella.stderr
    assert "'--time-tests'" in (workspace / "lit.log").read_text()
    assert "hlsl-test-depends" in (workspace / "cmake.log").read_text()


def test_private_standalone_test_uses_distribution_prerequisite(workspace,
                                                                   test_workspace,
                                                                   monkeypatch):
    llvm, offload, dxc = test_workspace
    standalone_flags(monkeypatch)
    before = files(workspace)
    dry = cli(workspace, "test", "clang-vk", "--in", str(offload),
              "--dxc", str(dxc), "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert "install-distribution" in dry.stdout
    assert files(workspace) == before
    run = cli(workspace, "test", "clang-vk", "--in", str(offload), "--dxc", str(dxc))
    assert run.returncode == 0, run.stderr
    assert (llvm / "build/install/lib/cmake/llvm/LLVMConfig.cmake").is_file()
    assert "check-hlsl-clang-vk" in (workspace / "cmake.log").read_text()


def test_gpu_status_list_and_export_are_read_only(workspace, test_workspace,
                                                   monkeypatch):
    llvm, _, _ = test_workspace
    icds = Path(os.environ["HLSL_VK_ICD_DIR"])
    quoted = icds / f"weird' name_icd.{os.uname().machine}.json"
    quoted.touch()
    before = files(workspace)
    for args in (("gpu", "vulkan", "status"), ("gpu", "vulkan", "list"),
                 ("gpu", "d3d12", "status", "--in", str(llvm))):
        result = cli(workspace, *args)
        assert result.returncode == 0, result.stderr
    assert "lvp_icd." in cli(workspace, "gpu", "vulkan", "list").stdout
    assert "unconfigured" in cli(workspace, "gpu", "d3d12", "status", "--in", str(llvm)).stdout
    for args in (("gpu", "vk"), ("gpu", "vulkan", "use"),
                 ("gpu", "d3d12"),
                 ("gpu", "vulkan", "status", "--export")):
        assert cli(workspace, *args).returncode != 0
    monkeypatch.setenv("HLSL_VK_DRIVER", str(quoted))
    exports = cli(workspace, "gpu", "vulkan", "--export")
    assert exports.returncode == 0, exports.stderr
    shell = subprocess.run(
        ["bash", "-c", 'eval "$1"; printf "%s\\n" "$VK_DRIVER_FILES" "$VK_ICD_FILENAMES"',
         "sh", exports.stdout], capture_output=True, text=True,
    )
    assert shell.returncode == 0 and shell.stdout.splitlines() == [str(quoted)] * 2
    monkeypatch.setenv("HLSL_VK_DRIVER", "system")
    assert cli(workspace, "gpu", "vulkan", "--export").stdout == (
        "unset VK_DRIVER_FILES VK_ICD_FILENAMES\n"
    )
    assert files(workspace) == before


def test_gpu_missing_manifest_and_per_call_loader_override(workspace, test_workspace,
                                                              monkeypatch):
    llvm, _, dxc = test_workspace
    before = files(workspace)
    missing = cli(workspace, "gpu", "vulkan", "use", str(workspace / "absent.json"))
    assert missing.returncode != 0 and "no manifest" in missing.stderr
    assert files(workspace) == before
    assert cli(workspace, "gpu", "vulkan", "use", "system").returncode == 0
    assert "system" in cli(workspace, "gpu", "vulkan", "status").stdout
    saved = files(workspace)
    monkeypatch.setenv("HLSL_VK_DRIVER", "missing")
    assert "missing" in cli(workspace, "gpu", "vulkan", "status").stdout
    assert cli(workspace, "test", "clang-vk", "--in", str(llvm),
               "--dxc", str(dxc)).returncode != 0
    assert files(workspace) == saved
    assert cli(workspace, "test", "clang-vk", "--in", str(llvm),
               "--dxc", str(dxc), "--vulkan-driver", "lavapipe").returncode == 0
    assert cli(workspace, "lit", "clang/test", "--in", str(llvm),
               "--vulkan-driver", "lavapipe").returncode == 0
    assert cli(workspace, "build", "check-hlsl", "--in", str(llvm),
               "--vulkan-driver", "lavapipe").returncode == 0
    assert cli(workspace, "build", "check-hlsl", "--in", str(llvm),
               "--vulkan-driver", str(workspace / "absent.json")).returncode != 0
    assert cli(workspace, "test", "clang-vk", "--in", str(llvm),
               "--vk", "lavapipe").returncode != 0
    assert cli(workspace, "gpu", "vulkan", "--export", cwd=llvm).returncode != 0
    assert "missing" in cli(workspace, "gpu", "vulkan", "status", cwd=llvm).stdout
    assert "system" in __import__("json").loads(
        (workspace / ".hlsl-dev/gpu.json").read_text()
    )["vk"]


def test_vulkan_use_accepts_driver_named_status(workspace, test_workspace):
    icds = Path(os.environ["HLSL_VK_ICD_DIR"])
    (icds / f"status_icd.{os.uname().machine}.json").touch()
    selected = cli(workspace, "gpu", "vulkan", "use", "status")
    assert selected.returncode == 0, selected.stderr
    assert "driver status" in cli(workspace, "gpu", "vulkan", "status").stdout


def test_gpu_d3d12_saved_switch_never_reconfigures_and_override_is_ephemeral(
    workspace, test_workspace, monkeypatch
):
    llvm, _, dxc = test_workspace
    assert cli(workspace, "configure", "--in", str(llvm), "--dxc", str(dxc)).returncode == 0
    log = workspace / "cmake.log"
    portable = llvm / "build"
    d3d12 = llvm / "build-d3d12"
    assert portable.is_dir() and not d3d12.exists()
    assert "CMAKE_DISABLE_FIND_PACKAGE_D3D12=ON" in log.read_text()
    result = cli(workspace, "gpu", "d3d12", "on", "--in", str(llvm))
    assert result.returncode == 0, result.stderr
    assert f"build tree {d3d12} (unconfigured)" in result.stdout
    assert not d3d12.exists()  # Switching modes never reconfigures the other tree.
    plan = cli(workspace, "build", "clang", "--in", str(llvm), "--dry-run")
    assert f"build dir {d3d12}" in plan.stdout
    assert "CMAKE_DISABLE_FIND_PACKAGE_D3D12=OFF" in plan.stdout
    assert cli(workspace, "build", "clang", "--in", str(llvm)).returncode == 0
    assert portable.is_dir() and d3d12.is_dir()
    records = workspace / ".hlsl-dev/selections"
    assert (records / "llvm-project.json").is_file()
    assert (records / "llvm-project@d3d12.json").is_file()
    saved = {path.name: path.read_bytes() for path in records.iterdir()}
    count = len(log.read_text().splitlines())
    assert cli(workspace, "gpu", "d3d12", "off", "--in", str(llvm)).returncode == 0
    assert len(log.read_text().splitlines()) == count  # Switching never invokes CMake.
    assert {path.name: path.read_bytes() for path in records.iterdir()} == saved
    assert cli(workspace, "gpu", "d3d12", "off", "--in", str(llvm)).returncode == 0
    assert len(log.read_text().splitlines()) == count
    assert cli(workspace, "build", "clang", "--in", str(llvm),
               "--no-auto").returncode == 0
    assert len(log.read_text().splitlines()) == count + 1  # build, not configure
    assert cli(workspace, "build", "clang", "--in", str(llvm),
               "--d3d12", "on", "--no-auto").returncode == 0
    assert f"build dir {d3d12}" in cli(
        workspace, "build", "clang", "--in", str(llvm),
        "--d3d12", "on", "--dry-run").stdout
    assert "setting off" in cli(workspace, "gpu", "d3d12", "status").stdout
    before_on = len(log.read_text().splitlines())
    assert cli(workspace, "gpu", "d3d12", "on", "--in", str(llvm)).returncode == 0
    assert len(log.read_text().splitlines()) == before_on
    assert {path.name: path.read_bytes() for path in records.iterdir()} == saved
    assert "setting on" in cli(workspace, "gpu", "d3d12", "status").stdout


def test_container_forces_portable_build_despite_shared_host_choice(
    workspace, test_workspace, monkeypatch
):
    llvm, offload, _ = test_workspace
    dxc = checkout(workspace, "DirectXShaderCompiler", "dxc")
    assert cli(workspace, "gpu", "d3d12", "on").returncode == 0
    assert f"build dir {llvm / 'build-d3d12'}" in cli(
        workspace, "info", "--in", str(llvm)).stdout
    monkeypatch.setenv("HLSL_D3D12", "off")
    for tree in (llvm, offload):
        assert f"build dir {tree / 'build'}" in cli(
            workspace, "info", "--in", str(tree)).stdout
    assert f"build dir {dxc / 'build'}" in cli(
        workspace, "info", "--in", str(dxc)).stdout
    assert "setting off" in cli(workspace, "gpu", "d3d12", "status").stdout
    assert cli(workspace, "gpu", "d3d12", "on").returncode != 0
    assert f"build dir {llvm / 'build-d3d12'}" in cli(
        workspace, "build", "clang", "--in", str(llvm), "--d3d12", "on",
        "--dry-run").stdout
    assert f"build dir {llvm / 'build'}" in cli(
        workspace, "info", "--in", str(llvm)).stdout


def test_gpu_d3d12_switch_does_not_invoke_failed_cmake(workspace, test_workspace,
                                                        monkeypatch):
    llvm, _, dxc = test_workspace
    assert cli(workspace, "configure", "--in", str(llvm), "--dxc", str(dxc)).returncode == 0
    selection = workspace / ".hlsl-dev/selections/llvm-project.json"
    validated = selection.read_bytes()
    log = workspace / "cmake.log"
    before = log.read_bytes()
    fake_cmake(workspace, fail=True)
    for mode in ("on", "off"):
        switched = cli(workspace, "gpu", "d3d12", mode, "--in", str(llvm))
        assert switched.returncode == 0, switched.stderr
        assert log.read_bytes() == before
        assert selection.read_bytes() == validated
    ready = cli(workspace, "build", "clang", "--in", str(llvm),
                "--no-auto", "--dry-run")
    assert ready.returncode == 0, ready.stderr


def test_clean_sweep_needs_confirmation_even_if_only_one_checkout(workspace):
    llvm = checkout(workspace, "llvm-project", "llvm")
    (llvm / "build").mkdir()
    (llvm / "build/CMakeCache.txt").touch()
    before = files(workspace)
    previewed = cli(workspace, "clean", "--all", "--dry-run")
    assert previewed.returncode == 0 and "Would remove" in previewed.stdout
    assert files(workspace) == before
    refused = cli(workspace, "clean", "--all")
    assert refused.returncode != 0 and "--yes" in refused.stderr
    assert files(workspace) == before
    removed = cli(workspace, "clean", "--all", "--yes")
    assert removed.returncode == 0, removed.stderr
    assert not (llvm / "build").exists()


@pytest.mark.parametrize("state_location", ["root", "override"])
def test_clean_blocks_legacy_before_lock_or_removal(workspace, tmp_path, monkeypatch,
                                                    state_location):
    llvm = checkout(workspace, "llvm-project", "llvm")
    (llvm / "build").mkdir()
    (llvm / "build/CMakeCache.txt").touch()
    other = tmp_path.parent / f"{tmp_path.name}-legacy-state"
    monkeypatch.setenv("HLSL_DEV_STATE", str(other))
    state = workspace / ".hlsl-dev" if state_location == "root" else other
    (state / "pins").mkdir(parents=True)
    (state / "pins/llvm-project.env").write_text("old\n")
    before = files(workspace)
    before_other = files(other) if other.exists() else {}
    previewed = cli(workspace, "clean", "--in", str(llvm), "--dry-run")
    assert previewed.returncode == 0 and "Would remove" in previewed.stdout
    refused = cli(workspace, "clean", "--in", str(llvm))
    assert refused.returncode != 0 and "hlsl workspace migrate --dry-run" in refused.stderr
    assert "hlsl-*" not in refused.stderr
    assert files(workspace) == before
    assert files(other) == before_other
    assert not (other / "locks").exists()


def test_cross_license_diagnostic_uses_current_cli(workspace, monkeypatch):
    monkeypatch.delenv("HLSL_MSVC_LICENSE", raising=False)
    result = cli(workspace, "cross", "fetch", "windows-x64", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "hlsl cross fetch windows-x64 --dry-run" in result.stdout
    assert "hlsl-cross" not in result.stdout
