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

# --- the self-contained package ---------------------------------------------
# What turns the staged prefix into something that runs elsewhere with no
# setup: DXC beside the compiler, and a lit configuration whose every path is
# relative to itself.

# a dxc build tree, with the symlink a real one has (bin/dxc -> dxc-3.7)
mkdir -p "$root/DirectXShaderCompiler/build/bin" "$root/DirectXShaderCompiler/build/lib"
mkdir -p "$root/DirectXShaderCompiler/tools/clang"
printf '#!/bin/sh\n' >"$root/DirectXShaderCompiler/build/bin/dxc-3.7"
chmod +x "$root/DirectXShaderCompiler/build/bin/dxc-3.7"
ln -sf dxc-3.7 "$root/DirectXShaderCompiler/build/bin/dxc"
printf '#!/bin/sh\n' >"$root/DirectXShaderCompiler/build/bin/dxv"
chmod +x "$root/DirectXShaderCompiler/build/bin/dxv"
: >"$root/DirectXShaderCompiler/build/lib/libdxcompiler.so"

hd_stage_dxc "$root/staged/dxc" "$root/DirectXShaderCompiler/build/bin" >/dev/null 2>&1
check "dxc: dxc and dxv are in bin/" \
    "$([ -x "$root/staged/dxc/bin/dxc" ] && [ -x "$root/staged/dxc/bin/dxv" ] && echo yes)" "yes"
check "dxc: the versioned symlink became a real dxc" \
    "$([ -L "$root/staged/dxc/bin/dxc" ] && echo link || echo file)" "file"
check "dxc: the library came too" \
    "$([ -f "$root/staged/dxc/lib/libdxcompiler.so" ] && echo yes)" "yes"

# A suite as CMake configures one, with this machine's paths in it.
suitedir="$root/offload-test-suite/build/test/clang-vk"
mkdir -p "$suitedir" "$root/offload-test-suite/build/test/Unit" \
    "$root/offload-test-suite/build/test/clang-vk-lavapipe" "$root/offload-golden-images/hlsl"
: >"$root/offload-golden-images/README.md"
: >"$root/offload-test-suite/build/test/Unit/lit.site.cfg.py"
: >"$root/offload-test-suite/build/test/clang-vk-lavapipe/lit.site.cfg.py"
cat >"$suitedir/lit.site.cfg.py" <<EOF
import os
import platform
def path(p):
    if not p: return ''
    return os.path.realpath(os.path.join(os.path.dirname(__file__), p))

config.offloadtest_obj_root = path(r"../..")
config.offloadtest_src_root = path(r"$root/offload-test-suite")
config.offloadtest_tools_dir = lit_config.substitute(path(r"$root/offload-test-suite/build/./bin"))
config.llvm_tools_dir = lit_config.substitute(path(r"../../../../llvm-project/build-dist/install/bin"))
config.offloadtest_dxc = '"' + path(r"$root/DirectXShaderCompiler/build/bin/dxc") + '"'
config.offloadtest_dxc_dir = r"$root/DirectXShaderCompiler/build/bin"
config.goldenimage_dir = r"$root/offload-golden-images"
config.offloadtest_suite = "clang-vk"

import lit.llvm
lit.llvm.initialize(lit_config, config)

lit_config.load_config(
    config, os.path.join(config.offloadtest_src_root, "test/lit.cfg.py"))
EOF

HD_OPT_DXC="$root/DirectXShaderCompiler/build/bin"
HD_OPT_GOLDEN="$root/offload-golden-images"
# `if`, because hd_stage_suites gives up with hd_die and that would take the
# whole self-test with it.
if ! staged_suites=$(hd_stage_suites "$root/offload-test-suite" "$root/staged" 2>&1); then
    staged_suites="failed: $staged_suites"
fi
HD_OPT_DXC="" HD_OPT_GOLDEN=""
check "suites: the configured ones, without Unit or the lavapipe aliases" "$staged_suites" "clang-vk"
cfg="$root/staged/test/clang-vk/lit.site.cfg.py"
check "suites: nothing absolute survives" "$(grep -c 'r"/' "$cfg" || true)" "0"
check "suites: the tools are the package's own" \
    "$(sed -n 's/^config.llvm_tools_dir = .*path(r"\(.*\)").*/\1/p' "$cfg")" "../../bin"
check "suites: so is dxc, under the name the tests call it by" \
    "$(sed -n 's/^config.offloadtest_dxc = .*path(r"\(.*\)").*/\1/p' "$cfg")" "../../dxc/bin/dxc"
check "suites: and the golden images" \
    "$(sed -n 's/^config.goldenimage_dir = path(r"\(.*\)")/\1/p' "$cfg")" \
    "../../share/hlsl-test-suite/golden-images"
check "suites: the test sources are the installed ones" \
    "$(sed -n 's/^config.offloadtest_src_root = path(r"\(.*\)")/\1/p' "$cfg")" \
    "../../share/hlsl-test-suite"
# The exec root lit computes is <obj root>/test/<suite>, so the config has to
# sit exactly there for the paths on the command line to resolve.
check "suites: the object root is the package root" \
    "$(sed -n 's/^config.offloadtest_obj_root = path(r"\(.*\)")/\1/p' "$cfg")" "../.."
# The suite asks for a per-test timeout in a way that this lit no longer acts
# on (it passes None for "--timeout not given", not 0), so the packaged config
# sets the per-suite knob instead. Without this a hung GPU test hangs the run.
# A package whose dxc/ is missing (or not filled in yet) should use a DXC from
# PATH rather than none: without dxv there is no signature, and D3D12 refuses
# every shader.
check "suites: a DXC on PATH is the fallback" \
    "$(grep -c 'shutil as _shutil' "$cfg")" "1"
check "suites: the per-test timeout is set where lit reads it" \
    "$(grep -c '^ *config.maxIndividualTestTime = 300' "$cfg")" "1"
# ... and the assignment lit.cfg.py itself makes is neutralised, because this
# lit raises on it -- which is every --timeout=0 run, traceback and all.
check "suites: lit.cfg.py's own assignment cannot abort the run" \
    "$(python3 "$here/lit-config-probe.py" "$cfg")" "survived"

# A path the package does not contain is a broken package, and has to fail
# here rather than on the machine that unpacks it.
sed -i "s|config.goldenimage_dir = .*|config.goldenimage_dir = r\"$root/elsewhere\"|" "$suitedir/lit.site.cfg.py"
if python3 "$HD_LIB_DIR/package/relocate-lit-config.py" --in "$suitedir/lit.site.cfg.py" \
    --out "$root/staged/test/clang-vk/lit.site.cfg.py" --root "$root/staged" \
    --map "$root/offload-test-suite=share/hlsl-test-suite" >/dev/null 2>&1; then
    check "suites: a path outside the package is refused" "accepted" "refused"
else
    check "suites: a path outside the package is refused" "refused" "refused"
fi

# lit comes out of the llvm checkout's own sources.
mkdir -p "$root/llvm-project/llvm/utils/lit/lit"
: >"$root/llvm-project/llvm/utils/lit/lit/__init__.py"
: >"$root/llvm-project/llvm/utils/lit/lit/main.py"
hd_stage_python "$root/staged" "$root/llvm-project" >/dev/null 2>&1
check "python: lit is in the package" \
    "$([ -x "$root/staged/bin/lit" ] && [ -f "$root/staged/share/hlsl-test-suite/lit/lit/main.py" ] && echo yes)" "yes"
check "python: and a Windows way to run it" "$([ -f "$root/staged/bin/lit.cmd" ] && echo yes)" "yes"
check "python: yaml travels with it" \
    "$([ -f "$root/staged/share/hlsl-test-suite/python/yaml/__init__.py" ] && echo yes)" "yes"
# psutil is deliberately *not* bundled: the real one is a C extension that
# would not run on the machine the package is for, and a stand-in for it was
# tried and reverted. Without it lit runs without a per-test timeout.
check "python: psutil is not bundled" \
    "$([ -e "$root/staged/share/hlsl-test-suite/python/psutil.py" ] && echo shipped)" ""
check "python: the bundled lit is not on the config's sys.path" \
    "$([ -e "$root/staged/share/hlsl-test-suite/python/lit" ] && echo yes)" ""

exit "$fail"
