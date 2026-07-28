# HLSL Developer Environment

This repository provides a self-contained, Nix-powered developer environment for working on LLVM's HLSL features, Microsoft's DirectXShaderCompiler (DXC), and related test suites. It utilizes git submodules (with shallow cloning) and a `maskfile.md` for task automation.

## What is Nix?

Nix is a powerful package manager and build system. In this project, we use it (via `flake.nix`) to provide a perfectly reproducible development environment. When you run `nix develop`, Nix automatically downloads and configures exact versions of all necessary build tools and dependencies (like CMake, Ninja, Python, and specific C++ toolchains) without polluting your host operating system. This ensures that every developer has the exact same environment, eliminating "works on my machine" issues.

## Quickstart

1.  **Enter the Nix Shell:**
    ```bash
    nix develop
    ```

2.  **Initialize Submodules:**
    We use `mask` as our task runner. Let's pull down a shallow clone of the dependencies to save time and disk space. This command automatically uses the `--recursive` flag to ensure that `DirectXShaderCompiler`'s own nested submodules (like `SPIRV-Tools` and `DirectX-Headers`) are fully checked out:
    ```bash
    mask setup
    ```

3.  **Configure and Build the Projects:**
    Once cloned, use the included tasks to configure and build the compilers:
    ```bash
    mask configure-llvm
    mask build-llvm
    
    mask configure-dxc
    mask build-dxc
    ```

## Running the Vulkan Offload Tests

The `check-hlsl-vk` and `check-hlsl-clang-vk` suites compile HLSL to SPIR-V and
then *execute* it, so they need a real Vulkan driver (an "ICD"). The dev shell
picks one for you.

### Why the shell pins a driver

The Vulkan loader loads **every** ICD manifest it can find and calls into each
one from `vkEnumeratePhysicalDevices`, so a single broken driver takes down the
whole process. Under WSL this happens by default: Mesa's `dzn`
(Vulkan-on-D3D12) driver is installed, fails to create a D3D12 device, and then
segfaults during enumeration — every test dies before it starts.

This cannot be fixed from `offloader`. Its `-adapter-regex` flag (and lit's
`OFFLOADTEST_GPU_NAME`, which forwards to it) filters the device list *after*
enumeration, i.e. after the crash. The only effective lever is the loader's
`VK_DRIVER_FILES`, which restricts it to an explicit set of manifests.

So the dev shell defaults to **lavapipe**, Mesa's CPU rasterizer: slow, but it
works everywhere and gives reproducible results. `offload-test-suite`'s
`lit.cfg.py` already forwards `VK_DRIVER_FILES` into the test environment, so the
setting reaches `offloader` without any test-suite changes.

### Choosing a driver

```bash
mask vk-info              # what am I running against right now?
mask vk-list              # what can I choose?
mask vk-use system        # switch to the real GPU
mask vk-use lavapipe      # switch back to the CPU rasterizer
```

`mask vk-use` records your choice in `.env` (gitignored). direnv watches that
file, so the change applies on your next prompt — no manual reload. `.env` is
loaded by `.envrc`, so it is a direnv-only convenience; if you use plain
`nix develop`, pass the variable explicitly instead:

```bash
HLSL_VK_DRIVER=system nix develop
```

Accepted values are `system` (let the loader discover drivers itself — use this
on a machine with a working native driver), `lavapipe`, any Mesa ICD short name
from `mask vk-list` (`radeon`, `intel`, `dzn`, …), or an absolute path to an ICD
manifest. All of it is just a wrapper around the `HLSL_VK_DRIVER` environment
variable.

For a one-off run you can bypass the shell setting entirely, since lit forwards
the loader's own variable:

```bash
VK_DRIVER_FILES=/path/to/some_icd.x86_64.json mask build-llvm check-hlsl-vk
```

### Running the suites

```bash
mask build-llvm check-hlsl-vk          # DXC on Vulkan
mask build-llvm check-hlsl-clang-vk    # Clang on Vulkan

# A single test
./llvm-project/build/bin/llvm-lit -v \
    ./llvm-project/build/tools/OffloadTest/test/vk/Feature/HLSLLib/log2.32.test
```

> **Note:** lavapipe is a software rasterizer and is not fully conformant. It is
> considerably slower than a GPU, and a test that fails *only* under lavapipe is
> more likely to be a driver limitation than a compiler bug — confirm on real
> hardware before filing an issue.

## Managing Submodules

By default, submodules are cloned with a depth of 2 (`shallow = true` in `.gitmodules`). This is enough for local testing, but it can be restrictive when preparing Pull Requests or checking out old branches.

### Updating to Latest Upstream

To easily update all submodules to the latest commits on their respective default remote branches (e.g., `main` or `master`), run:

```bash
mask update-submodules
```

### Fetching Full History

To fetch the full commit history of a submodule, use the `fetch-history` task:

```bash
# Example: Fetching history for LLVM
mask fetch-history llvm-project

# Example: Fetching history for DXC
mask fetch-history DirectXShaderCompiler
```

### Truncating History

If you previously fetched the full history and now want to free up some disk space by truncating it back to a shallow depth (depth 2), run:

```bash
mask truncate-history llvm-project
```

## Adding / Fixing Submodule URLs

If the placeholder URLs for `offload-test-suite` or `offload-golden-images` in `.gitmodules` are incorrect, edit the `.gitmodules` file with the correct repository URLs, then run `git submodule sync` and `mask setup`.
