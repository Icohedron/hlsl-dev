#!/usr/bin/env bash
# Self-test for `hlsl-trim`, which is the one task that deletes things out of a
# build directory rather than the whole of it.
#
# It builds a throwaway workspace with a *real* build.ninja -- the task reads
# the graph, it never builds -- and stocks the build directory with files that
# each stand for a rule: reachable from the kept target or not, ELF or script,
# executable or not, inside the install prefix or not.
set -eo pipefail

here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
# shellcheck source=./harness.sh
source "$here/harness.sh"

trim=$(command -v hlsl-trim || true)
[ -n "$trim" ] || trim="bash $here/../tasks/trim.sh"

root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
: >"$root/devenv.nix"

llvm=$root/llvm-project
mkdir -p "$llvm/llvm" "$llvm/clang"
: >"$llvm/llvm/CMakeLists.txt"
build=$llvm/build

export HLSL_DEV_ROOT=$root
export HLSL_DEV_STATE=$root/.hlsl-dev
export HLSL_INSTALL_HOOKS=0
unset HLSL_WT HLSL_LLVM HLSL_DXC HLSL_OFFLOAD HLSL_GOLDEN \
    HLSL_BUILD_DIR HLSL_BUILD_TYPE HLSL_DIST_PREFIX HLSL_AUTO
export HLSL_BUILD_DIR_NAME=build

elf() { # a plausible ELF executable, small
    mkdir -p "$(dirname "$1")"
    printf '\177ELF\002\001\001\000stand-in\n' >"$1"
    chmod +x "$1"
}

# --- a build directory whose graph says what is wanted -----------------------
mkdir -p "$build/bin" "$build/lib" "$build/unittests" \
    "$build/install/bin" "$build/CMakeFiles"
cat >"$build/build.ninja" <<'NINJA'
rule link
  command = touch $out
build bin/keeper: link
build bin/dropper: link
build lib/libDropped.so.1: link
build unittests/KeptTests: link
build check-hlsl: phony bin/keeper unittests/KeptTests
build check-other: phony lib/libDropped.so.1
NINJA

elf "$build/bin/keeper"
elf "$build/bin/dropper"
elf "$build/lib/libDropped.so.1"
elf "$build/unittests/KeptTests"
elf "$build/install/bin/dropper"  # an install prefix is an artifact, not spoil
elf "$build/CMakeFiles/dropper"   # cmake's own scratch is not ours either
ln -s libDropped.so.1 "$build/lib/libDropped.so"
ln -s keeper "$build/bin/keeper-alias"
printf '#!/bin/sh\necho lit\n' >"$build/bin/llvm-lit" # a script, in no graph
chmod +x "$build/bin/llvm-lit"
printf '\177ELF not executable\n' >"$build/lib/libDropped.a" # no +x, not ours

there() { [ -e "$1" ] && echo there || echo gone; }

# --- a dry run reports and removes nothing ----------------------------------
out=$($trim --in "$llvm" --dry-run 2>&1) && rc=0 || rc=$?
check "dry run: succeeds" "$rc" "0"
contains "dry run: counts the unreachable binaries" "$out" "would remove 2 binaries"
contains "dry run: names one" "$out" "bin/dropper"
check "dry run: removes nothing" "$(there "$build/bin/dropper")" "there"

# --- the real thing ---------------------------------------------------------
out=$($trim --in "$llvm" 2>&1) && rc=0 || rc=$?
check "trim: succeeds" "$rc" "0"
check "trim: the unreachable binary goes" "$(there "$build/bin/dropper")" "gone"
check "trim: so does the unreachable library" "$(there "$build/lib/libDropped.so.1")" "gone"
check "trim: what the target needs stays" "$(there "$build/bin/keeper")" "there"
check "trim: including its unit tests" "$(there "$build/unittests/KeptTests")" "there"
check "trim: scripts are never touched" "$(there "$build/bin/llvm-lit")" "there"
check "trim: nor are non-executables" "$(there "$build/lib/libDropped.a")" "there"
check "trim: the install prefix is left alone" "$(there "$build/install/bin/dropper")" "there"
check "trim: and cmake's scratch" "$(there "$build/CMakeFiles/dropper")" "there"
check "trim: a symlink to something removed goes too" \
    "$(there "$build/lib/libDropped.so")" "gone"
check "trim: one to something kept does not" "$(there "$build/bin/keeper-alias")" "there"

out=$($trim --in "$llvm" 2>&1) && rc=0 || rc=$?
check "trim: running it again is a no-op" "$rc" "0"
contains "trim: and says so" "$out" "nothing to trim"

# --- naming the targets yourself --------------------------------------------
elf "$build/bin/dropper"
out=$($trim --in "$llvm" --dry-run check-other 2>&1) && rc=0 || rc=$?
check "targets: a named target replaces the default" "$rc" "0"
contains "targets: what it does not need goes" "$out" "bin/keeper"

out=$($trim --in "$llvm" nonesuch 2>&1) && rc=0 || rc=$?
check "targets: an unknown target is refused" "$rc" "1"
contains "targets: and says why" "$out" "none of those targets exist"
check "targets: a refusal removes nothing" "$(there "$build/bin/keeper")" "there"

# --- an unconfigured build directory ----------------------------------------
rm -f "$build/build.ninja"
out=$($trim --in "$llvm" 2>&1) && rc=0 || rc=$?
check "unconfigured: refused" "$rc" "1"
contains "unconfigured: and says why" "$out" "not a configured Ninja build directory"

exit "$fail"
