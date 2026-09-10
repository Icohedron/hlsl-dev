#!/usr/bin/env bash
# summary: Remove a worktree's build directory
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_DESC="Removes the build directory of a worktree (and, with --dist, the standalone
distribution build and install prefix of an llvm worktree).

Never clean a worktree somebody else is building in: concurrent builds of the
same build directory are serialised by a lock, but a removal is not."
HD_TASK_OPTS="in= dist"
hd_parse "$@"
hd_init

wt=$(hd_target)
HD_WT=$wt
build=$(hd_build_dir "$wt")
if [ -d "$build" ]; then
    echo "Removing $build"
    rm -rf "$build"
fi

# Repoint (or drop) the compile_commands.json link the editors read, so a
# clean never leaves clangd following a database that is no longer there.
hd_link_cdb "$wt"

if [ -n "${dist:-}" ] && [ "$(hd_kind "$wt")" = "llvm" ]; then
    for d in "$(hd_dist_build_dir "$wt")" "$(hd_dist_prefix "$wt")"; do
        [ -d "$d" ] || continue
        echo "Removing $d"
        rm -rf "$d"
    done
fi
