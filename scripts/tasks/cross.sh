#!/usr/bin/env bash
# summary: Show, prepare or explain the cross-compilation platforms
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_ARGS="[platform]"
HD_TASK_DESC="Everything that builds -- hlsl-configure, hlsl-build, hlsl-dist, hlsl-clean --
takes --platform <name> and builds for another machine instead of this one:

  hlsl-build --platform windows-x64 clang        clang.exe, x64
  hlsl-build --platform windows-arm64 clang      clang.exe, arm64
  hlsl-build --platform linux-arm64 clang        clang, aarch64 Linux
  hlsl-configure --platform windows-x64 --in offload-test-suite
                                                 the offload suite for Windows

A cross build lives in <worktree>/build.<platform>, so it never disturbs the
native tree and both can exist at once; the same goes for the LLVM
distribution a standalone offload build links against (build-dist.<platform>).
Everything else -- which llvm-project worktree, which DXC, the build type --
is resolved exactly as it is natively, and remembered per platform.

What this task itself does:

  hlsl-cross                     the platforms, and what each one still needs
  hlsl-cross windows-x64         one platform, in detail
  hlsl-cross --fetch windows-x64 build its toolchain now instead of at the
                                 next configure (the MSVC SDK is a big
                                 download; this is where to pay for it)
  hlsl-cross --refresh <p>       build it again after 'devenv update'
  hlsl-cross --accept-msvc-license
                                 record that you accept Microsoft's licence,
                                 which the MSVC platforms cannot be built
                                 without

Tests are not run for a cross platform: those binaries do not run here. Build
them, take them to the target, and run the suites there."
HD_TASK_HELP="The toolchains come from scripts/cross/toolchains.nix, built against the same
pinned nixpkgs as the rest of the environment and cached (and GC-rooted) in
.hlsl-dev/toolchains/."
HD_TASK_OPTS="fetch refresh accept_msvc_license"
hd_parse "$@"
hd_init_root

# --- the licence ------------------------------------------------------------
# nixpkgs carries Microsoft's headers and import libraries but refuses to build
# them until this is said, and it is not ours to say on someone's behalf.
if [ -n "${accept_msvc_license:-}" ]; then
    hd_setting_set MSVC_LICENSE accepted
    echo "Recorded: the Visual Studio licence terms are accepted for this workspace."
    echo "  https://visualstudio.microsoft.com/license-terms/mt644918/"
    echo "  (undo with: hlsl-cross --accept-msvc-license=no is not a thing --"
    echo "   remove MSVC_LICENSE from $(hd_settings_file))"
    exit 0
fi

license="not accepted"
hd_msvc_license_accepted && license="accepted"

toolchain_of() { printf '%s\n' "$(hd_state_dir)/toolchains/$1/toolchain.cmake"; }

state_of() { # state_of <platform> -> one word about its toolchain
    local p=$1
    if [ -f "$(toolchain_of "$p")" ]; then
        echo "ready"
    elif [ "$(hd_platform_abi "$p")" = "msvc" ] && ! hd_msvc_license_accepted; then
        echo "needs licence"
    else
        echo "not built"
    fi
}

# --- build one --------------------------------------------------------------
if [ -n "${fetch:-}" ] || [ -n "${refresh:-}" ]; then
    [ "${#HD_ARGV[@]}" -ge 1 ] ||
        hd_die "which platform? one of: $(hd_platforms)"
    platform=${HD_ARGV[0]}
    HD_OPT_PLATFORM=$platform
    hd_platform_check "$platform"
    [ "$platform" != native ] || hd_die "'native' needs no toolchain"
    [ -z "${refresh:-}" ] || rm -f "$(hd_state_dir)/toolchains/$platform"
    file=$(hd_toolchain_file "$platform")
    echo "$platform: $(readlink -f "$file")"
    exit 0
fi

# --- one platform in detail -------------------------------------------------
if [ "${#HD_ARGV[@]}" -ge 1 ]; then
    platform=${HD_ARGV[0]}
    HD_OPT_PLATFORM=$platform
    hd_platform_check "$platform"
    printf '%-16s %s\n' "platform" "$platform"
    if [ "$platform" = native ]; then
        printf '%-16s %s\n' "triple" "this machine"
        exit 0
    fi
    printf '%-16s %s\n' "triple" "$(hd_platform_triple "$platform")"
    printf '%-16s %s\n' "abi" "$(hd_platform_abi "$platform")"
    printf '%-16s %s\n' "toolchain" "$(state_of "$platform")"
    [ -f "$(toolchain_of "$platform")" ] &&
        printf '%-16s %s\n' "" "$(readlink -f "$(toolchain_of "$platform")")"
    printf '%-16s %s\n' "build dirs" "<worktree>/build.$platform, <llvm>/build-dist.$platform"
    printf '%-16s %s\n' "host tools" "<llvm worktree>/build-native-tools"
    [ "$(hd_platform_abi "$platform")" = "msvc" ] &&
        printf '%-16s %s\n' "MSVC licence" "$license"
    echo
    echo "Build for it with:  hlsl-build --platform $platform clang"
    exit 0
fi

# --- the inventory ----------------------------------------------------------
host=$(hd_host_platform)
printf '%-16s %-28s %-6s %s\n' "platform" "triple" "abi" "toolchain"
printf '%-16s %-28s %-6s %s\n' "native" \
    "${host:+$(hd_platform_triple "$host")}" "-" "ready"
for p in $(hd_platforms); do
    printf '%-16s %-28s %-6s %s\n' \
        "$p" "$(hd_platform_triple "$p")" "$(hd_platform_abi "$p")" "$(state_of "$p")"
done
if [ -n "$host" ]; then
    echo
    echo "This machine is '$host', so that platform is not listed: building for"
    echo "it is just a build, without --platform."
fi

echo
printf '%-16s %s\n' "MSVC licence" "$license"
printf '%-16s %s\n' "toolchains" "$(hd_state_dir)/toolchains"
printf '%-16s %s\n' "settings" "$(hd_settings_file)"

if [ "$license" = "not accepted" ]; then
    echo
    echo "The two MSVC platforms need Microsoft's headers and import libraries."
    echo "nixpkgs has them (windows.sdk) but will not build them until you accept"
    echo "https://visualstudio.microsoft.com/license-terms/mt644918/ :"
    echo
    echo "    hlsl-cross --accept-msvc-license"
    echo
    echo "There is no licence-free Windows platform here on purpose: a GNU-ABI"
    echo "(MinGW) build cannot reach D3D12, which comes as MSVC import libraries"
    echo "in that same SDK, so it could carry neither the offload test suite nor"
    echo "DXC. See scripts/cross/toolchains.nix."
fi
