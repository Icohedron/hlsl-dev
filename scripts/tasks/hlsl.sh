#!/usr/bin/env bash
# summary: List the workspace tasks (hlsl <task> runs one)
#
# The umbrella entry point: every task is also its own command on PATH
# (hlsl-build, hlsl-test, ...), which is what tab completion finds.
set -eo pipefail

here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
# shellcheck source=../hlsl-dev.sh
source "$here/../hlsl-dev.sh"

usage() {
    local file name summary
    cat <<'EOF'
usage: hlsl <task> [options] [arguments]

Every task is also a command of its own: `hlsl build clang` and
`hlsl-build clang` are the same thing. `hlsl-<task> --help` explains one.

tasks:
EOF
    for file in "$here"/*.sh; do
        name=$(basename "$file" .sh)
        [ "$name" != "hlsl" ] || continue
        summary=$(sed -n 's/^# summary: //p' "$file" | head -1)
        printf '  %-20s %s\n' "$name" "$summary"
    done
    cat <<'EOF'

The workspace itself is managed with devenv:

  devenv shell    enter the developer environment
  devenv info     packages, scripts and environment on offer
  devenv test     smoke-test the environment, lint the tasks, run the self-tests
EOF
}

case "${1:-}" in
"" | -h | --help)
    usage
    exit 0
    ;;
esac

task=$1
shift
file="$here/$task.sh"
[ -f "$file" ] || {
    hd_warn "unknown task '$task'"
    usage >&2
    exit 2
}

exec env HD_TASK_NAME="hlsl-$task" "$file" "$@"
