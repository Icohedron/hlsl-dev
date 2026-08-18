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
| `offloader-scripts/` | Python CI monitoring/triage tooling (own `maskfile.md`) |
| `scripts/hlsl-dev.sh` | All the real logic: worktree/dependency resolution, CMake, locks |
| `maskfile.md` | The task surface (`mask <task>`) |
| `flake.nix` | Dev shell + CMake flag *templates* (`$HD_*` placeholders) |

`llvm-project.<suffix>/`, `offload-test-suite.<suffix>/` etc. are git worktrees
of the matching submodule, created with `wt` (worktrunk). They are gitignored.

## Environment

Everything runs inside the Nix dev shell (`nix develop`, or direnv, which is
already set up). Check with `echo $HLSL_DEV_ROOT` — if it is empty you are
outside the shell and `mask`, `cmake`, `ninja`, `sccache` and the Vulkan setup
will not behave.

`mask` is wrapped so it finds the workspace `maskfile.md` from any directory.

## Golden rules

1. **Drive everything through `mask`.** Do not invoke `cmake`, `ninja` or
   `llvm-lit` by hand: the tasks resolve which worktree you mean, which other
   worktrees it builds against, where the build directory is, expand the CMake
   flag templates and take the build lock. Raw invocations bypass all of that.
2. **Tasks act on the checkout you are standing in.** `cd` into the worktree, or
   pass `--in <worktree>`. `mask info` tells you what the current directory
   resolves to; `mask ls` lists every worktree and its state.
3. **Do not commit in the workspace root** unless explicitly asked. `git status`
   there always shows the submodules as modified; committing that records new
   submodule pointers, which is almost never what is wanted. Commit *inside* the
   submodule/worktree instead.
4. **Builds are expensive.** A cold `llvm-project` build is tens of minutes even
   with sccache. Prefer a specific target (`mask build clang`) and a specific
   test (`mask lit <path>`) over `mask build` / `mask test` with no arguments.
5. **Never re-configure or clean someone else's worktree.** Concurrent builds of
   the *same* build directory are serialised by a lock; different worktrees are
   independent and must stay that way.

## Common commands

```bash
mask ls                      # every worktree: branch, build state, pins
mask info                    # what does this directory resolve to, against what?

mask configure               # configure the current checkout
mask build [target]          # build it (configures first if needed)
mask clean [--dist]          # remove its build directory

mask dist --in llvm-project  # install the standalone LLVM distribution
                             # that standalone offload builds link against

mask test                    # whole check-hlsl umbrella
mask test clang-vk           # one suite
mask test clang-vk Feature/HLSLLib/log2.32.test    # one test
mask test clang-vk 'log2.*'  # non-path argument => lit --filter regex
mask lit clang/test/CodeGenHLSL/RootSignature      # any lit path, from llvm

mask codegraph               # index/refresh the current worktree for CodeGraph
```

Suites: `d3d12 vk mtl warp-d3d12 clang-d3d12 clang-vk clang-mtl
clang-warp-d3d12` (plus `unit`). Extra lit flags go through
`--lit-args=--time-tests` (use `=` when the value starts with a dash).

## Cross-worktree dependencies

An `offload-test-suite` build needs an `llvm-project` worktree; running its
suites needs a `dxc`. Resolution order, first match wins:

1. `--llvm` / `--dxc` / `--offload` / `--golden` flags
2. `$HLSL_LLVM` / `$HLSL_DXC` / `$HLSL_OFFLOAD` / `$HLSL_GOLDEN`
   (set these once per session if the whole task uses one combination)
3. a pin from `mask link` or the last successful `mask configure`
4. a worktree of that repository on the **same branch name**
5. the submodule in the workspace root

```bash
cd offload-test-suite.my-feature
mask link --llvm texture-store --dxc DirectXShaderCompiler   # remember it
mask build && mask test clang-vk
```

Pins live in `.hlsl-dev/pins/` at the workspace root, never inside a checkout,
so `git status` in a worktree stays clean. `mask unlink` forgets them.

A worktree spec may be a path, a directory name (`llvm-project.texture-store`),
just the suffix (`texture-store`), or a branch name. `--dxc` also accepts a
directory containing `dxc`/`dxv`, or `nix` for the prebuilt one in the shell.

Offload builds default to `--mode standalone` (fast: ~20 s configure, ~2 min
build, needs `mask dist` on the llvm worktree once). `--mode integrated` builds
the suite inside the llvm build tree; only one offload worktree can occupy an
llvm build tree at a time.

## Worktrees

```bash
cd llvm-project && wt switch --create my-feature   # -> ../llvm-project.my-feature
```

Artifacts live inside the worktree (`<worktree>/build`, `<worktree>/build-dist`),
so parallel agents never share a build directory and `wt remove` takes the
artifacts with it. All worktrees share one sccache (`.sccache/`), so a second
build of the same upstream sources is mostly cache hits.

## Code intelligence

Prefer the `codegraph_*` tools over grepping multi-gigabyte source trees for
architecture, symbol, caller/callee and impact questions. Index scopes are in
`scripts/codegraph-{llvm,dxc,offload}.json`. If a worktree has no index, run
`mask codegraph` (seeds from another worktree's database — seconds, not
minutes).

**Gotcha:** `mask codegraph` appends a marked block to the checkout's tracked
`.gitignore` (to un-hide `llvm/lib/Target`) and marks the file `skip-worktree`.
A `git checkout`/`rebase`/`pull` that wants to change `.gitignore` will then
fail with *"Your local changes … would be overwritten"* or *"Entry '.gitignore'
not uptodate"*. Fix:

```bash
mask codegraph --restore-gitignore
git rebase origin/main
mask codegraph
```

Never hand-edit or commit that block.

## Vulkan / running GPU tests

The `vk` and `clang-vk` suites execute SPIR-V, so they need a working ICD. The
shell pins **lavapipe** (CPU rasterizer) by default because a broken driver
(WSL's `dzn`) crashes the whole loader during enumeration.

```bash
mask vk-info      # what am I running against?
mask vk-list      # options
mask vk-use system / lavapipe / radeon / <path to icd.json>
```

`mask vk-use` writes `.env` (gitignored, direnv-watched). A test that fails
*only* under lavapipe is more likely a software-rasterizer limitation than a
compiler bug — say so rather than "fixing" the compiler.

## Environment variables

| Variable | Effect |
|---|---|
| `HLSL_WT` | act on this worktree, as if `--in` had been passed |
| `HLSL_LLVM`, `HLSL_DXC`, `HLSL_OFFLOAD`, `HLSL_GOLDEN` | default dependencies |
| `HLSL_MODE` | `standalone` (default) or `integrated` for offload worktrees |
| `HLSL_BUILD_TYPE`, `HLSL_BUILD_DIR` | build type / build directory |
| `HLSL_DIST_PREFIX` | an already-installed LLVM distribution to build against |
| `HLSL_VK_DRIVER` | Vulkan ICD selection |

## Changing build flags

CMake flags are *not* in `scripts/hlsl-dev.sh`; they are templates in
`flake.nix` (`llvmCMakeFlags`, `llvmDistCMakeFlags`, `offloadCMakeFlags`,
`dxcCMakeFlags`) with `$HD_*` placeholders filled in per invocation. Edit them
there so every worktree picks them up on its next configure. Placeholders must
stay free of whitespace and shell metacharacters — the expander rejects them on
purpose.

## Working on the submodules

These are upstream projects; follow *their* conventions, not this repository's:

- LLVM/Clang: LLVM coding standards, `clang-format` on the diff, commit subjects
  like `[HLSL] …`, tests under `clang/test/…` or `llvm/test/…` alongside the
  change.
- DXC: its own style and `test/` layout.
- offload-test-suite: `.test` files plus YAML data; check golden images when
  output changes.
- Keep unrelated formatting churn out of the diff; these trees are huge and
  reviewers are upstream.

## Notes

- `offloader-scripts/` has its own `maskfile.md` (`mask monitor`, `mask triage`,
  `mask site`) and is stdlib-only Python; run its tests with `mask test` from
  inside that directory.
