#!/usr/bin/env bash
# summary: Show what this directory resolves to, and what it builds against
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_DESC="Shows what the current directory (or --in <worktree>) resolves to: the target
worktree, its build directory, and every dependency a configure would use."
HD_TASK_OPTS="in= llvm= dxc= offload= dist_prefix= platform="
hd_parse "$@"
hd_init

wt=$(hd_target)
kind=$(hd_kind "$wt")

printf '%-16s %s\n' "workspace" "$HD_ROOT"
printf '%-16s %s (%s)\n' "worktree" "$wt" "$(hd_kind_label "$kind")"
printf '%-16s %s\n' "branch" "$(hd_branch "$wt")"
printf '%-16s %s\n' "build type" "$(hd_build_type "$wt")"
if hd_is_cross; then
    platform=$(hd_platform)
    printf '%-16s %s\n' "platform" "$platform (cross)"
    printf '%-16s %s\n' "triple" "$(hd_platform_triple "$platform")"
    toolchain="$(hd_state_dir)/toolchains/$platform/toolchain.cmake"
    if [ -f "$toolchain" ]; then
        printf '%-16s %s\n' "toolchain" "$(readlink -f "$toolchain")"
    else
        printf '%-16s %s\n' "toolchain" "not built yet (the next configure builds it)"
    fi
    if [ "$kind" = "llvm" ]; then
        tools=$(hd_native_tools_dir "$wt")
        if [ -x "$tools/bin/llvm-tblgen" ]; then
            printf '%-16s %s\n' "host tools" "$tools/bin"
        else
            printf '%-16s %s\n' "host tools" "$tools/bin (missing: built by the next configure)"
        fi
    fi
else
    printf '%-16s %s\n' "platform" "native ($(uname -m)-$(uname -s | tr '[:upper:]' '[:lower:]'))"
fi

case "$kind" in
llvm)
    printf '%-16s %s\n' "build dir" "$(hd_build_dir "$wt")"
    printf '%-16s %s\n' "dist prefix" "$(hd_dist_prefix "$wt")"
    printf '%-16s %s\n' "offload" "$(hd_dep offload "$wt")"
    printf '%-16s %s\n' "golden" "$(hd_dep golden "$wt")"
    printf '%-16s %s\n' "dxc" "$(hd_dxc_bin_dir "$wt")"
    ;;
dxc)
    printf '%-16s %s\n' "build dir" "$(hd_build_dir "$wt")"
    ;;
offload)
    llvm=$(hd_dep llvm "$wt")
    printf '%-16s %s\n' "build dir" "$(hd_build_dir "$wt")"
    printf '%-16s %s\n' "llvm" "$llvm"
    dist=$(hd_dist_prefix "$llvm")
    if [ -f "$dist/lib/cmake/llvm/LLVMConfig.cmake" ]; then
        printf '%-16s %s\n' "llvm dist" "$dist"
    else
        printf '%-16s %s (missing: installed by the next build)\n' "llvm dist" "$dist"
    fi
    printf '%-16s %s\n' "golden" "$(hd_dep golden "$wt")"
    printf '%-16s %s\n' "dxc" "$(hd_dxc_bin_dir "$wt")"
    ;;
esac

# What an editor's clangd will find: the root symlink is the only place it
# looks that does not depend on the build directory being called `build`.
# Refreshing it here makes `hlsl-info` the answer to "why is clangd dead?".
hd_link_cdb "$wt" "$(hd_build_dir "$wt")"
cdb="$wt/compile_commands.json"
if [ -L "$cdb" ]; then
    printf '%-16s %s -> %s\n' "clangd db" "$cdb" "$(readlink "$cdb")"
elif [ -f "$cdb" ]; then
    printf '%-16s %s (a file of your own; left alone)\n' "clangd db" "$cdb"
else
    printf '%-16s %s\n' "clangd db" "none yet (written by the next configure)"
fi

printf '%-16s %s\n' "pins" "$(hd_pin_file "$wt")"
