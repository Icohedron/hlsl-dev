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
