{
  description = "Developer environment for LLVM HLSL and DirectXShaderCompiler";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    flake-compat = {
      url = "github:edolstra/flake-compat";
      flake = false;
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
      flake-compat,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        # ----------------------------------------------------------------------
        # Project Paths
        # ----------------------------------------------------------------------
        # These point to the local git submodules where the source code lives.
        LLVMDir = "./llvm-project";
        DXCDir = "./DirectXShaderCompiler";
        OffloadTestDir = "./offload-test-suite";
        GoldenImagesDir = "./offload-golden-images";

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
          vkd3d-proton

          # Development / utility tools
          pythonDeps
          cvise
          directx-shader-compiler
          clang-tools
          mask # Used for task automation
        ];

        # ----------------------------------------------------------------------
        # CMake Configurations
        # ----------------------------------------------------------------------
        # We define flags here as a Nix list. When exported to the shell, Nix
        # automatically joins them with spaces, making them usable as shell variables.

        LLVMCMakeFlags = [
          # Base LLVM build options
          "-C ${LLVMDir}/clang/cmake/caches/HLSL.cmake"
          "-G Ninja"
          "-DCMAKE_BUILD_TYPE=RelWithDebInfo"
          "-DLLVM_ENABLE_ASSERTIONS=ON"
          "-DLLVM_ENABLE_LLD=ON"
          "-DLLVM_INCLUDE_SPIRV_TOOLS_TESTS=ON"
          "-DLLVM_INCLUDE_DXIL_TESTS=ON"
          "-DLLVM_OPTIMIZED_TABLEGEN=OFF" # Turn ON only for Debug configurations to save time

          # Sccache integration for faster rebuilds
          "-DCMAKE_C_COMPILER_LAUNCHER=${pkgs.sccache}/bin/sccache"
          "-DCMAKE_CXX_COMPILER_LAUNCHER=${pkgs.sccache}/bin/sccache"

          # Tooling support
          "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON" # Generates compile_commands.json for clangd

          # Offload Test Suite & DXC Integration
          "-DLLVM_EXTERNAL_PROJECTS=OffloadTest"
          "-DLLVM_EXTERNAL_OFFLOADTEST_SOURCE_DIR=${OffloadTestDir}"
          "-DGOLDENIMAGE_DIR=${GoldenImagesDir}"
          "-DOFFLOADTEST_TEST_CLANG=ON"
          "-DDXC_DIR=${DXCDir}/build/bin"
        ];

        DXCCMakeFlags = [
          # DirectXShaderCompiler build options
          "-C ${DXCDir}/cmake/caches/PredefinedParams.cmake"
          "-G Ninja"
          "-DCMAKE_BUILD_TYPE=RelWithDebInfo"
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
          pkgs.mkShell.override
            {
              stdenv = pkgs.overrideCC pkgs.llvmPackages.stdenv (
                pkgs.llvmPackages.stdenv.cc.override { inherit (pkgs.llvmPackages) bintools; }
              );
            }
            {
              name = "llvm-hlsl-env";

              buildInputs = devShellPackages;

              # Export these variables to the shell so `mask` tasks can utilize them
              inherit LLVMCMakeFlags DXCCMakeFlags;

              DXC_LIBS_DIR = "${DXCDir}/build/lib";
            };
      }
    );
}
