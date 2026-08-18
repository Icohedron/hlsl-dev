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
    Once cloned, use the included tasks to configure and build the compilers.
    The tasks act on *the checkout you are standing in*, so `cd` into it first:
    ```bash
    cd llvm-project && mask configure && mask build
    cd ../DirectXShaderCompiler && mask configure && mask build
    ```
    Or drive them from anywhere with `--in`:
    ```bash
    mask build --in llvm-project clang
    mask build --in DirectXShaderCompiler
    ```

## Worktrees

Every task that touches a checkout works the same way in a `wt` worktree as it
does in the submodule itself. There is no per-worktree setup: `mask` finds the
workspace `maskfile.md` from anywhere inside the dev shell, and the tasks work
out what you mean from the current directory.

```bash
cd llvm-project && wt switch --create texture-store   # -> llvm-project.texture-store
mask build                 # builds llvm-project.texture-store/build
mask lit clang/test/CodeGenHLSL/RootSignature
```

Build artifacts always live *inside* the worktree they belong to
(`<worktree>/build`, `<worktree>/build-dist`), so two agents working in two
worktrees never share a build directory, and `wt remove` takes the artifacts
with it. Concurrent `mask` invocations that would write to the *same* build
directory are serialised with a lock rather than corrupting it, and all
worktrees share one sccache instance, so the second build of the same upstream
sources is mostly cache hits.

### Seeing what is where

```bash
mask ls        # every worktree, its branch, whether it is built, its pins
mask info      # what the current directory resolves to, and against what
```

### Building across worktrees

A checkout rarely builds alone: an `offload-test-suite` build needs an
`llvm-project` worktree, and running its suites needs a `dxc`. Every task takes
the same dependency flags:

```bash
mask configure --llvm llvm-project.texture-store     # build against that LLVM
mask test clang-vk --dxc DirectXShaderCompiler.my-fix # run against that DXC
mask build --in offload-test-suite.my-feature --llvm ../llvm-project.texture-store
```

A dependency is resolved in this order, first match wins:

1. `--llvm` / `--dxc` / `--offload` / `--golden` on the command line
2. `$HLSL_LLVM` / `$HLSL_DXC` / `$HLSL_OFFLOAD` / `$HLSL_GOLDEN` in the
   environment (handy for an agent that wants one setting for a whole session)
3. a pin recorded by `mask link`, or by the last successful `mask configure`
4. a worktree of that repository checked out on the **same branch name**
5. the submodule checkout in the workspace root

Step 3 is what makes the common case terse — configure once with the flags, and
every later `mask build` / `mask test` in that worktree keeps using them:

```bash
cd offload-test-suite.my-feature
mask link --llvm texture-store --dxc DirectXShaderCompiler
mask build && mask test clang-vk
```

Pins live in `.hlsl-dev/pins/` at the workspace root, never inside the
checkouts, so `git status` in a worktree stays clean. `mask unlink` forgets
them.

A worktree spec can be a path, a directory name
(`llvm-project.texture-store`), just the suffix (`texture-store`), or a branch
name. `--dxc` additionally accepts a directory containing `dxc`/`dxv`, or `nix`
for the compiler that ships with the dev shell.

### Code intelligence (CodeGraph)

[CodeGraph](https://github.com/colbymchenry/codegraph) gives agents a symbol
graph of a checkout — callers, callees, impact radius — instead of grepping
2 GB of source. One index per worktree, for `llvm-project`,
`DirectXShaderCompiler` and `offload-test-suite` alike:

```bash
mask codegraph                                # index/refresh the current worktree
mask codegraph --in llvm-project.my-feature   # ... or a named one
mask codegraph --all                          # every worktree of every repository
mask codegraph --fresh                        # rebuild from scratch
```

Each repository has its own scope, in `scripts/codegraph-<kind>.json`:

| repository | indexed | left out | files |
| --- | --- | --- | --- |
| llvm-project | `clang/`, `llvm/` | the `test/` corpora, docs, bindings, the other 20 subprojects | ~11,400 of 116k |
| DirectXShaderCompiler | `lib/`, `include/`, `tools/`, `utils/`, `projects/`, `unittests/` | `test/`, `tools/clang/test/`, `external/`, docs, CI | ~3,700 of 18k |
| offload-test-suite | everything | `third-party/`, docs, CI | ~90 of 1.7k |

**Sharing between worktrees.** The index stores project-root-*relative* paths
and records no absolute root, so it is portable between checkouts of the same
repository: a worktree that has no index yet is seeded from a copy of another
worktree's database and then re-parses only what its branch changed — seconds
instead of the ~4 minutes a cold index takes. It is *not* shareable in place
(no symlinking `.codegraph/` between worktrees): the database is a live SQLite
WAL file that the indexer and its background daemon rewrite to match the tree
it sits in, so two worktrees on different branches would thrash it and fight
over the daemon lock.

**Nothing lands in git.** `.codegraph/` and `codegraph.json` are added to the
clone's `info/exclude`, which is shared by every worktree of the submodule and
is never committed. The one file CodeGraph forces us to touch is the tracked
`.gitignore`: its built-in ignore list drops every directory named `target` or
`coverage` case-insensitively — which would silently hide all of
`llvm/lib/Target` (the DirectX and SPIR-V backends included) and DXC's
`include/llvm/Target` — and only a root `.gitignore` negation overrides a
built-in default. `mask codegraph` appends a
marked block for that and marks the file `skip-worktree`, so it stays out of
`git status` and out of commits. The offload test suite has no such directory,
so its `.gitignore` is never touched.

The only time you have to think about that hidden edit is when a git operation
wants to change `.gitignore` itself — a `checkout`, `rebase` or `pull` will
then stop with `Your local changes to the following files would be overwritten
by checkout: .gitignore` (or `Entry '.gitignore' not uptodate. Cannot merge.`).
That is what `mask codegraph --restore-gitignore` is for: it restores the file to its
committed state and unhides it, so the git operation goes through. Re-run `mask
codegraph` afterwards to put the block back.

```bash
mask codegraph --restore-gitignore
git rebase origin/main
mask codegraph
```

## Building the offload test suite standalone

Building LLVM with the offload test suite as an external project gives you
`check-hlsl-*` out of one tree, but it also means every offload change costs an
LLVM-sized build directory. The suite's *standalone* mode
(`offload-test-suite/docs/offload-distribution.md`) splits that in two: LLVM is
built and installed once, and the test suite is then a small top-level CMake
project that links against it — a ~20 second configure and a ~2 minute build,
per worktree.

```bash
# Once per llvm-project worktree: install the distribution
# (clang, lit tooling, the LLVM libraries the offload tools link against).
mask dist --in llvm-project          # -> llvm-project/build-dist/install

# Then, in as many offload worktrees as you like:
cd offload-test-suite.my-feature
mask configure --llvm llvm-project   # standalone is the default mode
mask build
mask test clang-vk
```

One distribution serves every offload worktree pointed at that llvm worktree.
After changing Clang, `mask dist` again (it is incremental) and the offload
builds pick the new toolchain up.

To test a Clang *and* an offload change together, point the offload worktree at
the llvm worktree that has the Clang change:

```bash
mask dist --in llvm-project.my-clang-fix
cd offload-test-suite.my-feature && mask configure --llvm my-clang-fix && mask test clang-d3d12
```

If you already have a distribution built elsewhere — a shared one, or an
unpacked CI artifact — point at it instead of building one:

```bash
mask configure --dist-prefix /path/to/llvm-prefix
```

The integrated layout is still available for an offload worktree when you want
to exercise the in-tree build:

```bash
mask configure --mode integrated --llvm llvm-project.texture-store
```

In that mode the offload worktree has no build directory of its own: the llvm
worktree's build tree is configured to pull it in as `OffloadTest`, and
`mask build` / `mask test` operate there. Only one offload worktree can occupy
an llvm build tree at a time, which is why standalone is the default.

## Running tests

```bash
mask test                       # the whole check-hlsl umbrella
mask test clang-vk              # one suite, through its ninja target
mask test clang-vk Feature/HLSLLib          # a subdirectory
mask test clang-vk Feature/HLSLLib/log2.32.test
mask test clang-vk 'log2.*'     # anything that is not a path becomes a lit --filter

mask lit clang/test/CodeGenHLSL/some_test.hlsl   # any lit test, from an llvm worktree
mask build check-clang                            # or the usual ninja targets
```

Extra lit arguments go through `--lit-args`; use `=` when the value itself
starts with a dash: `mask test clang-vk Feature --lit-args=--time-tests`.

Switching DXC does not require a rebuild — `DXC_DIR` only feeds the lit
configuration, so `mask test <suite> --dxc <worktree>` regenerates the build
tree in seconds and compiles nothing.

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
VK_DRIVER_FILES=/path/to/some_icd.x86_64.json mask test vk
```

### Running the suites

```bash
mask test vk                # DXC on Vulkan
mask test clang-vk          # Clang on Vulkan

# A single test
mask test clang-vk Feature/HLSLLib/log2.32.test
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

## How the tasks are put together

| Where | What |
|---|---|
| `maskfile.md` | the task surface: `ls`, `info`, `link`, `configure`, `build`, `dist`, `test`, `lit`, `clean`, … |
| `scripts/hlsl-dev.sh` | worktree detection, dependency resolution, pins, locks, and the CMake invocations |
| `flake.nix` | the dev shell, and the CMake flag *templates* for each build flavour |

The flag lists in `flake.nix` are templates: placeholders such as
`$HD_LLVM_SRC`, `$HD_DXC_BIN_DIR` or `$HD_OFFLOAD_SRC` are left unexpanded in
the environment and filled in per invocation, once the tasks have resolved
which worktrees a command applies to. That is what lets one flag list serve
every worktree of a repository instead of a single hard-coded checkout — tune
build options in `flake.nix`, and every worktree picks them up on its next
configure.

`mask` itself is wrapped in the dev shell so that it finds this `maskfile.md`
from any directory (a `maskfile.md` in the current directory, or an explicit
`--maskfile`, still wins).

### Useful environment variables

| Variable | Effect |
|---|---|
| `HLSL_WT` | act on this worktree, as if `--in` had been passed |
| `HLSL_LLVM`, `HLSL_DXC`, `HLSL_OFFLOAD`, `HLSL_GOLDEN` | default dependencies for this shell |
| `HLSL_MODE` | `standalone` or `integrated` for offload worktrees |
| `HLSL_BUILD_TYPE`, `HLSL_BUILD_DIR` | build type / build directory |
| `HLSL_DIST_PREFIX` | an already-installed LLVM distribution to build against |
| `HLSL_VK_DRIVER` | Vulkan ICD selection (see above) |

They are the same knobs as the flags, which makes them convenient for an agent
that wants one setting to apply to a whole session:

```bash
export HLSL_LLVM=llvm-project.texture-store
mask build && mask test clang-vk        # both use that LLVM
```
