# CMake toolchain files for the cross-compilation platforms `hlsl-cross` lists.
#
# This is deliberately *not* part of devenv.nix: everything devenv.nix
# references is realised when the environment is entered, and a cross toolchain
# (the MSVC SDK above all) is gigabytes that most sessions never touch. The
# tasks build one on demand instead --
#
#     nix-build scripts/cross/toolchains.nix \
#       --argstr nixpkgs "$HLSL_NIXPKGS_PATH" --argstr platform windows-x64 \
#       -o .hlsl-dev/toolchains/windows-x64
#
# -- and the result is a directory holding `toolchain.cmake`. The `-o` symlink
# is both the cache and the GC root, so the second configure costs nothing and
# a `nix-collect-garbage` does not take the toolchain with it.
#
# $HLSL_NIXPKGS_PATH is the *pinned* nixpkgs of this workspace (devenv.nix
# exports `pkgs.path`), so a toolchain comes from the same revision as the rest
# of the environment and nothing here depends on a channel.
#
# The platforms:
#
#   linux-arm64     aarch64-unknown-linux-gnu   nixpkgs' cross gcc
#   linux-x64       x86_64-unknown-linux-gnu    nixpkgs' cross gcc
#   windows-x64     x86_64-pc-windows-msvc      clang-cl + nixpkgs' windows.sdk
#   windows-arm64   aarch64-pc-windows-msvc     clang-cl + nixpkgs' windows.sdk
#
# The two Linux platforms are symmetric on purpose: whichever of them is the
# machine you are on is the *native* build, and the other is the cross one, so
# this file reads the same on an x86-64 workstation and on an ARM laptop.
# `hd_host_platform` in scripts/hlsl-dev.sh is what decides which is which.
#
# The Windows pair needs Microsoft's headers and import libraries. nixpkgs has
# them (`windows.sdk`: an `xwin` splat of the official packages), but they are
# unfree and gated on accepting the Visual Studio licence, which is a decision
# for the person building, not for this file: `acceptMsvcLicense` is false
# here, and `hlsl-cross --accept-msvc-license` is what turns it on, once, in
# the workspace settings.
#
# There is no MinGW (GNU-ABI Windows) platform, although nixpkgs has those
# toolchains and they did work. Everything worth cross-compiling for Windows
# here needs D3D12, and D3D12 is reached through MSVC import libraries
# (d3d12.lib, dxguid.lib, dxcore.lib) out of the Windows SDK: the offload test
# suite refuses to configure without a runtime API for the target, and DXC's
# CMake does find_package(D3D12 REQUIRED) on any Windows target. A MinGW build
# could therefore answer "does clang still compile for Windows" and nothing
# else, while its binaries -- a different ABI -- could not be run beside a DXC
# or a runtime built the normal way. The MSVC licence is the price of a Windows
# build that means something.
{
  nixpkgs ? <nixpkgs>,
  platform,
  system ? builtins.currentSystem,
  acceptMsvcLicense ? false,
}:

let
  pkgs = import nixpkgs {
    inherit system;
    config = {
      allowUnfree = acceptMsvcLicense;
      microsoftVisualStudioLicenseAccepted = acceptMsvcLicense;
    };
  };
  inherit (pkgs) lib;

  # ------------------------------------------------------------------------
  # MSVC-ABI Windows: the environment's own clang, driven as clang-cl
  # ------------------------------------------------------------------------
  # No cross compiler is built at all -- clang is already one, and what a Linux
  # machine lacks is only Microsoft's headers and import libraries, which
  # `windows.sdk` provides in the layout clang-cl's /vctoolsdir and /winsdkdir
  # expect. That is what makes this the default route for Windows: it is a text
  # file plus a download, where an aarch64 GNU toolchain is a source build.
  #
  # clang-cl rather than plain clang with an MSVC triple, because the MSVC ABI
  # is also an MSVC *command line*: CMake and LLVM's own CMake both switch on
  # "is this compiler MSVC-like", and driving clang the other way puts those
  # two answers in conflict.
  msvc =
    {
      triple,
      processor,
      sdk,
      dlltoolMachine,
    }:
    let
      inherit (pkgs.llvmPackages) clang-unwrapped lld llvm;

      # Vulkan for a Windows target. The headers are architecture- and
      # OS-independent, so nixpkgs' are the right ones; what is missing is
      # `vulkan-1.lib`, the import library every Windows Vulkan application
      # links against, which normally comes from LunarG's SDK.
      #
      # It does not have to: an import library is a list of exported names, and
      # the loader publishes exactly that list as `loader/vulkan-1.def`
      # (Apache-2.0, in the source nixpkgs already fetches). llvm-dlltool turns
      # the one into the other in a second, per architecture. At run time the
      # real `vulkan-1.dll` comes from the machine's Vulkan runtime, as it does
      # for any other Windows application.
      vulkanImportLib = pkgs.runCommand "vulkan-1-implib-${dlltoolMachine}" { } ''
        mkdir -p "$out/lib"
        ${llvm}/bin/llvm-dlltool -m ${dlltoolMachine} \
          -d ${pkgs.vulkan-loader.src}/loader/vulkan-1.def \
          -l "$out/lib/vulkan-1.lib"
      '';

      # llvm-rc preprocesses .rc files by running `clang` from PATH, and the
      # clang on PATH here is nixpkgs' *wrapped* one, which injects this
      # machine's flags (`-fPIC`, a Linux target) into a Windows resource
      # compile and fails. CMake builds the llvm-rc command line itself -- for
      # the manifest of every executable it links -- so there is nowhere to
      # pass a flag: the fix has to be in what CMAKE_RC_COMPILER points at.
      # Preprocessing stays on (LLVM has .rc files that include <windows.h>);
      # it is only the compiler it reaches for that is corrected.
      rc = pkgs.writeShellScriptBin "hlsl-llvm-rc" ''
        export PATH=${clang-unwrapped}/bin''${PATH:+:$PATH}
        exec ${llvm}/bin/llvm-rc "$@"
      '';
    in
    assert lib.assertMsg acceptMsvcLicense ''
      ${platform} needs Microsoft's SDK, whose licence has not been accepted.
      Read https://visualstudio.microsoft.com/license-terms/mt644918/ and, if
      you agree, run: hlsl-cross --accept-msvc-license
    '';
    ''
      set(CMAKE_SYSTEM_NAME Windows)
      set(CMAKE_SYSTEM_PROCESSOR ${processor})
      set(CMAKE_SYSTEM_VERSION 10.0)

      set(CMAKE_C_COMPILER   ${clang-unwrapped}/bin/clang-cl)
      set(CMAKE_CXX_COMPILER ${clang-unwrapped}/bin/clang-cl)
      set(CMAKE_LINKER       ${lld}/bin/lld-link)
      set(CMAKE_AR           ${llvm}/bin/llvm-lib)
      set(CMAKE_MT           ${llvm}/bin/llvm-mt)
      set(CMAKE_RC_COMPILER  ${rc}/bin/hlsl-llvm-rc)

      set(HLSL_WINSDK "${sdk}")

      # Which spelling of the architecture the import libraries are filed
      # under: xwin can splat them as Microsoft names them (x64, arm64) or as
      # LLVM does (x86_64, aarch64), and which one nixpkgs asked for is not
      # worth hard-coding from out here.
      foreach(_hlsl_arch ${if processor == "ARM64" then "arm64 aarch64" else "x64 x86_64"})
        if(IS_DIRECTORY "''${HLSL_WINSDK}/crt/lib/''${_hlsl_arch}")
          set(HLSL_WINSDK_ARCH "''${_hlsl_arch}")
          break()
        endif()
      endforeach()
      if(NOT HLSL_WINSDK_ARCH)
        message(FATAL_ERROR "no ${processor} import libraries under ''${HLSL_WINSDK}/crt/lib")
      endif()

      # The SDK's headers, one joined -imsvc flag each rather than clang-cl's
      # /vctoolsdir + /winsdkdir. Those take their value as a *separate*
      # argument, and sccache -- which every compile goes through here --
      # reparses the command line and loses the association, so the compiler
      # ends up with no MSVC include path at all ("'cassert' file not found",
      # while the same command by hand works). Joined values survive.
      # -imsvc also keeps the SDK's own warnings out of the build, as MSVC does.
      set(_hlsl_msvc_incs
        "-imsvc''${HLSL_WINSDK}/crt/include"
        "-imsvc''${HLSL_WINSDK}/sdk/include/ucrt"
        "-imsvc''${HLSL_WINSDK}/sdk/include/um"
        "-imsvc''${HLSL_WINSDK}/sdk/include/shared"
        "-imsvc''${HLSL_WINSDK}/sdk/include/winrt"
        "-imsvc''${HLSL_WINSDK}/sdk/include/cppwinrt")
      list(JOIN _hlsl_msvc_incs " " _hlsl_msvc_incs)

      # /X drops the compiler's idea of %INCLUDE% (there is none on this
      # machine) and leaves the SDK as the only system headers.
      # -fuse-ld=lld because there are builds where clang-cl drives the link
      # itself rather than CMake calling the linker (DXC's, for one), and the
      # linker clang-cl reaches for by default is Microsoft's link.exe, which
      # is not here ("unable to execute command: posix_spawn failed").
      set(_hlsl_msvc_flags "--target=${triple} /X -fuse-ld=lld ''${_hlsl_msvc_incs}")
      set(CMAKE_C_FLAGS_INIT   "''${_hlsl_msvc_flags}")
      set(CMAKE_CXX_FLAGS_INIT "''${_hlsl_msvc_flags}")

      # The release CRT, in every configuration. nixpkgs' windows.sdk is an
      # xwin splat of the redistributable packages, and those carry no debug
      # CRT (msvcrtd.lib and friends are not redistributable) -- CMake's
      # default for a debug configuration is exactly that, so even its "can
      # this compiler link?" test fails without this.
      set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreadedDLL")

      # CMake drives lld-link itself rather than through the compiler driver,
      # so the library paths clang-cl would have passed are spelled out.
      set(_hlsl_msvc_libs
        "/libpath:''${HLSL_WINSDK}/crt/lib/''${HLSL_WINSDK_ARCH}"
        "/libpath:''${HLSL_WINSDK}/sdk/lib/um/''${HLSL_WINSDK_ARCH}"
        "/libpath:''${HLSL_WINSDK}/sdk/lib/ucrt/''${HLSL_WINSDK_ARCH}")
      list(JOIN _hlsl_msvc_libs " " _hlsl_msvc_libs)
      foreach(_hlsl_kind EXE SHARED MODULE)
        set(CMAKE_''${_hlsl_kind}_LINKER_FLAGS_INIT "''${_hlsl_msvc_libs}")
      endforeach()

      # offload-test-suite looks for d3d12.h the way a Windows machine has it
      # (a registry key, then a versioned Windows Kits directory), which is not
      # how it is here. Answering find_package(D3D12) up front is what lets the
      # suite cross-compile at all; the import libraries it then names
      # (d3d12.lib, dxguid.lib, ...) are in the paths just added.
      set(D3D12_INCLUDE_DIRS "''${HLSL_WINSDK}/sdk/include/um" CACHE PATH
          "Windows SDK headers, from the hlsl-dev cross toolchain")

      # And the same for Vulkan, for the same reason: left to search, CMake's
      # FindVulkan finds *this* machine's headers (they are the same headers)
      # and no library at all, which turns the suite's Vulkan backend on and
      # then fails to link it. Naming both halves is what makes the vk and
      # clang-vk suites exist in a Windows build.
      set(Vulkan_INCLUDE_DIR "${pkgs.vulkan-headers}/include" CACHE PATH
          "Vulkan headers, from the hlsl-dev cross toolchain")
      set(Vulkan_LIBRARY "${vulkanImportLib}/lib/vulkan-1.lib" CACHE FILEPATH
          "Vulkan import library, from the hlsl-dev cross toolchain")

      set(CMAKE_FIND_ROOT_PATH "''${HLSL_WINSDK}")
      list(PREPEND CMAKE_PREFIX_PATH "''${HLSL_WINSDK}")
      set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
      set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY BOTH)
      set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE BOTH)
      set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE BOTH)
    '';

  # ------------------------------------------------------------------------
  # GNU-ABI targets: nixpkgs' own cross toolchains
  # ------------------------------------------------------------------------
  # The cross cc-wrapper already knows its sysroot, its libc and its linker, so
  # the toolchain file is only a matter of naming it. `targetPrefix` is what
  # the wrapper calls its binaries (`aarch64-unknown-linux-gnu-gcc`), and
  # whether they are gcc or clang is nixpkgs' choice per platform.
  gnu =
    {
      cross,
      systemName,
      processor,
      extraRootPaths ? [ ],
    }:
    let
      cc = cross.stdenv.cc;
      prefix = cc.targetPrefix;
      cname = if cc.isClang then "clang" else "gcc";
      cxxname = if cc.isClang then "clang++" else "g++";
      roots = [ (toString cc) ] ++ map toString extraRootPaths;

      # These trees are built with clang wherever they are built for real, so
      # GCC's newer conformance diagnostics -- errors by default in GCC 15 --
      # fire on code clang accepts and upstream has no reason to change. This
      # belongs to the toolchain rather than to the flag templates in
      # devenv.nix: it is a fact about the compiler nixpkgs provides for the
      # platform, and a -DCMAKE_CXX_FLAGS on the command line would replace
      # whatever else the toolchain wanted to say.
      cxxflags = lib.optionalString (!cc.isClang) "-Wno-changes-meaning";
    in
    ''
      set(CMAKE_SYSTEM_NAME ${systemName})
      set(CMAKE_SYSTEM_PROCESSOR ${processor})

      set(CMAKE_C_COMPILER   ${cc}/bin/${prefix}${cname})
      set(CMAKE_CXX_COMPILER ${cc}/bin/${prefix}${cxxname})

      # The build machine's tools stay visible (cmake, ninja, python, the
      # tablegens the cross build runs); everything that ends up *in* a binary
      # comes from the target's own prefixes.
      #
      # Those prefixes are named twice on purpose. CMAKE_FIND_ROOT_PATH alone
      # is not enough: it only *re-roots* the prefixes CMake already searches,
      # and in this environment those are the store paths of the native
      # profile, so `<target zlib>/include` is never a candidate at all.
      # Naming them as prefixes too, with mode BOTH, is what makes a plain
      # find_package(ZLIB) resolve to the target's. The host's prefixes are
      # taken out of the environment by the tasks (see hd_init), so BOTH does
      # not mean "this machine's libraries are acceptable" -- there are none
      # left to find.
      set(CMAKE_CXX_FLAGS_INIT "${cxxflags}")

      set(CMAKE_FIND_ROOT_PATH "${lib.concatStringsSep ";" roots}")
      list(PREPEND CMAKE_PREFIX_PATH ${lib.concatStringsSep " " (map (r: ''"${r}"'') roots)})
      set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
      set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY BOTH)
      set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE BOTH)
      set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE BOTH)
    '';

  # The offload test suite needs a runtime API for the machine it is built
  # *for*: on Linux that is Vulkan, so the target's loader and headers are part
  # of the toolchain rather than something to find afterwards. Both Linux
  # platforms need the same set, so they are one function of the package set.
  linux =
    {
      cross,
      processor,
    }:
    gnu {
      inherit cross;
      systemName = "Linux";
      inherit processor;
      extraRootPaths = [
        cross.vulkan-headers
        cross.vulkan-loader
        # The suite vendors libpng, which links the target's zlib (it vendors
        # zlib as well, but only on Windows). nixpkgs splits the headers into
        # a `dev` output, so both halves have to be findable.
        cross.zlib
        cross.zlib.dev
      ];
    };

  platforms = {
    linux-arm64 = linux {
      cross = pkgs.pkgsCross.aarch64-multiplatform;
      processor = "aarch64";
    };

    linux-x64 = linux {
      cross = pkgs.pkgsCross.gnu64;
      processor = "x86_64";
    };

    # Each MSVC platform takes the SDK built for its own architecture: the
    # import libraries are per target, and nixpkgs fetches them per target.
    windows-x64 = msvc {
      triple = "x86_64-pc-windows-msvc";
      processor = "AMD64";
      sdk = pkgs.pkgsCross.x86_64-windows.windows.sdk;
      dlltoolMachine = "i386:x86-64";
    };

    windows-arm64 = msvc {
      triple = "aarch64-pc-windows-msvc";
      processor = "ARM64";
      sdk = pkgs.pkgsCross.aarch64-windows.windows.sdk;
      dlltoolMachine = "arm64";
    };
  };

  known = lib.concatStringsSep ", " (builtins.attrNames platforms);

  body =
    platforms.${platform}
      or (throw "unknown cross platform '${platform}'; expected one of: ${known}");

  header = ''
    # Generated by scripts/cross/toolchains.nix for '${platform}'.
    # Do not edit: 'hlsl-cross --refresh ${platform}' writes it again.
  '';
in
pkgs.runCommand "hlsl-toolchain-${platform}"
  {
    file = header + body;
    passAsFile = [ "file" ];
    meta.description = "CMake toolchain file for ${platform}";
  }
  ''
    mkdir -p "$out"
    cp "$filePath" "$out/toolchain.cmake"
  ''
