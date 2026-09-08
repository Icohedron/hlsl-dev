#!/usr/bin/env bash
# summary: Fetch a submodule's full commit history
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_ARGS="<repo>"
HD_TASK_DESC="Fetches the full commit history of a submodule, for when you need to rebase,
branch off older commits, or create pull requests.

<repo> is a submodule name (llvm-project, DirectXShaderCompiler, ...)."
hd_parse "$@"
hd_need_args 1
hd_init_root

repo=${HD_ARGV[0]}
[ -d "$HD_ROOT/$repo" ] || hd_die "no such submodule: $HD_ROOT/$repo"

# A shallow clone unshallows; a full one has nothing to unshallow and just
# refreshes instead, which is why the fallback is deliberate here.
# shellcheck disable=SC2015
cd "$HD_ROOT/$repo" && git fetch --unshallow || git fetch --all
