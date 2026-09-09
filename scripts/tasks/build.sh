#!/usr/bin/env bash
# summary: Build the checkout you are standing in (configuring it first if needed)
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_ARGS="[target]..."
HD_TASK_DESC="Builds the worktree you are standing in (or --in <worktree>), configuring it
-- and anything it has to be configured against -- first if needed. Without a
target the default target is built; prefer a specific one (hlsl-build clang):
a cold llvm-project build is expensive. --dry-run shows what that would be.

Several targets can be named at once, and are built in a single invocation --
one configure, one build lock, one dependency graph, so ninja schedules the
whole lot in parallel rather than one target after another:

  hlsl-build clang llvm-dis FileCheck"
HD_TASK_OPTS="in= llvm= dxc= offload= build_type= fresh dry_run no_auto"
hd_parse "$@"
hd_init

wt=$(hd_target)
HD_WT=$wt
if [ -n "$HD_OPT_FRESH" ]; then hd_configure "$wt"; fi
hd_build "$wt" "${HD_ARGV[@]}"
