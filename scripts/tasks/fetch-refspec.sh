#!/usr/bin/env bash
# summary: Widen a checkout's origin refspec to a user's branches
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_ARGS="[username]"
HD_TASK_DESC="Sets remote.origin.fetch of the checkout you are standing in (or --in
<worktree>) so that origin tracks main plus, if a username is given, that
user's users/<username>/* branches.

Submodules are cloned single-branch, so origin only ever fetches
refs/heads/main; anything referring to origin/users/... (for example
'gh stack checkout' on a pull request whose branch lives in the upstream
repository) then fails with 'not a valid object name'. Widening the refspec
fixes that without pulling in the thousands of other branches these
repositories carry.

Without a username the refspec is reset to main only. Git worktrees share the
configuration of their repository, so this applies to every worktree of that
submodule."
HD_TASK_OPTS="in= fetch"
hd_parse "$@"
hd_init

wt=$(hd_target)
username=${HD_ARGV[0]:-}

case "$username" in
*[!A-Za-z0-9._-]*) hd_die "invalid username '$username'" ;;
esac

# --unset-all exits 5 when there is nothing to unset; that is not an error here.
git -C "$wt" config --unset-all remote.origin.fetch || true
git -C "$wt" config --add remote.origin.fetch \
    '+refs/heads/main:refs/remotes/origin/main'
if [ -n "$username" ]; then
    git -C "$wt" config --add remote.origin.fetch \
        "+refs/heads/users/$username/*:refs/remotes/origin/users/$username/*"
fi

echo "remote.origin.fetch for $wt:"
git -C "$wt" config --get-all remote.origin.fetch | sed 's/^/    /'

if [ -n "${fetch:-}" ]; then
    echo
    git -C "$wt" fetch --prune origin
else
    echo
    echo "Run 'git fetch origin' in the checkout to pick the new refs up."
fi
