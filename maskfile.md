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
cd "$repo" && git fetch --depth 2
```

## update-submodules
Updates all submodules to the latest commits on their respective default remote branches (e.g., main or master).

```bash
git submodule update --remote --depth 2
git submodule update --init --recursive --depth 2
```
