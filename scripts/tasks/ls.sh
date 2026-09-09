#!/usr/bin/env bash
# summary: List every worktree, its branch, build state and pins
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_DESC="An inventory of the workspace: every checkout of every repository, grouped by
repository, in the order dependencies are resolved.

  worktree   the directory, relative to the workspace root. '*' marks the one
             the current directory belongs to -- the one a bare hlsl-build or
             hlsl-test acts on
  branch     '(detached)' is normal for a submodule: it sits at the commit the
             workspace records rather than on a branch. Two checkouts sharing a
             branch name find each other without being told to
  build      whether there is a build tree, and for llvm-project whether an
             LLVM distribution is installed beside it ('+ dist') -- that is
             what standalone offload builds link against
  against    what this checkout remembers building against, from the last
             hlsl-configure or an explicit --llvm/--dxc. Blank means it works
             it out each time: same branch name first, then the submodule

This is an inventory, not a resolution: 'hlsl-info' answers what one checkout
would build against right now, following all five rules."
hd_parse "$@"
hd_init

current=$(hd_wt_from "$PWD" || true)

# Collect first, print second, so the columns are as wide as their contents: a
# workspace of one submodule should not be laid out for one of twenty.
#
# Rows are <kind>US<marker>US<worktree>US<branch>US<build>US<against>, with a
# unit separator rather than a tab because `read` folds empty fields away when
# the separator is whitespace, and most of these fields can be empty.
US=$'\037'
rows=()
w_wt=8 w_branch=6 w_build=5
missing=0

row() { # row heading|entry <marker> <worktree> <branch> <build> <against>
    if [ "$1" = entry ]; then
        [ "${#3}" -le "$w_wt" ] || w_wt=${#3}
        [ "${#4}" -le "$w_branch" ] || w_branch=${#4}
        [ "${#5}" -le "$w_build" ] || w_build=${#5}
    fi
    rows+=("$1$US$2$US$3$US$4$US$5$US$6")
}

for kind in llvm dxc offload golden; do
    row heading "" "$(hd_kind_label "$kind")" "" "" ""
    found=0

    while IFS= read -r wt; do
        [ -n "$wt" ] || continue
        found=1

        marker=" "
        [ "$wt" = "$current" ] && marker="*"

        # offload-golden-images is data, not something that gets built.
        build=""
        if [ "$kind" != "golden" ]; then
            dir=$(hd_build_dir "$wt")
            if [ -f "$dir/build.ninja" ] || [ -f "$dir/Makefile" ]; then
                build="built"
                # Worth naming only when it is not the usual <worktree>/build.
                [ "$dir" = "$wt/build" ] || build="built ($(basename "$dir"))"
            else
                build="not built"
            fi
            if [ "$kind" = "llvm" ] &&
                [ -f "$(hd_dist_prefix "$wt")/lib/cmake/llvm/LLVMConfig.cmake" ]; then
                build="$build + dist"
            fi
        fi

        # What it remembers, spelled the way the flags are.
        against=""
        pinfile=$(hd_pin_file "$wt")
        if [ -f "$pinfile" ]; then
            while IFS='=' read -r key value; do
                case "$key" in
                LLVM | OFFLOAD | DXC) ;;
                *) continue ;;
                esac
                # A dxc pin is a directory of binaries, and one of the things it
                # can be is a store path a screen wide; name the checkout it
                # came from instead, or say where it came from. Pins inside the
                # workspace are stored relative to its root ("./llvm-project"),
                # which is not how the flags are spelled.
                value=$(hd_pin_abs "$value")
                case "$value" in
                "$HD_ROOT"/*) value=${value#"$HD_ROOT"/} ;;
                "${HLSL_DXC_PREBUILT_DIR:-/nonexistent}") value="(the prebuilt dxc)" ;;
                esac
                value=${value%/build/bin}
                against="$against${against:+,} ${key,,}: $value"
            done <"$pinfile"
            against=${against# }
        fi

        row entry "$marker" "${wt#"$HD_ROOT"/}" "$(hd_branch "$wt")" "$build" "$against"
    done <<< "$(hd_worktrees "$kind")"

    if [ "$found" = 0 ]; then
        missing=1
        row entry " " "(not checked out)" "" "" ""
    fi
done

# --- print ------------------------------------------------------------------
entry() { # entry <marker> <worktree> <branch> <build> <against>
    local out
    out=$(printf '%s %-*s  %-*s  %-*s  %s' \
        "$1" "$w_wt" "$2" "$w_branch" "$3" "$w_build" "$4" "$5")
    printf '%s\n' "${out%"${out##*[![:space:]]}"}" # never trail whitespace
}

entry " " "worktree" "branch" "build" "against"

for r in "${rows[@]}"; do
    IFS="$US" read -r kind marker wt branch build against <<< "$r"
    if [ "$kind" = heading ]; then
        printf '\n%s\n' "$wt"
    else
        entry "$marker" "$wt" "$branch" "$build" "$against"
    fi
done

echo
[ -z "$current" ] ||
    printf '%s\n' '* the checkout this directory belongs to; "hlsl-info" explains one in full'
[ "$missing" = 0 ] ||
    printf '%s\n' '  a repository with no checkout needs "hlsl-setup" to clone the submodules'
