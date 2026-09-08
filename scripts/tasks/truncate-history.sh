#!/usr/bin/env bash
# summary: Truncate a submodule's history back to a shallow clone
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_ARGS="<repo>"
HD_TASK_DESC="Truncates the commit history of a submodule back to a shallow depth of 2, to
reclaim disk space once you are done needing the full history.

<repo> is a submodule name (llvm-project, DirectXShaderCompiler, ...)."
hd_parse "$@"
hd_need_args 1
hd_init_root

repo=${HD_ARGV[0]}
[ -d "$HD_ROOT/$repo" ] || hd_die "no such submodule: $HD_ROOT/$repo"

cd "$HD_ROOT/$repo" &&
    git fetch --depth 2 &&
    git reflog expire --expire=now --all &&
    git gc --prune=now
