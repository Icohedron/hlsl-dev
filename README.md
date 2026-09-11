# HLSL Developer Environment

This repository provides a self-contained, Nix-powered developer environment
for working on LLVM's HLSL features, Microsoft's DirectXShaderCompiler (DXC),
and related test suites. It uses git submodules (with shallow cloning) and
[devenv](https://devenv.sh) for the environment and its task automation.

## What is devenv?

devenv is a thin, declarative layer over Nix. `devenv.nix` describes the whole
workspace in one file — the exact versions of CMake, Ninja, Python, the C++
toolchain, the Vulkan loader and everything else; the environment variables the
builds need; the tasks (`hlsl-build`, `hlsl-test`, …); and the dev container.
Nothing is installed into your host system, and every developer gets the same
environment, which eliminates "works on my machine" problems.

Two commands are enough to get it:

```bash
devenv shell     # enter the environment for one shell
devenv info      # what it provides: packages, tasks, environment variables
```

If you want the environment to activate automatically whenever you `cd` into
the workspace, use direnv: the workspace ships an `.envrc` (`use devenv`), and
that is what the dev container uses too.

```bash
eval "$(direnv hook bash)"   # in ~/.bashrc, then `direnv allow` once
```

devenv has a hook of its own (`devenv hook bash`), but it changes the shell's
working directory to the project root on activation
([devenv#3041](https://github.com/cachix/devenv/issues/3041)), so direnv is
less annoying to use.

## Quickstart

1.  **Enter the environment:**
    ```bash
    devenv shell
    ```

2.  **Initialize submodules:**
    A shallow clone of the dependencies, to save time and disk space. It is
    recursive, so `DirectXShaderCompiler`'s own nested submodules (`SPIRV-Tools`,
    `DirectX-Headers`, …) come along:
    ```bash
    hlsl-setup
    ```

3.  **Build something:**
    The tasks act on *the checkout you are standing in*, so `cd` into it first:
    ```bash
    cd llvm-project && hlsl-build clang
    cd ../DirectXShaderCompiler && hlsl-build
    ```
    Or drive them from anywhere with `--in`:
    ```bash
    hlsl-build --in llvm-project clang
    hlsl-test --in offload-test-suite clang-vk
    ```
    There is no separate configure step to remember, and nothing to install
    first: a task configures what it is about to build, and builds what it is
    about to link against. `--dry-run` shows what that would be, `--no-auto`
    refuses to do it.

`hlsl` on its own lists every task; `hlsl <task>` is the same as `hlsl-<task>`,
and `hlsl-<task> --help` explains one.

### In a container

`devenv.nix` also generates `.devcontainer/devcontainer.json`, which describes
this same environment as a dev container. Open the folder in an editor that
supports dev containers, or run `devcontainer up --workspace-folder .`, then
carry on from `hlsl-setup` as above.

The image ships Nix and devenv; the toolchain still comes from `devenv.nix`, so
what you build inside the container is what you build outside it. Creation runs
`devenv test`, which realises the environment (several GB, tens of minutes on a
cold cache) and smoke-tests it — on GitHub Codespaces that happens during a
prebuild. The devenv shell hook is installed into the container's shells, so
every terminal, and the shell an editor runs to capture the project
environment, gets the toolchain; that is how clangd and the Python tooling see
it rather than the bare image.

The container has no GPU, so Vulkan defaults to lavapipe (see
[Running the Vulkan offload tests](#running-the-vulkan-offload-tests)). To use
a real driver, pass the render node through with `"runArgs": ["--device=/dev/dri"]`
in `devcontainer.settings` in `devenv.nix`, and `hlsl-vk system` inside.

Never edit `.devcontainer/devcontainer.json` by hand: it is generated, and
devenv rewrites it on the next shell entry. Change `devcontainer.settings` in
`devenv.nix` instead, and commit the regenerated file.

## Worktrees

Every task that touches a checkout works the same way in a `wt` worktree as it
does in the submodule itself. There is no per-worktree setup: the tasks know
the workspace root from devenv, and work out what you mean from the current
directory.

```bash
cd llvm-project && wt switch --create texture-store   # -> llvm-project.texture-store
hlsl-build                 # builds llvm-project.texture-store/build
hlsl-lit clang/test/CodeGenHLSL/RootSignature
```

Use the same branch name in two repositories and they pair up on their own —
see [Building across worktrees](#building-across-worktrees).

Build artifacts always live *inside* the worktree they belong to
(`<worktree>/build`, `<worktree>/build-dist`), so two agents working in two
worktrees never share a build directory, and `wt remove` takes the artifacts
with it. Concurrent invocations that would write to the *same* build directory
are serialised with a lock rather than corrupting it, and all worktrees share
one sccache instance, so the second build of the same upstream sources is
mostly cache hits.

### Seeing what is where

```
$ hlsl-ls
  worktree                          branch      build         against

llvm-project
  llvm-project                      (detached)  not built
* llvm-project.texture-store        main        built + dist

offload-test-suite
  offload-test-suite                (detached)  not built
  offload-test-suite.texture-store  main        built         llvm: llvm-project.texture-store

* the checkout this directory belongs to; "hlsl-info" explains one in full
```

`*` is the checkout the current directory belongs to — the one a bare
`hlsl-build` acts on. `(detached)` is normal for a submodule: it sits at the
commit the workspace records. `+ dist` means an LLVM distribution is installed
beside that build, which is what standalone offload builds link against. The
`against` column is what a checkout *remembers*; blank means it works it out
each time, starting with the same branch name. `hlsl-ls --help` spells all of
that out, and `hlsl-info` answers it in full for one checkout:

```bash
hlsl-info      # what the current directory resolves to, and against what
```

### Building across worktrees

A checkout rarely builds alone: an `offload-test-suite` build needs an
`llvm-project` worktree, and running its suites needs a `dxc`.

**The easy way is to name the worktrees after the branch.** Checkouts on the
same branch name find each other, so a feature that spans two repositories
needs no flags and no configuration at all:

```bash
cd llvm-project           && wt switch --create texture-store
cd ../offload-test-suite  && wt switch --create texture-store

cd ../offload-test-suite.texture-store
hlsl-test clang-vk        # uses llvm-project.texture-store, builds what it needs
```

When they are *not* named alike, say so once — the choice is remembered for
every later command in that worktree:

```bash
cd offload-test-suite.my-feature
hlsl-configure --llvm texture-store --dxc DirectXShaderCompiler.my-fix
hlsl-build && hlsl-test clang-vk        # both keep using them
```

A dependency is resolved in this order, first match wins:

1. `--llvm` / `--dxc` on the command line
2. `$HLSL_LLVM` / `$HLSL_DXC` / `$HLSL_OFFLOAD` / `$HLSL_GOLDEN` in the
   environment (handy for an agent that wants one setting for a whole session)
3. what the last successful `hlsl-configure` in that worktree resolved to
4. a worktree of that repository checked out on the **same branch name**
5. the submodule checkout in the workspace root

Those memories live in `.hlsl-dev/pins/` at the workspace root, never inside
the checkouts, so `git status` in a worktree stays clean.
`hlsl-configure --forget` drops them, and `hlsl-info` shows what a directory
currently resolves to. A pin that no longer resolves is a warning, not an
error: resolution carries on at rule 4.

A worktree spec can be a path, a directory name
(`llvm-project.texture-store`), just the suffix (`texture-store`), or a branch
name. `--dxc` additionally accepts a directory containing `dxc`/`dxv`, or `nix`
for the compiler that ships with the environment.

### clangd

Every build exports a compilation database (`CMAKE_EXPORT_COMPILE_COMMANDS`),
and a configure links it into the root of the worktree:

```
llvm-project.texture1d/compile_commands.json -> build-container/compile_commands.json
```

clangd looks for that file beside the file being edited, in the parent
directories, and in a `build/` subdirectory of each — nowhere else. A worktree
built in the dev container keeps its database in `build-container/`
(`$HLSL_BUILD_DIR_NAME`), which none of those places is, so an editor opened on
the host finds nothing and clangd runs with no flags: no includes, no
navigation, errors everywhere. The symlink is the one path both sides find,
whatever the build directory is called, and it is in the checkout's
`info/exclude`, so `git status` stays clean.

It follows the build tree most recently configured or built. When the
environment you are in has no build directory of its own it falls back to
whatever database the worktree does have — which is what heals a worktree the
*other* side configured — and `hlsl-clean` takes the link away with the build
tree rather than leaving clangd chasing a database that is gone. `hlsl-info`
prints where it points, and a real `compile_commands.json` you put there
yourself is left alone.

### Code intelligence (CodeGraph)

[CodeGraph](https://github.com/colbymchenry/codegraph) gives agents a symbol
graph of a checkout — callers, callees, impact radius — instead of grepping
2 GB of source. One index per worktree, for `llvm-project`,
`DirectXShaderCompiler` and `offload-test-suite` alike:

```bash
hlsl-codegraph                                # index/refresh the current worktree
hlsl-codegraph --in llvm-project.my-feature   # ... or a named one
hlsl-codegraph --all                          # every worktree of every repository
hlsl-codegraph --fresh                        # rebuild from scratch
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
built-in default. `hlsl-codegraph` appends a
marked block for that and marks the file `skip-worktree`, so it stays out of
`git status` and out of commits. The offload test suite has no such directory,
so its `.gitignore` is never touched.

The only time you have to think about that hidden edit is when a git operation
wants to change `.gitignore` itself — a `checkout`, `rebase` or `pull` will
then stop with `Your local changes to the following files would be overwritten
by checkout: .gitignore` (or `Entry '.gitignore' not uptodate. Cannot merge.`).
That is what `hlsl-codegraph --restore-gitignore` is for: it restores the file
to its committed state and unhides it, so the git operation goes through.
Re-run `hlsl-codegraph` afterwards to put the block back.

```bash
hlsl-codegraph --restore-gitignore
git rebase origin/main
hlsl-codegraph
```

## Building the offload test suite standalone

Building LLVM with the offload test suite as an external project gives you
`check-hlsl-*` out of one tree, but it also means every offload change costs an
LLVM-sized build directory. The suite's *standalone* mode
(`offload-test-suite/docs/offload-distribution.md`) splits that in two: LLVM is
built and installed once, and the test suite is then a small top-level CMake
project that links against it — a ~20 second configure and a ~2 minute build,
per worktree.

This is the default layout, and installing the distribution is not a step you
have to take: an offload build does it if it is not there.

```bash
cd offload-test-suite.my-feature
hlsl-test clang-vk        # installs the LLVM distribution if needed, then runs
```

The first run through says so plainly, because it is the expensive part:

```
==> missing prerequisite: the LLVM distribution for llvm-project -- the expensive one -- building it now
```

Check first if you would rather not find that out afterwards:

```bash
hlsl-test clang-vk --dry-run     # what would be built and configured
hlsl-test clang-vk --no-auto     # or refuse to build prerequisites at all
```

One distribution serves every offload worktree pointed at that llvm worktree.
After changing Clang, refresh it explicitly — it is incremental, and nothing
tries to guess that an install has gone stale:

```bash
hlsl-dist --in llvm-project.my-clang-fix
cd offload-test-suite.my-feature && hlsl-test clang-d3d12
```

If you already have a distribution built elsewhere — a shared one, or an
unpacked CI artifact — point at it instead of building one:

```bash
hlsl-configure --dist-prefix /path/to/llvm-prefix
```

To build the suite *inside* an llvm build tree instead — the in-tree layout,
which is what upstream CI builds — configure that llvm worktree against these
sources and work there:

```bash
hlsl-configure --in llvm-project.texture-store --offload offload-test-suite.my-feature
hlsl-test --in llvm-project.texture-store clang-vk
```

An llvm build tree includes exactly one offload source tree at a time, which is
why standalone is the default.

## Running tests

```bash
hlsl-test                       # the whole check-hlsl umbrella
hlsl-test clang-vk              # one suite, through its ninja target
hlsl-test clang-vk Feature/HLSLLib          # a subdirectory
hlsl-test clang-vk Feature/HLSLLib/log2.32.test
hlsl-test clang-vk 'log2.*'     # anything that is not a path becomes a lit --filter

hlsl-lit clang/test/CodeGenHLSL/some_test.hlsl   # any lit test, from an llvm worktree
hlsl-build check-clang                           # or the usual ninja targets
hlsl-build clang llvm-dis FileCheck              # several at once, in one go
```

Extra lit arguments go through `--lit-args`; use `=` when the value itself
starts with a dash: `hlsl-test clang-vk Feature --lit-args=--time-tests`.

Switching DXC does not require a rebuild — `DXC_DIR` only feeds the lit
configuration, so `hlsl-test <suite> --dxc <worktree>` regenerates the build
tree in seconds and compiles nothing.

### Trimming a build that overreached

Some targets build far more than they sound like they do. `check-llvm` drags in
`llvm-test-depends` — the entire LLVM tool zoo, plus the Kaleidoscope and OrcV2
examples, which `llvm/test/CMakeLists.txt` adds whenever `LLVM_INCLUDE_EXAMPLES`
is on even though `LLVM_BUILD_EXAMPLES` is off. Each of those is a fully linked
copy of LLVM. One stray `hlsl-build check-llvm` is worth about 16 GB.

`hlsl-trim` takes it back, without a reconfigure and without rebuilding
anything you kept:

```bash
hlsl-trim --dry-run     # list what would go, remove nothing
hlsl-trim               # keep what check-hlsl and check-clang need
hlsl-trim check-hlsl    # HLSL only -- the clang unit tests go too
```

It asks Ninja what the targets you name actually depend on (`ninja -t graph`)
and deletes the executables that are not in the answer, leaving objects,
libraries, CMake state and the install prefix alone. Nothing is lost that a
link step cannot make again — the next build asking for one of those targets
relinks it — and only ELF files are considered, so `llvm-lit` and the other
scripts in `bin/` stay put. `hlsl-clean` is still the way to remove a build
directory outright.

Builds are also configured to stop duplicating debug info: llvm worktrees build
with `LLVM_USE_SPLIT_DWARF` (DWARF stays in `.dwo` files beside the objects
instead of being copied into every binary) and `LLVM_LINK_LLVM_DYLIB` (one
`libLLVM.so`, and with it `libclang-cpp.so`, rather than a static copy per
tool). The one consequence to know about: a binary copied out of its build
directory leaves its `.dwo` files behind and loses its debug info. The
standalone distribution is deliberately still linked statically, so
`build-dist/install` stays self-contained.

## Running the Vulkan offload tests

The `check-hlsl-vk` and `check-hlsl-clang-vk` suites compile HLSL to SPIR-V and
then *execute* it, so they need a real Vulkan driver (an "ICD"). The
environment picks one for you.

### Why a driver is pinned

The Vulkan loader loads **every** ICD manifest it can find and calls into each
one from `vkEnumeratePhysicalDevices`, so a single broken driver takes down the
whole process. Under WSL this happens by default: Mesa's `dzn`
(Vulkan-on-D3D12) driver is installed, fails to create a D3D12 device, and then
segfaults during enumeration — every test dies before it starts.

This cannot be fixed from `offloader`. Its `-adapter-regex` flag (and lit's
`OFFLOADTEST_GPU_NAME`, which forwards to it) filters the device list *after*
enumeration, i.e. after the crash. The only effective lever is the loader's
`VK_DRIVER_FILES`, which restricts it to an explicit set of manifests.

So the environment defaults to **lavapipe**, Mesa's CPU rasterizer: slow, but it
works everywhere and gives reproducible results. `offload-test-suite`'s
`lit.cfg.py` already forwards `VK_DRIVER_FILES` into the test environment, so the
setting reaches `offloader` without any test-suite changes.

### Choosing a driver

One command shows, lists and switches:

```bash
hlsl-vk                   # what am I running against, and what does it expose?
hlsl-vk --list            # what can I choose?
hlsl-vk system            # switch to the real GPU
hlsl-vk lavapipe          # switch back to the CPU rasterizer
```

A switch applies to **the next command**, not the next shell: the choice is
recorded in the workspace (`.hlsl-dev/settings.env`, gitignored, outside every
checkout) and every task resolves it as it starts.

Accepted values are `system` (let the loader discover drivers itself — use this
on a machine with a working native driver), `lavapipe`, any Mesa ICD short name
from `hlsl-vk --list` (`radeon`, `intel`, `dzn`, …), or an absolute path to an
ICD manifest.

For one command only, set `$HLSL_VK_DRIVER`; it wins over the recorded choice.
To bypass all of it, set the loader's own variable — lit forwards that into the
tests too:

```bash
HLSL_VK_DRIVER=system hlsl-test clang-vk
VK_DRIVER_FILES=/path/to/some_icd.x86_64.json hlsl-test vk
```

### Running the suites

```bash
hlsl-test vk                # DXC on Vulkan
hlsl-test clang-vk          # Clang on Vulkan

# A single test
hlsl-test clang-vk Feature/HLSLLib/log2.32.test
```

> **Note:** lavapipe is a software rasterizer and is not fully conformant. It is
> considerably slower than a GPU, and a test that fails *only* under lavapipe is
> more likely to be a driver limitation than a compiler bug — confirm on real
> hardware before filing an issue.

## Running the D3D12 offload tests

The `d3d12`, `warp-d3d12`, `clang-d3d12` and `clang-warp-d3d12` suites need
Direct3D 12. The test suite finds it at configure time — on Windows directly,
on Linux only under WSL, where it picks up the host driver from
`/usr/lib/wsl/lib` plus the two static libraries `directx-headers` ships — and
whatever it finds decides whether those suites exist in the build tree at all.

```bash
hlsl-d3d12                # is it available, is it on, does this build tree have it?
hlsl-d3d12 off            # build without it
hlsl-d3d12 on             # back to detecting it
```

The suite has no switch of its own, so `off` is expressed with CMake's
`CMAKE_DISABLE_FIND_PACKAGE_D3D12{,_WSL}`, which every configure passes
explicitly — so toggling never needs a `--fresh`. Like the Vulkan driver, the
choice belongs to the workspace rather than to a checkout, and switching it
reconfigures the worktree you are standing in when it already has a build tree
(a CMake re-run, not a rebuild).

Turn it off when the WSL driver is unstable, when a `d3d12` test wedges a run,
or to reproduce what a machine without D3D12 builds.

## Cross-compiling for another machine

HLSL ships on Windows and on ARM, and this workspace runs on x86-64 Linux.
`--platform` builds clang and the offload test suite for one of those other
machines from here — the same task, the same resolution rules, a different
toolchain:

```bash
hlsl-cross                                  # the platforms, and what each needs
hlsl-build --platform windows-x64   clang   # clang.exe, x64 MSVC ABI
hlsl-build --platform windows-arm64 clang   # clang.exe, arm64 MSVC ABI
hlsl-build --platform linux-arm64   clang   # clang, aarch64 Linux

cd offload-test-suite.my-feature
hlsl-build --platform windows-x64           # the suite, for Windows
```

| platform | triple | toolchain |
|---|---|---|
| `linux-arm64` | `aarch64-unknown-linux-gnu` | nixpkgs' cross gcc |
| `windows-x64` | `x86_64-pc-windows-msvc` | clang-cl + nixpkgs' `windows.sdk` |
| `windows-arm64` | `aarch64-pc-windows-msvc` | clang-cl + nixpkgs' `windows.sdk` |

A cross build is the native build plus a toolchain file, so everything else is
what it always was: which `llvm-project` worktree an offload build uses, which
DXC, the build type, `--dry-run`, the build lock. Two things do change:

- **The build tree is its own.** `<worktree>/build.<platform>`, beside the
  native `build/`, with its own LLVM distribution
  (`build-dist.<platform>`) for standalone offload builds and its own pins.
  Nothing a cross build does invalidates the native tree, and `hlsl-clean
  --platform windows-x64` removes only the cross one. `compile_commands.json`
  keeps pointing at the native tree: clangd should diagnose the code the way
  this machine compiles it.
- **Tests are refused.** Those binaries do not run here, so `hlsl-test` and
  `hlsl-lit` say so instead of trying. Build them, take them to the target,
  run the suites there. The suites that need external tools on the target
  (SPIR-V, DXIL) are left out of a cross tree for the same reason.

`--jobs N` (or `$HLSL_JOBS`) caps how many targets are built at once. The
default is one per core, which is right on a workstation and wrong in a
container with a process limit — 64 parallel compilations, each with its
sccache client, is what `ninja: fatal: posix_spawn: Resource temporarily
unavailable` means.

A cross build of LLVM has to *run* tablegen, and the tablegens it builds are
for the target, so the first one also builds host copies into
`<llvm worktree>/build-native-tools` (Release, no tests, one target — minutes,
and shared by every platform). `hlsl-info --platform <name>` shows where they
are, along with the toolchain and the triple.

### The Windows SDK

The two MSVC platforms need Microsoft's headers and import libraries. nixpkgs
has them — `windows.sdk`, an [xwin](https://github.com/Jake-Shadle/xwin) splat
of the official packages — but will not build them until the Visual Studio
licence has been accepted, which is not a decision this workspace can make for
you:

```bash
hlsl-cross --accept-msvc-license     # once, per workspace
hlsl-cross --fetch windows-x64       # download it now rather than mid-build
```

The acceptance is recorded in `.hlsl-dev/settings.env` like the other workspace
choices (`$HLSL_MSVC_LICENSE=accepted` does it for one command, for CI).

There is no licence-free Windows platform, and that is deliberate. nixpkgs has
MinGW (GNU-ABI) cross toolchains, and they were tried here — `clang.exe` builds
with them. But **D3D12 is reached through MSVC import libraries**
(`d3d12.lib`, `dxguid.lib`, `dxcore.lib`) that ship in the same Windows SDK, so
a MinGW build can carry neither of the two things worth cross-compiling: the
offload test suite refuses to configure without a runtime API for the target,
and DXC's CMake does `find_package(D3D12 REQUIRED)` on any Windows target. Its
binaries would also be a different ABI from every DXC and runtime they would
sit beside. "Does clang still compile for Windows" was not worth two more
platforms to maintain, so the MSVC licence is the price of a Windows build
here.

Three details of that toolchain are worth knowing, because each one is a build
that fails without it: the release CRT is forced (`/MD` everywhere — the SDK
carries no redistributable debug CRT), BLAKE3's MASM fast paths are dropped
(`LLVM_DISABLE_ASSEMBLY_FILES`, since assembling them wants Microsoft's
`ml64`), and the SDK's include directories are passed as joined `-imsvc` flags
rather than clang-cl's `/vctoolsdir` + `/winsdkdir`, whose separate values do
not survive sccache's command-line parsing.

Toolchains are built on demand from `scripts/cross/toolchains.nix` against the
same pinned nixpkgs as the rest of the environment, and cached (and GC-rooted)
in `.hlsl-dev/toolchains/`. Nothing is realised on shell entry, so a session
that never cross-compiles pays nothing; `hlsl-cross --refresh <platform>`
builds one again after a `devenv update`.

### Taking it to the machine that runs it

A cross build is only half of "build here, run there". The other half is
upstream's split build/test layout
(`offload-test-suite/docs/offload-distribution.md`), and `hlsl-package` is one
command for it:

```bash
hlsl-package --platform windows-x64      # -> <build tree>/hlsl-windows-x64.zip
hlsl-package --platform linux-arm64      # -> hlsl-linux-arm64.tar.gz
```

In an llvm-project checkout it runs the three install targets
(`install-distribution`,
`install-offload-tools`, `install-offload-test-suite`) into
`<build dir>/install` and archives that prefix — for `windows-x64`, 130 MB
zipped:

```
bin/          clang.exe, clang-dxc.exe, offloader.exe, api-query.exe,
              imgdiff.exe, FileCheck.exe, not.exe, obj2yaml.exe, split-file.exe
lib/clang/<ver>/include/    the HLSL resource headers
share/hlsl-test-suite/      the tests, the golden images,
                            configure-test-suite.py and the lit template
```

A Windows archive carries **no symlinks**, because a `.zip` that does produces
something Windows will not run — PowerShell reports it as *"The operation was
canceled by the user"*, which says nothing about the cause. LLVM installs its
driver aliases as links (`clang-dxc.exe -> clang.exe`), so packaging makes them
real files, as an install on Windows would. `clang++.exe`, `clang-cl.exe` and
`clang-cpp.exe` are left out rather than shipped as three more copies of a
135 MB binary; on the target they are one `copy clang.exe clang-cl.exe` away.
`hlsl-repro` goes further and keeps only the tools its tests invoke.

The archive is written inside the build tree, so it is never something `git
status` has an opinion about, and `--out <path>` puts it somewhere else.

It is not only for cross builds: without `--platform` it packages this
machine's build, and names the archive after the machine rather than after the
word "native" — `hlsl-x86_64-linux.tar.gz`, which still means something once
the file has been copied to a test runner.

The same task in a `DirectXShaderCompiler` checkout packages the *second*
prefix the runner needs:

```bash
cd DirectXShaderCompiler
hlsl-package --platform linux-arm64      # -> hlsl-dxc-linux-arm64.tar.gz
```

```
bin/    dxc, dxv                (dxc.exe, dxv.exe, dxcompiler.dll, dxil.dll on Windows)
lib/    libdxcompiler.so        (dxcompiler.lib, dxil.lib on Windows)
```

DXC has no install target that produces this, which is why the document says to
copy the files: a plain `ninja install` walks every `cmake_install.cmake`
including LLVM tools the build never made, `install-dxc` covers a subset, `dxv`
has no install target at all, and `dxil` is a prebuilt signing library with no
install rule. The task copies the documented list out of `bin/` and `lib/`,
dereferencing symlinks (`bin/dxc` is a link to `dxc-3.7`) and skipping what a
particular build does not produce — PDBs outside a debug-emitting config, and
`dxil`, which is Windows-only.

Two archives rather than one is also from the document: Clang's HLSL headers
and DXC's would collide in a single prefix. On the target, unpack both and
point the suite at the DXC one:

```bash
python share/hlsl-test-suite/configure-test-suite.py --dxc-path <dxc-dist>/bin/dxc
```

What it does *not* contain is DXC. The suite runs a `dxc` built for the same
machine, and that is a second prefix by design (Clang's HLSL headers and DXC's
would otherwise collide):

```bash
cd DirectXShaderCompiler
hlsl-build --platform linux-arm64 dxc        # works; bin/dxc is an aarch64 binary
hlsl-build --platform windows-x64 dxc        # needs the DIA SDK, see below
```

DXC is an LLVM 3.7 fork, so it builds its own host tools by configuring a
second CMake project under `<build dir>/NATIVE`, and that one is configured
with nothing but the flags it is handed. The environment passes it this
machine's clang and DXC's own exceptions/RTTI settings
(`crossDXCCMakeFlags` in `devenv.nix`) — without the latter its `ilist.h`
fails to compile its own `try`/`catch` while building `llvm-tblgen`.

For the MSVC platforms there is one thing this workspace cannot provide:
**Microsoft's DIA SDK**, which DXC uses for PDBs. It ships with Visual Studio,
not with the Windows SDK, so `windows.sdk` does not have it and a DXC configure
stops at `Could NOT find DiaSDK`. Copy the `DIA SDK` directory off a Windows
machine, put it somewhere without spaces in the path, and point
`$HLSL_DIA_SDK` at it:

```bash
HLSL_DIA_SDK=/opt/dia-sdk hlsl-build --platform windows-x64 dxc
```



### Packaging one test for a bug report

A whole suite is the wrong thing to attach to an issue. `hlsl-repro` takes the
tests you name and produces a self-contained archive that reproduces them:

```bash
hlsl-repro Feature/HLSLLib/log2.32.test
hlsl-repro --suite clang-d3d12 Feature/HLSLLib/log2.32.test Feature/HLSLLib/exp2.32.test
hlsl-repro --platform windows-x64 --suite vk Feature/Basic/DescriptorTable.test
```

Inside is the same install prefix as `hlsl-package`, with the test tree cut
down to what was named (each test, the `lit.local.cfg` files above it, the
golden images), plus three things a bug report needs:

- **`REPRO.md`** — what it is, the exact revision of every checkout it was
  built from (`llvm-project`, `offload-test-suite`, the golden images), the
  platform, the suite, the build type, the DXC that built it, the Vulkan
  driver and D3D12 setting for a native build, the RUN lines of each test, and
  a "What happened here" section to fill in.
- **`run-nolit.sh` / `run-nolit.cmd`** — the tests' own RUN lines with lit's
  substitutions already applied, against the binaries in `bin/`. **No Python,
  no pip, no lit**: a graphics driver and the archive are the whole
  dependency list. A test using a substitution this expansion does not know is
  left out of the script and named in `REPRO.md` rather than guessed at.
- **`run.sh` / `run.cmd`** — the same tests through lit, which is what CI runs
  and therefore the authority on feature detection and XFAILs. This is the
  path that wants Python with `lit` and `pyyaml`; an argument overrides the
  suite and anything after it goes to `configure-test-suite.py`
  (`--dxc-path …`).

Pick a `clang-*` suite when the compiler under test should be the one in the
archive; the DXC-compiled suites (`d3d12`, `vk`, `mtl`) need a `dxc` for that
machine as well, which `REPRO.md` says.

### What the suite needs on the other side

The offload test suite refuses to configure without a runtime API for the
machine it is built for, so each platform's toolchain brings one:

- **`linux-arm64`** — Vulkan, cross-built loader and headers. Verified: the
  suite's `offloader` builds as an aarch64 binary.
- **`windows-x64` / `windows-arm64`** — D3D12 *and* Vulkan, so a Windows build
  carries the `d3d12`, `warp-d3d12`, `vk` and `clang-vk` suites. Verified:
  `offloader.exe` and `clang.exe` build for both architectures, and the
  offloader imports `d3d12.dll` and `vulkan-1.dll`.

  D3D12 comes out of the Windows SDK; the toolchain answers
  `find_package(D3D12)` with its headers, because the module otherwise looks
  for a registry key and a versioned Windows Kits directory, neither of which
  exists here.

  Vulkan needs `vulkan-1.lib`, the import library a Windows application links
  against, which normally comes from LunarG's SDK — not something nixpkgs has.
  It does not need to: an import library is just a list of exported names, and
  the loader publishes exactly that as `loader/vulkan-1.def` (Apache-2.0, in
  the source nixpkgs already fetches), so the toolchain generates the `.lib`
  from it with `llvm-dlltool`, per architecture. At run time the real
  `vulkan-1.dll` comes from the machine's Vulkan runtime, as it does for any
  other Windows application. Both halves are named to CMake explicitly:
  `FindVulkan` left to search would find *this* machine's headers (they are
  the same headers) and no library, turning the backend on and then failing
  to link.

  WARP is left to the target (`WARP_VERSION=System`) rather than fetched from
  NuGet during the configure.


## Formatting

The submodules are upstream repositories, and reviewers there expect a
clang-formatted diff. `git clang-format` reformats only the lines a change
touches, which is what this wraps:

```bash
hlsl-format               # what would change in the staged diff
hlsl-format --diff        # the same, as a patch
hlsl-format --fix         # apply it
hlsl-format --since main  # everything this branch changed, not just what is staged
```

A **pre-commit hook that warns and lets the commit through** is installed in
every checkout that has a `.clang-format` — `llvm-project`,
`DirectXShaderCompiler` and `offload-test-suite`:

```
$ git commit -m "..."
clang-format would change these staged files:
    clang/lib/Sema/SemaHLSL.cpp

    hlsl-format --diff    to see it
    hlsl-format --fix     to apply it
[my-feature 0f31c9a] ...
```

It never blocks: formatting is not a gate this workspace gets to invent on top
of upstream's rules, and a hook that stops a commit at the wrong moment is a
hook people delete. When the staged diff is clean it says nothing at all.

The hook lives in the clone's git directory (`.git/modules/<name>/hooks/`), so
it is shared by every `wt` worktree of that submodule, is never part of a
commit, and leaves `git status` clean. Entering the environment keeps it in
place; an existing hook that is not ours is never overwritten.

```bash
hlsl-format --uninstall-hooks    # remove it
hlsl-format --install-hooks      # put it back
HLSL_INSTALL_HOOKS=0             # and stop the shell reinstalling it
```

It needs the tools from this environment, but not the shell: the hook adds the
devenv profile to `PATH` itself, so a commit from an editor or a bare terminal
gets the same check.

## Secrets

The workspace needs exactly one: a GitHub token for `offloader-monitor`, which
reads the API and the run logs of `llvm/offload-test-suite`. Public-repo read
scope is enough. Building compilers and running tests need no credentials at
all.

It is declared in [`secretspec.toml`](./secretspec.toml) and resolved when the
task runs, from whatever provider you keep secrets in:

```bash
secretspec config global init    # choose a provider, once per machine
secretspec set GH_TOKEN          # keyring, 1Password, dotenv, …
offloader-monitor                # takes it from there
```

A token already in the environment wins — that is CI providing its own, and the
dev container forwarding the host's `GH_TOKEN`/`GITHUB_TOKEN` — and if there is
neither, `monitor_failures.py` says so plainly.

The value never reaches Nix: devenv's `secretspec` integration is deliberately
off (`devenv.yaml`), because anything it resolves into `env` would land in the
shell derivation in the world-readable Nix store. The task calls `secretspec
run` itself, so the token exists only in that one process.

`.env` is *not* loaded into the shell either — devenv's dotenv integration is
off. Workspace choices live in `.hlsl-dev/settings.env` (via `hlsl-vk` and
`hlsl-d3d12`), where a change applies to the next command rather than the next
shell, and secrets live in a provider. If you want a `.env` anyway, secretspec's
`dotenv` provider will read one for you.

## Managing submodules

By default, submodules are cloned with a depth of 2 (`shallow = true` in
`.gitmodules`). This is enough for local testing, but it can be restrictive when
preparing pull requests or checking out old branches.

### Updating to latest upstream

To update all submodules to the latest commits on their respective default
remote branches (e.g. `main` or `master`):

```bash
hlsl-update-submodules
```

### Fetching full history

```bash
hlsl-fetch-history llvm-project
hlsl-fetch-history DirectXShaderCompiler
```

### Truncating history

If you previously fetched the full history and now want to free up disk space
by truncating it back to a shallow depth (depth 2):

```bash
hlsl-truncate-history llvm-project
```

## Adding / fixing submodule URLs

If the placeholder URLs for `offload-test-suite` or `offload-golden-images` in
`.gitmodules` are incorrect, edit `.gitmodules` with the correct repository
URLs, then run `git submodule sync` and `hlsl-setup`.

## How the environment is put together

| Where | What |
|---|---|
| `devenv.nix` | the whole environment: packages, variables, the CMake flag *templates*, the task list, the process definitions and the dev container |
| `devenv.yaml`, `devenv.lock` | which nixpkgs the above is built from, and the exact revision |
| `scripts/tasks/*.sh` | one file per `hlsl-<task>`: what it does |
| `scripts/hlsl-dev.sh` | shared by all of them: option parsing, worktree detection, dependency resolution, prerequisites, locks, the CMake invocations |
| `scripts/tests/hlsl-dev.test.sh` | self-test for that logic, run by `devenv test` |
| `.hlsl-dev/` | workspace state: dependency memories, build locks, and `settings.env` (the Vulkan driver, D3D12) |
| `offloader-scripts/tasks/*.sh` | the same, for the `offloader-<task>` CI monitoring commands |

Every executable in `scripts/tasks/` automatically becomes an `hlsl-<name>`
command; its one-line `# summary:` comment is what `devenv info` and `hlsl`
list. The command devenv puts on `PATH` only dispatches into the checkout, so
editing a task takes effect immediately, without re-entering the environment.

The flag lists in `devenv.nix` are templates: placeholders such as
`$HD_LLVM_SRC`, `$HD_DXC_BIN_DIR` or `$HD_OFFLOAD_SRC` are left unexpanded in
the environment and filled in per invocation, once the tasks have resolved
which worktrees a command applies to. That is what lets one flag list serve
every worktree of a repository instead of a single hard-coded checkout — tune
build options in `devenv.nix`, and every worktree picks them up on its next
configure.

### Checking the environment

```bash
devenv test                 # every check below, in order
devenv tasks list           # what they are
```

The checks are devenv tasks, so each is named, timed and reported separately:

| Task | What it checks |
|---|---|
| `hlsl:check:env` | the toolchain, the workspace layout, the CMake flag templates, the Vulkan and D3D12 setup |
| `hlsl:check:tasks` | every task is on `PATH` and answers `--help` |
| `hlsl:check:plan` | a `--dry-run` configure, build and test run for each checkout you actually have: real worktree discovery, real dependency resolution, and the cmake command that would follow |
| `hlsl:check:selftest` | `scripts/tests/hlsl-dev.test.sh` — resolution, prerequisites, settings and the hook, against fake checkouts |
| `hlsl:check:hook` | the clang-format hook, driven by real commits in a throwaway repository with devenv taken out of the environment |
| `hlsl:check:shellcheck` | ShellCheck over the whole task layer (last: it is the slow one) |

Checkouts that are not there are skipped, so this is green on a fresh clone —
which matters, because the dev container runs it before `hlsl-setup` ever
happens. The `offloader-scripts` unit tests are *not* part of it: that tooling
builds the GitHub Pages report, not a compiler, so it has `offloader-test` for
when you are working on it.

One failing check does not hide the ones after it, and any of them can be run
on its own while you work on it:

```bash
devenv tasks run hlsl:check:shellcheck --mode single
```

`devenv test` is the same command the dev container runs after creation, so a
green run locally means a container build will get that far too. It never
builds a compiler: the self-test exercises the resolution and prerequisite
logic against fake checkouts in a temporary directory.

### Compiler Explorer

```bash
hlsl-compiler-explorer          # :10240, wired to your builds
```

It generates `compiler-explorer/etc/config/hlsl.local.properties` from the
resolved `--llvm` / `--dxc` worktrees, then runs in the foreground.

### Updating the toolchain

```bash
devenv update           # move devenv.lock to the current nixos-unstable
devenv test             # check the result before committing the lock
```

### Useful environment variables

| Variable | Effect |
|---|---|
| `HLSL_WT` | act on this worktree, as if `--in` had been passed |
| `HLSL_LLVM`, `HLSL_DXC`, `HLSL_OFFLOAD`, `HLSL_GOLDEN` | default dependencies for this shell |
| `HLSL_BUILD_TYPE`, `HLSL_BUILD_DIR` | build type / build directory for this shell |
| `HLSL_BUILD_DIR_NAME` | the build directory's *name*, for every worktree (default `build`; the dev container uses `build-container`) |
| `HLSL_DIST_PREFIX` | an already-installed LLVM distribution to build against |
| `HLSL_AUTO=0` | never build a missing prerequisite; fail and say what is missing |
| `HLSL_INSTALL_HOOKS=0` | do not install the clang-format pre-commit hook |
| `HLSL_VK_DRIVER` | Vulkan ICD for this command, overriding `hlsl-vk` (see above) |
| `HLSL_PLATFORM` | cross-compile for this platform, as if `--platform` had been passed |
| `HLSL_JOBS` | build this many targets at once, as if `--jobs` had been passed |
| `HLSL_DIA_SDK` | a DIA SDK copied off a Windows machine, for cross-building DXC |
| `HLSL_MSVC_LICENSE=accepted` | accept the Visual Studio licence for one command (see `hlsl-cross`) |

They are the same knobs as the flags, which makes them convenient for an agent
that wants one setting to apply to a whole session:

```bash
export HLSL_LLVM=llvm-project.texture-store
hlsl-build && hlsl-test clang-vk        # both use that LLVM
```
