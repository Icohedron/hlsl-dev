# AGENTS.md — HLSL workspace agent instructions

Use `README.md` for the user-facing guide. This is the operational contract for
agents working in the workspace and its submodules/worktrees.

## Workspace and environment

This is a Nix-powered *workspace*, not a compiler source tree. Source lives in
`llvm-project/` (Clang/LLVM HLSL), `DirectXShaderCompiler/` (DXC), and
`offload-test-suite/`; `offload-golden-images/` holds reference images.
`wg-hlsl/`, `tc57/` and `hlsl-specs/` hold HLSL proposals/specifications.
`compiler-explorer/` is wired to locally built compilers. Worktrees such as
`llvm-project.feature/` and `offload-test-suite.feature/` are gitignored and
have independent build trees. `scripts/hlsl_cli/` implements the single
checkout-run Python **`hlsl`** CLI; `scripts/hlsl.py` is its entry point.
`devenv.nix` supplies packages, environment, CMake flag templates, CLI
registration and container settings. `scripts/cross/toolchains.nix` defines
on-demand cross toolchains. `offloader-scripts/` has independent CI tools.

Check `echo "$DEVENV_ROOT"`. Enter with `devenv shell` or activate `.envrc`
with direnv; tasks and CMake flag templates depend on devenv. The dev
container forces D3D12 off in `build/`; D3D12-on builds use
`build-d3d12/` on either side. A host with D3D12 off shares `build/`. Run
`hlsl --help` or a subcommand's `--help` to verify current flags; the old
`hlsl-*` task executables were retired together. Bare `hlsl` prints a command
index.

## Golden rules

1. **Use `hlsl` for workspace operations**, not raw `cmake`, `ninja` or
   `llvm-lit`. The CLI resolves dependencies, CMake flags and build directory,
   installs prerequisites, and holds build locks. Inspect expensive operations
   with `--dry-run`; `--no-auto` refuses implicit prerequisites where supported.
2. **Select your checkout.** Commands act on the enclosing source worktree or
   accept `--in <worktree>`. From the workspace root pass `--in` for build,
   test and info. `hlsl list` inventories all worktrees; `hlsl info --in X`
   reports selection without writing. Prefer a specific `hlsl build clang`,
   `hlsl test clang-vk PATH`, or `hlsl lit PATH` to a full test umbrella.
3. **Isolate worktrees.** Build, test, reconfigure, trim and clean only your
   selected checkout. Same-tree builds serialize with locks; standalone
   offload worktrees sharing an LLVM distribution also share its prefix lock.
   Do not reset another agent's tree. Preview
   `hlsl clean --dry-run` before removing artifacts. `compile_commands.json`
   is a local clangd link, not a tracked source file.
4. **Commit in the owning submodule/worktree.** Do not commit in the workspace
   root unless explicitly requested: root status shows modified submodule
   pointers, and such a commit records them. Run Git in the actual worktree.
5. **Fresh start is explicit.** New workspaces need no migration. Existing
   legacy pins/GPU/licence/toolchain state or CodeGraph indexes block new
   builds/tests and other state-dependent mutations. Run
   `hlsl workspace migrate --dry-run`, review the exact
   paths/conflicts, then `hlsl workspace migrate --yes`. It preserves source,
   configured build trees and finished archives. Resolve preflight conflicts
   instead of deleting tracked `.gitignore` changes by hand. CodeGraph is
   retired; do not index or restore its managed block as a new workflow.

## Command patterns

```bash
hlsl setup                            # shallow recursive submodules
hlsl workspace update                 # advance remote submodules; keep full history
hlsl list
hlsl info --in llvm-project
hlsl configure --in offload-test-suite --llvm llvm-project --dxc nix
hlsl build clang --in llvm-project --dry-run
hlsl test clang-vk Feature/HLSLLib/log2.32.test --in offload-test-suite
hlsl lit clang/test/CodeGenHLSL --in llvm-project
hlsl distribution install --in llvm-project --dry-run
hlsl trim --in llvm-project --dry-run
hlsl clean --in llvm-project --dry-run
```

`hlsl distribution install --in X` uses only an already-configured, validated
LLVM or standalone offload build tree: it builds its install targets without
changing selections, invoking configure or provisioning dependencies (CMake
may regenerate its build files). Configure that tree first.
`hlsl configure --reset` clears **new** saved choices for a selected worktree;
it does not import old pins. Dependencies resolve from explicit flags,
`HLSL_LLVM`/`HLSL_DXC`/`HLSL_OFFLOAD`/`HLSL_GOLDEN`, saved new JSON choices,
matching branch worktrees, then the root submodule. Name matching branches to
pair worktrees without flags; otherwise choose dependencies explicitly. A
standalone offload worktree needs an LLVM distribution, DXC/dxv and golden
images. `--dxc nix` selects host devenv tools, not Windows cross tools. A
missing distribution can trigger an expensive LLVM install. Do not assume a
filtered test is cheap: it may first build test dependencies.

**Preserved configured trees need explicit revalidation after migration.** Use
`hlsl configure --in <llvm> --offload <offload> --dxc <dxc> --platform native`
or `hlsl configure --in <offload> --llvm <llvm> --dxc <dxc> --platform native`;
for DXC, use `--platform native --build-type TYPE`. Replace `native` with the
old tree's cross platform, and preserve external `--dist-prefix` where used.
Inspect with `hlsl info`/`--dry-run` first. A fresh/unconfigured tree can
auto-configure after migration. If a preserved distribution, DXC or host
native-tools tree requires separate revalidation, follow the CLI diagnostic
rather than rebuilding over old artifacts. New JSON selections are stored
under `.hlsl-dev/selections/`, isolated by worktree/platform/D3D12 mode;
cross choices fall back to saved native choices.

## GPU and cross execution

`hlsl gpu vulkan status` inspects the current ICD. Lavapipe is the default;
`hlsl gpu vulkan list` enumerates choices; `hlsl gpu vulkan use system` selects loader
discovery. `hlsl gpu vulkan --export` emits quoted loader variables for `eval` in
a shell that needs raw Vulkan or independent offloader tools. `--vulkan-driver DRIVER` on test
or build is a one-call override. A lavapipe-only test failure is not proof of
a compiler bug. `hlsl gpu d3d12 status|on|off --in X` inspects/changes the
workspace-wide choice without reconfiguring either build tree;
`--d3d12 on|off` on applicable commands overrides only that call. D3D12
runtime tests need Windows or WSL, not ordinary Linux.

`hlsl cross list` describes `linux-arm64`, `linux-x64`, `windows-x64`, and
`windows-arm64` (excluding this machine's native platform). Use
`hlsl cross fetch PLATFORM --dry-run` before fetching toolchains;
`hlsl build TARGET --platform PLATFORM` builds in a separate tree and may
refresh LLVM native tablegens. Cross-built binaries cannot run in `hlsl test`/`hlsl lit` here.
Windows SDK use requires reading its licence and setting
`HLSL_MSVC_LICENSE=accepted` explicitly for the command; old recorded
acceptance is **not** imported. DXC cross builds for Windows require a DIA SDK
copied from a Visual Studio installation (`HLSL_DIA_SDK`), unless using
appropriate target DXC binaries. **Windows execution is unverified** unless
actually run on the target; never claim target pass results from a cross build.

## Portable outputs and ancillary commands

```bash
hlsl package full --in offload-test-suite --dry-run
hlsl package dxc --in DirectXShaderCompiler --dry-run
hlsl package compiler --in llvm-project --dry-run
hlsl package precompiled clang-vk --in offload-test-suite --dry-run
hlsl package repro Feature/HLSLLib/log2.32.test --suite clang-vk --in offload-test-suite --dry-run
hlsl format --in llvm-project --since main --diff
hlsl tools explorer --llvm llvm-project --dxc nix --dry-run
```

`full` stages installed compiler, configured suites and golden images;
`dxc` is a separate curated archive to avoid Clang/DXC header collisions;
`compiler` omits offload suites. `precompiled` compiles shaders on the host,
shipping runtime and verdicts: target needs Python and GPU driver, not a
compiler or DXC. `repro` ships named tests and provenance with Python-free
`run.sh`/`run.cmd` when faithful; unsupported RUN semantics trigger a
lit/Python 3 fallback for **all** selected tests, documented in the archive.
Neither skips selected tests silently. Archives for Windows are not proven to
run until tested on Windows. Use `--out` for a chosen archive path.

`hlsl format` checks changed-line formatting; its Git hook is warning-only,
installed explicitly with `--install-hooks`. `hlsl tools explorer` starts a
foreground local service only when invoked. `hlsl clean --all --dry-run`
previews a multi-worktree sweep; examine selection before execution. Direct
Git replaces retired shallow-history/refspec wrappers; use `wt` or Git
worktree commands directly. `sccache --start-server` replaces the old start
wrapper. For current CLI options consult `hlsl <command> --help`.

## Changing code, environment and tests

For **every code change** in this workspace, a submodule or a worktree, read
and follow **both** `llvm-project/llvm/docs/CodingStandards.md` and
`llvm-project/llvm/docs/ProgrammersManual.md`. Apply relevant language rules
and local conventions where they do not specify a rule. In a code review,
every finding citing one of these documents must identify the violated rule
by section heading or line number(s) **and** locate the offending code.

For **every HLSL-related change**, consult relevant text in **all three**
`wg-hlsl/`, `tc57/`, and `hlsl-specs/`. `tc57/` supersedes `hlsl-specs/` on
conflict, but earlier material can still be relevant. Every review finding
claiming a spec/proposal inconsistency must cite a source link or line
number(s) **and** the offending code's location.

LLVM/Clang changes follow upstream style (`[HLSL] …` commit subjects,
`clang/test/…` or `llvm/test/…` tests, changed-line `hlsl format`); DXC
follows its own style and `test/` layout; offload tests use `.test` and YAML
with golden images where output changes. Avoid unrelated formatting churn.
CLI orchestration is stdlib Python in `scripts/hlsl_cli/`; tests are in
`scripts/tests/`. Change CMake flag templates and package/environment
configuration in `devenv.nix`, not in a checkout's build cache. Build actions
are exercised via `hlsl`; `devenv test` runs developer checks without building
a compiler. `.devcontainer/devcontainer.json` is generated from
`devcontainer.settings` in `devenv.nix`: edit the source setting and commit
its regenerated output, never hand-edit the generated file.

`offloader-scripts/` is separate stdlib Python with `offloader-monitor`,
`offloader-triage`, `offloader-site` and `offloader-test`; its tests are not
part of the main CLI's `devenv test`. Its GitHub token is declared in
`secretspec.toml`; use `GH_TOKEN`/`GITHUB_TOKEN` or the secrets provider, not
`devenv.nix`, an `env` file, a build cache, or an archive. Nix would store
secrets in plaintext. Workspace GPU choices are not secrets.

## Agent documentation pointers

- **Issue tracking and local `.scratch/` tickets:** `docs/agents/issue-tracker.md`.
- **Triage label rules:** `docs/agents/triage-labels.md`.
- **Domain terminology or ADR changes:** root `CONTEXT.md`, `docs/adr/` and
  `docs/agents/domain.md` (single-context policy).
