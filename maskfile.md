# Tasks

Task runner for the HLSL developer workspace. Every task that touches a
checkout is *worktree-aware*: it works out which checkout you mean from the
current directory (or `--in`), which other checkouts it needs (`--llvm`,
`--dxc`, `--offload`, `--golden`), and where the artifacts go.

```
mask ls                                  # what exists, what is built
mask info                                # what would this directory build against?
mask configure && mask build             # build the checkout you are standing in
mask dist                                # llvm: install the standalone distribution
mask test clang-vk                       # run an offload suite
mask link --llvm llvm-project.my-feature # remember a cross-worktree dependency
```

The resolution rules live in `scripts/hlsl-dev.sh`; the CMake flag templates
live in `flake.nix`.

## setup
Initializes the submodules with a shallow clone (`--depth 2`) to save time and disk space.

```bash
git submodule update --init --recursive --depth 2
```

## ls
Lists every known worktree of every repository, with its branch, build state
and any recorded cross-worktree dependencies.

```bash
set -eo pipefail
source "$MASKFILE_DIR/scripts/hlsl-dev.sh"
hd_init

current=$(hd_wt_from "$PWD" || true)

for kind in llvm dxc offload golden; do
    printf '%s\n' "$(hd_kind_label "$kind")"
    while IFS= read -r wt; do
        [ -n "$wt" ] || continue
        marker="  "
        [ "$wt" = "$current" ] && marker="* "

        state=""
        if [ "$kind" != "golden" ]; then
            build=$(hd_build_dir "$wt")
            if [ -f "$build/build.ninja" ]; then
                state="built:$(basename "$build")"
            else
                state="unconfigured"
            fi
            if [ "$kind" = "llvm" ] && [ -f "$(hd_dist_prefix "$wt")/lib/cmake/llvm/LLVMConfig.cmake" ]; then
                state="$state dist:yes"
            fi
        fi

        pins=""
        pinfile=$(hd_pin_file "$wt")
        if [ -f "$pinfile" ]; then
            pins=$(sed -n 's|^\(LLVM\|DXC\|OFFLOAD\|MODE\)=|\1=|p' "$pinfile" |
                sed "s|=$HD_ROOT/|=|" | paste -sd' ')
        fi

        printf '%s%-46s %-22s %-24s %s\n' \
            "$marker" "${wt#"$HD_ROOT"/}" "$(hd_branch "$wt")" "$state" "$pins"
    done <<< "$(hd_worktrees "$kind")"
    printf '\n'
done
```

## info
Shows what the current directory (or `--in <worktree>`) resolves to: the target
worktree, its build directory, and every dependency that a configure would use.

**OPTIONS**
* in
    * flags: --in
    * type: string
    * desc: Worktree to inspect (path, directory name, suffix or branch); defaults to the current directory
* llvm
    * flags: --llvm
    * type: string
    * desc: Override the llvm-project worktree
* dxc
    * flags: --dxc
    * type: string
    * desc: Override the DirectXShaderCompiler worktree, a directory holding dxc/dxv, or `nix`
* offload
    * flags: --offload
    * type: string
    * desc: Override the offload-test-suite worktree
* golden
    * flags: --golden
    * type: string
    * desc: Override the offload-golden-images worktree
* mode
    * flags: --mode
    * type: string
    * desc: For offload worktrees, `standalone` (default) or `integrated`
* dist_prefix
    * flags: --dist-prefix
    * type: string
    * desc: Install prefix of an LLVM standalone distribution to build/test against (e.g. an unpacked CI artifact)

```bash
set -eo pipefail
source "$MASKFILE_DIR/scripts/hlsl-dev.sh"
hd_init

wt=$(hd_target)
kind=$(hd_kind "$wt")

printf '%-16s %s\n' "workspace" "$HD_ROOT"
printf '%-16s %s (%s)\n' "worktree" "$wt" "$(hd_kind_label "$kind")"
printf '%-16s %s\n' "branch" "$(hd_branch "$wt")"
printf '%-16s %s\n' "build type" "$(hd_build_type "$wt")"

case "$kind" in
llvm)
    printf '%-16s %s\n' "build dir" "$(hd_build_dir "$wt")"
    printf '%-16s %s\n' "dist prefix" "$(hd_dist_prefix "$wt")"
    printf '%-16s %s\n' "offload" "$(hd_dep offload "$wt")"
    printf '%-16s %s\n' "golden" "$(hd_dep golden "$wt")"
    printf '%-16s %s\n' "dxc" "$(hd_dxc_bin_dir "$wt")"
    ;;
dxc)
    printf '%-16s %s\n' "build dir" "$(hd_build_dir "$wt")"
    ;;
offload)
    mode=$(hd_mode "$wt")
    llvm=$(hd_dep llvm "$wt")
    printf '%-16s %s\n' "mode" "$mode"
    printf '%-16s %s\n' "build dir" "$(hd_effective_build "$wt")"
    printf '%-16s %s\n' "llvm" "$llvm"
    if [ "$mode" = "standalone" ]; then
        dist=$(hd_dist_prefix "$llvm")
        if [ -f "$dist/lib/cmake/llvm/LLVMConfig.cmake" ]; then
            printf '%-16s %s\n' "llvm dist" "$dist"
        else
            printf '%-16s %s (missing: run `mask dist --in %s`)\n' \
                "llvm dist" "$dist" "$(basename "$llvm")"
        fi
    fi
    printf '%-16s %s\n' "golden" "$(hd_dep golden "$wt")"
    printf '%-16s %s\n' "dxc" "$(hd_dxc_bin_dir "$wt")"
    ;;
esac

printf '%-16s %s\n' "pins" "$(hd_pin_file "$wt")"
```

## link
Records cross-worktree dependencies for a worktree so later `configure`,
`build` and `test` invocations pick them up without repeating the flags.
Written outside the checkout, so `git status` stays clean.

**OPTIONS**
* in
    * flags: --in
    * type: string
    * desc: Worktree to pin dependencies for; defaults to the current directory
* llvm
    * flags: --llvm
    * type: string
    * desc: llvm-project worktree to build/test against
* dxc
    * flags: --dxc
    * type: string
    * desc: DirectXShaderCompiler worktree, a directory holding dxc/dxv, or `nix`
* offload
    * flags: --offload
    * type: string
    * desc: offload-test-suite worktree to build/test against
* golden
    * flags: --golden
    * type: string
    * desc: offload-golden-images worktree
* mode
    * flags: --mode
    * type: string
    * desc: For offload worktrees, `standalone` (default) or `integrated`
* dist_prefix
    * flags: --dist-prefix
    * type: string
    * desc: Install prefix of an LLVM standalone distribution to build/test against (e.g. an unpacked CI artifact)
* build_type
    * flags: --build-type
    * type: string
    * desc: CMake build type to use for this worktree
* build_dir
    * flags: --build-dir
    * type: string
    * desc: Build directory for this worktree (absolute, or relative to it)

```bash
set -eo pipefail
source "$MASKFILE_DIR/scripts/hlsl-dev.sh"
hd_init

wt=$(hd_target)

# Resolve before recording, so a typo fails here instead of at configure time.
if [ -n "$HD_OPT_LLVM" ]; then hd_pin_set "$wt" LLVM "$(hd_resolve llvm "$HD_OPT_LLVM")"; fi
if [ -n "$HD_OPT_OFFLOAD" ]; then hd_pin_set "$wt" OFFLOAD "$(hd_resolve offload "$HD_OPT_OFFLOAD")"; fi
if [ -n "$HD_OPT_GOLDEN" ]; then hd_pin_set "$wt" GOLDEN "$(hd_resolve golden "$HD_OPT_GOLDEN")"; fi
if [ -n "$HD_OPT_DXC" ]; then hd_pin_set "$wt" DXC "$(hd_dxc_bin_dir "$wt")"; fi
if [ -n "$HD_OPT_MODE" ]; then hd_pin_set "$wt" MODE "$HD_OPT_MODE"; fi
if [ -n "$HD_OPT_BUILD_TYPE" ]; then hd_pin_set "$wt" BUILD_TYPE "$HD_OPT_BUILD_TYPE"; fi
if [ -n "$HD_OPT_BUILD_DIR" ]; then
    HD_WT=$wt
    hd_pin_set "$wt" BUILD_DIR "$(hd_build_dir "$wt")"
fi
if [ -n "$HD_OPT_DIST_PREFIX" ]; then
    # Belongs to the llvm worktree that owns the distribution, not to $wt.
    if [ "$(hd_kind "$wt")" = "llvm" ]; then
        hd_pin_set "$wt" DIST_PREFIX "$(hd_dist_prefix "$wt")"
    else
        hd_pin_set "$(hd_dep llvm "$wt")" DIST_PREFIX "$(hd_dist_prefix "$(hd_dep llvm "$wt")")"
    fi
fi

echo "Pins for $wt:"
sed 's/^/    /' "$(hd_pin_file "$wt")" 2>/dev/null || echo "    (none)"
echo
echo "Note: a configure is needed for a changed dependency to reach the build tree."
```

## unlink
Forgets the recorded dependencies of a worktree, so resolution falls back to
the defaults (same-branch worktree, else the submodule in the workspace root).

**OPTIONS**
* in
    * flags: --in
    * type: string
    * desc: Worktree to clear; defaults to the current directory

```bash
set -eo pipefail
source "$MASKFILE_DIR/scripts/hlsl-dev.sh"
hd_init

wt=$(hd_target)
hd_pin_clear "$wt"
echo "Cleared pins for $wt"
```

## configure
Configures the worktree you are standing in (or `--in <worktree>`).

* llvm-project     -> LLVM + Clang + OffloadTest in `<worktree>/build`
* DirectXShaderCompiler -> DXC in `<worktree>/build`
* offload-test-suite    -> standalone build in `<worktree>/build`, against the
  LLVM distribution of the resolved llvm-project worktree (`mask dist`);
  with `--mode integrated` it instead configures that llvm worktree to build
  this offload source tree as an external project.

**OPTIONS**
* in
    * flags: --in
    * type: string
    * desc: Worktree to configure; defaults to the current directory
* llvm
    * flags: --llvm
    * type: string
    * desc: llvm-project worktree to build against
* dxc
    * flags: --dxc
    * type: string
    * desc: DirectXShaderCompiler worktree, a directory holding dxc/dxv, or `nix`
* offload
    * flags: --offload
    * type: string
    * desc: offload-test-suite worktree to include (llvm builds)
* golden
    * flags: --golden
    * type: string
    * desc: offload-golden-images worktree
* mode
    * flags: --mode
    * type: string
    * desc: For offload worktrees, `standalone` (default) or `integrated`
* dist_prefix
    * flags: --dist-prefix
    * type: string
    * desc: Install prefix of an LLVM standalone distribution to build/test against (e.g. an unpacked CI artifact)
* build_type
    * flags: --build-type
    * type: string
    * desc: CMake build type (Debug, Release, RelWithDebInfo, MinSizeRel); defaults to RelWithDebInfo
* build_dir
    * flags: --build-dir
    * type: string
    * desc: Build directory (absolute, or relative to the worktree); defaults to `build`
* fresh
    * flags: --fresh
    * desc: Delete the build directory before configuring

```bash
set -eo pipefail
source "$MASKFILE_DIR/scripts/hlsl-dev.sh"
hd_init

wt=$(hd_target)
HD_WT=$wt
hd_configure "$wt"
```

## build [target]
Builds the worktree you are standing in (or `--in <worktree>`), configuring it
first if needed.

**OPTIONS**
* in
    * flags: --in
    * type: string
    * desc: Worktree to build; defaults to the current directory
* llvm
    * flags: --llvm
    * type: string
    * desc: llvm-project worktree to build against (used if a configure is needed)
* dxc
    * flags: --dxc
    * type: string
    * desc: DirectXShaderCompiler worktree, a directory holding dxc/dxv, or `nix`
* offload
    * flags: --offload
    * type: string
    * desc: offload-test-suite worktree to include (llvm builds)
* golden
    * flags: --golden
    * type: string
    * desc: offload-golden-images worktree
* mode
    * flags: --mode
    * type: string
    * desc: For offload worktrees, `standalone` (default) or `integrated`
* build_type
    * flags: --build-type
    * type: string
    * desc: CMake build type used if a configure is needed
* build_dir
    * flags: --build-dir
    * type: string
    * desc: Build directory (absolute, or relative to the worktree)
* fresh
    * flags: --fresh
    * desc: Reconfigure from scratch before building

```bash
set -eo pipefail
source "$MASKFILE_DIR/scripts/hlsl-dev.sh"
hd_init

wt=$(hd_target)
HD_WT=$wt
if [ -n "$HD_OPT_FRESH" ]; then hd_configure "$wt"; fi
hd_build "$wt" ${target:+"$target"}
```

## dist
Builds and installs the LLVM half of the standalone offload distribution for an
llvm-project worktree: Clang, the lit tooling and the LLVM libraries that the
offload tools link against, installed into `<worktree>/build-dist/install`.

This is the prefix that standalone `offload-test-suite` builds consume, so one
`mask dist` serves every offload worktree pointed at this llvm worktree. See
`offload-test-suite/docs/offload-distribution.md` ("Standalone Build
Distribution").

**OPTIONS**
* in
    * flags: --in
    * type: string
    * desc: llvm-project worktree to build the distribution from; defaults to the current directory
* offload
    * flags: --offload
    * type: string
    * desc: offload-test-suite worktree providing StandaloneDistribution.cmake
* dist_prefix
    * flags: --dist-prefix
    * type: string
    * desc: Install prefix of an LLVM standalone distribution to build/test against (e.g. an unpacked CI artifact)
* build_type
    * flags: --build-type
    * type: string
    * desc: CMake build type for the distribution build; defaults to Release
* fresh
    * flags: --fresh
    * desc: Delete the distribution build directory first

```bash
set -eo pipefail
source "$MASKFILE_DIR/scripts/hlsl-dev.sh"
hd_init

wt=$(hd_target llvm)
HD_WT=$wt
hd_dist "$wt"
```

## test [suite] [filter]
Runs offload tests for the worktree you are standing in (or `--in`).

* `mask test` — the whole `check-hlsl` umbrella target
* `mask test clang-vk` — one suite, via its `check-hlsl-clang-vk` target
  (so its dependencies are rebuilt first)
* `mask test clang-vk Feature/HLSLLib` — a subdirectory or a single `.test`
  file inside the suite
* `mask test clang-vk 'log2.*'` — anything that is not a path is handed to
  lit's `--filter` as a regular expression
* `mask test clang-vk --dxc DirectXShaderCompiler.my-fix` — retarget this build
  tree at another DXC before running (cheap: no recompilation)

Suites: d3d12, vk, mtl, warp-d3d12, clang-d3d12, clang-vk, clang-mtl,
clang-warp-d3d12.

**OPTIONS**
* in
    * flags: --in
    * type: string
    * desc: Worktree whose build tree runs the tests; defaults to the current directory
* dxc
    * flags: --dxc
    * type: string
    * desc: DirectXShaderCompiler worktree, a directory holding dxc/dxv, or `nix`
* llvm
    * flags: --llvm
    * type: string
    * desc: llvm-project worktree (offload worktrees)
* golden
    * flags: --golden
    * type: string
    * desc: offload-golden-images worktree
* mode
    * flags: --mode
    * type: string
    * desc: For offload worktrees, `standalone` (default) or `integrated`
* dist_prefix
    * flags: --dist-prefix
    * type: string
    * desc: Install prefix of an LLVM standalone distribution to build/test against (e.g. an unpacked CI artifact)
* build_dir
    * flags: --build-dir
    * type: string
    * desc: Build directory (absolute, or relative to the worktree)
* lit_args
    * flags: --lit-args
    * type: string
    * desc: Extra arguments forwarded to llvm-lit for filtered runs (default `-v`)

```bash
set -eo pipefail
source "$MASKFILE_DIR/scripts/hlsl-dev.sh"
hd_init

wt=$(hd_target llvm offload)
HD_WT=$wt
hd_ensure_configured "$wt"
build=$(hd_effective_build "$wt")

# `--dxc` retargets the configured build tree; DXC only feeds the lit
# configuration, so this regenerates in seconds and compiles nothing.
if [ -n "$HD_OPT_DXC" ]; then
    hd_sync_dxc "$build" "$(hd_dxc_bin_dir "$wt")"
    hd_pin_set "$wt" DXC "$(hd_dxc_bin_dir "$wt")"
fi

suite=${suite:-}
filter=${filter:-}

if [ -n "$suite" ]; then
    case " $HD_SUITES unit " in
    *" $suite "*) ;;
    *) hd_die "unknown suite '$suite'; expected one of: $HD_SUITES unit" ;;
    esac
fi

if [ -z "$filter" ]; then
    hd_build "$wt" "check-hlsl${suite:+-$suite}"
    exit 0
fi

[ -n "$suite" ] || hd_die "a filter needs a suite: mask test <suite> <path-or-regex>"

root=$(hd_test_root "$wt" "$build")
[ -d "$root/$suite" ] || hd_die "suite '$suite' is not configured in $build"
src=$(hd_suite_src "$root/$suite")

# Bring the tools the tests invoke up to date, then hand the selection to lit.
hd_build "$wt" hlsl-test-depends

if [ -n "$src" ] && [ -e "$src/test/$filter" ]; then
    # lit resolves a build-tree path back to the source tree, so subdirectories
    # that only exist in the sources can be addressed this way.
    hd_lit "$build" ${lit_args:--v} "$root/$suite/$filter"
else
    hd_lit "$build" ${lit_args:--v} --filter "$filter" "$root/$suite"
fi
```

## lit (path)
Runs the worktree's `llvm-lit` on an arbitrary path — clang/LLVM regression
tests, or offload tests addressed through the build tree.

Paths are taken as given (relative to the current directory), so
`mask lit clang/test/CodeGenHLSL` works from inside an llvm worktree.

**OPTIONS**
* in
    * flags: --in
    * type: string
    * desc: Worktree whose build tree provides llvm-lit; defaults to the current directory
* build_dir
    * flags: --build-dir
    * type: string
    * desc: Build directory (absolute, or relative to the worktree)
* lit_args
    * flags: --lit-args
    * type: string
    * desc: Extra arguments for llvm-lit (default `-v`)

```bash
set -eo pipefail
source "$MASKFILE_DIR/scripts/hlsl-dev.sh"
hd_init

wt=$(hd_target)
HD_WT=$wt
build=$(hd_effective_build "$wt")
hd_lit "$build" ${lit_args:--v} "$path"
```

## codegraph
Builds or refreshes the [CodeGraph](https://github.com/colbymchenry/codegraph)
index of a worktree, so that codegraph-aware agents can answer structural
questions about it. Works on `llvm-project`, `DirectXShaderCompiler` and
`offload-test-suite` checkouts.

Each repository has its own scope, in `scripts/codegraph-<kind>.json`, copied
into the worktree as `codegraph.json`:

| repository | indexed | left out |
| --- | --- | --- |
| llvm-project | `clang/`, `llvm/` | `test/` corpora, docs, bindings, the other 20 subprojects |
| DirectXShaderCompiler | `lib/`, `include/`, `tools/`, `utils/`, `projects/`, `unittests/` | `test/`, `tools/clang/test/`, `external/`, docs, CI |
| offload-test-suite | everything | `third-party/`, docs, CI |

A worktree that has no index yet starts from a copy of another worktree's
database and then re-parses only what its branch changed -- seconds instead of
the several minutes a cold index takes. Both `codegraph.json` and `.codegraph/`
are added to the clone's `info/exclude`, so they never show up in `git status`
and are never committed.

The one exception is `.gitignore`, which is tracked upstream. CodeGraph's
built-in ignore list drops every directory called `target` or `coverage`
(case-insensitively), which would hide all of `llvm/lib/Target` -- the DirectX
and SPIR-V backends included -- and nothing in `codegraph.json` can override a
built-in default; only a negation in the root `.gitignore` can. So this task
appends a small marked block there and marks the file `skip-worktree`, which
keeps the edit out of `git status` and out of every commit.

That hidden edit has exactly one failure mode: a `git checkout`, `rebase` or
`pull` that wants to change `.gitignore` *itself* will refuse to run, with
`Your local changes to the following files would be overwritten by checkout:
.gitignore` (or, from a merge/rebase, `Entry '.gitignore' not uptodate. Cannot
merge.`). **That** is what `--restore-gitignore` is for -- it puts the file
back exactly as git has it and unhides it, so the git operation goes through:

```bash
mask codegraph --restore-gitignore   # .gitignore is now pristine and visible again
git rebase origin/main               # ... the operation git was refusing
mask codegraph                       # re-applies the block, refreshes the index
```

It is not needed in day-to-day work, and it does not touch the index.

```
mask codegraph                                  # index/refresh the current worktree
mask codegraph --in llvm-project.my-feature     # ... or a named one
mask codegraph --in DirectXShaderCompiler       # dxc and the offload suite work too
mask codegraph --fresh                          # rebuild from scratch
mask codegraph --all                            # every worktree of every repository
mask codegraph --restore-gitignore              # unblock a git op that wants .gitignore
```

**OPTIONS**
* in
    * flags: --in
    * type: string
    * desc: Worktree to index; defaults to the current directory
* from
    * flags: --from
    * type: string
    * desc: Worktree to seed a missing index from; must be the same repository (default: whichever worktree of that repository already has one)
* fresh
    * flags: --fresh
    * desc: Rebuild the index from scratch instead of seeding/syncing
* all
    * flags: --all
    * desc: Index every llvm-project, DirectXShaderCompiler and offload-test-suite worktree
* restore_gitignore
    * flags: --restore-gitignore
    * desc: Put .gitignore back exactly as git has it, and stop hiding it; use this if a checkout or rebase refuses to touch .gitignore

```bash
set -eo pipefail
source "$MASKFILE_DIR/scripts/hlsl-dev.sh"
hd_init

# Every repository the index knows how to scope: one codegraph-<kind>.json each.
kinds="llvm dxc offload"

if [ -n "${restore_gitignore:-}" ]; then
    if [ -n "${all:-}" ]; then
        for kind in $kinds; do
            while IFS= read -r wt; do
                [ -n "$wt" ] || continue
                hd_codegraph_ungitignore "$wt"
            done <<< "$(hd_worktrees "$kind")"
        done
    else
        hd_codegraph_ungitignore "$(hd_target $kinds)"
    fi
    exit 0
fi
if [ -n "${all:-}" ]; then
    [ -z "${from:-}" ] ||
        hd_die "--from names one worktree of one repository; it cannot be combined with --all"
    for kind in $kinds; do
        # Each repository seeds its own worktrees from the first index in it.
        donor=""
        while IFS= read -r wt; do
            [ -n "$wt" ] || continue
            hd_codegraph "$wt" "$donor"
            [ -n "$donor" ] || donor=$wt
        done <<< "$(hd_worktrees "$kind")"
    done
else
    wt=$(hd_target $kinds)
    donor=""
    [ -z "${from:-}" ] || donor=$(hd_resolve "$(hd_kind "$wt")" "$from")
    hd_codegraph "$wt" "$donor"
fi
```

## clean
Removes the build directory of a worktree (and, with `--dist`, the standalone
distribution build and install prefix of an llvm worktree).

**OPTIONS**
* in
    * flags: --in
    * type: string
    * desc: Worktree to clean; defaults to the current directory
* build_dir
    * flags: --build-dir
    * type: string
    * desc: Build directory to remove (absolute, or relative to the worktree)
* dist
    * flags: --dist
    * desc: Also remove the standalone distribution build and install prefix

```bash
set -eo pipefail
source "$MASKFILE_DIR/scripts/hlsl-dev.sh"
hd_init

wt=$(hd_target)
HD_WT=$wt
build=$(hd_build_dir "$wt")
if [ -d "$build" ]; then
    echo "Removing $build"
    rm -rf "$build"
fi

if [ -n "${dist:-}" ] && [ "$(hd_kind "$wt")" = "llvm" ]; then
    for d in "$(hd_dist_build_dir "$wt")" "$(hd_dist_prefix "$wt")"; do
        [ -d "$d" ] || continue
        echo "Removing $d"
        rm -rf "$d"
    done
fi
```

## configure-llvm [build_type]
Deprecated alias for `mask configure --in llvm-project`, kept so existing
scripts and muscle memory keep working.

```bash
set -eo pipefail
$MASK configure --in "${HLSL_REPO_LLVM:-llvm-project}" ${build_type:+--build-type "$build_type"}
```

## build-llvm [target]
Deprecated alias for `mask build --in llvm-project`.

```bash
set -eo pipefail
$MASK build --in "${HLSL_REPO_LLVM:-llvm-project}" ${target:+"$target"}
```

## configure-dxc [build_type]
Deprecated alias for `mask configure --in DirectXShaderCompiler`.

```bash
set -eo pipefail
$MASK configure --in "${HLSL_REPO_DXC:-DirectXShaderCompiler}" ${build_type:+--build-type "$build_type"}
```

## build-dxc [target]
Deprecated alias for `mask build --in DirectXShaderCompiler`.

```bash
set -eo pipefail
$MASK build --in "${HLSL_REPO_DXC:-DirectXShaderCompiler}" ${target:+"$target"}
```

## vk-info
Shows which Vulkan driver (ICD) the offload test suite will run against, and
which device that driver actually exposes. Inside the dev shell this should
report `llvmpipe` (lavapipe) by default, never `dzn`.

```bash
echo "HLSL_VK_DRIVER = ${HLSL_VK_DRIVER:-lavapipe (default)}"
echo "VK_DRIVER_FILES = ${VK_DRIVER_FILES:-<unset> -> loader discovers drivers itself}"
echo
vulkaninfo --summary 2>/dev/null | sed -n '/^Devices:/,$p' ||
    echo "vulkaninfo not available; enter the Nix dev shell first"
```

## vk-list
Lists the driver names accepted by `mask vk-use`.

```bash
if [ -z "${HLSL_VK_ICD_DIR:-}" ]; then
    echo "Not in the Nix dev shell; run 'nix develop' or 'direnv allow' first" >&2
    exit 1
fi

echo "system      let the Vulkan loader discover drivers itself (real GPU)"
echo "lavapipe    Mesa's CPU rasterizer - slow, but always works (default)"
echo
echo "Mesa ICDs in $HLSL_VK_ICD_DIR:"
find "$HLSL_VK_ICD_DIR" -maxdepth 1 -name '*_icd.*.json' -printf '%f\n' |
    sed -e 's/_icd\..*\.json$//' -e 's/^/    /' | sort
echo
echo "An absolute path to any ICD manifest is also accepted."
```

## vk-use (driver)
Switches the Vulkan driver used by the offload test suite. The choice is written
to `.env` (gitignored), which direnv watches, so the shell picks it up on the
next prompt.

**OPTIONS**
* driver (required): `system`, `lavapipe`, a Mesa ICD short name (see `mask vk-list`), or a path to an ICD manifest

```bash
cd "$MASKFILE_DIR"
sed -i '/^HLSL_VK_DRIVER=/d' .env 2>/dev/null || true
echo "HLSL_VK_DRIVER=$driver" >> .env

echo "Set HLSL_VK_DRIVER=$driver in .env"
if [ -n "${DIRENV_DIR:-}" ]; then
    echo "direnv will reload on your next prompt; then run 'mask vk-info' to verify."
else
    echo
    echo "NOTE: .env is loaded by .envrc, so it only applies under direnv."
    echo "Without direnv, pass the variable explicitly instead:"
    echo "    HLSL_VK_DRIVER=$driver nix develop"
fi
```

## fetch-history (repo)
Fetches the full commit history of a specific submodule for when you need to rebase, branch off older commits, or create pull requests.

**OPTIONS**
* repo (required): Name of the submodule (e.g., llvm-project, DirectXShaderCompiler)

```bash
cd "$MASKFILE_DIR/$repo" && git fetch --unshallow || git fetch --all
```

## truncate-history (repo)
Truncates the commit history of a specific submodule back to a shallow depth of 2 to save disk space after you are done needing the full history.

**OPTIONS**
* repo (required): Name of the submodule (e.g., llvm-project, DirectXShaderCompiler)

```bash
cd "$MASKFILE_DIR/$repo" && git fetch --depth 2 && git reflog expire --expire=now --all && git gc --prune=now
```

## fetch-refspec [username]
Sets `remote.origin.fetch` of the checkout you are standing in (or `--in
<worktree>`) so that `origin` tracks `main` plus, if a username is given, that
user's `users/<username>/*` branches.

Submodules are cloned single-branch, so `origin` only ever fetches
`refs/heads/main`; anything referring to `origin/users/...` (for example `gh
stack checkout` on a pull request whose branch lives in the upstream repository)
then fails with *not a valid object name*. Widening the refspec fixes that
without pulling in the thousands of other branches these repositories carry.

Without a username the refspec is reset to `main` only. Git worktrees share the
configuration of their repository, so this applies to every worktree of that
submodule.

**OPTIONS**
* in
    * flags: --in
    * type: string
    * desc: Worktree whose `origin` to reconfigure; defaults to the current directory
* fetch
    * flags: --fetch
    * desc: Fetch from `origin` afterwards, so the new refs are available right away

```bash
set -eo pipefail
source "$MASKFILE_DIR/scripts/hlsl-dev.sh"
hd_init

wt=$(hd_target)

case "${username:-}" in
*[!A-Za-z0-9._-]*) hd_die "invalid username '$username'" ;;
esac

# --unset-all exits 5 when there is nothing to unset; that is not an error here.
git -C "$wt" config --unset-all remote.origin.fetch || true
git -C "$wt" config --add remote.origin.fetch \
    '+refs/heads/main:refs/remotes/origin/main'
if [ -n "${username:-}" ]; then
    git -C "$wt" config --add remote.origin.fetch \
        "+refs/heads/users/$username/*:refs/remotes/origin/users/$username/*"
fi

echo "remote.origin.fetch for $wt:"
git -C "$wt" config --get-all remote.origin.fetch | sed 's/^/    /'

if [ -n "${fetch:-}" ]; then
    echo
    git -C "$wt" fetch --prune origin
else
    echo
    echo "Run 'git fetch origin' in the checkout to pick the new refs up."
fi
```

## update-submodules
Updates all submodules to the latest commits on their respective default remote branches (e.g., main or master).

Submodules that already have their full history (e.g. after `mask fetch-history`) are
updated with a full fetch so the history is preserved; only shallow or not-yet-cloned
submodules are fetched with `--depth 2`.

```bash
set -e
cd "$MASKFILE_DIR"

paths=$(git config -f .gitmodules --get-regexp '^submodule\..*\.path$' | cut -d' ' -f2-)

for path in $paths; do
    if [ -e "$path/.git" ] &&
       [ "$(git -C "$path" rev-parse --is-shallow-repository 2>/dev/null)" = "false" ]; then
        depth=""
        echo "==> $path: full history detected, updating without truncating"
    else
        depth="--depth 2"
        echo "==> $path: shallow, updating with --depth 2"
    fi

    git submodule update --init --recursive $depth -- "$path"
    git submodule update --remote --recursive $depth -- "$path"
done
```

## compiler-explorer
Runs Compiler Explorer with local DXC, clang, and clang-dxc compilers, taken
from the resolved worktrees (`--llvm` / `--dxc`, defaulting to the submodules in
the workspace root).

**OPTIONS**
* llvm
    * flags: --llvm
    * type: string
    * desc: llvm-project worktree whose build provides clang / clang-dxc
* dxc
    * flags: --dxc
    * type: string
    * desc: DirectXShaderCompiler worktree, a directory holding dxc/dxv, or `nix`

```bash
set -eo pipefail
source "$MASKFILE_DIR/scripts/hlsl-dev.sh"
hd_init

llvm_wt=$(hd_dep llvm "")
llvm_bin="$(hd_build_dir "$llvm_wt")/bin"
dxc_bin=$(hd_dxc_bin_dir "")

HLSL_LOCAL="$MASKFILE_DIR/compiler-explorer/etc/config/hlsl.local.properties"

cat > "$HLSL_LOCAL" <<EOF
compilers=&dxc:&clang

defaultCompiler=dxc_local

group.dxc.compilers=dxc_local
compiler.dxc_local.exe=$dxc_bin/dxc
compiler.dxc_local.name=DXC ($(basename "$dxc_bin"))

group.clang.compilers=clang_local:clang_dxc_local
group.clang.compilerType=clang-dxc

compiler.clang_local.exe=$llvm_bin/clang
compiler.clang_local.name=Clang ($(basename "$llvm_wt"))

compiler.clang_dxc_local.exe=$llvm_bin/clang-dxc
compiler.clang_dxc_local.name=Clang-DXC ($(basename "$llvm_wt"))
EOF

echo "Generated $HLSL_LOCAL"
cd "$MASKFILE_DIR/compiler-explorer" && make dev EXTRA_ARGS="--language hlsl"
```

## start-sccache

Starts the sccache server with no timeout. Convenient for agents running bash
in sandboxes where it is inappropriate or disallowed for them to start the
sccache server themselves.

```bash
SCCACHE_IDLE_TIMEOUT=0 sccache --start-server
```
