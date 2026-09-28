"""Compiler Explorer uses resolved local binaries without starting on inspection."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from test_hlsl_cli import checkout, cli, files, workspace  # noqa: F401
from hlsl_cli.command import Request, preview


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def explorer_inputs(workspace, monkeypatch):
    for key in ("HLSL_LLVM", "HLSL_DXC", "HLSL_DXC_PREBUILT_DIR"):
        monkeypatch.delenv(key, raising=False)
    llvm = checkout(workspace, "llvm-project", "llvm")
    dxc = checkout(workspace, "DirectXShaderCompiler", "dxc")
    ce = workspace / "compiler-explorer"
    (ce / "etc/config").mkdir(parents=True)
    (ce / "Makefile").touch()
    clang = executable(llvm / "build/bin/clang")
    clang_dxc = executable(llvm / "build/bin/clang-dxc")
    dxc_exe = executable(dxc / "build/bin/dxc")
    return llvm, dxc, ce, clang, clang_dxc, dxc_exe


def fake_make(workspace, monkeypatch, *, status=0):
    tool = workspace / "fake-bin/make"
    executable(tool)
    tool.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "pathlib.Path(os.environ['MAKE_LOG']).write_text("
        "os.getcwd() + '\\n' + repr(sys.argv[1:]))\n"
        f"sys.exit({status})\n"
    )
    monkeypatch.setenv("PATH", f"{tool.parent}:{os.environ['PATH']}")
    monkeypatch.setenv("MAKE_LOG", str(workspace / "make.log"))


def test_explorer_help_and_plan_are_read_only(explorer_inputs, workspace):
    llvm, dxc, ce, clang, clang_dxc, dxc_exe = explorer_inputs
    help_result = cli(workspace, "tools", "explorer", "--help")
    assert help_result.returncode == 0
    assert "--llvm" in help_result.stdout and "--dry-run" in help_result.stdout
    before = files(workspace)
    request = Request("tools explorer", workspace, llvm=str(llvm), dxc=str(dxc))
    plan = preview(request)
    command = cli(workspace, "tools", "explorer", "--llvm", str(llvm),
                  "--dxc", str(dxc), "--dry-run")
    assert command.returncode == 0, command.stderr
    assert command.stdout == plan.text
    for path in (clang, clang_dxc, dxc_exe, ce / "etc/config/hlsl.local.properties"):
        assert str(path) in plan.text
    assert "foreground" in plan.text and "no compiler build" in plan.text
    assert files(workspace) == before
    assert not (workspace / "make.log").exists()


def test_explicit_launch_writes_config_and_forwards_exit(explorer_inputs, workspace,
                                                          monkeypatch):
    llvm, dxc, ce, clang, clang_dxc, dxc_exe = explorer_inputs
    fake_make(workspace, monkeypatch, status=23)
    result = cli(workspace, "tools", "explorer", "--in", str(llvm))
    assert result.returncode == 23, result.stderr
    content = (ce / "etc/config/hlsl.local.properties").read_text()
    assert "compilers=&dxc:&clang\n" in content
    assert "defaultCompiler=dxc_local\n" in content
    assert "group.clang.compilerType=clang-dxc\n" in content
    assert f"compiler.clang_local.exe={clang}\n" in content
    assert f"compiler.clang_dxc_local.exe={clang_dxc}\n" in content
    assert f"compiler.dxc_local.exe={dxc_exe}\n" in content
    assert str(ce) in (workspace / "make.log").read_text()
    assert "'dev', 'EXTRA_ARGS=--language hlsl'" in (
        workspace / "make.log").read_text()
    assert str(ce / "etc/config/hlsl.local.properties") in result.stdout


def test_explorer_resolution_obeys_explicit_env_and_saved_choices(
    explorer_inputs, workspace, monkeypatch
):
    import json

    llvm, dxc, ce, _, _, _ = explorer_inputs
    other = checkout(workspace, "llvm-project.other", "llvm")
    executable(other / "build/bin/clang")
    executable(other / "build/bin/clang-dxc")
    standalone = workspace / "standalone"
    executable(standalone / "dxc")
    monkeypatch.setenv("HLSL_LLVM", str(other))
    monkeypatch.setenv("HLSL_DXC", str(standalone))
    selected = cli(workspace, "tools", "explorer", "--in", str(dxc), "--dry-run")
    assert selected.returncode == 0, selected.stderr
    assert str(other / "build/bin/clang") in selected.stdout
    assert str(standalone / "dxc") in selected.stdout
    explicit = cli(workspace, "tools", "explorer", "--llvm", str(llvm),
                   "--dxc", str(dxc), "--dry-run")
    assert explicit.returncode == 0, explicit.stderr
    assert str(llvm / "build/bin/clang") in explicit.stdout
    assert str(dxc / "build/bin/dxc") in explicit.stdout
    monkeypatch.delenv("HLSL_LLVM")
    monkeypatch.delenv("HLSL_DXC")
    selections = workspace / ".hlsl-dev/selections"
    selections.mkdir(parents=True)
    (selections / "llvm-project.json").write_text(json.dumps({
        "version": 1, "configured": {}, "choices": {"dxc": str(standalone)}
    }))
    saved = cli(workspace, "tools", "explorer", "--in", str(llvm), "--dry-run")
    assert saved.returncode == 0, saved.stderr
    assert str(standalone / "dxc") in saved.stdout


def test_missing_inputs_and_cross_platform_never_launch(explorer_inputs, workspace,
                                                        monkeypatch):
    llvm, dxc, ce, clang, clang_dxc, _ = explorer_inputs
    fake_make(workspace, monkeypatch)
    for args, expected in (
        (("--platform", "windows-x64"), "native"),
        (("--llvm", "not-found"), "not-found"),
    ):
        failed = cli(workspace, "tools", "explorer", *args)
        assert failed.returncode != 0 and expected in failed.stderr
    clang_dxc.unlink()
    missing = cli(workspace, "tools", "explorer", "--in", str(llvm))
    assert missing.returncode != 0
    assert "clang-dxc" in missing.stderr and "hlsl build" in missing.stderr
    assert not (ce / "etc/config/hlsl.local.properties").exists()
    assert not (workspace / "make.log").exists()
    clang_dxc = executable(clang_dxc)
    (ce / "Makefile").unlink()
    unavailable = cli(workspace, "tools", "explorer", "--in", str(llvm))
    assert unavailable.returncode != 0 and "hlsl setup" in unavailable.stderr
    assert not (workspace / "make.log").exists()


def test_explorer_fresh_migration_guard_and_prebuilt(explorer_inputs, workspace,
                                                     monkeypatch):
    llvm, dxc, ce, _, _, _ = explorer_inputs
    fake_make(workspace, monkeypatch)
    prebuilt = workspace / "prebuilt"
    executable(prebuilt / "dxc")
    monkeypatch.setenv("HLSL_DXC_PREBUILT_DIR", str(prebuilt))
    dry = cli(workspace, "tools", "explorer", "--dxc", "nix", "--in",
              str(llvm), "--dry-run")
    assert dry.returncode == 0 and str(prebuilt / "dxc") in dry.stdout
    pins = workspace / ".hlsl-dev/pins"
    pins.mkdir(parents=True)
    (pins / "legacy.env").touch()
    refused = cli(workspace, "tools", "explorer", "--dxc", "nix", "--in", str(llvm))
    assert refused.returncode != 0 and "migration" in refused.stderr
    assert not (ce / "etc/config/hlsl.local.properties").exists()
    assert not (workspace / "make.log").exists()


def test_explorer_service_signal_status(explorer_inputs, workspace, monkeypatch):
    llvm, _, _, _, _, _ = explorer_inputs
    tool = workspace / "fake-bin/make"
    executable(tool)
    tool.write_text("#!/bin/sh\nkill -TERM $$\n")
    monkeypatch.setenv("PATH", f"{tool.parent}:{os.environ['PATH']}")
    result = cli(workspace, "tools", "explorer", "--in", str(llvm))
    assert result.returncode == 143, result.stderr


def test_explorer_interrupt_forwards_to_child(explorer_inputs, workspace, monkeypatch):
    llvm, _, _, _, _, _ = explorer_inputs
    tool = workspace / "fake-bin/make"
    executable(tool)
    tool.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, time\n"
        "pathlib.Path(os.environ['MAKE_PID']).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    monkeypatch.setenv("PATH", f"{tool.parent}:{os.environ['PATH']}")
    pid_file = workspace / "make.pid"
    monkeypatch.setenv("MAKE_PID", str(pid_file))
    with subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "hlsl.py"),
         "tools", "explorer", "--in", str(llvm)],
        cwd=workspace, env=os.environ.copy(), start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as wrapper:
        try:
            for _ in range(100):
                if pid_file.is_file():
                    break
                time.sleep(0.02)
            assert pid_file.is_file(), "fake foreground service did not start"
            child = int(pid_file.read_text())
            wrapper.send_signal(signal.SIGINT)
            _, stderr = wrapper.communicate(timeout=5)
            assert wrapper.returncode == 130, stderr
            with pytest.raises(ProcessLookupError):
                os.kill(child, 0)
        finally:
            if wrapper.poll() is None:
                wrapper.kill()
            if pid_file.is_file():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_explorer_replaces_symlink_without_writing_its_target(
    explorer_inputs, workspace, monkeypatch
):
    llvm, _, ce, _, _, _ = explorer_inputs
    fake_make(workspace, monkeypatch)
    outside = workspace / "personal.properties"
    outside.write_text("keep this config\n")
    config = ce / "etc/config/hlsl.local.properties"
    config.symlink_to(outside)
    result = cli(workspace, "tools", "explorer", "--in", str(llvm))
    assert result.returncode == 0, result.stderr
    assert outside.read_text() == "keep this config\n"
    assert not config.is_symlink()
    assert "compiler.clang_local.exe=" in config.read_text()
