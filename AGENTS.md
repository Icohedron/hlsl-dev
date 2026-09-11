# AGENTS.md — HLSL developer workspace

Instructions for coding agents working in this repository. Read `README.md` for
the full prose version; this file is the operational summary.

## What this repository is

A Nix-powered *workspace*, not a source tree. It contains no product code of its
own — the code lives in git submodules and their worktrees:

| Path | What |
|---|---|
| `llvm-project/` | LLVM/Clang (upstream). HLSL frontend + DirectX/SPIR-V backends |
| `DirectXShaderCompiler/` | Microsoft DXC (upstream) |
| `offload-test-suite/` | GPU execution tests (`check-hlsl-*` suites) |
| `offload-golden-images/` | Reference images for the offload suite |
| `wg-hlsl/` | HLSL working group docs and proposals (documentation only) |
| `compiler-explorer/` | Compiler Explorer, wired to the locally built compilers |
| `offloader-scripts/` | Python CI monitoring/triage tooling (`offloader-*` tasks) |
| `scripts/hlsl-dev.sh` | All the real logic: option parsing, worktree/dependency resolution, prerequisites, CMake, locks |
| `scripts/cross/toolchains.nix` | CMake toolchain files for the cross-compilation platforms, built on demand |
| `scripts/tasks/*.sh` | One file per task; devenv puts each on PATH as `hlsl-<task>` |
| `scripts/tests/hlsl-dev.test.sh` | Self-test for the resolution/prerequisite logic (`devenv test`) |
| `devenv.nix` | The environment: packages, variables, CMake flag *templates* (`$HD_*` placeholders), tasks, dev container |

`llvm-project.<suffix>/`, `offload-test-suite.<suffix>/` etc. are git worktrees
of the matching submodule, created with `wt` (worktrunk). They are gitignored.

## Environment

Everything runs inside the devenv environment. Check with `echo $DEVENV_ROOT` —
if it is empty you are outside it and the `hlsl-*` tasks, `cmake`, `ninja`,
`sccache` and the Vulkan setup will not behave. Enter it with `devenv shell`,
or prefix a single command with `devenv shell <cmd>`.

Activation on `cd` is direnv's job here: the workspace ships an `.envrc`
(`use devenv`), so `eval "$(direnv hook bash)"` in `~/.bashrc` plus one
`direnv allow` is enough, and that is what the dev container does. devenv's
own hook works too, but it moves the shell to the project root on activation
([devenv#3041](https://github.com/cachix/devenv/issues/3041)).

The tasks are commands on PATH, not a subcommand of a runner: `hlsl-build`,
not `mask build`. `hlsl` lists them all, `hlsl <task>` is an alias for
`hlsl-<task>`, and `hlsl-<task> --help` documents one, including every flag.

## Golden rules

1. **Drive everything through the `hlsl-*` tasks.** Do not invoke `cmake`,
   `ninja` or `llvm-lit` by hand: the tasks resolve which worktree you mean,
   which other worktrees it builds against, where the build directory is,
   expand the CMake flag templates and take the build lock. Raw invocations
   bypass all of that.
2. **Tasks act on the checkout you are standing in.** `cd` into the worktree, or
   pass `--in <worktree>`. `hlsl-info` tells you what the current directory
   resolves to; `hlsl-ls` lists every worktree and its state.
3. **Do not commit in the workspace root** unless explicitly asked. `git status`
   there always shows the submodules as modified; committing that records new
   submodule pointers, which is almost never what is wanted. Commit *inside* the
   submodule/worktree instead.
4. **Builds are expensive, and tasks build what they need.** A task configures
   what it is about to build and installs what it is about to link against — a
   `hlsl-test` in an offload worktree can therefore trigger a full LLVM
   distribution build. Prefer a specific target (`hlsl-build clang`) and a
   specific test (`hlsl-lit <path>`), and when the cost matters, look before
   you leap:

   ```bash
   hlsl-test clang-vk --dry-run    # what would be built and configured
   hlsl-test clang-vk --no-auto    # refuse to build prerequisites ($HLSL_AUTO=0)
   ```

   A target can also build far more than its name suggests: `check-llvm` drags
   in `llvm-test-depends`, which is every LLVM tool plus the Kaleidoscope
   examples, and each one is a linked copy of LLVM. `hlsl-trim` deletes the
   binaries the targets you actually use do not depend on (`--dry-run` first).
5. **Never re-configure or clean someone else's worktree.** Concurrent builds of
   the *same* build directory are serialised by a lock; different worktrees are
   independent and must stay that way.

## Common commands

```bash
hlsl                         # every task, one line each
devenv info                  # packages, tasks and environment variables on offer

hlsl-ls                      # every worktree: branch, build state, pins
hlsl-info                    # what does this directory resolve to, against what?

hlsl-build [target]...       # build the current checkout (configuring first);
                             # several targets go in one cmake invocation
hlsl-clean [--dist]          # remove its build directory
hlsl-trim [--dry-run]        # remove just the binaries the targets you use do
                             # not need (a stray check-llvm costs ~16 GB)
hlsl-configure --llvm X      # change what it builds against; remembered after
hlsl-configure --forget      # drop what it remembered

hlsl-dist --in llvm-project  # refresh the standalone LLVM distribution after a
                             # Clang change (a missing one is installed by the
                             # build that needs it)

hlsl-test                    # whole check-hlsl umbrella
hlsl-test clang-vk           # one suite
hlsl-test clang-vk Feature/HLSLLib/log2.32.test    # one test
hlsl-test clang-vk 'log2.*'  # non-path argument => lit --filter regex
hlsl-lit clang/test/CodeGenHLSL/RootSignature      # any lit path, from llvm

hlsl-codegraph               # index/refresh the current worktree for CodeGraph

hlsl-cross                   # cross-compilation: platforms, toolchains, licence
hlsl-build --platform windows-x64 clang    # build for another machine
hlsl-package --platform windows-x64        # zip up tools+tests to run there
hlsl-package --in DirectXShaderCompiler --platform windows-x64   # its dxc prefix
```

Suites: `d3d12 vk mtl warp-d3d12 clang-d3d12 clang-vk clang-mtl
clang-warp-d3d12` (plus `unit`). Extra lit flags go through
`--lit-args=--time-tests` (use `=` when the value starts with a dash).

## Cross-worktree dependencies

An `offload-test-suite` build needs an `llvm-project` worktree; running its
suites needs a `dxc`. Resolution order, first match wins:

1. `--llvm` / `--dxc` flags
2. `$HLSL_LLVM` / `$HLSL_DXC` / `$HLSL_OFFLOAD` / `$HLSL_GOLDEN`
   (set these once per session if the whole task uses one combination)
3. what the last successful `hlsl-configure` in that worktree resolved to
4. a worktree of that repository on the **same branch name**
5. the submodule in the workspace root

Rule 4 is the one to lean on: give the worktrees of two repositories the same
branch name and they pair up with no flags and no state. Otherwise say it once
and it sticks:

```bash
cd offload-test-suite.my-feature
hlsl-configure --llvm texture-store --dxc DirectXShaderCompiler
hlsl-build && hlsl-test clang-vk        # both keep using them
```

Those memories live in `.hlsl-dev/pins/` at the workspace root, never inside a
checkout, so `git status` in a worktree stays clean. A pin that no longer
resolves warns and falls through to rule 4 instead of failing.
`hlsl-configure --forget` drops them.

A worktree spec may be a path, a directory name (`llvm-project.texture-store`),
just the suffix (`texture-store`), or a branch name. `--dxc` also accepts a
directory containing `dxc`/`dxv`, or `nix` for the prebuilt one in the
environment.

An offload worktree builds standalone against an installed LLVM distribution
(fast: ~20 s configure, ~2 min build); the distribution is installed on demand
and shared by every offload worktree pointed at that llvm worktree. To build
the suite inside an llvm build tree instead — the in-tree layout — configure
that llvm worktree against its sources and work there:

```bash
hlsl-configure --in llvm-project.my-feature --offload offload-test-suite.mine
hlsl-test --in llvm-project.my-feature clang-vk
```

## Cross-compilation

`--platform <name>` (or `$HLSL_PLATFORM`) builds clang and the offload test
suite for another machine. `hlsl-cross` lists the platforms and what each one
still needs:

| platform | triple | toolchain |
|---|---|---|
| `linux-arm64` | `aarch64-unknown-linux-gnu` | nixpkgs cross gcc |
| `windows-x64` | `x86_64-pc-windows-msvc` | clang-cl + nixpkgs `windows.sdk` |
| `windows-arm64` | `aarch64-pc-windows-msvc` | clang-cl + nixpkgs `windows.sdk` |

The Windows platforms carry both runtime APIs: D3D12 from the SDK, and Vulkan
via a `vulkan-1.lib` the toolchain generates from the loader's own `.def` with
`llvm-dlltool` (the `.dll` comes from the target machine's Vulkan runtime). So
`d3d12`, `warp-d3d12`, `vk` and `clang-vk` all exist in a Windows build tree.
`linux-arm64` has Vulkan only.

```bash
hlsl-cross --accept-msvc-license          # once: the MSVC SDK is licence-gated
hlsl-cross --fetch windows-x64            # build its toolchain now, not mid-build
hlsl-build --platform windows-x64 clang
hlsl-info  --platform windows-x64         # triple, toolchain, host tools, deps
hlsl-clean --platform windows-x64         # only the cross tree
```

Rules that differ from a native build:

1. **The build tree is `<worktree>/build.<platform>`**, with its own
   distribution (`build-dist.<platform>`) and its own pins, falling back to the
   native pins for anything not said twice. The native tree is never touched,
   and `compile_commands.json` keeps pointing at it.
2. **Tests are refused** (`hlsl-test`, `hlsl-lit`): the binaries are for another
   machine. Build here, run there.
3. **The first cross build of an llvm worktree also builds host tablegens**
   into `<worktree>/build-native-tools` — a cross build cannot run its own.
4. **Toolchains are built on demand** from `scripts/cross/toolchains.nix`
   (pinned nixpkgs, cached and GC-rooted in `.hlsl-dev/toolchains/`). Nothing
   is realised on shell entry. Cross-specific CMake flags are templates in
   `devenv.nix` (`crossCMakeFlags`, `crossLLVMCMakeFlags`,
   `crossLinuxCMakeFlags`, `crossWindowsCMakeFlags`, `crossMSVCCMakeFlags`,
   `nativeToolsCMakeFlags`), appended to the native list so a later `-D` wins.
5. **The Windows platforms need Microsoft's licence accepted** once
   (`hlsl-cross --accept-msvc-license`, or `$HLSL_MSVC_LICENSE=accepted` for a
   single command). There is no licence-free (MinGW, GNU-ABI) Windows platform
   on purpose: D3D12 comes as MSVC import libraries from the same SDK, so such
   a build could carry neither the offload suite (no runtime API) nor DXC
   (`find_package(D3D12 REQUIRED)`).
6. **`hlsl-package` is how a cross build leaves this machine.** In an llvm
   worktree it runs `install-distribution` / `install-offload-tools` /
   `install-offload-test-suite` into `<build dir>/install` and archives it; in
   a DXC worktree it assembles the curated `bin/` + `lib/` prefix from
   docs/offload-distribution.md (DXC has no install target that produces it)
   into `<build dir>/dxc-dist`. Two archives, because Clang's HLSL headers and
   DXC's collide in one prefix. .zip for Windows, .tar.gz otherwise.
7. **DXC cross-builds too**, with two caveats: it configures its own
   `<build>/NATIVE` host build (handled in `crossDXCCMakeFlags`), and for the
   MSVC platforms it needs Microsoft's DIA SDK, which is not in nixpkgs --
   copy it off a Windows machine and set `$HLSL_DIA_SDK`.
8. **`--jobs N` (or `$HLSL_JOBS`) caps parallelism.** The default is one job
   per core; in a container with a pids limit that is how a build dies as
   `ninja: fatal: posix_spawn: Resource temporarily unavailable`.

## Worktrees

```bash
cd llvm-project && wt switch --create my-feature   # -> ../llvm-project.my-feature
```

Artifacts live inside the worktree (`<worktree>/build`, `<worktree>/build-dist`),
so parallel agents never share a build directory and `wt remove` takes the
artifacts with it. All worktrees share one sccache (`.sccache/`), so a second
build of the same upstream sources is mostly cache hits.

## Code intelligence

A configure links the build's `compile_commands.json` to the root of the
worktree (`<worktree>/compile_commands.json -> build-container/...`), because
clangd only looks in the worktree, its parents and their `build/` — not in the
container's `build-container/`. `hlsl-info` shows where it points; `hlsl-clean`
removes it with the build tree. Never commit it (it is in `info/exclude`).

Prefer the `codegraph_*` tools over grepping multi-gigabyte source trees for
architecture, symbol, caller/callee and impact questions. Index scopes are in
`scripts/codegraph-{llvm,dxc,offload}.json`. If a worktree has no index, run
`hlsl-codegraph` (seeds from another worktree's database — seconds, not
minutes).

**Gotcha:** `hlsl-codegraph` appends a marked block to the checkout's tracked
`.gitignore` (to un-hide `llvm/lib/Target`) and marks the file `skip-worktree`.
A `git checkout`/`rebase`/`pull` that wants to change `.gitignore` will then
fail with *"Your local changes … would be overwritten"* or *"Entry '.gitignore'
not uptodate"*. Fix:

```bash
hlsl-codegraph --restore-gitignore
git rebase origin/main
hlsl-codegraph
```

Never hand-edit or commit that block.

## Vulkan / running GPU tests

The `vk` and `clang-vk` suites execute SPIR-V, so they need a working ICD. The
environment pins **lavapipe** (CPU rasterizer) by default because a broken
driver (WSL's `dzn`) crashes the whole loader during enumeration.

```bash
hlsl-vk           # what am I running against, and what does it expose?
hlsl-vk --list    # options
hlsl-vk system / lavapipe / radeon / <path to icd.json>
```

A switch applies to the next command, not the next shell: the choice is kept in
`.hlsl-dev/settings.env`, and every task resolves it as it starts. For one
command only, set `$HLSL_VK_DRIVER`. A test that fails *only* under lavapipe is
more likely a software-rasterizer limitation than a compiler bug — say so
rather than "fixing" the compiler.

The `d3d12`, `warp-d3d12`, `clang-d3d12` and `clang-warp-d3d12` suites need
Direct3D 12, which exists on Windows and — on Linux — only under WSL. It is
detected at configure time, and `hlsl-d3d12` is the switch:

```bash
hlsl-d3d12        # available? on? does this build tree have the suites?
hlsl-d3d12 off    # build without it (reconfigures a configured worktree)
hlsl-d3d12 on     # back to detecting it
```

## Environment variables

| Variable | Effect |
|---|---|
| `HLSL_WT` | act on this worktree, as if `--in` had been passed |
| `HLSL_LLVM`, `HLSL_DXC`, `HLSL_OFFLOAD`, `HLSL_GOLDEN` | default dependencies |
| `HLSL_BUILD_TYPE`, `HLSL_BUILD_DIR` | build type / build directory for this shell |
| `HLSL_BUILD_DIR_NAME` | build directory *name* for every worktree (default `build`; the dev container uses `build-container`, so its trees and the host's stay apart) |
| `HLSL_DIST_PREFIX` | an already-installed LLVM distribution to build against |
| `HLSL_AUTO=0` | never build a missing prerequisite; fail and say what is missing |
| `HLSL_INSTALL_HOOKS=0` | do not install the clang-format pre-commit hook |
| `HLSL_VK_DRIVER` | Vulkan ICD for this command, overriding `hlsl-vk` |
| `HLSL_PLATFORM` | cross-compile for this platform, as if `--platform` had been passed |
| `HLSL_JOBS` | build this many targets at once, as if `--jobs` had been passed |
| `HLSL_DIA_SDK` | DIA SDK directory (from a Visual Studio install) for a DXC cross build |
| `HLSL_MSVC_LICENSE` | `accepted` accepts the Visual Studio licence for one command |

## Changing the environment

CMake flags are *not* in `scripts/hlsl-dev.sh`; they are templates in
`devenv.nix` (`llvmCMakeFlags`, `llvmDistCMakeFlags`, `offloadCMakeFlags`,
`dxcCMakeFlags`) with `$HD_*` placeholders filled in per invocation. Edit them
there so every worktree picks them up on its next configure. Placeholders must
stay free of whitespace and shell metacharacters — the expander rejects them on
purpose. Packages, environment variables, the process list and the dev
container live in the same file.

`.devcontainer/devcontainer.json` is *generated* from `devcontainer.settings` in
`devenv.nix`; never hand-edit it, and commit it when it changes.

## Adding or changing a task

1. Add or edit a file in `scripts/tasks/` (or `offloader-scripts/tasks/`). Copy
   the shape of an existing one: `# summary:` line, `HD_TASK_ARGS` /
   `HD_TASK_DESC` / `HD_TASK_OPTS`, then `hd_parse "$@"` and `hd_init`.
   In `HD_TASK_OPTS`, `name=` takes a value and a bare `name` is a switch;
   `--build-type` sets `$build_type`, positionals land in `HD_ARGV`.
2. `chmod +x` it. A new file needs one re-entry of the environment to appear on
   PATH (devenv discovers the directory at evaluation time); edits to an
   existing task take effect immediately.
3. Run `devenv test` — it runs the `hlsl:check:*` tasks: the environment,
   `--help` on every task, a `--dry-run` configure/build/test for each real
   checkout, the `scripts/tests/*.test.sh` suites (resolution logic against
   fake checkouts, and the clang-format hook against real commits) and
   ShellCheck over the task layer. Keep it green; it is what the dev container
   runs on creation, and it never builds a compiler. While iterating, run one
   check on its own with
   `devenv tasks run hlsl:check:shellcheck --mode single`.

## Working on the submodules

These are upstream projects; follow *their* conventions, not this repository's:

- LLVM/Clang: LLVM coding standards, `clang-format` on the diff, commit subjects
  like `[HLSL] …`, tests under `clang/test/…` or `llvm/test/…` alongside the
  change. `hlsl-format` reports where the staged diff disagrees with
  `.clang-format` and `hlsl-format --fix` applies it; a pre-commit hook prints
  the same warning but never blocks, so a warning at commit time is yours to
  act on, not something to work around.
- DXC: its own style and `test/` layout.
- offload-test-suite: `.test` files plus YAML data; check golden images when
  output changes.
- Keep unrelated formatting churn out of the diff; these trees are huge and
  reviewers are upstream.

## Notes

- `offloader-scripts/` is stdlib-only Python with its own tasks
  (`offloader-monitor`, `offloader-triage`, `offloader-site`); run its unit
  tests with `offloader-test`. They are not part of `devenv test`: that tooling
  feeds the GitHub Pages report, not a compiler.
- The one secret in the workspace is a GitHub token for `offloader-monitor`,
  declared in `secretspec.toml`. A token in `$GH_TOKEN`/`$GITHUB_TOKEN` is used
  as-is; otherwise the task asks secretspec for one. Never put it in
  `devenv.nix` or any `env`: that writes it into the Nix store. There is no
  dotenv integration — `.hlsl-dev/settings.env` holds workspace choices, a
  provider holds secrets.
