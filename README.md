# HLSL developer workspace

This is a Nix/devenv workspace for LLVM/Clang HLSL development, Microsoft's
DirectXShaderCompiler (DXC), and GPU offload tests. Compiler sources live in Git
submodules and their worktrees; this repository supplies the environment and the
single checkout-run **`hlsl`** Python command. Run `hlsl --help` or
`hlsl <command> --help` for the current options. The old `hlsl-*` commands are
retired; `offloader-*` is a separate toolset and is unaffected.

## Quickstart (new checkout)

```bash
devenv shell                         # toolchain and hlsl on PATH
hlsl setup                           # shallow, recursive submodule initialization
hlsl list                            # worktrees and build state
hlsl build clang --in llvm-project --dry-run
hlsl build clang --in llvm-project  # configures as necessary; can take a while
```

A fresh workspace with **no legacy state needs no migration**. A command acts
on the source checkout containing your current directory, or on the selected
`--in <worktree>` (path, directory name, worktree suffix, or branch). From the
workspace root, pass `--in` to build, test, or inspect one checkout.
`hlsl info --in llvm-project` reports the effective build directory,
dependencies and platform without changing anything. `--dry-run` previews operations without
building or modifying the workspace; `--no-auto` refuses implicit prerequisites
on commands that support it. Prefer one target/test over the default umbrella
build; a missing LLVM distribution or DXC can be expensive. Use `--jobs N` to
limit parallelism, especially under container process limits.

To activate automatically when entering the directory, install a direnv shell
hook and run `direnv allow`: the included `.envrc` uses devenv. Or use
`devenv shell` explicitly. Check `echo "$DEVENV_ROOT"` before troubleshooting
missing flags or tools. The dev container uses the same environment; its
`build-container` trees are separate from host `build` trees. Change container
configuration in `devcontainer.settings` in `devenv.nix`, not the generated
`.devcontainer/devcontainer.json`.

## Existing workspace: explicit one-time migration

The new CLI does **not** read/import old Bash pins, GPU selections, MSVC licence
acceptance, toolchain roots or CodeGraph indexes. Read-only commands and
previews do not delete them. Legacy state blocks new builds/tests and other
state-dependent mutations. In an existing workspace, do this deliberately:

```bash
hlsl workspace migrate --dry-run   # inventory exact paths and conflicts; no writes
hlsl workspace migrate --yes       # confirm only after reviewing the preview
```

The migration preflights Git worktrees, managed `.gitignore` blocks and build
locks. If it reports dirty worktrees, unknown files, or active locks, resolve
the reported conflicts before rerunning; do not force-reset a worktree or
manually delete an index to bypass preflight. It removes named old workspace
state (including pins/settings/old toolchains), CodeGraph indexes and only
recognizable managed ignore blocks. **Source checkouts, build trees and finished
archives are preserved.** Repeating it after an interruption is safe; a fresh
workspace is ready without running it. CodeGraph is retired, not replaced.

**Preserved configured build trees require explicit revalidation** before
`hlsl build`/`hlsl test` can reuse them. Supply the actual intended dependencies
and the platform, even for the native platform:

```bash
hlsl configure --in llvm-project --offload offload-test-suite --dxc nix --platform native
hlsl configure --in offload-test-suite --llvm llvm-project --dxc nix --platform native
hlsl configure --in DirectXShaderCompiler --platform native --build-type RelWithDebInfo
# Example of a preserved cross tree (use a target-compatible DXC, not host nix DXC):
hlsl configure --in offload-test-suite --llvm llvm-project --dxc /path/to/windows-dxc/bin --platform windows-x64
```

For LLVM, select `--offload`, `--dxc` and `--platform`; for standalone offload,
`--llvm`, `--dxc` and `--platform`; for DXC, `--platform` and `--build-type`.
Use the values the old build actually used (including `--dist-prefix` when
applicable); inspect first with `hlsl info` and `--dry-run`. An old distribution
or native tablegen tree can separately require explicit revalidation; follow
the CLI's error rather than overwriting a different worktree's artifacts. A
new/unconfigured build tree instead follows normal auto-configure policy after
migration. `hlsl configure --reset --in <worktree>` clears saved new-CLI
choices; it does not import old pins. New choices are kept as JSON under
`.hlsl-dev/selections/` per worktree and platform (cross choices fall back to
native choices). An explicit Windows toolchain fetch may be needed again after
old toolchain roots are removed.

GPU settings also start fresh: Vulkan defaults to lavapipe and D3D12 defaults
to detection. Re-select a real Vulkan ICD with `hlsl gpu vulkan use DRIVER`, and
explicitly accept Microsoft's licence again before fetching a Windows SDK
(see [GPU drivers](#gpu-drivers) and [Cross compilation](#cross-compilation)).

## Command migration reference

| Old executable | New `hlsl` command |
|---|---|
| `hlsl-setup`, `hlsl-update-submodules` | `hlsl setup`, `hlsl workspace update` |
| `hlsl-ls`, `hlsl-info` | `hlsl list`, `hlsl info` |
| `hlsl-configure`, `hlsl-build`, `hlsl-dist` | `hlsl configure`, `hlsl build`, `hlsl distribution refresh` |
| `hlsl-test`, `hlsl-lit` | `hlsl test`, `hlsl lit` |
| `hlsl-clean`, `hlsl-trim` | `hlsl clean`, `hlsl trim` |
| `hlsl-vk`, `hlsl-d3d12`, `hlsl-cross` | `hlsl gpu vulkan status`, `hlsl gpu d3d12`, `hlsl cross list/fetch/refresh` |
| `hlsl-package`, `hlsl-precompile`, `hlsl-repro` | `hlsl package full/dxc/compiler`, `hlsl package precompiled`, `hlsl package repro` |
| `hlsl-format`, `hlsl-compiler-explorer` | `hlsl format`, `hlsl tools explorer` |
| `hlsl-fetch-history`, `hlsl-fetch-refspec`, `hlsl-truncate-history` | Direct Git; no history-truncation wrapper |
| `hlsl-start-sccache`, `hlsl-codegraph` | Direct `sccache --start-server`; CodeGraph retired |

Old command **options are not necessarily identical**. See individual `--help`:
`hlsl test SUITE PATH` selects a test path; `--filter REGEX` selects a lit
filter explicitly. Flags after `--` pass through to lit for `test` and `lit`.
Preview with `hlsl clean --all --dry-run`; a multi-worktree sweep requires
explicit `hlsl clean --all --yes` after reviewing the plan. `--all-build-dirs`,
`--dist` and `--platform all` refine the selection.
`hlsl gpu vulkan list` lists ICDs; `hlsl gpu vulkan --export` prints shell exports.

Retired thin wrappers have direct alternatives: `git -C llvm-project fetch
--unshallow` for a shallow checkout, `git -C llvm-project fetch origin
<refspec>` for another branch, and `git -C llvm-project worktree ...` (or
`wt`) for worktrees. Avoid re-shallowing a full clone; `hlsl workspace update`
preserves full history. Use `sccache --start-server` directly if needed.
`hlsl-codegraph` and its indexes have no replacement command.

## Worktrees and dependencies

```bash
cd llvm-project && wt switch --create texture-store
cd ../offload-test-suite && wt switch --create texture-store
hlsl test clang-vk --in offload-test-suite.texture-store --dry-run
hlsl configure --in offload-test-suite.texture-store --llvm texture-store --dxc nix
```

Worktrees on the same branch name pair automatically; otherwise use selectors
once with `hlsl configure`. For dependencies, command flags override `HLSL_LLVM`,
`HLSL_DXC`, `HLSL_OFFLOAD`, `HLSL_GOLDEN` and saved new-CLI selections, then
the same-branch worktree and finally the submodule checkout are considered.
`--dxc nix` selects the host prebuilt dxc/dxv from devenv; `--dxc` also accepts
an appropriate worktree or directory containing both binaries. Windows cross
builds need **target** DXC, never host `nix` DXC. Standalone offload builds link
against an installed distribution of the selected LLVM worktree, created on
demand unless `--no-auto` or `HLSL_AUTO=0` forbids it. After an LLVM change:

```bash
hlsl distribution refresh --in llvm-project --dry-run
hlsl distribution refresh --in llvm-project
```

Build output stays inside the selected worktree (`build`, `build-container`,
`build.<platform>` and `build-dist[.<platform>]`). The CLI locks a build directory
while using it; separate worktrees have separate build trees, but standalone
offload worktrees sharing an LLVM distribution serialize on its prefix lock.
Never reconfigure/clean someone else's worktree. A native configure links `compile_commands.json` for
clangd; the link is locally excluded from Git. Use `hlsl info --in ...` to see
its target. `hlsl trim --in llvm-project --dry-run` previews unused ELF build
binaries; `hlsl clean --in llvm-project --dry-run` previews a disposable tree.

## HLSL offload tests

An integrated LLVM checkout and a standalone offload checkout both work:

```bash
hlsl test clang-vk --in llvm-project --dry-run          # integrated checkout
hlsl test clang-vk --in offload-test-suite             # standalone checkout
hlsl test clang-vk Feature/HLSLLib/log2.32.test --in offload-test-suite
hlsl test clang-vk --filter 'log2.*' --in offload-test-suite -- --time-tests
hlsl lit clang/test/CodeGenHLSL/RootSignature --in llvm-project
```

`hlsl test` alone builds/runs the full `check-hlsl` umbrella. Suites include
`d3d12`, `vk`, `mtl`, `warp-d3d12`, `clang-d3d12`, `clang-vk`, `clang-mtl`,
`clang-warp-d3d12` and `unit`, where configured and available. A filtered test
first builds test dependencies, then invokes lit; `hlsl lit PATH...` uses the
selected tree's lit on arbitrary paths. `hlsl test` may install an LLVM
distribution and build DXC prerequisites. Preview first; `--no-auto` refuses
missing prerequisites. Native binaries run here; `hlsl test` and `hlsl lit`
refuse a `--platform` cross target. Cross-build here and package to run there.

## GPU drivers

The Vulkan suites execute SPIR-V and need a working ICD. Lavapipe (CPU
rasterizer) is the default to avoid a broken system ICD crashing enumeration.

```bash
hlsl gpu vulkan status              # current choice and manifest
hlsl gpu vulkan list                  # available manifests, system discovery
hlsl gpu vulkan use system                  # choose system loader/real GPU
hlsl gpu vulkan use /absolute/path/icd.json
hlsl test clang-vk --vulkan-driver radeon --in offload-test-suite  # one invocation
```

The choice is stored in `.hlsl-dev/gpu.json`, not the old `settings.env`.
`HLSL_VK_DRIVER` also overrides one invocation. For raw `vulkaninfo` or the
independent offloader tools in a shell, run `eval "$(hlsl gpu vulkan --export)"`
to export/unset `VK_DRIVER_FILES` and `VK_ICD_FILENAMES` for that shell. A
lavapipe-only failure does not establish a compiler defect on a real driver.

D3D12 tests need Windows or WSL D3D12, unavailable on ordinary Linux. Inspect
with `hlsl gpu d3d12 status --in offload-test-suite`; use
`hlsl gpu d3d12 off --in offload-test-suite` to save the choice and reconcile
a configured checkout, or `on` to re-enable detection. Applicable configure/build/test commands also accept
`--d3d12 on|off` for that call; it does not save the override. The CLI
reconciles the build configuration with the selected mode.

## Cross compilation

`hlsl cross list` shows platforms other than this machine's native platform:
`linux-arm64`, `linux-x64`, `windows-x64`, `windows-arm64`. Toolchains are
produced on demand from `scripts/cross/toolchains.nix`; no cross build is
started by entering the shell. Cross trees have separate artifacts and
platform-specific selections, and LLVM cross builds refresh native tablegens
under `build-native-tools`.

```bash
hlsl cross fetch linux-arm64 --dry-run
hlsl cross fetch linux-arm64
hlsl build clang --in llvm-project --platform linux-arm64 --jobs 8
hlsl cross fetch windows-x64 --dry-run
HLSL_MSVC_LICENSE=accepted hlsl cross fetch windows-x64
hlsl build clang --in llvm-project --platform windows-x64 --dry-run
```

Read [Microsoft's SDK licence](https://visualstudio.microsoft.com/license-terms/mt644918/)
before setting `HLSL_MSVC_LICENSE=accepted` **for a command**. The old stored
`hlsl-cross --accept-msvc-license` choice is reset; the new CLI has no
persistent acceptance command. The Windows MSVC SDK provides D3D12 and the
Vulkan import library; the target machine supplies a Vulkan runtime. For
Windows DXC cross builds, copy Microsoft's DIA SDK from a Visual Studio
installation yourself and set `HLSL_DIA_SDK=/path/to/dia-sdk`. It is not in
nixpkgs. Or select prebuilt target DXC/dxv with `--dxc` where applicable.
`hlsl cross refresh PLATFORM` deliberately refreshes an existing toolchain.
Windows **execution is unverified**: a cross build or archive is not evidence
that a test ran successfully on a target machine.

## Portable packages and reproducers

```bash
hlsl package full --in offload-test-suite --dry-run
hlsl package full --in offload-test-suite --platform windows-x64
hlsl package dxc --in DirectXShaderCompiler --platform windows-x64 --dry-run
hlsl package compiler --in llvm-project --dry-run
hlsl package precompiled clang-vk --in offload-test-suite --dry-run
hlsl package repro Feature/HLSLLib/log2.32.test --suite clang-vk --in offload-test-suite --dry-run
```

`full` archives configured offload suites, runtime tools, LLVM compiler and
headers, lit/PyYAML and golden images; a standalone offload package merges its
selected LLVM distribution. `dxc` archives a **separate** curated DXC prefix
(avoid mixing its HLSL headers with Clang's). `compiler` is LLVM tools,
resource headers and lit without offload tests. `--out ARCHIVE` chooses the
output; Windows uses `.zip`, other targets `.tar.gz`. When cross install
artifacts are missing, build the indicated install targets first; package
previews expose the prerequisites and `--no-auto` rejects implicit builds.

`precompiled [SUITE]...` compiles shaders on this host and ships test objects,
runtime and compiler exit verdicts. The **target needs Python 3 and a GPU
driver**, but neither compiler nor DXC. It must have the chosen suites
configured; omitted suite names mean all configured suites. Check the included
`PRECOMPILED.md` and per-suite `precompiled.json` for skipped tests and
verdicts. `repro TEST... --suite SUITE` packs selected tests and provenance:
when the RUN lines can be replayed faithfully, its `run.sh`/`run.cmd` need
only the target shell and GPU driver. Otherwise the whole selection falls back
to bundled lit and PyYAML, **requiring target Python 3**; the preview and
archive `REPRO.md`/`requirements.txt` say which mode and why. The host must
compile every selected test; none is silently omitted. Rejected compiler
outputs retain their original status, important for expected failures/XFAIL.
For all Windows packages, **target execution is unverified** until actually
run on Windows.

## Developer checks and auxiliary tools

```bash
hlsl format --in llvm-project --since main --diff   # inspect changed-line formatting
hlsl format --in llvm-project --fix                # apply to selected diff
hlsl format --install-hooks --in llvm-project       # warning-only Git hook
hlsl tools explorer --llvm llvm-project --dxc nix --dry-run
hlsl tools explorer --llvm llvm-project --dxc nix  # foreground service; never auto-started
```

Run `devenv test` for workspace checks without building a compiler. Direct
`offloader-monitor`, `offloader-triage`, `offloader-site` and `offloader-test`
remain independent; their stdlib-only Python tooling is not part of the new
`hlsl` CLI. Follow their own help for CI monitoring/reporting.
