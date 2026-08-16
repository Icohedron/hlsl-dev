{
  description = "Developer environment for LLVM HLSL and DirectXShaderCompiler";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        # ----------------------------------------------------------------------
        # Vulkan Runtime Configuration
        # ----------------------------------------------------------------------
        # The `check-hlsl-vk` / `check-hlsl-clang-vk` suites run compiled SPIR-V
        # on a real Vulkan implementation, so the dev shell has to decide which
        # driver ("ICD") the Vulkan loader will use.
        #
        # Why this needs handling at all: the loader loads *every* ICD manifest
        # it discovers and calls into each one from vkEnumeratePhysicalDevices.
        # A single broken driver therefore takes down the whole process. On WSL
        # that is exactly what happens -- Mesa's `dzn` (Vulkan-on-D3D12) ICD is
        # present, fails to create a D3D12 device, and then segfaults inside
        # dzn_physical_device_create, so no test can run.
        #
        # `offloader` cannot work around this: its `-adapter-regex` flag (and
        # lit's OFFLOADTEST_GPU_NAME, which forwards to it) filters the device
        # list *after* enumeration, i.e. after the crash. The only lever is the
        # loader's own VK_DRIVER_FILES / VK_ICD_FILENAMES, which restrict it to
        # an explicit set of manifests. offload-test-suite/test/lit.cfg.py
        # already forwards both variables into the test environment.
        #
        # So the shell pins a known-good ICD by default (lavapipe, Mesa's CPU
        # rasterizer) and exposes HLSL_VK_DRIVER as the single knob to change it.
        mesaIcdDir = "${pkgs.mesa}/share/vulkan/icd.d";
        vkArch = pkgs.stdenv.hostPlatform.parsed.cpu.name;

        # Explicit layer manifests (validation layers) for `offloader
        # -validation-layer`. mkShell does not set XDG_DATA_DIRS, so the loader
        # would not find them otherwise, and the layer would be silently skipped.
        vulkanLayerPath = "${pkgs.vulkan-validation-layers}/share/vulkan/explicit_layer.d";

        # ----------------------------------------------------------------------
        # Build Dependencies
        # ----------------------------------------------------------------------
        # Python with necessary packages for LLVM's lit testing framework and scripts.
        pythonDeps = pkgs.python3.withPackages (
          python-pkgs: with python-pkgs; [
            pyyaml
            virtualenv
          ]
        );

        # ----------------------------------------------------------------------
        # `mask` wrapper
        # ----------------------------------------------------------------------
        # `mask` only looks for a maskfile.md in the *current* directory, but
        # the tasks are meant to be run from inside any worktree
        # (llvm-project.my-feature/, offload-test-suite.my-feature/, ...),
        # possibly outside the workspace root entirely. This wrapper points
        # mask at the workspace maskfile whenever the current directory does
        # not have one of its own, so `mask build` works everywhere.
        maskWrapper = pkgs.writeShellScriptBin "mask" ''
          real=${pkgs.mask}/bin/mask
          # An explicit --maskfile, or a maskfile.md in the current directory,
          # always wins: never surprise a caller who knows what they want.
          for arg in "$@"; do
            if [ "$arg" = "--maskfile" ]; then
              exec "$real" "$@"
            fi
          done
          if [ ! -f ./maskfile.md ] &&
             [ -n "''${HLSL_DEV_ROOT:-}" ] &&
             [ -f "$HLSL_DEV_ROOT/maskfile.md" ]; then
            exec "$real" --maskfile "$HLSL_DEV_ROOT/maskfile.md" "$@"
          fi
          exec "$real" "$@"
        '';

        # All packages exposed to the Nix shell environment.
        devShellPackages = with pkgs; [
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
          worktrunk
          pythonDeps
          cvise
          directx-shader-compiler
          clang-tools
          maskWrapper # Must precede `mask` so it wins on PATH
          mask # Used for task automation
          nodejs_22 # Required for Compiler Explorer (pinned to v22 LTS)
        ];

        # ----------------------------------------------------------------------
        # CMake Configurations
        # ----------------------------------------------------------------------
        # These lists are *templates*: the `$HD_...` placeholders are left
        # unexpanded in the environment and are filled in per invocation by
        # scripts/hlsl-dev.sh, once it has worked out which worktrees the
        # command applies to. That is what lets one flag list serve every
        # worktree of a repository instead of one hard-coded checkout.
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
        # Keep the placeholders free of spaces and shell metacharacters; the
        # expander rejects anything fancier on purpose.

        # Shared by every build we drive.
        commonCMakeFlags = [
          "-G Ninja"
          "-DCMAKE_BUILD_TYPE=$HD_BUILD_TYPE"

          # Sccache integration for faster rebuilds. The cache is shared by
          # every worktree (see SCCACHE_DIR below), so a second agent building
          # the same sources in its own worktree mostly hits the cache.
          "-DCMAKE_C_COMPILER_LAUNCHER=${pkgs.sccache}/bin/sccache"
          "-DCMAKE_CXX_COMPILER_LAUNCHER=${pkgs.sccache}/bin/sccache"

          # Tooling support
          "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON" # Generates compile_commands.json for clangd
        ];

        # The integrated build: LLVM + Clang with the offload test suite pulled
        # in as an external project. Provides check-clang, check-llvm and the
        # check-hlsl-* suites out of a single build tree.
        llvmCMakeFlags = commonCMakeFlags ++ [
          "-DLLVM_ENABLE_ASSERTIONS=ON"
          "-DLLVM_ENABLE_LLD=ON"
          "-DLLVM_INCLUDE_SPIRV_TOOLS_TESTS=ON"
          "-DLLVM_INCLUDE_DXIL_TESTS=ON"
          "-DLLVM_OPTIMIZED_TABLEGEN=OFF" # Turn ON only for Debug configurations to save time
          "-DCMAKE_INSTALL_PREFIX=$HD_INSTALL_PREFIX"

          # Offload Test Suite & DXC Integration. DXC_EXECUTABLE/DXV_EXECUTABLE
          # are spelled out so that `mask test --dxc <worktree>` can retarget an
          # existing build tree at another DXC without a fresh configure.
          "-DLLVM_EXTERNAL_PROJECTS=OffloadTest"
          "-DLLVM_EXTERNAL_OFFLOADTEST_SOURCE_DIR=$HD_OFFLOAD_SRC"
          "-DGOLDENIMAGE_DIR=$HD_GOLDEN_DIR"
          "-DOFFLOADTEST_TEST_CLANG=ON"
          "-DDXC_DIR=$HD_DXC_BIN_DIR"
          "-DDXC_EXECUTABLE=$HD_DXC_BIN_DIR/dxc"
          "-DDXV_EXECUTABLE=$HD_DXC_BIN_DIR/dxv"
          "-DOFFLOADTEST_USE_CLANG_TIDY=ON"
          "-DHLSL_ENABLE_OFFLOAD_DISTRIBUTION=ON"

          # HLSL cache. Must come last: the cache script reads the values set
          # by the -D flags above (see offload-test-suite/docs/offload-distribution.md).
          "-C $HD_LLVM_SRC/clang/cmake/caches/HLSL.cmake"
        ];

        # The LLVM half of the "Standalone Build Distribution" flow from
        # offload-test-suite/docs/offload-distribution.md: Clang, the lit
        # testing tools and the LLVM libraries the offload tools link against,
        # installed into a prefix that standalone offload builds consume.
        llvmDistCMakeFlags = commonCMakeFlags ++ [
          "-DLLVM_ENABLE_ASSERTIONS=ON"
          "-DLLVM_ENABLE_LLD=ON"
          "-DLLVM_OPTIMIZED_TABLEGEN=OFF"
          "-DCMAKE_INSTALL_PREFIX=$HD_INSTALL_PREFIX"
          "-C $HD_OFFLOAD_SRC/cmake/caches/StandaloneDistribution.cmake"
        ];

        # A standalone offload-test-suite build: the test suite is the
        # top-level CMake project and links against an installed LLVM
        # distribution, which makes configure+build a matter of minutes.
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
        # ----------------------------------------------------------------------
        # Development Shell Definition
        # ----------------------------------------------------------------------
        # We override the stdenv to use clang + lld natively. This is a workaround
        # for a known Nixpkgs issue: https://github.com/NixOS/nixpkgs/issues/142901
        devShell =
          let
            llvmPkg = pkgs.llvmPackages;
          in
          pkgs.mkShell.override
            {
              stdenv = pkgs.overrideCC llvmPkg.stdenv (
                llvmPkg.stdenv.cc.override { inherit (llvmPkg) bintools; }
              );
            }
            {
              name = "hlsl";

              buildInputs = devShellPackages;

              # Resolve project paths to absolute paths at shell entry time
              # and export CMake flag variables for `mask` tasks.
              shellHook = ''
                # --- Workspace layout ------------------------------------------
                # Everything downstream is expressed relative to the workspace
                # root rather than to $PWD, so tasks behave identically when run
                # from a worktree several directories away.
                export HLSL_DEV_ROOT="$PWD"

                # CMake flag templates; see the "CMake Configurations" section
                # above. Single-quoted so the $HD_* placeholders survive into
                # the environment unexpanded.
                export HLSL_CMAKE_FLAGS_LLVM='${flagsToString llvmCMakeFlags}'
                export HLSL_CMAKE_FLAGS_LLVM_DIST='${flagsToString llvmDistCMakeFlags}'
                export HLSL_CMAKE_FLAGS_OFFLOAD='${flagsToString offloadCMakeFlags}'
                export HLSL_CMAKE_FLAGS_DXC='${flagsToString dxcCMakeFlags}'

                # Fallback compiler for offload runs when no DirectXShaderCompiler
                # worktree has been built yet (`--dxc nix` selects it explicitly).
                export HLSL_DXC_PREBUILT_DIR="${pkgs.directx-shader-compiler}/bin"

                # One compilation cache for all worktrees: parallel agents
                # building the same upstream sources share the hits.
                export SCCACHE_DIR="''${SCCACHE_DIR:-$HLSL_DEV_ROOT/.sccache}"

                # --- Vulkan runtime -------------------------------------------
                # Pick the Vulkan driver (ICD) that the offload test suite runs
                # against. See the "Vulkan Runtime Configuration" section above
                # for why this is pinned rather than left to the loader.
                #
                # HLSL_VK_DRIVER accepts:
                #   lavapipe        Mesa's CPU rasterizer (default; slow but
                #                   always works, including under WSL)
                #   system          Let the loader discover drivers itself, i.e.
                #                   use the real GPU. Do this on a machine with a
                #                   working native driver.
                #   <name>          A Mesa ICD short name: radeon, intel,
                #                   nouveau, dzn, ... (resolved below)
                #   /path/to.json   Any ICD manifest, e.g. a vendor driver
                #                   outside of Mesa
                #
                # Change it with `mask vk-use <driver>` (persists to .env and
                # triggers a direnv reload), or per-command with
                # `HLSL_VK_DRIVER=system nix develop`. Inspect the result with
                # `mask vk-info`, list the options with `mask vk-list`.
                #
                # For a single test run you can also bypass this entirely and set
                # VK_DRIVER_FILES directly -- lit forwards it to `offloader`.
                export VK_ADD_LAYER_PATH="${vulkanLayerPath}''${VK_ADD_LAYER_PATH:+:''${VK_ADD_LAYER_PATH}}"
                export HLSL_VK_ICD_DIR="${mesaIcdDir}"
                case "''${HLSL_VK_DRIVER:-lavapipe}" in
                  system) _icd="" ;;
                  lavapipe | lvp) _icd="$HLSL_VK_ICD_DIR/lvp_icd.${vkArch}.json" ;;
                  /*) _icd="$HLSL_VK_DRIVER" ;;
                  *) _icd="$HLSL_VK_ICD_DIR/''${HLSL_VK_DRIVER}_icd.${vkArch}.json" ;;
                esac
                if [ -z "$_icd" ]; then
                  unset VK_DRIVER_FILES VK_ICD_FILENAMES
                elif [ ! -e "$_icd" ]; then
                  echo "warning: HLSL_VK_DRIVER='$HLSL_VK_DRIVER' resolves to a" \
                       "missing ICD manifest ($_icd); run 'mask vk-list'" >&2
                else
                  # VK_DRIVER_FILES is honoured by loader >= 1.3.207;
                  # VK_ICD_FILENAMES is the legacy name, kept for older loaders.
                  export VK_DRIVER_FILES="$_icd"
                  export VK_ICD_FILENAMES="$_icd"
                fi
                unset _icd

                # clang-tidy runs its own bare frontend and ignores the
                # cc-wrapper's NIX_CFLAGS_COMPILE, so it cannot find libstdc++,
                # glibc, directx-headers, etc. (causing spurious 'file not found'
                # errors on <cstddef>, <wsl/wrladapter.h>, ...). Mirror the
                # compiler's real system include search list into the
                # *_INCLUDE_PATH vars (which clang treats as -isystem, so the
                # normal build is unaffected) so clang-tidy resolves them too.
                _sysIncludes="$(c++ -E -x c++ - -v </dev/null 2>&1 \
                  | awk '/#include <...> search starts here:/{f=1;next} /End of search list./{f=0} f{gsub(/^ +/,"");print}' \
                  | paste -sd:)"
                export CPLUS_INCLUDE_PATH="''${_sysIncludes}''${CPLUS_INCLUDE_PATH:+:''${CPLUS_INCLUDE_PATH}}"
                export C_INCLUDE_PATH="''${_sysIncludes}''${C_INCLUDE_PATH:+:''${C_INCLUDE_PATH}}"
                unset _sysIncludes
              '';
            };
      }
    );
}
