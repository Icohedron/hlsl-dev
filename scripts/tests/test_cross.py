"""Fake toolchain and CMake coverage for private cross commands."""

import ast
import json
import os
from pathlib import Path

import pytest

from test_hlsl_cli import checkout, cli, files, native_inputs, workspace


@pytest.fixture
def cross_env(workspace, monkeypatch):
    monkeypatch.setenv("HLSL_HOST_PLATFORM", "linux-x64")
    monkeypatch.setenv("HLSL_NIXPKGS_PATH", str(workspace))
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_LLVM", "-G Ninja -DDXC=$HD_DXC_BIN_DIR -DCMAKE_INSTALL_PREFIX=$HD_INSTALL_PREFIX")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_DXC", "-G Ninja -DCMAKE_BUILD_TYPE=$HD_BUILD_TYPE")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_CROSS", "-DCMAKE_TOOLCHAIN_FILE=$HD_TOOLCHAIN_FILE")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_CROSS_LINUX", "-DLLVM_ENABLE_LLD=OFF")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_CROSS_WINDOWS", "-DLLVM_LINK_LLVM_DYLIB=OFF -DWARP_VERSION=System")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_CROSS_DXC_DIA", "-DMSVC_DIA_SDK_DIR=$HD_DIA_SDK")
    monkeypatch.delenv("HLSL_MSVC_LICENSE", raising=False)
    monkeypatch.delenv("HLSL_DIA_SDK", raising=False)
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_CROSS_LLVM", "-DLLVM_NATIVE_TOOL_DIR=$HD_NATIVE_TOOL_DIR -DLLVM_HOST_TRIPLE=$HD_TARGET_TRIPLE")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_CROSS_LLVM_WINDOWS",
                       "-DLLVM_DISTRIBUTION_COMPONENTS=clang${HD_SEMI}cmake-exports")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_CROSS_DXC", "-DCROSS_TOOLCHAIN_FLAGS_NATIVE=-DLLVM_ENABLE_EH=ON${HD_SEMI}-DLLVM_ENABLE_RTTI=ON")
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_NATIVE_TOOLS", "-G Ninja -DLLVM_ENABLE_PROJECTS=clang${HD_SEMI}clang-tools-extra")
    tools = workspace / "fake-bin"
    tools.mkdir()
    nix = tools / "nix-build"
    nix.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "with open(os.environ['NIX_LOG'], 'a') as out: out.write(repr(sys.argv[1:]) + '\\n')\n"
        "if os.environ.get('NIX_FAIL'): sys.exit(2)\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
        "store = pathlib.Path(os.environ['NIX_LOG']).parent / 'fake-store' / str(len(open(os.environ['NIX_LOG']).readlines()))\n"
        "store.mkdir(parents=True)\n"
        "(store / 'toolchain.cmake').write_text(str(store))\n"
        "output.symlink_to(store, target_is_directory=True)\n"
    )
    nix.chmod(0o755)
    cmake = tools / "cmake"
    cmake.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "with open(os.environ['CMAKE_LOG'], 'a') as out:\n"
        "    out.write(repr((sys.argv[1:], os.environ.get('C_INCLUDE_PATH'))) + '\\n')\n"
        "if '-B' in sys.argv:\n"
        "    build = pathlib.Path(sys.argv[sys.argv.index('-B') + 1])\n"
        "    build.mkdir(parents=True, exist_ok=True)\n"
        "    (build / 'build.ninja').touch()\n"
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
        "    (build / 'bin').mkdir(exist_ok=True)\n"
        "    if 'install-distribution' in sys.argv and (build / '.prefix').is_file():\n"
        "        prefix = pathlib.Path((build / '.prefix').read_text())\n"
        "        (prefix / 'lib/cmake/llvm').mkdir(parents=True, exist_ok=True)\n"
        "        (prefix / 'lib/cmake/llvm/LLVMConfig.cmake').touch()\n"
        "    if build.name == 'build-native-tools':\n"
        "        for name in ('llvm-min-tblgen', 'llvm-tblgen', 'clang-tblgen'):\n"
        "            (build / 'bin' / name).touch()\n"
        "    if build.parent.name == 'DirectXShaderCompiler':\n"
        "        names = ('dxc.exe', 'dxv.exe', 'dxcompiler.dll', 'dxil.dll') if 'windows-' in build.name else ('dxc', 'dxv')\n"
        "        for name in names: (build / 'bin' / name).touch()\n"
    )
    cmake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setenv("NIX_LOG", str(workspace / "nix.log"))
    monkeypatch.setenv("CMAKE_LOG", str(workspace / "cmake.log"))
    return workspace


def calls(path):
    return [ast.literal_eval(line) for line in path.read_text().splitlines()]


def test_windows_distribution_component_override_applies_only_to_llvm(cross_env):
    from hlsl_cli import cross, workspace as ws

    llvm = checkout(cross_env, "llvm-project", "llvm")
    tree = ws.Worktree(llvm, "llvm", "")
    llvm_flags = cross.flags(cross_env, "windows-x64", "llvm", llvm=tree)
    offload_flags = cross.flags(cross_env, "windows-x64", "offload")
    assert llvm_flags[-1] == "-DLLVM_DISTRIBUTION_COMPONENTS=clang;cmake-exports"
    assert not any("LLVM_DISTRIBUTION_COMPONENTS" in flag for flag in offload_flags)


def test_list_and_dry_runs_are_read_only(cross_env):
    root = cross_env
    before = files(root)
    listing = cli(root, "cross", "list")
    assert listing.returncode == 0, listing.stderr
    assert "linux-arm64" in listing.stdout and "linux-x64" not in listing.stdout
    assert "windows-x64" in listing.stdout and "windows-arm64" in listing.stdout
    assert "needs licence" in listing.stdout
    for action in ("fetch", "refresh"):
        report = cli(root, "cross", action, "linux-arm64", "--dry-run")
        assert report.returncode == 0, report.stderr
        assert "nix-build" in report.stdout
        assert "aarch64-unknown-linux-gnu" in report.stdout
    for platform in ("linux-x64", "typo"):
        assert cli(root, "cross", "fetch", platform, "--dry-run").returncode != 0
    win = cli(root, "cross", "fetch", "windows-x64", "--dry-run")
    assert win.returncode == 0 and "blocked:" in win.stdout
    assert "acceptMsvcLicense" not in win.stdout
    assert files(root) == before


def test_fetch_refresh_and_failure_preserve_toolchain(cross_env, monkeypatch):
    root = cross_env
    target = root / ".hlsl-dev/toolchains/linux-arm64"
    assert cli(root, "cross", "fetch", "linux-arm64").returncode == 0
    first = os.readlink(target)
    assert (target / "toolchain.cmake").is_file()
    assert "ready" in cli(root, "cross", "list").stdout
    assert cli(root, "cross", "fetch", "linux-arm64").returncode == 0
    assert len(calls(root / "nix.log")) == 1
    monkeypatch.setenv("NIX_FAIL", "1")
    failed = cli(root, "cross", "refresh", "linux-arm64")
    assert failed.returncode != 0
    assert os.readlink(target) == first
    assert not list(target.parent.glob("*.pending-*"))
    monkeypatch.delenv("NIX_FAIL")
    assert cli(root, "cross", "refresh", "linux-arm64").returncode == 0
    assert os.readlink(target) != first
    assert len(calls(root / "nix.log")) == 3


def test_llvm_cross_plan_host_tools_and_build(cross_env, monkeypatch):
    root = cross_env
    llvm = checkout(root, "llvm-project", "llvm")
    native_inputs(root)
    monkeypatch.setenv("C_INCLUDE_PATH", "/host/include")
    before = files(root)
    args = ("--in", str(llvm), "--platform", "linux-arm64")
    plan = cli(root, "build", "clang", *args, "--dry-run")
    assert plan.returncode == 0, plan.stderr
    assert "nix-build" in plan.stdout and "build-native-tools" in plan.stdout
    assert str(llvm / "build.linux-arm64") in plan.stdout
    no_d3d12 = cli(root, "build", "clang", *args, "--d3d12", "on", "--dry-run")
    assert no_d3d12.returncode == 0, no_d3d12.stderr
    assert str(llvm / "build.linux-arm64") in no_d3d12.stdout
    assert "build-d3d12.linux-arm64" not in no_d3d12.stdout
    assert str(llvm / "build-native-tools/bin") in plan.stdout
    assert files(root) == before
    refused = cli(root, "build", "clang", *args, "--no-auto")
    assert refused.returncode != 0 and "--no-auto" in refused.stderr
    assert files(root) == before
    built = cli(root, "build", "clang", *args)
    assert built.returncode == 0, built.stderr
    assert (llvm / "build-native-tools/bin/clang-tblgen").is_file()
    assert (llvm / "build.linux-arm64/build.ninja").is_file()
    assert not (llvm / "compile_commands.json").exists()
    cmds = calls(root / "cmake.log")
    assert len(cmds) == 4  # configure/build generators, then target
    assert str(llvm / "build-native-tools") in repr(cmds[0])
    assert "--target', 'llvm-min-tblgen', 'llvm-tblgen', 'clang-tblgen'" in repr(cmds[1])
    assert "-DLLVM_HOST_TRIPLE=aarch64-unknown-linux-gnu" in repr(cmds[2])
    assert "-DCMAKE_TOOLCHAIN_FILE=" in repr(cmds[2])
    assert cmds[2][1] is None and cmds[3][1] is None
    # An unchanged host tool set must not be rebuilt by --no-auto.
    assert cli(root, "build", "clang", *args, "--no-auto").returncode == 0
    assert len(calls(root / "cmake.log")) == 5
    assert cli(root, "build", "clang", *args).returncode == 0
    assert len(calls(root / "cmake.log")) == 7  # incremental refresh, target
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_NATIVE_TOOLS", "-G Ninja -DREV=two")
    snapshot = files(root)
    stale = cli(root, "build", "clang", *args, "--no-auto")
    assert stale.returncode != 0 and "host tools" in stale.stderr
    assert files(root) == snapshot


def test_dxc_cross_build_flags_and_no_auto(cross_env):
    root = cross_env
    dxc = checkout(root, "DirectXShaderCompiler", "dxc")
    args = ("--in", str(dxc), "--platform", "linux-arm64")
    before = files(root)
    denied = cli(root, "build", "dxc", *args, "--no-auto")
    assert denied.returncode != 0 and "toolchain" in denied.stderr
    assert files(root) == before
    assert cli(root, "cross", "fetch", "linux-arm64").returncode == 0
    report = cli(root, "build", "dxc", *args)
    assert report.returncode == 0, report.stderr
    assert (dxc / "build.linux-arm64/bin/dxc").is_file()
    commands = calls(root / "cmake.log")
    assert len(commands) == 2
    assert "-DCROSS_TOOLCHAIN_FLAGS_NATIVE=-DLLVM_ENABLE_EH=ON;-DLLVM_ENABLE_RTTI=ON" in repr(commands[0])
    assert cli(root, "build", "dxc", *args, "--no-auto").returncode == 0
    assert len(calls(root / "cmake.log")) == 3
    saved = json.loads((root / ".hlsl-dev/selections/DirectXShaderCompiler@linux-arm64.json").read_text())
    assert saved["configured"]["build_dir"] == str(dxc / "build.linux-arm64")


@pytest.mark.parametrize("kind", ("llvm", "offload", "dxc"))
def test_linux_cross_check_targets_refused_before_provisioning(cross_env, monkeypatch, kind):
    root = cross_env
    tree = checkout(root, {
        "llvm": "llvm-project", "offload": "offload-test-suite",
        "dxc": "DirectXShaderCompiler",
    }[kind], kind)
    if kind == "llvm":
        native_inputs(root)
    elif kind == "offload":
        checkout(root, "llvm-project", "llvm")
        checkout(root, "offload-golden-images", "golden")
        dxc = root / "host-dxc"
        dxc.mkdir()
        (dxc / "dxc").touch()
        (dxc / "dxv").touch()
        monkeypatch.setenv("HLSL_DXC", str(dxc))
        monkeypatch.setenv("HLSL_CMAKE_FLAGS_OFFLOAD", "-G Ninja -DLLVM_DIR=$HD_LLVM_CMAKE_DIR")
    args = ("build", "check-hlsl", "--in", str(tree), "--platform", "linux-arm64")
    before = files(root)
    for extra in (("--dry-run",), ()):
        refused = cli(root, *args, *extra)
        assert refused.returncode != 0
        assert "test targets cannot run on this host" in refused.stderr
        assert files(root) == before
    assert not (root / "nix.log").exists()
    assert not (root / "cmake.log").exists()


def test_offload_cross_installs_matching_distribution(cross_env, monkeypatch):
    root = cross_env
    llvm = checkout(root, "llvm-project", "llvm")
    offload, _, dxc = native_inputs(root)
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_OFFLOAD", "-G Ninja -DLLVM_DIR=$HD_LLVM_CMAKE_DIR")
    args = ("--in", str(offload), "--platform", "linux-arm64", "--dxc", str(dxc))
    before = files(root)
    report = cli(root, "build", "install-offload-test-suite", *args, "--dry-run")
    assert report.returncode == 0, report.stderr
    assert str(llvm / "build.linux-arm64/install") in report.stdout
    assert "host tools" in report.stdout
    assert files(root) == before
    denied = cli(root, "build", "install-offload-test-suite", *args, "--no-auto")
    assert denied.returncode != 0 and "--no-auto" in denied.stderr
    assert files(root) == before
    built = cli(root, "build", "install-offload-test-suite", *args)
    assert built.returncode == 0, built.stderr
    assert (llvm / "build.linux-arm64/install/lib/cmake/llvm/LLVMConfig.cmake").is_file()
    assert (offload / "build.linux-arm64/build.ninja").is_file()
    assert not (offload / "compile_commands.json").exists()
    commands = calls(root / "cmake.log")
    assert len(commands) == 6  # host tools, distribution, standalone build
    assert all(env is None for _, env in commands[2:])
    assert "-DCMAKE_TOOLCHAIN_FILE=" in repr(commands[2])
    assert "-DCMAKE_TOOLCHAIN_FILE=" in repr(commands[4])
    assert str(llvm / "build.linux-arm64/install/lib/cmake/llvm") in repr(commands[4])
    assert cli(root, "build", "install-offload-test-suite", *args, "--no-auto").returncode == 0
    assert len(calls(root / "cmake.log")) == 7
    assert (root / ".hlsl-dev/selections/llvm-project@linux-arm64.json").is_file()


def test_explicit_cross_distribution_install_and_native_isolation(cross_env, monkeypatch):
    root = cross_env
    llvm = checkout(root, "llvm-project", "llvm")
    _, _, dxc = native_inputs(root)
    monkeypatch.setenv("HLSL_DXC", str(dxc))
    args = ("--in", str(llvm), "--platform", "linux-arm64")
    before = files(root)
    missing = cli(root, "distribution", "install", *args, "--dry-run")
    assert missing.returncode != 0 and "hlsl configure" in missing.stderr
    assert files(root) == before and not (root / "cmake.log").exists()
    configured = cli(root, "configure", *args, "--dxc", str(dxc))
    assert configured.returncode == 0, configured.stderr
    assert len(calls(root / "cmake.log")) == 3  # Host tools and cross configure.
    report = cli(root, "distribution", "install", *args, "--dry-run")
    assert report.returncode == 0, report.stderr
    assert "--target install-distribution" in report.stdout
    assert "configure:" not in report.stdout and "prerequisite:" not in report.stdout
    result = cli(root, "distribution", "install", *args)
    assert result.returncode == 0, result.stderr
    assert (llvm / "build.linux-arm64/install/lib/cmake/llvm/LLVMConfig.cmake").is_file()
    assert not (llvm / "build").exists()
    assert len(calls(root / "cmake.log")) == 4
    assert cli(root, "distribution", "install", *args).returncode == 0
    assert len(calls(root / "cmake.log")) == 5  # Reinstall, no host-tools build.
    windows = cli(root, "distribution", "install", "--in", str(llvm),
                  "--platform", "windows-x64", "--dry-run")
    assert windows.returncode != 0 and "hlsl configure" in windows.stderr


def test_cross_preserved_build_requires_explicit_revalidation(cross_env):
    root = cross_env
    dxc = checkout(root, "DirectXShaderCompiler", "dxc")
    build = dxc / "build.linux-arm64"
    build.mkdir()
    (build / "CMakeCache.txt").write_text("CMAKE_BUILD_TYPE:STRING=Debug\n")
    args = ("--in", str(dxc), "--platform", "linux-arm64")
    before = files(root)
    assert "revalidat" in cli(root, "configure", *args, "--dry-run").stderr
    assert "configure" in cli(root, "build", "dxc", *args, "--dry-run").stderr
    assert files(root) == before
    explicit = cli(root, "configure", *args, "--build-type", "Debug", "--dry-run")
    assert explicit.returncode == 0, explicit.stderr
    assert files(root) == before


def test_cross_existing_external_distribution_never_installs(cross_env, monkeypatch):
    root = cross_env
    checkout(root, "llvm-project", "llvm")
    offload, _, dxc = native_inputs(root)
    prefix = root / "shared-dist"
    (prefix / "lib/cmake/llvm").mkdir(parents=True)
    (prefix / "lib/cmake/llvm/LLVMConfig.cmake").touch()
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_OFFLOAD", "-G Ninja -DLLVM_DIR=$HD_LLVM_CMAKE_DIR")
    args = ("--in", str(offload), "--platform", "linux-arm64", "--dxc", str(dxc),
            "--dist-prefix", str(prefix))
    assert cli(root, "cross", "fetch", "linux-arm64").returncode == 0
    report = cli(root, "build", "install-offload-test-suite", *args, "--dry-run")
    assert report.returncode == 0, report.stderr
    assert "external" in report.stdout and "install-distribution" not in report.stdout
    built = cli(root, "build", "install-offload-test-suite", *args)
    assert built.returncode == 0, built.stderr
    assert len(calls(root / "cmake.log")) == 2
    assert not (root / "llvm-project/build.linux-arm64").exists()
    assert cli(root, "build", "install-offload-test-suite", *args, "--no-auto").returncode == 0


@pytest.mark.parametrize("platform", ("windows-x64", "windows-arm64"))
def test_windows_licence_blocks_fetch_without_realizing_sdk(cross_env, platform):
    root = cross_env
    before = files(root)
    for action in ("fetch", "refresh"):
        plan = cli(root, "cross", action, platform, "--dry-run")
        assert plan.returncode == 0, plan.stderr
        assert "blocked:" in plan.stdout and "MSVC licence: not accepted" in plan.stdout
        assert "acceptMsvcLicense" not in plan.stdout
        assert files(root) == before
        refused = cli(root, "cross", action, platform)
        assert refused.returncode != 0
        assert "licence" in refused.stderr and "No SDK has been fetched" in refused.stderr
    assert not (root / f".hlsl-dev/toolchains/{platform}").exists()
    assert not (root / "nix.log").exists()


def test_windows_fake_toolchain_requires_explicit_per_command_consent(cross_env, monkeypatch):
    root = cross_env
    platform = "windows-x64"
    monkeypatch.setenv("HLSL_MSVC_LICENSE", "accepted")
    plan = cli(root, "cross", "fetch", platform, "--dry-run")
    assert plan.returncode == 0, plan.stderr
    assert "--arg acceptMsvcLicense true" in plan.stdout
    assert "D3D12 (Windows SDK), Vulkan (vulkan-1.lib" in plan.stdout
    assert "Windows execution: unverified" in plan.stdout
    assert not (root / "nix.log").exists()
    fetched = cli(root, "cross", "fetch", platform)
    assert fetched.returncode == 0, fetched.stderr
    assert (root / ".hlsl-dev/toolchains/windows-x64/toolchain.cmake").is_file()
    assert ("--arg", "acceptMsvcLicense", "true") == tuple(
        calls(root / "nix.log")[0][7:10]
    )
    monkeypatch.delenv("HLSL_MSVC_LICENSE")
    assert "ready" in cli(root, "cross", "list").stdout
    assert cli(root, "cross", "fetch", platform).returncode == 0
    assert len(calls(root / "nix.log")) == 1
    refused = cli(root, "cross", "refresh", platform)
    assert refused.returncode != 0 and "licence" in refused.stderr
    assert len(calls(root / "nix.log")) == 1
    assert (root / ".hlsl-dev/toolchains/windows-x64/toolchain.cmake").is_file()


@pytest.mark.parametrize("platform,triple", (
    ("windows-x64", "x86_64-pc-windows-msvc"),
    ("windows-arm64", "aarch64-pc-windows-msvc"),
))
def test_windows_dxc_requires_dia_and_uses_target_flags(cross_env, monkeypatch, platform, triple):
    root = cross_env
    dxc = checkout(root, "DirectXShaderCompiler", "dxc")
    args = ("--in", str(dxc), "--platform", platform)
    before = files(root)
    missing = cli(root, "build", "dxc", *args, "--dry-run")
    assert missing.returncode != 0
    assert "HLSL_DIA_SDK" in missing.stderr and "not in nixpkgs" in missing.stderr
    assert files(root) == before
    dia = root / "copied-dia"
    dia.mkdir()
    monkeypatch.setenv("HLSL_DIA_SDK", str(dia))
    plan = cli(root, "build", "dxc", *args, "--dry-run")
    assert plan.returncode == 0, plan.stderr
    assert str(dxc / f"build.{platform}") in plan.stdout
    assert f"-DMSVC_DIA_SDK_DIR={dia}" in plan.stdout
    assert "-DLLVM_LINK_LLVM_DYLIB=OFF" in plan.stdout
    assert "-DWARP_VERSION=System" in plan.stdout
    assert "-DLLVM_ENABLE_LLD=OFF" not in plan.stdout
    assert "-DCMAKE_TOOLCHAIN_FILE=" in plan.stdout
    assert triple in plan.stdout
    assert files(root) == {**before, "copied-dia": ("dir",)}
    # Only fake nix-build and cmake are on PATH: no SDK or compiler is fetched.
    monkeypatch.setenv("HLSL_MSVC_LICENSE", "accepted")
    built = cli(root, "build", "dxc", *args)
    assert built.returncode == 0, built.stderr
    assert (dxc / f"build.{platform}/bin/dxc.exe").is_file()
    assert (dxc / f"build.{platform}/bin/dxcompiler.dll").is_file()
    assert not (dxc / "build/bin/dxc").exists()
    assert f"-DMSVC_DIA_SDK_DIR={dia}" in repr(calls(root / "cmake.log")[0])
    assert (root / f".hlsl-dev/selections/DirectXShaderCompiler@{platform}.json").is_file()


def test_windows_llvm_plan_never_uses_host_dxc_and_separates_builds(cross_env, monkeypatch):
    root = cross_env
    llvm = checkout(root, "llvm-project", "llvm")
    native_inputs(root)
    args = ("--in", str(llvm), "--platform", "windows-x64")
    denied = cli(root, "build", "clang", *args, "--dxc", "nix", "--dry-run")
    assert denied.returncode != 0 and "for this host" in denied.stderr
    win = root / "windows-dxc"
    win.mkdir()
    for name in ("dxc.exe", "dxv.exe", "dxcompiler.dll", "dxil.dll"):
        (win / name).touch()
    monkeypatch.setenv("VK_DRIVER_FILES", "/missing/host/icd.json")
    before = files(root)
    plan = cli(root, "build", "clang", *args, "--dxc", str(win),
               "--d3d12", "on", "--dry-run")
    assert plan.returncode == 0, plan.stderr
    assert str(llvm / "build-d3d12.windows-x64") in plan.stdout
    assert str(win) in plan.stdout and str(root / "dxc-bin") not in plan.stdout
    assert "-DCMAKE_DISABLE_FIND_PACKAGE_D3D12=OFF" in plan.stdout
    assert "-DCMAKE_DISABLE_FIND_PACKAGE_D3D12_WSL=OFF" in plan.stdout
    assert "target APIs: D3D12" in plan.stdout
    assert "build-native-tools" in plan.stdout
    assert files(root) == before
    monkeypatch.setenv("HLSL_MSVC_LICENSE", "accepted")
    built = cli(root, "build", "clang", *args, "--dxc", str(win),
                "--d3d12", "on", "--vulkan-driver", "missing")
    assert built.returncode == 0, built.stderr
    assert (llvm / "build-d3d12.windows-x64/build.ninja").is_file()
    assert not (llvm / "build/build.ninja").exists()
    assert not (llvm / "compile_commands.json").exists()
    assert "-DDXC=" + str(win) in repr(calls(root / "cmake.log")[2])


def test_windows_offload_plans_target_apis_without_host_execution(cross_env, monkeypatch):
    root = cross_env
    checkout(root, "llvm-project", "llvm")
    offload, _, _ = native_inputs(root)
    win = root / "windows-dxc"
    win.mkdir()
    for name in ("dxc.exe", "dxv.exe", "dxcompiler.dll", "dxil.dll"):
        (win / name).touch()
    prefix = root / "windows-dist"
    (prefix / "lib/cmake/llvm").mkdir(parents=True)
    (prefix / "lib/cmake/llvm/LLVMConfig.cmake").touch()
    monkeypatch.setenv("HLSL_CMAKE_FLAGS_OFFLOAD", "-G Ninja -DLLVM_DIR=$HD_LLVM_CMAKE_DIR -DDXC=$HD_DXC_BIN_DIR")
    args = ("--in", str(offload), "--platform", "windows-arm64", "--dxc", str(win),
            "--dist-prefix", str(prefix))
    before = files(root)
    plan = cli(root, "build", "install-offload-test-suite", *args,
               "--d3d12", "on", "--dry-run")
    assert plan.returncode == 0, plan.stderr
    assert str(offload / "build-d3d12.windows-arm64") in plan.stdout
    assert str(prefix / "lib/cmake/llvm") in plan.stdout
    assert "-DCMAKE_DISABLE_FIND_PACKAGE_D3D12=OFF" in plan.stdout
    assert "target APIs: D3D12" in plan.stdout and "vulkan-1.lib" in plan.stdout
    assert "-DLLVM_ENABLE_LLD=OFF" not in plan.stdout
    off = cli(root, "configure", *args, "--d3d12", "off", "--dry-run")
    assert off.returncode == 0, off.stderr
    assert "-DCMAKE_DISABLE_FIND_PACKAGE_D3D12=ON" in off.stdout
    assert str(offload / "build.windows-arm64") in off.stdout
    assert files(root) == before
    for target in (root / "llvm-project", offload):
        refused = cli(root, "build", "check-hlsl-clang-vk", "--in", str(target),
                      "--platform", "windows-arm64", "--dxc", str(win), "--dry-run")
        assert refused.returncode != 0 and "run tests on Windows" in refused.stderr
    for action in ("test", "lit"):
        extra = ("clang-vk",) if action == "test" else ("clang/test",)
        refused = cli(root, action, *extra, "--in", str(offload),
                      "--platform", "windows-arm64")
        assert refused.returncode != 0
        assert "cross" in refused.stderr.lower() or "native" in refused.stderr.lower()
    assert files(root) == before
    assert not (root / "nix.log").exists()
    assert not (root / "cmake.log").exists()


@pytest.mark.parametrize("target", ["check", "check-hlsl", "test"])
@pytest.mark.parametrize("platform", ["linux-arm64", "windows-x64"])
def test_cross_cmake_test_aliases_refuse_before_provisioning(
    cross_env, monkeypatch, target, platform
):
    root = cross_env
    llvm = checkout(root, "llvm-project", "llvm")
    native_inputs(root)
    before = files(root)
    result = cli(root, "build", target, "--in", str(llvm),
                 "--platform", platform, "--dry-run")
    assert result.returncode != 0 and "test targets cannot run" in result.stderr
    assert files(root) == before
    assert not (root / "nix.log").exists()
