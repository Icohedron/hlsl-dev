#!/usr/bin/env bash
# summary: Show or toggle whether D3D12 support is built into the test suite
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_ARGS="[on|off]"
HD_TASK_DESC="Shows, or switches, whether the offload test suite is built with D3D12 support
-- the d3d12, warp-d3d12, clang-d3d12 and clang-warp-d3d12 suites.

The suite detects D3D12 at configure time and has no switch of its own: on
Windows through find_package(D3D12), on Linux only under WSL, where it picks up
the host driver from /usr/lib/wsl/lib together with the two static libraries
DirectX-Headers ships. 'off' takes that decision back with CMake's own
CMAKE_DISABLE_FIND_PACKAGE_D3D12{,_WSL}, which is worth doing when the WSL
driver is unstable, when a d3d12 test hangs the whole suite, or to keep a build
tree comparable with one from a machine that has no D3D12 at all.

The choice is remembered for the whole workspace and applied by every
configure. Switching it reconfigures the worktree you are standing in, if it
has a build tree already -- that is a CMake re-run, not a rebuild, though the
targets that appear or disappear do change what a later build has to do."
HD_TASK_OPTS="in= dry_run"
hd_parse "$@"
hd_init

# The worktree this applies to, if any: only llvm-project and offload-test-suite
# build trees carry the suites, and running from anywhere else is fine -- the
# setting is workspace-wide.
if [ -n "${HD_OPT_IN:-}" ]; then
    wt=$(hd_resolve_any "$HD_OPT_IN")
else
    wt=$(hd_wt_from "$PWD" || true)
fi
case "$(hd_kind "$wt")" in
llvm | offload) ;;
*) wt="" ;;
esac

# --- switch -----------------------------------------------------------------
if [ "${#HD_ARGV[@]}" -gt 0 ]; then
    case "${HD_ARGV[0]}" in
    on | off) want=${HD_ARGV[0]} ;;
    *) hd_die "expected 'on' or 'off', got '${HD_ARGV[0]}'" ;;
    esac

    if [ "$want" = "on" ] && ! hd_d3d12_available; then
        hd_warn "this machine offers no D3D12: neither Windows nor a WSL driver
         under /usr/lib/wsl/lib. The setting is recorded, but a configure here
         will still find nothing to enable."
    fi

    hd_setting_set D3D12 "$want"
    hd_log "D3D12 support: $want"

    # The setting only reaches a build tree through a configure, so do it here
    # rather than leaving a build tree that disagrees with the setting.
    if [ -n "$wt" ] && [ -f "$(hd_build_dir "$wt")/build.ninja" ]; then
        HD_WT=$wt
        hd_configure "$wt"
    elif [ -n "$wt" ]; then
        hd_log "$(basename "$wt") has no build tree yet; it picks this up when it is configured"
    fi
    echo
fi

# --- status -----------------------------------------------------------------
if hd_d3d12_available; then
    case "$(uname -s)" in
    Linux) platform="yes (WSL: /usr/lib/wsl/lib)" ;;
    *) platform="yes ($(uname -s))" ;;
    esac
else
    platform="no (not Windows, and no WSL driver under /usr/lib/wsl/lib)"
fi

printf '%-16s %s\n' "setting" "$(hd_d3d12)"
printf '%-16s %s\n' "available" "$platform"
printf '%-16s %s\n' "settings" "$(hd_settings_file)"

if [ -n "$wt" ]; then
    build=$(hd_build_dir "$wt")
    printf '%-16s %s\n' "worktree" "$wt"
    if [ ! -f "$build/build.ninja" ]; then
        printf '%-16s %s\n' "build tree" "$build (unconfigured)"
    elif [ -d "$(hd_test_root "$wt" "$build")/d3d12" ]; then
        printf '%-16s %s\n' "build tree" "$build (d3d12 suites configured)"
    else
        printf '%-16s %s\n' "build tree" "$build (no d3d12 suites)"
    fi
fi
