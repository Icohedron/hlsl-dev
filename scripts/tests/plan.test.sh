#!/usr/bin/env bash
# Plans a configure and a build for every checkout the workspace actually has,
# with --dry-run, and checks what came out.
#
# The unit test next door works on fake checkouts, so it says nothing about
# real worktree discovery, real dependency resolution or the flag templates
# surviving expansion. This does: it runs the same code path a real configure
# takes, right up to the cmake invocation, and then looks at the command.
#
# A checkout that is not there is skipped, not failed -- `devenv test` runs in
# the dev container before `hlsl-setup` has ever been called.
#
# `$HD_...` is matched literally throughout: a placeholder reaching a cmake
# command line means the expander did not run.
# shellcheck disable=SC2016
set -eo pipefail

here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
# shellcheck source=./harness.sh
source "$here/harness.sh"

root=${HLSL_DEV_ROOT:-${DEVENV_ROOT:?not in the developer environment}}
llvm=$root/llvm-project
dxc=$root/DirectXShaderCompiler
offload=$root/offload-test-suite
golden=$root/offload-golden-images

# The build directory is named, not assumed: the dev container sets
# HLSL_BUILD_DIR_NAME=build-container so its trees and the host's stay apart,
# and this suite plans against whatever environment it is run in.
build=${HLSL_BUILD_DIR_NAME:-build}

# Nothing here may touch the workspace: --dry-run exists precisely so a plan
# costs nothing. Anything it creates is a bug in the dry-run guards.
state() { find "$root/.hlsl-dev" -type f 2>/dev/null | sort || true; }
builds() {
    local d
    for d in "$llvm/$build" "$dxc/$build" "$offload/$build"; do
        [ -d "$d" ] && printf '%s\n' "$d"
    done
    return 0
}
state_before=$(state)
builds_before=$(builds)

plan() { # plan <task> <args...> -> stdout+stderr of the dry run, exit code in $rc
    rc=0
    out=$("$@" --dry-run 2>&1) || rc=$?
}

# --- llvm-project -----------------------------------------------------------
if [ -f "$llvm/llvm/CMakeLists.txt" ]; then
    plan hlsl-configure --in "$llvm"
    check "llvm: the plan succeeds" "$rc" "0"
    contains "llvm: configures its llvm/ directory" "$out" "would run: cmake -S $llvm/llvm -B $llvm/$build"
    contains "llvm: build type reaches the flags" "$out" "-DCMAKE_BUILD_TYPE=RelWithDebInfo"
    lacks "llvm: no placeholder survives expansion" "$out" '$HD_'
    contains "llvm: the HLSL cache comes last" "$out" "-C $llvm/clang/cmake/caches/HLSL.cmake"
    contains "llvm: it includes the offload suite" "$out" "-DLLVM_EXTERNAL_OFFLOADTEST_SOURCE_DIR=$offload"
    contains "llvm: with the golden images" "$out" "-DGOLDENIMAGE_DIR=$golden"
    contains "llvm: and a dxc to test against" "$out" "-DDXC_EXECUTABLE="
    contains "llvm: the D3D12 decision is explicit" "$out" "-DCMAKE_DISABLE_FIND_PACKAGE_D3D12_WSL="

    plan hlsl-build --in "$llvm" clang
    check "llvm: a build plans too" "$rc" "0"
    contains "llvm: on the target asked for" "$out" "would run: cmake --build $llvm/$build --target clang"
else
    skip "llvm-project is not checked out"
fi

# --- DirectXShaderCompiler --------------------------------------------------
if [ -f "$dxc/cmake/caches/PredefinedParams.cmake" ]; then
    plan hlsl-configure --in "$dxc"
    check "dxc: the plan succeeds" "$rc" "0"
    contains "dxc: configures its top level" "$out" "would run: cmake -S $dxc -B $dxc/$build"
    contains "dxc: with its own cache" "$out" "-C $dxc/cmake/caches/PredefinedParams.cmake"
    lacks "dxc: no placeholder survives expansion" "$out" '$HD_'
else
    skip "DirectXShaderCompiler is not checked out"
fi

# --- offload-test-suite -----------------------------------------------------
if [ -d "$offload/tools/offloader" ]; then
    plan hlsl-configure --in "$offload"
    check "offload: the plan succeeds" "$rc" "0"
    contains "offload: configures standalone" "$out" "would run: cmake -S $offload -B $offload/$build"
    contains "offload: against an llvm distribution" "$out" "-DCMAKE_PREFIX_PATH=$llvm/build-dist/install/lib/cmake/llvm"
    contains "offload: knowing the llvm sources" "$out" "-DLLVM_MAIN_SRC_DIR=$llvm/llvm"
    contains "offload: with the golden images" "$out" "-DGOLDENIMAGE_DIR=$golden"
    lacks "offload: no placeholder survives expansion" "$out" '$HD_'

    # The distribution is the one prerequisite a plan has to announce rather
    # than build, whichever way round it is.
    if [ -f "$llvm/build-dist/install/lib/cmake/llvm/LLVMConfig.cmake" ]; then
        lacks "offload: an installed distribution is not rebuilt" "$out" "would build: the LLVM distribution"
    else
        contains "offload: a missing distribution is announced" "$out" "would build: the LLVM distribution"
    fi

    plan hlsl-test --in "$offload" clang-vk
    check "offload: a test run plans too" "$rc" "0"
    contains "offload: through the suite's target" "$out" "--target check-hlsl-clang-vk"
else
    skip "offload-test-suite is not checked out"
fi

# --- the inventory ----------------------------------------------------------
# hlsl-ls is the one task that reads the whole workspace at once, so this is
# also where a bad pin file or a mangled column shows up.
state=$(mktemp -d)
trap 'rm -rf "$state"' EXIT
if [ -d "$offload/tools/offloader" ]; then
    mkdir -p "$state/pins"
    printf 'LLVM=%s\nDXC=%s/build/bin\n' "$llvm" "$dxc" \
        >"$state/pins/$(basename "$offload").env"
fi

out=$(HLSL_DEV_STATE=$state hlsl-ls)
contains "ls: has a header" "$out" "worktree"
contains "ls: names every repository" "$out" "offload-golden-images"
lacks "ls: no line trails whitespace" "$(printf '%s\n' "$out" | grep -c '[[:space:]]$')" "1"
if [ -d "$offload/tools/offloader" ]; then
    contains "ls: shows what a checkout is pinned to" "$out" "llvm: llvm-project"
    contains "ls: and its dxc, by checkout not by path" "$out" "dxc: DirectXShaderCompiler"
    lacks "ls: without the build/bin tail" "$out" "dxc: DirectXShaderCompiler/build/bin"
fi

# --- and it was all free ----------------------------------------------------
check "a plan writes no state" "$(state)" "$state_before"
check "a plan creates no build directory" "$(builds)" "$builds_before"

exit "$fail"
