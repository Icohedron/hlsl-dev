#!/usr/bin/env bash
# summary: Run offload tests: a whole suite, a subdirectory or a lit filter
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_ARGS="[suite] [filter]"
HD_TASK_DESC="Runs offload tests for the worktree you are standing in (or --in).

  hlsl-test                            the whole check-hlsl umbrella target
  hlsl-test clang-vk                   one suite, via its check-hlsl-clang-vk
                                       target (so its dependencies are rebuilt)
  hlsl-test clang-vk Feature/HLSLLib   a subdirectory or a single .test file
  hlsl-test clang-vk 'log2.*'          anything that is not a path is handed to
                                       lit's --filter as a regular expression
  hlsl-test clang-vk --dxc dxc.my-fix  retarget the build tree at another DXC
                                       first (cheap: nothing is recompiled)

Everything the run needs is provided on the way: the suite is configured and
built, and a standalone offload worktree installs the LLVM distribution it
links against if that is missing. --dry-run reports that plan instead.

Suites: d3d12, vk, mtl, warp-d3d12, clang-d3d12, clang-vk, clang-mtl,
clang-warp-d3d12, unit."
HD_TASK_OPTS="in= dxc= llvm= dist_prefix= lit_args= platform= jobs= dry_run no_auto"
hd_parse "$@"
hd_init

wt=$(hd_target llvm offload)
HD_WT=$wt
# Tests execute what was built, so a cross build tree is the wrong one to ask.
# Say so before configuring or building anything.
hd_require_native "run tests"
hd_ensure_configured "$wt"
build=$(hd_build_dir "$wt")

# `--dxc` retargets the configured build tree; DXC only feeds the lit
# configuration, so this regenerates in seconds and compiles nothing.
if [ -n "$HD_OPT_DXC" ]; then
    dxcbin=$(hd_dxc_bin_dir "$wt" ensure)
    hd_sync_dxc "$build" "$dxcbin"
    hd_pin_set "$wt" DXC "$dxcbin"
fi

suite=${HD_ARGV[0]:-}
filter=${HD_ARGV[1]:-}

if [ -n "$suite" ]; then
    case " $HD_SUITES unit " in
    *" $suite "*) ;;
    *) hd_die "unknown suite '$suite'; expected one of: $HD_SUITES unit" ;;
    esac
fi

if [ -z "$filter" ]; then
    hd_build "$wt" "check-hlsl${suite:+-$suite}"
    exit 0
fi

[ -n "$suite" ] || hd_die "a filter needs a suite: hlsl-test <suite> <path-or-regex>"

root=$(hd_test_root "$wt" "$build")
[ -d "$root/$suite" ] || hd_die "suite '$suite' is not configured in $build"
src=$(hd_suite_src "$root/$suite")

# Bring the tools the tests invoke up to date, then hand the selection to lit.
hd_build "$wt" hlsl-test-depends

if [ -n "$src" ] && [ -e "$src/test/$filter" ]; then
    # lit resolves a build-tree path back to the source tree, so subdirectories
    # that only exist in the sources can be addressed this way.
    # shellcheck disable=SC2086 # --lit-args is a flag list; splitting it is the point
    hd_lit "$build" ${lit_args:--v} "$root/$suite/$filter"
else
    # shellcheck disable=SC2086
    hd_lit "$build" ${lit_args:--v} --filter "$filter" "$root/$suite"
fi
