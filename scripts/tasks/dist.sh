#!/usr/bin/env bash
# summary: Install the standalone LLVM distribution offload builds link against
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_DESC="Builds and installs the LLVM half of the standalone offload distribution for
an llvm-project worktree: Clang, the lit tooling and the LLVM libraries the
offload tools link against, installed into <worktree>/build-dist/install.

That prefix is what standalone offload-test-suite builds consume, so one
hlsl-dist serves every offload worktree pointed at this llvm worktree. See
offload-test-suite/docs/offload-distribution.md ('Standalone Build
Distribution').

An offload build installs a missing distribution by itself, so the reason to
run this directly is to *refresh* one after changing Clang. It is incremental."
HD_TASK_OPTS="in= offload= dist_prefix= build_type= fresh dry_run"
hd_parse "$@"
hd_init

wt=$(hd_target llvm)
HD_WT=$wt
hd_dist "$wt"
