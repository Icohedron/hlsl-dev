#!/usr/bin/env bash
# summary: Update every submodule to its remote default branch
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_DESC="Updates all submodules to the latest commits on their respective default remote
branches (e.g. main or master).

Submodules that already have their full history (after hlsl-fetch-history) are
updated with a full fetch so the history is preserved; only shallow or
not-yet-cloned submodules are fetched with --depth 2."
hd_parse "$@"
hd_init_root

cd "$HD_ROOT"

paths=$(git config -f .gitmodules --get-regexp '^submodule\..*\.path$' | cut -d' ' -f2-)

for path in $paths; do
    if [ -e "$path/.git" ] &&
       [ "$(git -C "$path" rev-parse --is-shallow-repository 2>/dev/null)" = "false" ]; then
        depth=""
        echo "==> $path: full history detected, updating without truncating"
    else
        depth="--depth 2"
        echo "==> $path: shallow, updating with --depth 2"
    fi

    # shellcheck disable=SC2086 # $depth is an intentional flag word split
    git submodule update --init --recursive $depth -- "$path"
    # shellcheck disable=SC2086
    git submodule update --remote --recursive $depth -- "$path"
done
