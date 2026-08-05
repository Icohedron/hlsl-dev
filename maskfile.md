# Tasks

## setup
Initializes the submodules with a shallow clone (`--depth 2`) to save time and disk space.

```bash
git submodule update --init --recursive --depth 2
```

## configure-llvm [build_type]
Configures LLVM using the environment variables set by Nix.

**OPTIONS**
* build_type: Optional CMake build type (e.g., Debug, Release, RelWithDebInfo, MinSizeRel). Defaults to RelWithDebInfo.

```bash
BUILD_TYPE=${build_type:-RelWithDebInfo}
cmake -S ./llvm-project/llvm -B ./llvm-project/build -DCMAKE_BUILD_TYPE=$BUILD_TYPE $LLVMCMakeFlags
```

## build-llvm [target]
Builds LLVM. Automatically configures first if the build directory is missing.

**OPTIONS**
* target: Optional specific target to build (e.g., clang, check-hlsl)

```bash
if [ ! -f "./llvm-project/build/build.ninja" ]; then
    mask configure-llvm
fi

if [ -n "$target" ]; then
    cmake --build ./llvm-project/build --target "$target"
else
    cmake --build ./llvm-project/build
fi
```

## configure-dxc [build_type]
Configures DirectXShaderCompiler using the environment variables set by Nix.

**OPTIONS**
* build_type: Optional CMake build type (e.g., Debug, Release, RelWithDebInfo, MinSizeRel). Defaults to RelWithDebInfo.

```bash
BUILD_TYPE=${build_type:-RelWithDebInfo}
cmake -S ./DirectXShaderCompiler -B ./DirectXShaderCompiler/build -DCMAKE_BUILD_TYPE=$BUILD_TYPE $DXCCMakeFlags
```

## build-dxc [target]
Builds DirectXShaderCompiler. Automatically configures first if the build directory is missing.

**OPTIONS**
* target: Optional specific target to build

```bash
if [ ! -f "./DirectXShaderCompiler/build/build.ninja" ]; then
    mask configure-dxc
fi

if [ -n "$target" ]; then
    cmake --build ./DirectXShaderCompiler/build --target "$target"
else
    cmake --build ./DirectXShaderCompiler/build
fi
```

## vk-info
Shows which Vulkan driver (ICD) the offload test suite will run against, and
which device that driver actually exposes. Inside the dev shell this should
report `llvmpipe` (lavapipe) by default, never `dzn`.

```bash
echo "HLSL_VK_DRIVER = ${HLSL_VK_DRIVER:-lavapipe (default)}"
echo "VK_DRIVER_FILES = ${VK_DRIVER_FILES:-<unset> -> loader discovers drivers itself}"
echo
vulkaninfo --summary 2>/dev/null | sed -n '/^Devices:/,$p' ||
    echo "vulkaninfo not available; enter the Nix dev shell first"
```

## vk-list
Lists the driver names accepted by `mask vk-use`.

```bash
if [ -z "${HLSL_VK_ICD_DIR:-}" ]; then
    echo "Not in the Nix dev shell; run 'nix develop' or 'direnv allow' first" >&2
    exit 1
fi

echo "system      let the Vulkan loader discover drivers itself (real GPU)"
echo "lavapipe    Mesa's CPU rasterizer - slow, but always works (default)"
echo
echo "Mesa ICDs in $HLSL_VK_ICD_DIR:"
find "$HLSL_VK_ICD_DIR" -maxdepth 1 -name '*_icd.*.json' -printf '%f\n' |
    sed -e 's/_icd\..*\.json$//' -e 's/^/    /' | sort
echo
echo "An absolute path to any ICD manifest is also accepted."
```

## vk-use (driver)
Switches the Vulkan driver used by the offload test suite. The choice is written
to `.env` (gitignored), which direnv watches, so the shell picks it up on the
next prompt.

**OPTIONS**
* driver (required): `system`, `lavapipe`, a Mesa ICD short name (see `mask vk-list`), or a path to an ICD manifest

```bash
sed -i '/^HLSL_VK_DRIVER=/d' .env 2>/dev/null || true
echo "HLSL_VK_DRIVER=$driver" >> .env

echo "Set HLSL_VK_DRIVER=$driver in .env"
if [ -n "${DIRENV_DIR:-}" ]; then
    echo "direnv will reload on your next prompt; then run 'mask vk-info' to verify."
else
    echo
    echo "NOTE: .env is loaded by .envrc, so it only applies under direnv."
    echo "Without direnv, pass the variable explicitly instead:"
    echo "    HLSL_VK_DRIVER=$driver nix develop"
fi
```

## fetch-history (repo)
Fetches the full commit history of a specific submodule for when you need to rebase, branch off older commits, or create pull requests.

**OPTIONS**
* repo (required): Name of the submodule (e.g., llvm-project, DirectXShaderCompiler)

```bash
cd "$repo" && git fetch --unshallow || git fetch --all
```

## truncate-history (repo)
Truncates the commit history of a specific submodule back to a shallow depth of 2 to save disk space after you are done needing the full history.

**OPTIONS**
* repo (required): Name of the submodule (e.g., llvm-project, DirectXShaderCompiler)

```bash
cd "$repo" && git fetch --depth 2 && git reflog expire --expire=now --all && git gc --prune=now
```

## update-submodules
Updates all submodules to the latest commits on their respective default remote branches (e.g., main or master).

Submodules that already have their full history (e.g. after `mask fetch-history`) are
updated with a full fetch so the history is preserved; only shallow or not-yet-cloned
submodules are fetched with `--depth 2`.

```bash
set -e

paths=$(git config -f .gitmodules --get-regexp '^submodule\..*\.path$' | cut -d' ' -f2-)

for path in $paths; do
    if [ -e "$path/.git" ] &&
       [ "$(git -C "$path" rev-parse --is-shallow-repository 2>/dev/null)" = "false" ]; then
        depth=""
        echo "==> $path: full history detected, updating without truncating"
    else
        depth="--depth 2"
        echo "==> $path: shallow, updating with --depth 2"
    fi

    git submodule update --init --recursive $depth -- "$path"
    git submodule update --remote --recursive $depth -- "$path"
done
```

## compiler-explorer
Runs Compiler Explorer with local DXC, clang, and clang-dxc compilers configured from the build directories.

```bash
HLSL_LOCAL="./compiler-explorer/etc/config/hlsl.local.properties"

cat > "$HLSL_LOCAL" <<EOF
compilers=&dxc:&clang

defaultCompiler=dxc_local

group.dxc.compilers=dxc_local
compiler.dxc_local.exe=$PWD/DirectXShaderCompiler/build/bin/dxc
compiler.dxc_local.name=DXC (local)

group.clang.compilers=clang_local:clang_dxc_local
group.clang.compilerType=clang-dxc

compiler.clang_local.exe=$PWD/llvm-project/build/bin/clang
compiler.clang_local.name=Clang (local)

compiler.clang_dxc_local.exe=$PWD/llvm-project/build/bin/clang-dxc
compiler.clang_dxc_local.name=Clang-DXC (local)
EOF

echo "Generated $HLSL_LOCAL"
cd compiler-explorer && make dev EXTRA_ARGS="--language hlsl"
```
