#!/usr/bin/env bash
# Self-test for hd_stage_prefix: the prefix `hlsl-package` and `hlsl-repro`
# archive. A standalone offload build is two prefixes merged -- its own install
# plus the LLVM distribution it links against -- and a Windows archive may
# contain no symlinks; both are checked here against fake checkouts, because a
# real standalone build needs an hour of LLVM first.
# Exercises hd_stage_prefix for a standalone offload layout, without the hour
# of LLVM builds a real one needs: a fake distribution prefix and a fake
# offload install, merged the way the task would merge them.
set -eo pipefail
here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
# shellcheck source=../hlsl-dev.sh
source "$here/../hlsl-dev.sh"
root=$(mktemp -d); trap 'rm -rf "$root"' EXIT
: >"$root/devenv.nix"; mkdir -p "$root/scripts"
export HLSL_DEV_ROOT=$root HLSL_DEV_STATE=$root/.hlsl-dev
export HLSL_CMAKE_FLAGS_LLVM="-DX=1"
unset HLSL_BUILD_DIR_NAME HLSL_BUILD_DIR
HD_OPT_PLATFORM="" HD_OPT_BUILD_DIR="" HD_OPT_BUILD_TYPE="" HD_OPT_DIST_PREFIX=""
HD_OPT_LLVM="" HD_OPT_DXC="" HD_OPT_OFFLOAD="" HD_OPT_GOLDEN="" HD_DRY_RUN="" HD_WT=""
hd_init_root

# an llvm worktree with a distribution installed beside it
mkdir -p "$root/llvm-project/llvm" "$root/llvm-project/clang"
: >"$root/llvm-project/llvm/CMakeLists.txt"
mkdir -p "$root/llvm-project/build-dist/install/bin" "$root/llvm-project/build-dist/install/lib/clang/23/include"
for t in clang clang-dxc FileCheck not split-file; do
    printf '#!/bin/sh\n' >"$root/llvm-project/build-dist/install/bin/$t"
    chmod +x "$root/llvm-project/build-dist/install/bin/$t"
done
ln -sf clang "$root/llvm-project/build-dist/install/bin/clang++"
: >"$root/llvm-project/build-dist/install/lib/clang/23/include/hlsl.h"
# ... and LLVM libraries, which a *run* prefix has no use for
mkdir -p "$root/llvm-project/build-dist/install/lib/cmake/llvm"
: >"$root/llvm-project/build-dist/install/lib/libLLVMSupport.a"

# a standalone offload build, installed
mkdir -p "$root/offload-test-suite/tools/offloader" "$root/offload-test-suite/lib/API"
mkdir -p "$root/offload-test-suite/build/install/bin" "$root/offload-test-suite/build/install/share/hlsl-test-suite/test"
for t in offloader api-query imgdiff; do
    printf '#!/bin/sh\n' >"$root/offload-test-suite/build/install/bin/$t"
    chmod +x "$root/offload-test-suite/build/install/bin/$t"
done
: >"$root/offload-test-suite/build/install/share/hlsl-test-suite/test/lit.cfg.py"

check() {
    if [ "$2" = "$3" ]; then
        echo "ok   $1"
    else
        echo "FAIL $1: got [$2] want [$3]"
        fail=1
    fi
}
fail=0
check "targets: llvm" "$(hd_install_targets llvm)" "install-distribution install-offload-tools install-offload-test-suite"
check "targets: offload" "$(hd_install_targets offload)" "install-offload-tools install-offload-test-suite"

hd_stage_prefix "$root/offload-test-suite" "$root/staged" >/dev/null 2>&1
check "staged: the suite's own tools" "$([ -x "$root/staged/bin/offloader" ] && echo yes)" "yes"
check "staged: the compiler from the distribution" "$([ -x "$root/staged/bin/clang-dxc" ] && echo yes)" "yes"
check "staged: lit's tooling too" "$([ -x "$root/staged/bin/FileCheck" ] && echo yes)" "yes"
check "staged: the resource headers" "$([ -f "$root/staged/lib/clang/23/include/hlsl.h" ] && echo yes)" "yes"
check "staged: the tests" "$([ -f "$root/staged/share/hlsl-test-suite/test/lit.cfg.py" ] && echo yes)" "yes"
check "staged: not LLVM's build-time libraries" "$([ -e "$root/staged/lib/libLLVMSupport.a" ] && echo left)" ""

# Windows: no symlinks may survive into the archive. The cross build tree is a
# different directory, so the fake install has to exist there too.
# A cross platform has its own build tree *and* its own distribution, so both
# fakes have to exist under their platform names.
cp -a "$root/offload-test-suite/build" "$root/offload-test-suite/build.windows-x64"
cp -a "$root/llvm-project/build-dist" "$root/llvm-project/build-dist.windows-x64"
HD_OPT_PLATFORM=windows-x64
hd_stage_prefix "$root/offload-test-suite" "$root/staged-win" >/dev/null 2>&1
check "staged: symlinks materialised for windows" "$(find "$root/staged-win" -type l | wc -l)" "0"
check "staged: and the alias is a real file" \
    "$([ -f "$root/staged-win/bin/clang++" ] && [ ! -L "$root/staged-win/bin/clang++" ] && echo yes)" "yes"
HD_OPT_PLATFORM=""

# --- what `hlsl-clean --platform all` walks ---------------------------------
# The task resolves 'all' into this list before hd_init sees it, so the names
# have to keep matching what the build directories are called.
check "clean: every platform, native first" "native $(HLSL_HOST_PLATFORM=linux-x64 hd_platforms)" \
    "native linux-arm64 windows-x64 windows-arm64"
for p in native $(HLSL_HOST_PLATFORM=linux-x64 hd_platforms); do
    HD_OPT_PLATFORM=$p
    case "$(hd_build_dir "$root/offload-test-suite")" in
    "$root/offload-test-suite/build") [ "$p" = native ] || fail=1 ;;
    "$root/offload-test-suite/build.$p") ;;
    *) echo "FAIL clean: $p resolves to $(hd_build_dir "$root/offload-test-suite")"; fail=1 ;;
    esac
done
HD_OPT_PLATFORM=""
check "clean: each platform has its own build tree" "$fail" "0"

exit "$fail"
