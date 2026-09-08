#!/usr/bin/env bash
# summary: Index or refresh a worktree for CodeGraph
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_DESC="Builds or refreshes the CodeGraph index of a worktree, so that codegraph-aware
agents can answer structural questions about it. Works on llvm-project,
DirectXShaderCompiler and offload-test-suite checkouts; each repository has its
own scope in scripts/codegraph-<kind>.json.

A worktree with no index yet starts from a copy of another worktree's database
and re-parses only what its branch changed -- seconds instead of the several
minutes a cold index takes."
HD_TASK_HELP="  hlsl-codegraph                              index/refresh the current worktree
  hlsl-codegraph --in llvm-project.my-feature ... or a named one
  hlsl-codegraph --fresh                      rebuild from scratch
  hlsl-codegraph --all                        every worktree of every repository
  hlsl-codegraph --restore-gitignore          unblock a git operation that wants
                                              to change .gitignore

The index needs llvm/lib/Target to stay visible, which CodeGraph's built-in
ignore list would drop, so this task appends a marked block to the checkout's
tracked .gitignore and marks the file skip-worktree. A checkout, rebase or pull
that wants to change .gitignore itself then refuses to run; --restore-gitignore
puts the file back, and a later plain run re-applies the block."
HD_TASK_OPTS="in= from= fresh all restore_gitignore"
hd_parse "$@"
hd_init

# Every repository the index knows how to scope: one codegraph-<kind>.json each.
kinds="llvm dxc offload"

if [ -n "${restore_gitignore:-}" ]; then
    if [ -n "${all:-}" ]; then
        for kind in $kinds; do
            while IFS= read -r wt; do
                [ -n "$wt" ] || continue
                hd_codegraph_ungitignore "$wt"
            done <<< "$(hd_worktrees "$kind")"
        done
    else
        # shellcheck disable=SC2086 # $kinds is an intentional list of kinds
        hd_codegraph_ungitignore "$(hd_target $kinds)"
    fi
    exit 0
fi
if [ -n "${all:-}" ]; then
    [ -z "${from:-}" ] ||
        hd_die "--from names one worktree of one repository; it cannot be combined with --all"
    for kind in $kinds; do
        # Each repository seeds its own worktrees from the first index in it.
        donor=""
        while IFS= read -r wt; do
            [ -n "$wt" ] || continue
            hd_codegraph "$wt" "$donor"
            [ -n "$donor" ] || donor=$wt
        done <<< "$(hd_worktrees "$kind")"
    done
else
    # shellcheck disable=SC2086 # $kinds is an intentional list of kinds
    wt=$(hd_target $kinds)
    donor=""
    [ -z "${from:-}" ] || donor=$(hd_resolve "$(hd_kind "$wt")" "$from")
    hd_codegraph "$wt" "$donor"
fi
