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

Lock contention shows a spinner on interactive terminals after 0.25 seconds;
redirected output receives one wait notice and an acquisition notice instead.
If a build is stuck or later builds are waiting on its lock, preview
`hlsl builds stop --dry-run`, then run `hlsl builds stop --yes` to stop all
visible, same-user build-related commands in this workspace (across worktrees):
`build`, `configure`, `test`, `distribution install`, `package`, and
`cross fetch/refresh`. It includes queued invocations and their child compiler
processes. This Linux command does not acquire build locks or delete build
artifacts. It cannot identify workers already detached from a terminated
command or builds launched directly with CMake/Ninja; it does not touch
processes from other workspaces.

To activate automatically when entering the directory, install a direnv shell
hook and run `direnv allow`: the included `.envrc` uses devenv. Or use
`devenv shell` explicitly. Check `echo "$DEVENV_ROOT"` before troubleshooting
missing flags or tools. The dev container forces D3D12 off and uses `build/`,
even if a shared GPU choice enables it on the host. A host with D3D12 on uses
`build-d3d12/`; turn it off with `hlsl gpu d3d12 off` to share `build/` with
the container. Sccache uses its per-user default cache location on the host
and inside the container, so the two do not contend for `.sccache` in the
workspace. The container cache does not persist across container rebuilds.
Change container configuration in `devcontainer.settings` in `devenv.nix`,
not the generated `.devcontainer/devcontainer.json`.

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
`.hlsl-dev/selections/` per worktree, platform and D3D12 mode (cross choices
fall back to native choices). An explicit Windows toolchain fetch may be needed again after
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
| `hlsl-configure`, `hlsl-build`, `hlsl-dist` | `hlsl configure`, `hlsl build`, `hlsl distribution install` |
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
demand unless `--no-auto` or `HLSL_AUTO=0` forbids it. From already-configured
LLVM and standalone offload build trees, install their prefixes:

```bash
hlsl distribution install --in llvm-project --dry-run
hlsl distribution install --in llvm-project
hlsl distribution install --in offload-test-suite --dry-run
hlsl distribution install --in offload-test-suite
# Select the other native build tree explicitly:
hlsl distribution install --in llvm-project --d3d12 on --dry-run
# For a custom LLVM build directory (relative to the LLVM worktree):
HLSL_BUILD_DIR=custom-build hlsl distribution install --in llvm-project --dry-run
```

`hlsl distribution install` **only builds install targets** from a validated,
configured build tree: `install-distribution` in LLVM, or
`install-offload-tools` and `install-offload-test-suite` in standalone offload.
The CLI does not invoke configure, change build selections, fetch toolchains
or provision missing LLVM/DXC dependencies. CMake may regenerate its build
files when sources have changed. Run `hlsl configure --in <worktree>` first if
necessary (LLVM before standalone offload). If an existing LLVM cache lacks
`cmake-exports` or LLVM libraries, preview
`hlsl configure --in llvm-project --dxc nix --dry-run`, then explicitly
configure before retrying the install;
install itself will not rewrite the cache. `--in`, `--d3d12 on|off` and
`--platform` select the existing tree; `--jobs` limits parallel work. Preview
with `--dry-run` to verify its build directory and install prefix. A custom
build selected with `HLSL_BUILD_DIR` installs to its own `install/`; standalone
offload builds can select that LLVM prefix with `--dist-prefix` at configure
time. For current options see `hlsl distribution install --help`.

Build output stays inside the selected worktree (`build` with D3D12 off,
`build-d3d12` with D3D12 on, and their `.<platform>` cross variants).
Each LLVM build installs its distribution in its own `install/` directory;
standalone offload builds use the corresponding LLVM mode and platform.
Linux cross builds always use `build.<platform>` because they cannot enable
D3D12. Existing `build-dist[.<platform>]/install` directories are left intact;
select one explicitly with `--dist-prefix` if needed. The CLI locks a build
directory while using it; separate worktrees have separate build trees, but
standalone offload worktrees sharing an LLVM distribution serialize on its
prefix lock.
Never reconfigure/clean someone else's worktree. A native configure links `compile_commands.json` for
clangd; the link is locally excluded from Git. Use `hlsl info --in ...` to see
its target. `hlsl trim --in llvm-project --dry-run` previews unused ELF build
binaries. `hlsl clean --in llvm-project --d3d12 on --dry-run` previews only
the D3D12 build tree; `--d3d12 off` selects the portable tree.
`--all-build-dirs` covers both modes and cannot be combined with `--d3d12`.

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
`hlsl gpu d3d12 off --in offload-test-suite` to save the choice and select
`build/`, or `on` to select `build-d3d12/`. Switching the saved choice never
configures either tree; use `hlsl configure` explicitly if a tree needs it.
Applicable configure/build/test commands also accept `--d3d12 on|off` for that call; the
override selects the matching tree without saving the choice. The container's
`HLSL_D3D12=off` overrides saved choices; unset it before using
`hlsl gpu d3d12 on` inside the container. Existing `build-container/` trees are
preserved but no longer selected automatically; use an explicit
`HLSL_BUILD_DIR` if you need to reuse one.

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
**Target execution is unverified** for any cross build (including Linux ARM64
on x64 and Windows targets): a build or archive does not mean the binaries ran
on the target machine.

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
only the target shell and GPU driver. The caller must choose a machine
satisfying each selected test's `REQUIRES` and not matching `UNSUPPORTED`;
Python-free repros list these assumptions in the preview, `REPRO.md` and
`provenance.json` rather than evaluating them at runtime. Repros omit `XFAIL`
directives, so a known bug reports `FAIL` instead of being treated as expected.
Unsupported RUN semantics or other lit constructs still trigger bundled lit
and PyYAML for all selected tests, **requiring target Python 3**; the preview
and archive `REPRO.md`/`requirements.txt` say which mode and why. The host must
compile every selected test; none is silently omitted. Rejected compiler
outputs retain their original status so failing compiles remain reproducible.
For all non-native packages, **target execution is unverified** until the
archive is actually run on the target. The archive instructions and
`provenance.json` (`target_execution_unverified`) record this without repeating
it in build or package command output.

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
