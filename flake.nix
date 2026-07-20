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
            llvmPkg = pkgs.llvmPackages_22;
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
