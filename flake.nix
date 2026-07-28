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

        # All packages exposed to the Nix shell environment.
        devShellPackages = with pkgs; [
          # Build tools
          cmake
          ninja
          sccache

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
          pythonDeps
          cvise
          directx-shader-compiler
          clang-tools
          mask # Used for task automation
          nodejs_22 # Required for Compiler Explorer (pinned to v22 LTS)
        ];

        # ----------------------------------------------------------------------
        # CMake Configurations
        # ----------------------------------------------------------------------
        # These are defined as functions of a root directory so that the
        # shellHook can materialise them with the real workspace path.

        mkLLVMCMakeFlags = root: [
          # Base LLVM build options
          "-G Ninja"
          "-DLLVM_ENABLE_ASSERTIONS=ON"
          "-DLLVM_ENABLE_LLD=ON"
          "-DLLVM_INCLUDE_SPIRV_TOOLS_TESTS=ON"
          "-DLLVM_INCLUDE_DXIL_TESTS=ON"
          "-DLLVM_OPTIMIZED_TABLEGEN=OFF" # Turn ON only for Debug configurations to save time
          "-DCMAKE_INSTALL_PREFIX=${root}/llvm-project/build/install"

          # Sccache integration for faster rebuilds
          "-DCMAKE_C_COMPILER_LAUNCHER=${pkgs.sccache}/bin/sccache"
          "-DCMAKE_CXX_COMPILER_LAUNCHER=${pkgs.sccache}/bin/sccache"

          # Tooling support
          "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON" # Generates compile_commands.json for clangd

          # Offload Test Suite & DXC Integration
          "-DLLVM_EXTERNAL_PROJECTS=OffloadTest"
          "-DLLVM_EXTERNAL_OFFLOADTEST_SOURCE_DIR=${root}/offload-test-suite"
          "-DGOLDENIMAGE_DIR=${root}/offload-golden-images"
          "-DOFFLOADTEST_TEST_CLANG=ON"
          "-DDXC_DIR=${root}/DirectXShaderCompiler/build/bin"
          "-DOFFLOADTEST_USE_CLANG_TIDY=ON"
          "-DHLSL_ENABLE_OFFLOAD_DISTRIBUTION=ON"

          # HLSL cache
          "-C ${root}/llvm-project/clang/cmake/caches/HLSL.cmake"
        ];

        mkDXCCMakeFlags = root: [
          # DirectXShaderCompiler build options
          "-C ${root}/DirectXShaderCompiler/cmake/caches/PredefinedParams.cmake"
          "-G Ninja"
          "-DHLSL_DISABLE_SOURCE_GENERATION=ON"
        ];

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
                export LLVMCMakeFlags="${builtins.concatStringsSep " " (mkLLVMCMakeFlags "\$PWD")}"
                export DXCCMakeFlags="${builtins.concatStringsSep " " (mkDXCCMakeFlags "\$PWD")}"
                export DXC_LIBS_DIR="$PWD/DirectXShaderCompiler/build/lib"

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
