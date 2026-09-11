#!/usr/bin/env bash
# summary: Run the worktree's llvm-lit on any test path
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_ARGS="<path>..."
HD_TASK_DESC="Runs the worktree's llvm-lit on arbitrary paths -- clang/LLVM regression
tests, or offload tests addressed through the build tree.

Paths are taken as given (relative to the current directory), so
'hlsl-lit clang/test/CodeGenHLSL' works from inside an llvm worktree."
HD_TASK_OPTS="in= lit_args= platform="
hd_parse "$@"
hd_need_args 1
hd_init

wt=$(hd_target)
HD_WT=$wt
build=$(hd_build_dir "$wt")
# shellcheck disable=SC2086 # --lit-args is a flag list; splitting it is the point
hd_lit "$build" ${lit_args:--v} "${HD_ARGV[@]}"
