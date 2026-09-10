{
  pkgs,
  lib,
  config,
  ...
}:

let
  # ----------------------------------------------------------------------
  # Vulkan
  # ----------------------------------------------------------------------
  # The vk / clang-vk suites execute SPIR-V, so they need a driver (an "ICD").
  # Which one is not a Nix question -- it changes between machines and between
  # runs -- so the choice, and the reasons it has to be made at all, live in
  # `hlsl-vk` and scripts/hlsl-dev.sh. This file only says where Mesa's ICD
  # manifests and the validation layers are.
  mesaIcdDir = "${pkgs.mesa}/share/vulkan/icd.d";
  vulkanLayerPath = "${pkgs.vulkan-validation-layers}/share/vulkan/explicit_layer.d";

  # ----------------------------------------------------------------------
  # Build Dependencies
  # ----------------------------------------------------------------------
  # Python with the packages LLVM's lit testing framework and the scripts need.
  pythonDeps = pkgs.python3.withPackages (
    python-pkgs: with python-pkgs; [
      pyyaml
      virtualenv
    ]
  );

  # ----------------------------------------------------------------------
  # Task scripts
  # ----------------------------------------------------------------------
  # Every executable in scripts/tasks/ becomes an `hlsl-<name>` command, every
  # one in offloader-scripts/tasks/ an `offloader-<name>` command. The wrapper
  # devenv puts on PATH only dispatches: the body is read from the checkout at
  # run time, so editing a task takes effect immediately, and `--help` and the
  # option parsing come from scripts/hlsl-dev.sh.
  #
  # The one-line `# summary:` comment in each script is what `devenv info` and
  # `hlsl` (the umbrella command) list, so it lives in exactly one place.
  summaryOf =
    file:
    let
      hit = lib.findFirst (l: lib.hasPrefix "# summary: " l) null (
        lib.splitString "\n" (builtins.readFile file)
      );
    in
    if hit == null then "" else lib.removePrefix "# summary: " hit;

  mkTasks =
    { subdir, prefix }:
    let
      dir = ./. + "/${subdir}";
      isTask = fileName: type: type == "regular" && lib.hasSuffix ".sh" fileName;
    in
    lib.mapAttrs' (
      fileName: _:
      let
        base = lib.removeSuffix ".sh" fileName;
        # `hlsl.sh` is the umbrella command itself, not `hlsl-hlsl`.
        name = if base == prefix then base else "${prefix}-${base}";
      in
      lib.nameValuePair name {
        description = summaryOf (dir + "/${fileName}");
        exec = ''
          exec env HD_TASK_NAME=${name} \
            "''${DEVENV_ROOT:?not in the developer environment; run 'devenv shell'}/${subdir}/${fileName}" "$@"
        '';
      }
    ) (lib.filterAttrs isTask (builtins.readDir dir));

  # ----------------------------------------------------------------------
  # CMake Configurations
  # ----------------------------------------------------------------------
  # These lists are *templates*: the `$HD_...` placeholders are left unexpanded
  # in the environment and are filled in per invocation by scripts/hlsl-dev.sh,
  # once it has worked out which worktrees the command applies to. That is what
  # lets one flag list serve every worktree of a repository instead of one
  # hard-coded checkout.
  #
  #   HD_BUILD_TYPE      CMake build type for this build directory
  #   HD_INSTALL_PREFIX  install prefix for this build directory
  #   HD_LLVM_SRC        llvm-project worktree
  #   HD_LLVM_CMAKE_DIR  <llvm distribution prefix>/lib/cmake/llvm
  #   HD_DXC_SRC         DirectXShaderCompiler worktree
  #   HD_DXC_BIN_DIR     directory containing dxc/dxv
  #   HD_OFFLOAD_SRC     offload-test-suite worktree
  #   HD_GOLDEN_DIR      offload-golden-images worktree
  #
  # Keep the placeholders free of spaces and shell metacharacters; the expander
  # rejects anything fancier on purpose.

  # Shared by every build we drive.
  commonCMakeFlags = [
    "-G Ninja"
    "-DCMAKE_BUILD_TYPE=$HD_BUILD_TYPE"

    # Sccache integration for faster rebuilds. The cache is shared by every
    # worktree (see SCCACHE_DIR below), so a second agent building the same
    # sources in its own worktree mostly hits the cache.
    "-DCMAKE_C_COMPILER_LAUNCHER=${pkgs.sccache}/bin/sccache"
    "-DCMAKE_CXX_COMPILER_LAUNCHER=${pkgs.sccache}/bin/sccache"

    # Tooling support
    "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON" # Generates compile_commands.json for clangd
  ];

  # The integrated build: LLVM + Clang with the offload test suite pulled in as
  # an external project. Provides check-clang, check-llvm and the check-hlsl-*
  # suites out of a single build tree.
  llvmCMakeFlags = commonCMakeFlags ++ [
    "-DLLVM_ENABLE_ASSERTIONS=ON"
    "-DLLVM_ENABLE_LLD=ON"
    "-DLLVM_INCLUDE_SPIRV_TOOLS_TESTS=ON"
    "-DLLVM_INCLUDE_DXIL_TESTS=ON"
    "-DLLVM_OPTIMIZED_TABLEGEN=OFF" # Turn ON only for Debug configurations to save time
    "-DCMAKE_INSTALL_PREFIX=$HD_INSTALL_PREFIX"

    # Keep the build tree from being mostly duplicated debug info. A default
    # RelWithDebInfo tree is ~85% DWARF by size, copied into every one of the
    # ~150 executables that link the same static archives:
    #
    #   split DWARF   leaves the debug info in .dwo files next to the objects
    #                 instead of the linked binary. sccache (0.16) treats the
    #                 .dwo as an extra output and replays it on a cache hit,
    #                 so the shared .sccache/ still works.
    #   dylib linking builds one libLLVM.so and links the tools against it
    #                 rather than into them. It also flips CLANG_LINK_CLANG_DYLIB
    #                 (clang/CMakeLists.txt), which is where most of the mass is.
    #
    # Both cost link-time indirection, not compile time: LLVM_ENABLE_PIC is
    # already ON, so flipping them relinks rather than rebuilds. Deliberately
    # not in llvmDistCMakeFlags -- an install carries no .dwo files, and the
    # standalone distribution's component list enumerates the static LLVM
    # libraries that offload builds link against.
    "-DLLVM_USE_SPLIT_DWARF=ON"
    "-DLLVM_LINK_LLVM_DYLIB=ON"

    # Offload Test Suite & DXC Integration. DXC_EXECUTABLE/DXV_EXECUTABLE are
    # spelled out so that `hlsl-test --dxc <worktree>` can retarget an existing
    # build tree at another DXC without a fresh configure.
    "-DLLVM_EXTERNAL_PROJECTS=OffloadTest"
    "-DLLVM_EXTERNAL_OFFLOADTEST_SOURCE_DIR=$HD_OFFLOAD_SRC"
    "-DGOLDENIMAGE_DIR=$HD_GOLDEN_DIR"
    "-DOFFLOADTEST_TEST_CLANG=ON"
    "-DDXC_DIR=$HD_DXC_BIN_DIR"
    "-DDXC_EXECUTABLE=$HD_DXC_BIN_DIR/dxc"
    "-DDXV_EXECUTABLE=$HD_DXC_BIN_DIR/dxv"
    "-DOFFLOADTEST_USE_CLANG_TIDY=ON"
    "-DHLSL_ENABLE_OFFLOAD_DISTRIBUTION=ON"

    # HLSL cache. Must come last: the cache script reads the values set by the
    # -D flags above (see offload-test-suite/docs/offload-distribution.md).
    "-C $HD_LLVM_SRC/clang/cmake/caches/HLSL.cmake"
  ];

  # The LLVM half of the "Standalone Build Distribution" flow from
  # offload-test-suite/docs/offload-distribution.md: Clang, the lit testing
  # tools and the LLVM libraries the offload tools link against, installed into
  # a prefix that standalone offload builds consume.
  llvmDistCMakeFlags = commonCMakeFlags ++ [
    "-DLLVM_ENABLE_ASSERTIONS=ON"
    "-DLLVM_ENABLE_LLD=ON"
    "-DLLVM_OPTIMIZED_TABLEGEN=OFF"
    "-DCMAKE_INSTALL_PREFIX=$HD_INSTALL_PREFIX"
    "-C $HD_OFFLOAD_SRC/cmake/caches/StandaloneDistribution.cmake"
  ];

  # A standalone offload-test-suite build: the test suite is the top-level
  # CMake project and links against an installed LLVM distribution, which makes
  # configure+build a matter of minutes.
  offloadCMakeFlags = commonCMakeFlags ++ [
    "-DCMAKE_PREFIX_PATH=$HD_LLVM_CMAKE_DIR"
    "-DLLVM_MAIN_SRC_DIR=$HD_LLVM_SRC/llvm"
    "-DCMAKE_INSTALL_PREFIX=$HD_INSTALL_PREFIX"
    "-DOFFLOADTEST_TEST_CLANG=On"
    "-DGOLDENIMAGE_DIR=$HD_GOLDEN_DIR"
    "-DDXC_DIR=$HD_DXC_BIN_DIR"
    "-DDXC_EXECUTABLE=$HD_DXC_BIN_DIR/dxc"
    "-DDXV_EXECUTABLE=$HD_DXC_BIN_DIR/dxv"
    # clang-tidy is shipped by the distribution, but running it on every
    # translation unit defeats the point of the fast standalone loop.
    "-DOFFLOADTEST_USE_CLANG_TIDY=OFF"
  ];

  dxcCMakeFlags = commonCMakeFlags ++ [
    "-DHLSL_DISABLE_SOURCE_GENERATION=ON"
    "-C $HD_DXC_SRC/cmake/caches/PredefinedParams.cmake"
  ];

  flagsToString = builtins.concatStringsSep " ";
in
{
  name = "hlsl";

  # ------------------------------------------------------------------------
  # Toolchain
  # ------------------------------------------------------------------------
  # clang + lld natively, rather than the default gcc stdenv. This is also a
  # workaround for a known Nixpkgs issue:
  # https://github.com/NixOS/nixpkgs/issues/142901
  stdenv = pkgs.overrideCC pkgs.llvmPackages.stdenv (
    pkgs.llvmPackages.stdenv.cc.override { inherit (pkgs.llvmPackages) bintools; }
  );

  packages = with pkgs; [
    # Build tools
    cmake
    ninja
    sccache
    util-linux # flock, used to serialise builds of the same build dir

    # Required libraries & headers
    zlib
    libxml2
    spirv-tools
    directx-headers
    vulkan-headers
    vulkan-loader
    vulkan-validation-layers
    vulkan-tools # vulkaninfo, for inspecting ICD selection
    mesa # provides the lavapipe (lvp) software Vulkan ICD

    # Development / utility tools
    git
    worktrunk # `wt`: the worktrees every task resolves against
    pythonDeps
    cvise
    directx-shader-compiler
    clang-tools
    shellcheck # the task layer is bash; `devenv test` lints it
    nodejs_22 # Required for Compiler Explorer (pinned to v22 LTS)
  ];

  # ------------------------------------------------------------------------
  # Environment
  # ------------------------------------------------------------------------
  env = {
    # Everything downstream is expressed relative to the workspace root rather
    # than to $PWD, so tasks behave identically when run from a worktree
    # several directories away. devenv knows it exactly (DEVENV_ROOT).
    HLSL_DEV_ROOT = config.devenv.root;

    # CMake flag templates; see the "CMake Configurations" section above. The
    # $HD_* placeholders reach the environment unexpanded on purpose.
    HLSL_CMAKE_FLAGS_LLVM = flagsToString llvmCMakeFlags;
    HLSL_CMAKE_FLAGS_LLVM_DIST = flagsToString llvmDistCMakeFlags;
    HLSL_CMAKE_FLAGS_OFFLOAD = flagsToString offloadCMakeFlags;
    HLSL_CMAKE_FLAGS_DXC = flagsToString dxcCMakeFlags;

    # Fallback compiler for offload runs when no DirectXShaderCompiler worktree
    # has been built yet (`--dxc nix` selects it explicitly).
    HLSL_DXC_PREBUILT_DIR = "${pkgs.directx-shader-compiler}/bin";

    # Where `hlsl-vk` looks for Mesa's ICD manifests.
    HLSL_VK_ICD_DIR = mesaIcdDir;

    # Explicit layer manifests for `offloader -validation-layer`: the shell sets
    # no XDG_DATA_DIRS, so the loader would not find them by itself.
    VK_ADD_LAYER_PATH = vulkanLayerPath;
  };

  # No dotenv integration. Nothing here needs one any more: the choices tasks
  # make (the Vulkan driver, D3D12) live in .hlsl-dev/settings.env, where they
  # apply to the next command rather than the next shell, and the one secret is
  # secretspec's (see secretspec.toml). A `.env` would be a third place to look
  # for the same kind of thing. devenv still points it out if one appears.

  enterShell = ''
    # One compilation cache for all worktrees: parallel agents building the
    # same upstream sources share the hits.
    export SCCACHE_DIR="''${SCCACHE_DIR:-$DEVENV_ROOT/.sccache}"

    # --- Vulkan runtime -------------------------------------------
    # Pin the Vulkan loader to one driver, so that a plain vulkaninfo, offloader
    # or llvm-lit run in this shell is as safe as one made through a task (the
    # loader calls into *every* manifest it discovers, and under WSL one of them
    # segfaults). `hlsl-vk` owns the choice and the resolution; this only asks
    # it what the answer is.
    eval "$(hlsl-vk --export)"

    # clang-tidy runs its own bare frontend and ignores the cc-wrapper's
    # NIX_CFLAGS_COMPILE, so it cannot find libstdc++, glibc, directx-headers,
    # etc. (causing spurious 'file not found' errors on <cstddef>,
    # <wsl/wrladapter.h>, ...). Mirror the compiler's real system include
    # search list into the *_INCLUDE_PATH vars (which clang treats as -isystem,
    # so the normal build is unaffected) so clang-tidy resolves them too.
    _sysIncludes="$(c++ -E -x c++ - -v </dev/null 2>&1 \
      | awk '/#include <...> search starts here:/{f=1;next} /End of search list./{f=0} f{gsub(/^ +/,"");print}' \
      | paste -sd:)"
    export CPLUS_INCLUDE_PATH="''${_sysIncludes}''${CPLUS_INCLUDE_PATH:+:''${CPLUS_INCLUDE_PATH}}"
    export C_INCLUDE_PATH="''${_sysIncludes}''${C_INCLUDE_PATH:+:''${C_INCLUDE_PATH}}"
    unset _sysIncludes
  '';

  # ------------------------------------------------------------------------
  # Tasks
  # ------------------------------------------------------------------------
  scripts =
    mkTasks {
      subdir = "scripts/tasks";
      prefix = "hlsl";
    }
    // mkTasks {
      subdir = "offloader-scripts/tasks";
      prefix = "offloader";
    };

  # No `processes` here. Compiler Explorer is the one long-running thing in the
  # workspace, and it is `hlsl-compiler-explorer`: registering it as a devenv
  # process would also start it from `devenv test` -- which npm-installs the
  # checkout and boots a server -- and `devenv test` is what the dev container
  # runs on creation. One way to start it is also one way fewer to explain.

  # ------------------------------------------------------------------------
  # `devenv test`
  # ------------------------------------------------------------------------
  # A smoke test of the environment itself: no submodule, no build tree and no
  # GPU required, so it is also what the dev container runs after creation.
  #
  # The checks are devenv *tasks* rather than one shell hook, so each is named
  # and timed on its own, one failure does not hide the next (they are chained
  # with @completed, and `devenv test` still fails if any of them did), and one
  # can be re-run on its own while working on it:
  #
  #     devenv tasks run hlsl:check:shellcheck --mode single
  #
  # The chain is there for readable output: without it they interleave. It is
  # entered from enterTest below, by name, rather than by declaring
  # `before = [ "devenv:enterTest" ]` on each task: devenv 2.3 runs the tasks
  # that enterTest depends on when *entering the shell* too, which put a
  # 20-second ShellCheck run in front of every `direnv export` -- that is,
  # in front of every `podman exec` and every new terminal in the dev
  # container. Nothing here may run on shell entry.
  #
  # They run as their own processes, so they see what the *profile* provides.
  # What only a shell can see -- the variables enterShell exports -- is checked
  # by enterTest itself, below.
  #
  # Not here: the offloader-scripts unit tests. That tooling produces the
  # GitHub Pages report and has nothing to do with building a compiler, so it
  # is `offloader-test` when you are working on it, not a gate on every shell
  # in the workspace.
  tasks = {
    "hlsl:check:env" = {
      description = "Toolchain, workspace layout, CMake flag templates, GPU setup";
      showOutput = true;
      exec = ''
        set -e
        c++ --version | head -1
        cmake --version | head -1
        ninja --version
        sccache --version
        python3 --version
        wt --version >/dev/null && echo "worktrunk ok"

        test "$HLSL_DEV_ROOT" = "$DEVENV_ROOT"
        test -f "$HLSL_DEV_ROOT/devenv.nix"

        # The flag templates must reach the environment with their placeholders
        # intact: expanding them is scripts/hlsl-dev.sh's job, per invocation.
        case "$HLSL_CMAKE_FLAGS_LLVM" in
          *'-DCMAKE_BUILD_TYPE=$HD_BUILD_TYPE'*) ;;
          *) echo "HLSL_CMAKE_FLAGS_LLVM lost its placeholders" >&2; exit 1 ;;
        esac
        for v in HLSL_CMAKE_FLAGS_LLVM_DIST HLSL_CMAKE_FLAGS_OFFLOAD HLSL_CMAKE_FLAGS_DXC; do
          test -n "''${!v}" || { echo "$v is empty" >&2; exit 1; }
        done

        hlsl-vk
        eval "$(hlsl-vk --export)"
        test -e "''${VK_DRIVER_FILES:-$HLSL_VK_ICD_DIR}"
        hlsl-d3d12
      '';
    };

    "hlsl:check:tasks" = {
      description = "Every task is on PATH and answers --help";
      after = [ "hlsl:check:env@completed" ];
      showOutput = true;
      exec = ''
        set -e
        hlsl >/dev/null
        for t in "$DEVENV_ROOT"/scripts/tasks/*.sh; do
          t=$(basename "$t" .sh)
          [ "$t" = "hlsl" ] || "hlsl-$t" --help >/dev/null
        done
        for t in "$DEVENV_ROOT"/offloader-scripts/tasks/*.sh; do
          "offloader-$(basename "$t" .sh)" --help >/dev/null
        done
        hlsl-ls >/dev/null
        echo "$(hlsl | grep -c '^  [a-z]') tasks, all with --help"
      '';
    };

    "hlsl:check:plan" = {
      description = "Dry-run a configure and a build for each real checkout";
      after = [ "hlsl:check:tasks@completed" ];
      showOutput = true;
      exec = ''bash "$DEVENV_ROOT/scripts/tests/plan.test.sh"'';
    };

    "hlsl:check:selftest" = {
      description = "scripts/hlsl-dev.sh self-test (fake checkouts, no compiler)";
      after = [ "hlsl:check:plan@completed" ];
      showOutput = true;
      exec = ''bash "$DEVENV_ROOT/scripts/tests/hlsl-dev.test.sh"'';
    };

    "hlsl:check:trim" = {
      description = "hlsl-trim against a real build graph in a throwaway tree";
      after = [ "hlsl:check:selftest@completed" ];
      showOutput = true;
      exec = ''bash "$DEVENV_ROOT/scripts/tests/trim.test.sh"'';
    };

    "hlsl:check:hook" = {
      description = "The clang-format hook, driven by a real commit";
      after = [ "hlsl:check:trim@completed" ];
      showOutput = true;
      exec = ''bash "$DEVENV_ROOT/scripts/tests/format-hook.test.sh"'';
    };

    # Last: it is by far the slowest, and everything above gives faster
    # feedback on the thing you just changed.
    "hlsl:check:shellcheck" = {
      description = "ShellCheck over the task layer";
      after = [ "hlsl:check:hook@completed" ];
      showOutput = true;
      exec = ''
        set -e
        shellcheck --shell=bash "$DEVENV_ROOT"/scripts/hlsl-dev.sh \
          "$DEVENV_ROOT"/scripts/tasks/*.sh "$DEVENV_ROOT"/offloader-scripts/tasks/*.sh \
          "$DEVENV_ROOT"/scripts/tests/*.sh
        echo "clean"
      '';
    };

    # Keep the clang-format pre-commit hook in place in every checkout that has
    # a .clang-format. It warns and lets the commit through; `hlsl-format` is
    # the same check by hand. Opt out with HLSL_INSTALL_HOOKS=0 (in .env, say).
    "hlsl:hooks" = {
      description = "Install the clang-format pre-commit hook in the checkouts";
      after = [ "devenv:enterShell" ];
      showOutput = true;
      status = ''
        [ "''${HLSL_INSTALL_HOOKS:-1}" = "0" ] || hlsl-format --check-hooks
      '';
      exec = ''hlsl-format --install-hooks'';
    };

    # Not part of the tests: a hint on shell entry, once, when the workspace has
    # no sources yet. `status` exiting 0 skips the task, so it costs one `test`
    # per shell in the normal case.
    "hlsl:submodules" = {
      description = "Point at hlsl-setup when the submodules are not checked out";
      after = [ "devenv:enterShell" ];
      showOutput = true;
      status = ''test -f "$DEVENV_ROOT/llvm-project/llvm/CMakeLists.txt"'';
      exec = ''
        echo "The submodules are not checked out yet: run 'hlsl-setup'."
      '';
    };
  };

  # What is left for the shell hook is what only a shell has: the variables
  # enterShell exports -- and running the check chain, which hangs off its last
  # link: `--mode before` (the default) pulls in everything it depends on, in
  # order.
  enterTest = ''
    set -e
    test -n "$SCCACHE_DIR"
    test -e "''${VK_DRIVER_FILES:-$HLSL_VK_ICD_DIR}"
    echo "shell environment ok"
    devenv tasks run hlsl:check:shellcheck
  '';

  # ------------------------------------------------------------------------
  # Dev container
  # ------------------------------------------------------------------------
  # `.devcontainer/devcontainer.json` is generated from this: devenv writes it
  # on shell entry, so edit it here, never there. The image ships Nix and
  # devenv; everything else -- cmake, ninja, clang, lld, sccache, wt, the
  # Vulkan loader and lavapipe -- comes from this file, so what you build
  # inside the container is what you build outside it.
  devcontainer.enable = true;
  devcontainer.settings = {
    name = "hlsl-dev";

    # The workspace is mounted where the host keeps it, not under /workspaces.
    #
    # Everything in a build tree is absolute: CMakeCache.txt records the
    # directory it was created in, ninja records the source paths it was given,
    # compile_commands.json records both, and a `wt` worktree's .git file
    # records the gitdir it belongs to. Mount the same tree somewhere else and
    # all of it points into thin air -- a `hlsl-build` in a worktree the host
    # configured fails with "the current CMakeCache.txt directory is different
    # than the directory where CMakeCache.txt was created", and git in that
    # worktree with "not a git repository". Path parity makes the container and
    # the host interchangeable instead: the same build trees, the same caches,
    # the same git.
    #
    # ${localWorkspaceFolder} is wherever the workspace is on the machine
    # running the container, so this stays correct on someone else's checkout
    # and on Codespaces.
    workspaceMount = "source=\${localWorkspaceFolder},target=\${localWorkspaceFolder},type=bind";
    workspaceFolder = "\${localWorkspaceFolder}";

    # For `gh auth token`, the easy way to feed offloader-scripts.
    features."ghcr.io/devcontainers/features/github-cli:1" = { };

    # Reap the process trees that ninja / lit leave behind.
    init = true;

    # gdb/lldb on clang and the offload tools.
    capAdd = [ "SYS_PTRACE" ];
    securityOpt = [ "seccomp=unconfined" ];

    # Codespaces only; building LLVM is the constraint.
    hostRequirements = {
      cpus = 8;
      memory = "16gb";
      storage = "128gb";
    };

    # One compilation cache for every worktree, kept across rebuilds -- and a
    # devenv/direnv state directory of this container's own.
    #
    # `.devenv/` and `.direnv/` are per-environment caches that happen to sit
    # in the workspace, so the bind mount would have the container and the host
    # writing to the same ones. They do not agree: the image ships a different
    # devenv than the host is likely to have (2.3.0 against 2.2.2 here), and
    # the two generate different `.devenv/bootstrap/*.nix` -- which direnv
    # watches. Every command on one side then rewrote them and made the other
    # side's direnv reload the whole environment at its next prompt or `cd`,
    # back and forth. Volumes keep each environment's cache to itself.
    mounts = [
      "source=hlsl-dev-sccache,target=\${containerWorkspaceFolder}/.sccache,type=volume"
      "source=hlsl-dev-devenv,target=\${containerWorkspaceFolder}/.devenv,type=volume"
      "source=hlsl-dev-direnv,target=\${containerWorkspaceFolder}/.direnv,type=volume"
    ];

    # HLSL_VK_DRIVER is deliberately not set here: lavapipe is the default
    # anyway, and an ambient value would override what `hlsl-vk` records.
    containerEnv.SCCACHE_IDLE_TIMEOUT = "0";

    # Builds here go to <worktree>/build-container, not <worktree>/build.
    #
    # The workspace is mounted at its host path, so a build tree made on the
    # host is otherwise usable in here -- except for build dependency
    # differences between host and container such as on WSL where the offload
    # suite finds D3D12 and links /usr/lib/wsl/lib/libd3d12core.so,
    # which does not exist in this container, and every llvm worktree in this
    # workspace carries the offload suite in-tree. A second build directory is
    # the portable half of that trade: it configures itself, finds no D3D12, and
    # leaves the host's tree alone.
    #
    # It is the *name* rather than one directory, so it holds for every
    # worktree: what `hlsl-ls` calls built in here is what is built in here,
    # and the same command on the host still reports the host's trees. What
    # they do share is the memory of what builds against what,
    # <llvm worktree>/build-dist, the plain-LLVM distribution an offload
    # worktree builds against, and <worktree>/compile_commands.json -- the
    # symlink a configure leaves at the root of the worktree, because clangd
    # looks in `build/` and its own directory and nowhere else, so an editor
    # opened on the host would otherwise never find what was built in here
    # (see hd_link_cdb in scripts/hlsl-dev.sh).
    containerEnv.HLSL_BUILD_DIR_NAME = "build-container";

    # Empty when unset on the host, which offloader-scripts reads as "no token".
    remoteEnv = {
      GH_TOKEN = "\${localEnv:GH_TOKEN}";
      GITHUB_TOKEN = "\${localEnv:GITHUB_TOKEN}";
    };

    # The image already activates the environment for interactive shells: it
    # ends ~/.bashrc with direnv's hook (`.envrc` says `use devenv`), Ubuntu's
    # ~/.profile already sources ~/.bashrc, and ~/.config/direnv/config.toml
    # whitelists /workspaces -- so no hook, no allow, nothing to append. What
    # is left is git: the workspace, the submodules and any `wt` worktree can
    # be owned by a foreign uid, depending on how the container is run.
    postCreateCommand = ''
      set -eu
      git config --global --get-all safe.directory | grep -qx '\*' || git config --global --add safe.directory '*'
    '';

    # The other half: interactive shells pick the hook up from ~/.bashrc, but
    # `podman exec` / `devcontainer exec` run *non-interactive* shells, whose
    # commands already expect the toolchain on PATH while direnv's hook only
    # fires at an interactive prompt -- so those execs would see the bare
    # image. This profile.d snippet exports the environment for them, and
    # quietly -- direnv narrates on stderr, hence 2>/dev/null. Interactive
    # shells take the `*i*` branch and keep the hook, banner and all. The
    # export is done from the workspace folder because an exec's working
    # directory is often `/`, where there is no `.envrc` to find; and it is
    # guarded on bash because /etc/profile is also read by dash, which cannot
    # eval everything `direnv export bash` emits.
    #
    # onCreate, so that it is in place before updateContentCommand: a failing
    # `devenv test` stops the rest of the lifecycle, and a container without an
    # environment cannot even be used to investigate why. /etc belongs to the
    # container rather than the image, so a rebuild writes it again.
    #
    # `direnv allow` is needed because the workspace is mounted where the host
    # keeps it: the image whitelists /workspaces in ~/.config/direnv/config.toml
    # and nothing else, so without it direnv refuses the `.envrc` and every
    # shell here is the bare image.
    onCreateCommand = ''
      set -eu
      direnv allow
      printf '%s\n' \
        '# Generated by devenv.nix (devcontainer.settings.onCreateCommand).' \
        '# Non-interactive shells (podman exec, devcontainer exec) get the' \
        '# project environment here; interactive ones get the direnv hook.' \
        'if [ -n "$BASH_VERSION" ]; then' \
        '  case $- in' \
        '    *i*) ;;' \
        "    *) eval \"\$(cd '$PWD' 2>/dev/null && direnv export bash 2>/dev/null)\" ;;" \
        '  esac' \
        'fi' \
        | sudo tee /etc/profile.d/10-direnv.sh >/dev/null
    '';

    # `devcontainer exec` takes the environment from a probe shell rather than
    # from the command's own shell; a login (non-interactive) one is the shell
    # the snippet above is written for. The default, loginInteractiveShell,
    # would take the `*i*` branch and capture nothing, because direnv's hook
    # only runs at a prompt.
    userEnvProbe = "loginShell";

    # Realises the whole environment (several GB, tens of minutes cold) and
    # smoke-tests it. On Codespaces this happens during a prebuild.
    updateContentCommand = "devenv test";

    # Compiler Explorer. Numeric: string entries are not honoured everywhere.
    forwardPorts = [ 10240 ];

    # No editor-specific configuration: the config is plain dev container spec,
    # so any implementation that reads it produces the same container.
    customizations.vscode.extensions = [ ];
  };
}
