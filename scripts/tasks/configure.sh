#!/usr/bin/env bash
# summary: Configure the checkout you are standing in
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_DESC="Configures the worktree you are standing in (or --in <worktree>).

  llvm-project            -> LLVM + Clang + OffloadTest in <worktree>/build
  DirectXShaderCompiler   -> DXC in <worktree>/build
  offload-test-suite      -> a standalone build in <worktree>/build, against
                             the LLVM distribution of the resolved llvm-project
                             worktree, which is installed first if it is not
                             there yet

Configuring is rarely something to do on its own: hlsl-build and hlsl-test
configure what they need. Run it directly to change what a worktree builds
against -- the dependency flags are remembered for later commands.

To build the offload suite inside an llvm build tree instead of standalone,
configure that llvm worktree against its sources:

  hlsl-configure --in llvm-project.my-feature --offload offload-test-suite.mine"
HD_TASK_OPTS="in= llvm= dxc= offload= dist_prefix= build_type= fresh forget dry_run no_auto"
hd_parse "$@"
hd_init

wt=$(hd_target)
HD_WT=$wt
hd_configure "$wt"
