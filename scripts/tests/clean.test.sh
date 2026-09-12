#!/usr/bin/env bash
# Self-test for `hlsl-clean`, the task that removes build trees -- including
# the sweeps (--all, --all-build-dirs) that reach worktrees the caller is not
# standing in, and therefore have to be exact about what they touch.
#
# It builds a throwaway workspace of fake checkouts stocked with build
# directories of every shape: this environment's, the other environment's, a
# cross tree, the distribution trees and a plain directory that only looks like
# one. Nothing is ever configured or built.
set -eo pipefail

here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
# shellcheck source=./harness.sh
source "$here/harness.sh"

clean=$(command -v hlsl-clean || true)
[ -n "$clean" ] || clean="bash $here/../tasks/clean.sh"

root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
: >"$root/devenv.nix"

export HLSL_DEV_ROOT=$root
export HLSL_DEV_STATE=$root/.hlsl-dev
export HLSL_INSTALL_HOOKS=0
unset HLSL_WT HLSL_LLVM HLSL_DXC HLSL_OFFLOAD HLSL_GOLDEN \
    HLSL_BUILD_DIR HLSL_BUILD_TYPE HLSL_DIST_PREFIX HLSL_AUTO HLSL_PLATFORM
export HLSL_BUILD_DIR_NAME=build

llvm=$root/llvm-project
other=$root/llvm-project.feature
dxc=$root/DirectXShaderCompiler
offload=$root/offload-test-suite

mkdir -p "$llvm/llvm" "$llvm/clang" "$other/llvm" "$other/clang"
: >"$llvm/llvm/CMakeLists.txt"
: >"$other/llvm/CMakeLists.txt"
mkdir -p "$dxc/cmake/caches" "$dxc/tools/clang"
: >"$dxc/cmake/caches/PredefinedParams.cmake"
mkdir -p "$offload/tools/offloader" "$offload/lib/API"
mkdir -p "$root/offload-golden-images/hlsl"
: >"$root/offload-golden-images/README.md"

stock() { # stock <worktree> <directory>...
    local wt=$1 d
    shift
    for d in "$@"; do
        mkdir -p "$wt/$d"
        : >"$wt/$d/CMakeCache.txt"
    done
}

reset() { # every build tree back, so each case starts from the same workspace
    rm -rf "$llvm"/build* "$other"/build* "$dxc"/build* "$offload"/build*
    stock "$llvm" build build-container build.windows-x64 \
        build-native-tools build-dist build-dist/install
    stock "$other" build-container
    stock "$dxc" build
    stock "$offload" build build-container
    mkdir -p "$llvm/buildbot-notes" # looks like one, is not one
    : >"$llvm/buildbot-notes/keep-me"
}

there() { [ -e "$1" ] && echo there || echo gone; }

# --- one worktree, this environment's tree only -----------------------------
reset
out=$($clean --in "$llvm" 2>&1) && rc=0 || rc=$?
check "single: succeeds" "$rc" "0"
contains "single: says what went" "$out" "Removing $llvm/build"
check "single: its own build tree" "$(there "$llvm/build")" "gone"
check "single: not the other environment's" "$(there "$llvm/build-container")" "there"
check "single: not the cross tree" "$(there "$llvm/build.windows-x64")" "there"
check "single: not the distribution" "$(there "$llvm/build-dist")" "there"
check "single: not another worktree" "$(there "$offload/build")" "there"

# --- --all-build-dirs: every tree of that worktree, whatever its name -------
reset
out=$($clean --in "$llvm" --all-build-dirs 2>&1) && rc=0 || rc=$?
check "all-build-dirs: succeeds" "$rc" "0"
check "all-build-dirs: this environment's" "$(there "$llvm/build")" "gone"
check "all-build-dirs: the other one's" "$(there "$llvm/build-container")" "gone"
check "all-build-dirs: the cross tree" "$(there "$llvm/build.windows-x64")" "gone"
check "all-build-dirs: the host tablegens" "$(there "$llvm/build-native-tools")" "gone"
check "all-build-dirs: leaves the distribution" "$(there "$llvm/build-dist")" "there"
check "all-build-dirs: leaves a lookalike" "$(there "$llvm/buildbot-notes/keep-me")" "there"
check "all-build-dirs: leaves other worktrees" "$(there "$other/build-container")" "there"

# --dist takes the distribution build and its install prefix with it.
reset
$clean --in "$llvm" --all-build-dirs --dist >/dev/null 2>&1
check "all-build-dirs --dist: the distribution too" "$(there "$llvm/build-dist")" "gone"

# It says what it covers, rather than accepting a narrower-sounding --platform.
out=$($clean --in "$llvm" --all-build-dirs --platform windows-x64 2>&1) && rc=0 || rc=$?
check "all-build-dirs: --platform is refused" "$rc" "1"
contains "all-build-dirs: and says why" "$out" "drop --platform"

# --- --dry-run removes nothing ----------------------------------------------
reset
out=$($clean --all --all-build-dirs --dist --dry-run 2>&1) && rc=0 || rc=$?
check "dry run: succeeds" "$rc" "0"
contains "dry run: reports in the conditional" "$out" "Would remove $llvm/build"
lacks "dry run: does not claim to have removed" "$out" "Removing "
check "dry run: leaves the build tree" "$(there "$llvm/build")" "there"
check "dry run: leaves the distribution" "$(there "$llvm/build-dist")" "there"

# --- --all: every worktree of every repository ------------------------------
reset
out=$($clean --all --all-build-dirs 2>&1) && rc=0 || rc=$?
check "all: succeeds" "$rc" "0"
check "all: the submodule" "$(there "$llvm/build")" "gone"
check "all: its worktree" "$(there "$other/build-container")" "gone"
check "all: another repository" "$(there "$dxc/build")" "gone"
check "all: and another" "$(there "$offload/build-container")" "gone"
check "all: not the distribution" "$(there "$llvm/build-dist")" "there"
contains "all: totals the sweep" "$out" "checkouts"

# --- --all <repository>: one repository's worktrees --------------------------
reset
out=$($clean --all offload-test-suite 2>&1) && rc=0 || rc=$?
check "repo: succeeds" "$rc" "0"
check "repo: that repository" "$(there "$offload/build")" "gone"
check "repo: not another" "$(there "$llvm/build")" "there"
$clean --all llvm >/dev/null 2>&1
check "repo: the short kind works too" "$(there "$llvm/build")" "gone"

out=$($clean --all bogus 2>&1) && rc=0 || rc=$?
check "repo: an unknown name is refused" "$rc" "1"
contains "repo: and lists the real ones" "$out" "offload-test-suite"
out=$($clean --all offload-golden-images 2>&1) && rc=0 || rc=$?
check "repo: the image data is refused" "$rc" "1"
contains "repo: and says it is data" "$out" "not something that gets built"
out=$($clean llvm-project 2>&1) && rc=0 || rc=$?
check "repo: a name without --all is refused" "$rc" "1"
contains "repo: and says how to mean it" "$out" "--all"
out=$($clean --all --in "$llvm" 2>&1) && rc=0 || rc=$?
check "repo: --all and --in are refused" "$rc" "1"
contains "repo: and says why" "$out" "cannot be combined with --all"

# --- nothing to remove is still an answer -----------------------------------
reset
$clean --in "$dxc" >/dev/null 2>&1
out=$($clean --in "$dxc" 2>&1) && rc=0 || rc=$?
check "empty: succeeds" "$rc" "0"
contains "empty: names what it looked at" "$out" "$dxc/build does not exist"

exit "$fail"
