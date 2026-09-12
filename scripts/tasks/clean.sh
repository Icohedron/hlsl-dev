#!/usr/bin/env bash
# summary: Remove build directories of one worktree, or of the whole workspace
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_ARGS="[repository]"
HD_TASK_DESC="Removes the build directory of a worktree (and, with --dist, the standalone
distribution build and install prefix of an llvm worktree).

  hlsl-clean                       this machine's build tree
  hlsl-clean --platform windows-x64    that platform's, leaving the native one
  hlsl-clean --platform all        every platform's, including native
  hlsl-clean --platform all --dist ... and the LLVM distributions with them
  hlsl-clean --all-build-dirs      every build tree of this worktree, whatever
                                   its name: build, build-container, the cross
                                   trees and build-native-tools
  hlsl-clean --all                 every worktree of every repository
  hlsl-clean --all llvm-project    every worktree of one repository
  hlsl-clean --all --all-build-dirs --dist
                                   the whole workspace, back to sources
  hlsl-clean --all --dry-run       list what that would remove, remove nothing

A cross build has a build tree of its own (<worktree>/build.<platform>), so
they accumulate one per platform; 'hlsl-info --platform <name>' says where each
one is and 'hlsl-ls' which exist. A second environment has trees of its own as
well: the dev container builds in build-container so that its trees and the
host's stay apart, and only --all-build-dirs reaches both from either side.

The repository argument goes with --all and names one of llvm-project,
DirectXShaderCompiler, offload-test-suite (or the short kinds llvm, dxc,
offload).

Never clean a worktree somebody else is building in: concurrent builds of the
same build directory are serialised by a lock, but a removal is not. --all
sweeps worktrees you are not standing in, so run it with --dry-run first."
HD_TASK_OPTS="in= platform= dist all all_build_dirs dry_run"
# shellcheck disable=SC2034 # read by hd_usage as the per-task flag description
HD_DESC_dry_run="List what would be removed, and remove nothing"
hd_parse "$@"

# 'all' is not a platform -- nothing can be *built* for it -- so it is resolved
# here, before hd_init would reject it, into the list to walk.
every=""
if [ "${platform:-}" = "all" ]; then
    every=1
    platform=native
fi

hd_init

# --all-build-dirs finds the trees by name instead of by platform, so it
# already covers every platform: a --platform alongside it would say something
# narrower than what is about to happen.
if [ -n "${all_build_dirs:-}" ] && [ -n "${platform:-}" ] && [ -z "$every" ]; then
    hd_die "--all-build-dirs already removes every build tree of the worktree,
       whatever its name or platform; drop --platform"
fi

# offload-golden-images is data: it has no build tree, so a sweep skips it.
kinds="llvm dxc offload"

# The repository argument restricts the sweep to one repository's worktrees.
repo=""
if [ "${#HD_ARGV[@]}" -gt 0 ]; then
    [ "${#HD_ARGV[@]}" = 1 ] ||
        hd_die "one repository at a time (got: ${HD_ARGV[*]})"
    [ -n "${all:-}" ] ||
        hd_die "'${HD_ARGV[0]}' names a repository, which only means something with --all;
       to clean one worktree, cd into it or pass --in ${HD_ARGV[0]}"
    repo=$(hd_kind_named "${HD_ARGV[0]}") ||
        hd_die "unknown repository '${HD_ARGV[0]}';
       expected one of: llvm-project, DirectXShaderCompiler, offload-test-suite"
    [ "$repo" != "golden" ] ||
        hd_die "$(hd_kind_label golden) is data, not something that gets built"
fi

# --- what to walk -----------------------------------------------------------
targets=()
if [ -n "${all:-}" ]; then
    [ -z "${in:-}" ] ||
        hd_die "--in names one worktree; it cannot be combined with --all"
    for kind in ${repo:-$kinds}; do
        while IFS= read -r wt; do
            [ -n "$wt" ] || continue
            targets+=("$wt")
        done <<< "$(hd_worktrees "$kind")"
    done
    [ "${#targets[@]}" -gt 0 ] ||
        hd_die "no checkouts to clean; 'hlsl-setup' clones the submodules"
else
    wt=$(hd_target)
    targets=("$wt")
    # Only a single target honours $HLSL_BUILD_DIR: it names one directory,
    # which cannot be the build tree of every worktree at once.
    HD_WT=$wt
fi

platforms=$(hd_platform)
# Every platform the workspace knows, not just the ones worth building for
# here: a tree or a pin may have been left by a machine of another
# architecture, and cleaning should still reach it.
[ -z "$every$all_build_dirs" ] || platforms="native $HD_ALL_PLATFORMS"

removed=0
remove() { # remove <directory>
    [ -d "$1" ] || return 0
    if [ -n "${dry_run:-}" ]; then
        echo "Would remove $1"
    else
        echo "Removing $1"
        rm -rf "$1"
    fi
    removed=$((removed + 1))
}

for wt in "${targets[@]}"; do
    kind=$(hd_kind "$wt")
    # A pin that no longer resolves is worth one warning per worktree, not one
    # per platform below (hd_dist_prefix is asked once for each).
    HD_QUIET_WARNINGS=""

    # By name: every build tree this worktree has, from either environment and
    # for any platform. The distribution trees are named build-dist* and are
    # --dist's business, not this option's. A directory is only taken for a
    # build tree when it holds one of CMake's or Ninja's own files: a sweep
    # must not carry off somebody's build-notes/ because of its name.
    if [ -n "${all_build_dirs:-}" ]; then
        for d in "$wt"/build*/; do
            d=${d%/}
            [ -d "$d" ] || continue # no match: the glob stayed literal
            case "${d##*/}" in
            build-dist*) continue ;;
            esac
            [ -e "$d/CMakeCache.txt" ] || [ -e "$d/build.ninja" ] ||
                [ -e "$d/Makefile" ] || [ -d "$d/CMakeFiles" ] || continue
            remove "$d"
        done
    fi

    for p in $platforms; do
        HD_OPT_PLATFORM=$p

        # The cross trees also hold what `hlsl-package` staged and archived,
        # which lives inside them and goes with them; nothing else to do for
        # those.
        [ -n "${all_build_dirs:-}" ] || remove "$(hd_build_dir "$wt")"

        if [ -n "${dist:-}" ] && [ "$kind" = "llvm" ]; then
            remove "$(hd_dist_build_dir "$wt")"
            remove "$(hd_dist_prefix "$wt")"
            HD_QUIET_WARNINGS=1
        fi
    done
    HD_OPT_PLATFORM=native
    HD_QUIET_WARNINGS=""

    # Repoint (or drop) the compile_commands.json link the editors read, so a
    # clean never leaves clangd following a database that is no longer there.
    hd_link_cdb "$wt"
done

# Saying nothing at all reads like a command that did not run. There was
# nothing to remove, which is worth one line -- and worth naming what was
# looked at, since a cross tree is somewhere else than the native one.
if [ "$removed" = 0 ]; then
    if [ -n "${all:-}" ]; then
        echo "Nothing to remove: no build tree in ${#targets[@]} ${repo:+$(hd_kind_label "$repo") }checkout(s)"
    elif [ -n "$every$all_build_dirs" ]; then
        echo "Nothing to remove: $(basename "${targets[0]}") has no build tree for any platform"
    else
        echo "Nothing to remove: $(hd_build_dir "${targets[0]}") does not exist"
    fi
elif [ "${#targets[@]}" -gt 1 ]; then
    # A sweep is worth a total: the per-directory lines scroll past.
    if [ -n "${dry_run:-}" ]; then
        echo "Would remove $removed directories in ${#targets[@]} checkouts"
    else
        echo "Removed $removed directories in ${#targets[@]} checkouts"
    fi
fi
