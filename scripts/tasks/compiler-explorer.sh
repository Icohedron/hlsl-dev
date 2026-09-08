#!/usr/bin/env bash
# summary: Run Compiler Explorer against the locally built compilers
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_DESC="Runs Compiler Explorer with the local DXC, clang and clang-dxc, taken from the
resolved worktrees (--llvm / --dxc, defaulting to the submodules in the
workspace root). Listens on port 10240, and runs in the foreground until you
stop it."
HD_TASK_OPTS="llvm= dxc="
hd_parse "$@"
hd_init

[ -f "$HD_ROOT/compiler-explorer/Makefile" ] ||
    hd_die "the compiler-explorer checkout is empty; run 'hlsl-setup' first"
llvm_wt=$(hd_dep llvm "")
llvm_bin="$(hd_build_dir "$llvm_wt")/bin"
dxc_bin=$(hd_dxc_bin_dir "")

HLSL_LOCAL="$HD_ROOT/compiler-explorer/etc/config/hlsl.local.properties"

cat > "$HLSL_LOCAL" <<EOF
compilers=&dxc:&clang

defaultCompiler=dxc_local

group.dxc.compilers=dxc_local
compiler.dxc_local.exe=$dxc_bin/dxc
compiler.dxc_local.name=DXC ($(basename "$dxc_bin"))

group.clang.compilers=clang_local:clang_dxc_local
group.clang.compilerType=clang-dxc

compiler.clang_local.exe=$llvm_bin/clang
compiler.clang_local.name=Clang ($(basename "$llvm_wt"))

compiler.clang_dxc_local.exe=$llvm_bin/clang-dxc
compiler.clang_dxc_local.name=Clang-DXC ($(basename "$llvm_wt"))
EOF

echo "Generated $HLSL_LOCAL"
cd "$HD_ROOT/compiler-explorer" && make dev EXTRA_ARGS="--language hlsl"
