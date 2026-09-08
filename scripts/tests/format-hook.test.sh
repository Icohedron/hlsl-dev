#!/usr/bin/env bash
# Drives the clang-format pre-commit hook the way a commit does: a throwaway
# repository, the hook exactly as it is installed, and `git commit` run with
# the developer environment taken *out* of the environment.
#
# That last part is the point. Commits come from editors and bare terminals,
# where nothing of devenv is on PATH; the hook is supposed to find the tools
# through the workspace profile by itself. The unit test checks the hook's
# content, this checks that it works.
set -eo pipefail

here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
# shellcheck source=./harness.sh
source "$here/harness.sh"
# shellcheck source=../hlsl-dev.sh
source "$here/../hlsl-dev.sh"

hd_init_root
real=$HD_ROOT

# The hook finds the workspace by walking up from itself, so the repository has
# to sit under one. This is a stand-in: the marker file plus symlinks to the
# real script and profile, which is enough for the walk to land somewhere the
# hook can work from -- and it means the discovery is exercised too, not just
# the formatting.
ws=$(mktemp -d)
trap 'rm -rf "$ws"' EXIT
: >"$ws/devenv.nix"
mkdir -p "$ws/scripts/tasks"
ln -s "$real/scripts/hlsl-dev.sh" "$ws/scripts/hlsl-dev.sh"
ln -s "$real/scripts/tasks/format.sh" "$ws/scripts/tasks/format.sh"
ln -s "$real/.devenv" "$ws/.devenv"

repo=$ws/repo
mkdir -p "$repo"

git -C "$repo" init -q
git -C "$repo" config user.email hook@test
git -C "$repo" config user.name "Hook Test"
git -C "$repo" config commit.gpgsign false
printf 'BasedOnStyle: LLVM\n' >"$repo/.clang-format"
printf 'int already_here(int  x){return x;}\n' >"$repo/a.cpp"
git -C "$repo" add -A
git -C "$repo" commit -qm "initial"

hd_hook_body >"$repo/.git/hooks/pre-commit"
chmod +x "$repo/.git/hooks/pre-commit"

lacks "the hook has no path written into it" "$(hd_hook_body)" "$real"

# A commit as it happens outside the developer environment: git is on PATH
# (it has to be, to run the hook at all), and nothing else from the profile is.
commit() {
    ( cd "$repo" && env -u DEVENV_ROOT -u DEVENV_PROFILE -u HLSL_DEV_ROOT \
        PATH="$(dirname "$(command -v git)"):/usr/bin:/bin" \
        git commit -m "$1" 2>&1 ) || return $?
}

# --- a change clang-format disagrees with -----------------------------------
printf 'int   badly ( int  y ) {return   y;}\n' >>"$repo/a.cpp"
git -C "$repo" add a.cpp

rc=0
out=$(commit "unformatted") || rc=$?
check "the commit is not blocked" "$rc" "0"
check "and it exists" "$(git -C "$repo" rev-list --count HEAD)" "2"
contains "the hook warned" "$out" "clang-format would change these staged files"
contains "naming the file" "$out" "a.cpp"
contains "and how to deal with it" "$out" "hlsl-format --fix"
lacks "without needing the shell" "$out" "not on PATH"

# --- a change it is happy with ----------------------------------------------
printf 'int fine(int z) { return z; }\n' >>"$repo/a.cpp"
git -C "$repo" add a.cpp

rc=0
out=$(commit "formatted") || rc=$?
check "a clean commit succeeds" "$rc" "0"
check "and it exists too" "$(git -C "$repo" rev-list --count HEAD)" "3"
lacks "with nothing to say" "$out" "clang-format"

# --- only the lines the commit touches --------------------------------------
# a.cpp line 1 has been badly formatted since the initial commit; a commit that
# does not touch it must not be nagged about it, which is what makes this
# warning worth reading at all.
printf 'int another(int w) { return w; }\n' >>"$repo/a.cpp"
git -C "$repo" add a.cpp
rc=0
out=$(commit "untouched lines stay untouched") || rc=$?
check "an unrelated commit is quiet" "$rc" "0"
lacks "about pre-existing formatting" "$out" "clang-format"

# --- and the hook is not in the way of anything -----------------------------
check "the checkout is clean" "$(git -C "$repo" status --porcelain | wc -l)" "0"
check "the hook is not tracked" \
    "$(git -C "$repo" ls-files | grep -c 'hooks/pre-commit' || true)" "0"

exit "$fail"
