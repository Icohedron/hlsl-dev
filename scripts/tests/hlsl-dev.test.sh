#!/usr/bin/env bash
# Self-test for scripts/hlsl-dev.sh -- the resolution, prerequisite and dry-run
# logic that every task depends on. Run by `devenv test`.
#
# It builds a throwaway workspace of fake checkouts in $TMPDIR (a "checkout" is
# recognised by its contents, so a handful of empty files is enough), stubs the
# two functions that would otherwise compile something, and never touches the
# real workspace or its pin store.
set -eo pipefail

here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
# shellcheck source=./harness.sh
source "$here/harness.sh"
# shellcheck source=../hlsl-dev.sh
source "$here/../hlsl-dev.sh"

# --- a workspace made of fake checkouts -------------------------------------
root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
: >"$root/devenv.nix"
mkdir -p "$root/scripts"
mkdir -p "$root/llvm-project/llvm" "$root/llvm-project/clang"
: >"$root/llvm-project/llvm/CMakeLists.txt"
mkdir -p "$root/llvm-project.pinned/llvm" "$root/llvm-project.pinned/clang"
: >"$root/llvm-project.pinned/llvm/CMakeLists.txt"
mkdir -p "$root/DirectXShaderCompiler/cmake/caches" "$root/DirectXShaderCompiler/tools/clang"
: >"$root/DirectXShaderCompiler/cmake/caches/PredefinedParams.cmake"
mkdir -p "$root/offload-test-suite/tools/offloader" "$root/offload-test-suite/lib/API"
mkdir -p "$root/offload-golden-images/hlsl"
: >"$root/offload-golden-images/README.md"

export HLSL_DEV_ROOT=$root
export HLSL_DEV_STATE=$root/.hlsl-dev
export HLSL_CMAKE_FLAGS_LLVM="-DCMAKE_BUILD_TYPE=\$HD_BUILD_TYPE"
llvm=$root/llvm-project
offload=$root/offload-test-suite

hd_init_root
check "workspace root" "$HD_ROOT" "$root"

# --- kinds are read from the contents, not the name -------------------------
check "kind: llvm"    "$(hd_kind "$llvm")" "llvm"
check "kind: dxc"     "$(hd_kind "$root/DirectXShaderCompiler")" "dxc"
check "kind: offload" "$(hd_kind "$offload")" "offload"
check "kind: golden"  "$(hd_kind "$root/offload-golden-images")" "golden"
check "kind: none"    "$(hd_kind "$root/scripts")" ""

# --- dependency resolution --------------------------------------------------
HD_OPT_LLVM="" HD_OPT_DXC="" HD_OPT_OFFLOAD="" HD_OPT_GOLDEN=""
HD_OPT_BUILD_DIR="" HD_OPT_BUILD_TYPE="" HD_OPT_DIST_PREFIX="" HD_OPT_FRESH=""
HD_AUTO=1 HD_DRY_RUN="" HD_WT=""

check "dep: falls back to the submodule" "$(hd_dep llvm "$offload")" "$llvm"
hd_pin_set "$offload" LLVM "$root/llvm-project.pinned"
check "dep: a pin wins over the fallback" "$(hd_dep llvm "$offload")" "$root/llvm-project.pinned"
HD_OPT_LLVM=$llvm
check "dep: a flag wins over the pin" "$(hd_dep llvm "$offload")" "$llvm"
HD_OPT_LLVM=""
hd_pin_clear "$offload"
check "dep: cleared pins fall back again" "$(hd_dep llvm "$offload")" "$llvm"

# A pin is a memory, not an instruction: one that no longer resolves must not
# take the checkout down with it. This is what a store written against another
# path looks like -- the dev container reading pins the host wrote.
printf 'LLVM=%s\n' "/elsewhere/hlsl-dev/llvm-project" >"$(hd_pin_file "$offload")"
check "dep: a stale pin falls back" "$(hd_dep llvm "$offload" 2>/dev/null)" "$llvm"
contains "dep: and says so" "$(hd_dep llvm "$offload" 2>&1 >/dev/null)" \
    "is pinned to the llvm-project checkout"
hd_pin_clear "$offload"

# An explicit --llvm is not a memory, and still fails loudly.
HD_OPT_LLVM=/elsewhere/hlsl-project
contains "dep: an explicit spec still fails" \
    "$( (hd_dep llvm "$offload") 2>&1 || true)" "no llvm-project worktree matches"
HD_OPT_LLVM=""

# --- build directories ------------------------------------------------------
check "build dir: default"     "$(hd_build_dir "$llvm")" "$llvm/build"
check "dist prefix: default"   "$(hd_dist_prefix "$llvm")" "$llvm/build-dist/install"
HD_WT=$llvm HD_OPT_BUILD_DIR=build-debug
check "build dir: \$HLSL_BUILD_DIR, target only" "$(hd_build_dir "$llvm")" "$llvm/build-debug"
check "build dir: not for dependencies"          "$(hd_build_dir "$offload")" "$offload/build"
HD_WT="" HD_OPT_BUILD_DIR=""

# The name, on the other hand, is what an environment that must not share
# build trees sets -- the dev container -- so it holds for every worktree, and
# `hlsl-ls` reports that environment's trees rather than the other one's.
HLSL_BUILD_DIR_NAME="build-container"
check "build dir: \$HLSL_BUILD_DIR_NAME, every worktree" \
    "$(hd_build_dir "$llvm")" "$llvm/build-container"
check "build dir: dependencies too" \
    "$(hd_build_dir "$offload")" "$offload/build-container"
check "build dir: the distribution is still shared" \
    "$(hd_dist_prefix "$llvm")" "$llvm/build-dist/install"
HD_WT=$llvm HD_OPT_BUILD_DIR=build-debug
check "build dir: \$HLSL_BUILD_DIR still wins for the target" \
    "$(hd_build_dir "$llvm")" "$llvm/build-debug"
HD_WT="" HD_OPT_BUILD_DIR="" HLSL_BUILD_DIR_NAME=""

# --- pins -------------------------------------------------------------------
hd_pin_set "$llvm" DXC "/some/bin"
check "pin: roundtrip" "$(hd_pin_get "$llvm" DXC)" "/some/bin"
HD_DRY_RUN=1 hd_pin_set "$llvm" DXC "/other/bin"
check "pin: a dry run writes nothing" "$(hd_pin_get "$llvm" DXC)" "/some/bin"
hd_pin_clear "$llvm"
check "pin: cleared" "$(hd_pin_get "$llvm" DXC)" ""

# What is inside the workspace is stored relative to it, so the same store
# read through another path -- /workspaces/hlsl-dev in the dev container --
# still names the same checkouts.
hd_pin_set "$offload" LLVM "$root/llvm-project.pinned"
check "pin: a workspace path is stored relative" \
    "$(sed -n 's/^LLVM=//p' "$(hd_pin_file "$offload")")" "./llvm-project.pinned"
check "pin: and comes back absolute" \
    "$(hd_pin_get "$offload" LLVM)" "$root/llvm-project.pinned"

# Anything else is left alone: a dxc outside the tree, or a word like "nix".
hd_pin_set "$offload" DXC nix
check "pin: a word is stored as it is" \
    "$(sed -n 's/^DXC=//p' "$(hd_pin_file "$offload")")" "nix"
check "pin: and read back as it is" "$(hd_pin_get "$offload" DXC)" "nix"

# A store written before pins were relative migrates on the next write.
printf 'LLVM=%s\nBUILD_TYPE=Debug\n' "$root/llvm-project.pinned" >"$(hd_pin_file "$offload")"
hd_pin_set "$offload" GOLDEN "$root/offload-golden-images"
check "pin: an absolute store is rewritten" \
    "$(sed -n 's/^LLVM=//p' "$(hd_pin_file "$offload")")" "./llvm-project.pinned"
check "pin: without disturbing the scalars" \
    "$(hd_pin_get "$offload" BUILD_TYPE)" "Debug"
hd_pin_clear "$offload"

# --- prerequisites ----------------------------------------------------------
HD_DRY_RUN=1 HD_AUTO=1
check "provide: a dry run only reports" "$(hd_provide thing 2>&1)" "==> would build: thing"
HD_DRY_RUN="" HD_AUTO=1
check "provide: auto says what it is doing" \
    "$(hd_provide thing 2>&1)" "==> missing prerequisite: thing -- building it now"
HD_AUTO=""
contains "provide: --no-auto refuses" "$( (hd_provide thing) 2>&1 || true)" \
    "error: missing prerequisite: thing"
HD_AUTO=1

# hd_dist is the expensive part; stub it out and check the decisions around it.
hd_dist() {
    printf 'HD_WT=%s\n' "${HD_WT:-}" >&2
    mkdir -p "$(hd_dist_prefix "$1")/lib/cmake/llvm"
    : >"$(hd_dist_prefix "$1")/lib/cmake/llvm/LLVMConfig.cmake"
}

HD_DRY_RUN=1
check "ensure_dist: a dry run installs nothing" \
    "$( hd_ensure_dist "$llvm" >/dev/null 2>&1; [ -e "$llvm/build-dist" ] && echo built || echo clean)" \
    "clean"

HD_DRY_RUN=""
HD_WT=$offload
out=$(hd_ensure_dist "$llvm" 2>/dev/null)
check "ensure_dist: installs what is missing" "$out" "$llvm/build-dist/install"
check "ensure_dist: leaves the caller's target alone" "$HD_WT" "$offload"
check "ensure_dist: an existing one is left alone" "$(hd_ensure_dist "$llvm" 2>&1)" \
    "$llvm/build-dist/install"

rm -rf "$llvm/build-dist"
hd_dist() { :; } # a build that installs nothing must not pass unnoticed
contains "ensure_dist: checks the result" "$( (hd_ensure_dist "$llvm") 2>&1 || true)" \
    "no LLVMConfig.cmake"

HD_OPT_DIST_PREFIX=/nowhere
contains "ensure_dist: never builds into --dist-prefix" \
    "$( (hd_ensure_dist "$llvm") 2>&1 || true)" "no LLVM distribution at /nowhere"
HD_OPT_DIST_PREFIX=""

# --- workspace settings: the Vulkan driver and D3D12 ------------------------
export HLSL_VK_ICD_DIR=$root/icd.d
mkdir -p "$HLSL_VK_ICD_DIR"
arch=$(uname -m)
: >"$HLSL_VK_ICD_DIR/lvp_icd.$arch.json"
: >"$HLSL_VK_ICD_DIR/radeon_icd.$arch.json"
unset HLSL_VK_DRIVER VK_DRIVER_FILES VK_ICD_FILENAMES

check "vk: lavapipe by default" "$(hd_vk_icd)" "$HLSL_VK_ICD_DIR/lvp_icd.$arch.json"
hd_setting_set VK_DRIVER radeon
check "vk: a saved choice applies at once" "$(hd_vk_icd)" "$HLSL_VK_ICD_DIR/radeon_icd.$arch.json"
check "vk: \$HLSL_VK_DRIVER overrides it" \
    "$(HLSL_VK_DRIVER=lvp hd_vk_icd)" "$HLSL_VK_ICD_DIR/lvp_icd.$arch.json"
check "vk: system means no manifest" "$(HLSL_VK_DRIVER=system hd_vk_icd)" ""
check "vk: a path is taken as given" "$(HLSL_VK_DRIVER=/some/icd.json hd_vk_icd)" "/some/icd.json"

hd_vk_export
check "vk: the loader is pinned" "$VK_DRIVER_FILES" "$HLSL_VK_ICD_DIR/radeon_icd.$arch.json"
check "vk: and the legacy name too" "$VK_ICD_FILENAMES" "$VK_DRIVER_FILES"
HLSL_VK_DRIVER=system hd_vk_export
check "vk: system unsets it" "${VK_DRIVER_FILES:-unset}" "unset"
HLSL_VK_DRIVER=nosuch hd_vk_export 2>/dev/null
check "vk: a missing manifest is not pinned" "${VK_DRIVER_FILES:-unset}" "unset"
contains "vk: and it says so" "$(HLSL_VK_DRIVER=nosuch hd_vk_export 2>&1)" "missing manifest"
hd_setting_set VK_DRIVER ""

check "d3d12: on by default" "$(hd_d3d12)" "on"
check "d3d12: on lets cmake detect" "$(hd_d3d12_flags | tr '\n' ' ')" \
    "-DCMAKE_DISABLE_FIND_PACKAGE_D3D12=OFF -DCMAKE_DISABLE_FIND_PACKAGE_D3D12_WSL=OFF "
hd_setting_set D3D12 off
check "d3d12: off is remembered" "$(hd_d3d12)" "off"
check "d3d12: off disables detection" "$(hd_d3d12_flags | tr '\n' ' ')" \
    "-DCMAKE_DISABLE_FIND_PACKAGE_D3D12=ON -DCMAKE_DISABLE_FIND_PACKAGE_D3D12_WSL=ON "
HD_DRY_RUN=1 hd_setting_set D3D12 on
check "d3d12: a dry run changes nothing" "$(hd_d3d12)" "off"
HD_DRY_RUN=""
hd_setting_set D3D12 ""
check "settings: cleared" "$(hd_setting_get D3D12)" ""

# --- the clang-format pre-commit hook ---------------------------------------
git -C "$llvm" init -q 2>/dev/null
: >"$llvm/.clang-format"
hook=$(hd_hook_path "$llvm")
check "hook: path is in the git dir" "$hook" "$llvm/.git/hooks/pre-commit"

hd_hook_ok "$llvm" && hook_rc=0 || hook_rc=$?
check "hook: reported missing" "$hook_rc" "1"
hd_hook_install "$llvm" && hook_rc=0 || hook_rc=$?
check "hook: installing reports the change" "$hook_rc" "1"
check "hook: it is executable" "$([ -x "$hook" ] && echo yes || echo no)" "yes"
contains "hook: it carries the marker" "$(cat "$hook")" "$HD_HOOK_MARKER"
contains "hook: and never blocks" "$(cat "$hook")" "exit 0"
hd_hook_ok "$llvm" && hook_rc=0 || hook_rc=$?
check "hook: now current" "$hook_rc" "0"
hd_hook_install "$llvm" && hook_rc=0 || hook_rc=$?
check "hook: installing again is a no-op" "$hook_rc" "0"

printf 'stale\n%s\n' "$HD_HOOK_MARKER" >"$hook"
hd_hook_ok "$llvm" && hook_rc=0 || hook_rc=$?
check "hook: an outdated one is noticed" "$hook_rc" "1"

printf '#!/bin/sh\nexit 0\n' >"$hook"
hd_hook_ok "$llvm" && hook_rc=0 || hook_rc=$?
check "hook: someone else's is left to them" "$hook_rc" "2"
hd_hook_install "$llvm" 2>/dev/null && hook_rc=0 || hook_rc=$?
check "hook: and never overwritten" "$hook_rc" "2"
check "hook: really untouched" "$(cat "$hook")" "$(printf '#!/bin/sh\nexit 0')"
hd_hook_remove "$llvm" && hook_rc=0 || hook_rc=$?
check "hook: nor removed" "$hook_rc" "2"

hd_hook_install "$llvm" >/dev/null 2>&1 || true
rm -f "$hook"
hd_hook_install "$llvm" >/dev/null 2>&1 || true
HD_DRY_RUN=1 hd_hook_remove "$llvm" >/dev/null 2>&1 || true
check "hook: a dry run removes nothing" "$([ -f "$hook" ] && echo there || echo gone)" "there"
HD_DRY_RUN=""
hd_hook_remove "$llvm" && hook_rc=0 || hook_rc=$?
check "hook: removed" "$([ -f "$hook" ] && echo there || echo gone)" "gone"

rm -f "$llvm/.clang-format"
rm -rf "$llvm/.git"

# --- a dry run touches nothing ----------------------------------------------
HD_DRY_RUN=1
check "dry run: no command is run" "$(hd_run echo hello 2>&1)" "==> would run: echo hello"
HD_OPT_FRESH=1
hd_prepare_build_dir "$root/dry-build" 2>/dev/null
check "dry run: no build directory is made" \
    "$([ -e "$root/dry-build" ] && echo made || echo clean)" "clean"
HD_OPT_FRESH=""
HD_DRY_RUN=""
hd_prepare_build_dir "$root/dry-build"
check "otherwise the build directory is made" \
    "$([ -d "$root/dry-build" ] && echo made || echo clean)" "made"

exit "$fail"
