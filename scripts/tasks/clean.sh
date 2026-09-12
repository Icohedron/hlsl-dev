#!/usr/bin/env bash
# summary: Remove a worktree's build directory
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_DESC="Removes the build directory of a worktree (and, with --dist, the standalone
distribution build and install prefix of an llvm worktree).

  hlsl-clean                       this machine's build tree
  hlsl-clean --platform windows-x64    that platform's, leaving the native one
  hlsl-clean --platform all        every platform's, including native
  hlsl-clean --platform all --dist ... and the LLVM distributions with them

A cross build has a build tree of its own (<worktree>/build.<platform>), so
they accumulate one per platform; 'hlsl-info --platform <name>' says where each
one is and 'hlsl-ls' which exist.

Never clean a worktree somebody else is building in: concurrent builds of the
same build directory are serialised by a lock, but a removal is not."
HD_TASK_OPTS="in= platform= dist"
hd_parse "$@"

# 'all' is not a platform -- nothing can be *built* for it -- so it is resolved
# here, before hd_init would reject it, into the list to walk.
every=""
if [ "${platform:-}" = "all" ]; then
    every=1
    platform=native
fi

hd_init

wt=$(hd_target)
HD_WT=$wt
kind=$(hd_kind "$wt")

platforms=$(hd_platform)
# Every platform the workspace knows, not just the ones worth building for
# here: a tree or a pin may have been left by a machine of another
# architecture, and cleaning should still reach it.
[ -z "$every" ] || platforms="native $HD_ALL_PLATFORMS"

removed=0
for p in $platforms; do
    HD_OPT_PLATFORM=$p

    build=$(hd_build_dir "$wt")
    if [ -d "$build" ]; then
        echo "Removing $build"
        rm -rf "$build"
        removed=$((removed + 1))
    fi

    # The cross trees also hold what `hlsl-package` staged and archived, which
    # lives inside them and goes with them; nothing else to do for those.
    if [ -n "${dist:-}" ] && [ "$kind" = "llvm" ]; then
        for d in "$(hd_dist_build_dir "$wt")" "$(hd_dist_prefix "$wt")"; do
            [ -d "$d" ] || continue
            echo "Removing $d"
            rm -rf "$d"
            removed=$((removed + 1))
        done
    fi
done
HD_OPT_PLATFORM=$(printf '%s' "$platforms" | awk '{print $1}')

# Repoint (or drop) the compile_commands.json link the editors read, so a clean
# never leaves clangd following a database that is no longer there.
HD_OPT_PLATFORM=native hd_link_cdb "$wt"

# Saying nothing at all reads like a command that did not run. There was
# nothing to remove, which is worth one line -- and worth naming what was
# looked at, since a cross tree is somewhere else than the native one.
if [ "$removed" = 0 ]; then
    if [ -n "$every" ]; then
        echo "Nothing to remove: $(basename "$wt") has no build tree for any platform"
    else
        echo "Nothing to remove: $(hd_build_dir "$wt") does not exist"
    fi
fi
