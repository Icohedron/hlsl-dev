#!/usr/bin/env bash
# Shared argument handling for the standalone offloader task wrappers.
# The workspace CLI does not depend on this file.

hd_die() { printf 'error: %s\n' "$*" >&2; exit 1; }
hd_log() { printf '==> %s\n' "$*" >&2; }

hd_usage() {
    printf 'usage: %s%s\n\n%s\n' "${HD_TASK_NAME:-$(basename "$0" .sh)}" \
        "${HD_TASK_ARGS:+ $HD_TASK_ARGS}" "$HD_TASK_DESC"
    printf '  %-24s %s\n' '--help' 'Show this message'
}

hd_parse() {
    HD_ARGV=()
    while [ "$#" -gt 0 ]; do
        case "$1" in
        -h | --help) hd_usage; exit 0 ;;
        --) shift; HD_ARGV+=("$@"); break ;;
        --*) hd_die "unknown option '$1' (try '${HD_TASK_NAME:-$(basename "$0" .sh)} --help')" ;;
        *) HD_ARGV+=("$1") ;;
        esac
        shift
    done
}

hd_need_args() {
    [ "${#HD_ARGV[@]}" -ge "$1" ] && return 0
    hd_usage >&2
    exit 2
}

hd_init_root() {
    # The wrappers may run outside devenv or from another working directory.
    HD_ROOT=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd -P)
    export HD_ROOT
}
