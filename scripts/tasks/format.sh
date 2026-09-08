#!/usr/bin/env bash
# summary: Check the staged changes against clang-format (and manage the hook)
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_DESC="Reports where the staged changes disagree with the checkout's .clang-format.

'git clang-format' reformats only the lines a change touches, so this says
nothing about code you did not write -- which is what upstream reviewers ask
for, and why running it on a whole file is rarely what you want.

  hlsl-format              what would change in the staged diff
  hlsl-format --diff       the same, as a patch
  hlsl-format --fix        apply it to the staged changes
  hlsl-format --since main everything this branch changed, not just what is staged

A pre-commit hook that runs the first form is installed in every checkout that
has a .clang-format. It warns and lets the commit through -- these are upstream
repositories, and formatting is not a gate this workspace gets to invent. The
hook lives in the clone's git directory, shared by every worktree of it and
never part of a commit.

  hlsl-format --install-hooks     put it back (also done on entering the shell)
  hlsl-format --uninstall-hooks   remove it
  HLSL_INSTALL_HOOKS=0            stop the shell reinstalling it"
HD_TASK_OPTS="in= since= diff fix install_hooks uninstall_hooks check_hooks quiet"
hd_parse "$@"
hd_init_root

# --- hook management --------------------------------------------------------
if [ -n "${install_hooks:-}${uninstall_hooks:-}${check_hooks:-}" ]; then
    changed=0 blocked=0 stale=0
    while IFS= read -r wt; do
        [ -n "$wt" ] || continue
        rc=0
        if [ -n "${check_hooks:-}" ]; then
            hd_hook_ok "$wt" || rc=$?
            [ "$rc" = 1 ] && stale=$((stale + 1))
        elif [ -n "${uninstall_hooks:-}" ]; then
            hd_hook_remove "$wt" || rc=$?
            [ "$rc" = 1 ] && { changed=$((changed + 1)); [ -n "${quiet:-}" ] || echo "removed the hook from $(basename "$wt")"; }
        else
            hd_hook_install "$wt" || rc=$?
            [ "$rc" = 1 ] && { changed=$((changed + 1)); [ -n "${quiet:-}" ] || echo "installed the hook in $(basename "$wt")"; }
            [ "$rc" = 2 ] && blocked=$((blocked + 1))
        fi
    done <<< "$(hd_hook_repos)"

    # --check-hooks is the status check of the hlsl:hooks task: non-zero means
    # "there is work to do". A foreign hook is not work; it is somebody's file.
    [ -z "${check_hooks:-}" ] || exit $((stale > 0 ? 1 : 0))
    [ -n "${quiet:-}" ] || [ "$changed" -gt 0 ] || echo "nothing to do"
    exit $((blocked > 0 ? 2 : 0))
fi

# --- formatting -------------------------------------------------------------
# The repository containing the current directory, so this works in a checkout
# the workspace has never heard of -- a fresh clone, or a hook running from an
# editor -- as well as in a worktree it knows.
if [ -n "${HD_OPT_IN:-}" ]; then
    repo=$(hd_resolve_any "$HD_OPT_IN")
else
    repo=$(git rev-parse --show-toplevel 2>/dev/null) ||
        hd_die "not inside a git checkout; pass --in <worktree>"
fi

[ -f "$repo/.clang-format" ] || {
    [ -n "${quiet:-}" ] || echo "$repo has no .clang-format; nothing to check"
    exit 0
}

command -v git-clang-format >/dev/null 2>&1 ||
    hd_die "git-clang-format is not on PATH; enter the developer environment ('devenv shell')"

# `git clang-format <commit>` compares against that commit; with no commit it
# looks at what is staged.
range=()
[ -z "${since:-}" ] || range=("$since")

if [ -n "${fix:-}" ]; then
    hd_log "reformatting the changed lines in $(basename "$repo")"
    git -C "$repo" clang-format "${range[@]}"
    echo
    echo "The working tree was updated; 'git add' the files you want in this commit."
    exit 0
fi

out=$(git -C "$repo" clang-format --diff "${range[@]}" 2>/dev/null) || true
case "$out" in
"" | "no modified files to format"* | "clang-format did not modify any files"*)
    [ -n "${quiet:-}" ] || echo "clang-format: nothing to fix"
    exit 0
    ;;
esac

if [ -n "${diff:-}" ]; then
    printf '%s\n' "$out"
else
    # The summary is what the hook prints: enough to know it matters, short
    # enough not to bury the commit message in a diff.
    files=$(printf '%s\n' "$out" | sed -n 's|^+++ b/||p' | sort -u)
    echo "clang-format would change these staged files:"
    printf '%s\n' "$files" | sed 's/^/    /'
    echo
    echo "    hlsl-format --diff    to see it"
    echo "    hlsl-format --fix     to apply it"
fi
exit 1
