#!/usr/bin/env bash
# summary: Package offload tests with their shaders already compiled, for a machine with no compiler
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_ARGS="[suite]..."
HD_TASK_DESC="Compiles every shader in the offload test suite here, and packages the results
with the offloader and the checks around it. The machine that runs the tests
needs a GPU driver and nothing else -- no compiler, no DXC, no signing, no
Python packages, no configuration step:

  ./bin/lit -v test/clang-d3d12          (bin\\lit.cmd on Windows)

This is the difference from 'hlsl-package', which ships the install prefix
upstream's docs/offload-distribution.md describes and expects the test machine
to compile. Here the compiling half of each test happens on this machine:

  # RUN: split-file %s %t                     <- runs there (200 KB tool)
  # RUN: %dxc_target -T cs_6_5 -Fo %t.o ...   <- ran here; expands to an echo
  # RUN: %offloader %t/pipeline.yaml %t.o     <- runs there, on the GPU

which works because DXIL and SPIR-V are the same bytes on every machine, and
so is dxv's signature -- the thing D3D12 refuses a shader for lacking. One
consequence worth stating: what the target exercises is the *runtime*, with
the compiler pinned to the revision that made the package.

  hlsl-precompile --platform windows-x64             # every suite this build has
  hlsl-precompile --platform windows-x64 clang-d3d12 # one of them
  hlsl-precompile                                    # for this machine

A test whose RUN lines cannot be accounted for is left uncompiled rather than
guessed at, and named in the report (PRECOMPILED.md) with the reason.

The archive is .zip for a Windows platform and .tar.gz otherwise, written next
to the build tree; --out puts it elsewhere and --no-archive leaves the
directory."
HD_TASK_OPTS="in= llvm= dxc= offload= platform= jobs= out= no_archive dry_run no_auto"
# shellcheck disable=SC2034 # read by hd_usage, through its name
HD_DESC_dry_run="Print what would be built, compiled and packaged, and stop"
hd_parse "$@"
hd_init

wt=$(hd_target llvm offload)
HD_WT=$wt
kind=$(hd_kind "$wt")
build=$(hd_build_dir "$wt")
platform=$(hd_platform)
prefix="$build/precompiled"

case "$(hd_platform_os "$platform")" in
windows) ext="zip" ;;
*) ext="tar.gz" ;;
esac
if hd_is_cross; then
    label=$platform
else
    label="$(uname -m)-$(uname -s | tr '[:upper:]' '[:lower:]')"
fi
archive=${out:-$build/hlsl-precompiled-$label.$ext}
case "$archive" in
/*) ;;
*) archive="$PWD/$archive" ;;
esac
if [ -n "${no_archive:-}" ] && [ -n "${out:-}" ]; then
    hd_die "--out names an archive, and --no-archive writes none;
       the directory is always $prefix"
fi

# --- which suites -------------------------------------------------------------
# The build tree has one directory per suite it configured, which is the only
# honest list: a suite that was not configured for this platform has no
# configuration to ship.
test_root=$(hd_test_root "$wt" "$build")
available=""
if [ -d "$test_root" ]; then
    for dir in "$test_root"/*/; do
        [ -f "$dir/lit.site.cfg.py" ] || continue
        case "$(basename "$dir")" in
        Unit | *-lavapipe) continue ;;
        esac
        available="${available:+$available }$(basename "$dir")"
    done
fi

suites=""
if [ "${#HD_ARGV[@]}" -gt 0 ]; then
    for want in "${HD_ARGV[@]}"; do
        case " $available " in
        *" $want "*) suites="${suites:+$suites }$want" ;;
        *) hd_die "this build has no '$want' suite.
       It configured: ${available:-none}
       (a suite appears once its runtime API is available at configure time)" ;;
        esac
    done
else
    suites=$available
fi
[ -n "$suites" ] ||
    hd_die "no configured suites in $test_root; build the offload test suite first"

# --- the compiler that does the work, for *this* machine ----------------------
# Everything else in the package is for the target; the compiler is not in the
# package at all, so it is resolved natively even when --platform is set.
native_tools=$( HD_OPT_PLATFORM=native hd_ensure_dist "$(
    if [ "$kind" = "llvm" ]; then printf '%s' "$wt"; else hd_dep llvm "$wt"; fi
)" )/bin
dxc_dir=$( HD_OPT_PLATFORM=native hd_dxc_bin_dir "$wt" ensure ) || dxc_dir=""

for tool in clang-dxc split-file; do
    [ -x "$native_tools/$tool" ] ||
        hd_die "$native_tools has no $tool; the shaders are compiled on *this* machine,
       so it needs a native LLVM distribution ('hlsl-dist')"
done

hd_log "packaging ${suites// /, } for $platform, shaders compiled here"
if [ -n "${no_archive:-}" ]; then
    hd_report_deps "suites" "$suites" "compiler" "$native_tools/clang-dxc" \
        "dxc/dxv" "${dxc_dir:-none: DXIL will not be signed}" "directory" "$prefix"
else
    hd_report_deps "suites" "$suites" "compiler" "$native_tools/clang-dxc" \
        "dxc/dxv" "${dxc_dir:-none: DXIL will not be signed}" "archive" "$archive"
fi

# --- build the target half ----------------------------------------------------
# shellcheck disable=SC2046 # a list of target names; splitting is the point
hd_build "$wt" $(hd_install_targets "$kind")

if [ -n "${HD_DRY_RUN:-}" ]; then
    hd_log "would compile every shader of: $suites"
    hd_log "would assemble $prefix"
    exit 0
fi

hd_log "staging the prefix in $prefix"
hd_stage_prefix "$wt" "$prefix"

# The compiler and its headers are the point of the exercise: they do not go.
# (clang is 135 MB, the resource headers are only useful to it, and a package
# that contains a compiler invites someone to use it -- which is exactly what
# this flow is avoiding.)
# -L as well as -e: clang-dxc is a symlink to clang, and once clang is gone the
# link is dangling, which -e reports as "not there" and rm -f then never runs.
for f in clang clang++ clang-cl clang-cpp clang-dxc clang-tidy run-clang-tidy; do
    for ff in "$prefix/bin/$f" "$prefix/bin/$f.exe"; do
        if [ -e "$ff" ] || [ -L "$ff" ]; then rm -f "$ff"; fi
    done
done
rm -f "$prefix"/bin/clang-[0-9]* "$prefix"/bin/clang-[0-9]*.exe
rm -rf "$prefix/lib/clang" "$prefix/include"
find "$prefix" -depth -type d -empty -delete 2>/dev/null || true

# --- the lit configuration, with the compile step turned into a no-op ---------
HD_PRECOMPILED=1 hd_stage_suites "$wt" "$prefix" >/dev/null
for s in $available; do
    case " $suites " in
    *" $s "*) ;;
    *) rm -rf "${prefix:?}/test/$s" ;;
    esac
done
# The stand-in for the compiler, and the interpreter is lit's own.
cp -a "$HD_LIB_DIR/package/precompiled-cc.py" "$prefix/bin/precompiled-cc.py"
hd_stage_python "$prefix" "$(if [ "$kind" = "llvm" ]; then printf '%s' "$wt"; else hd_dep llvm "$wt" 2>/dev/null; fi)"

# --- compile the shaders ------------------------------------------------------
# The arguments mirror what offload-test-suite's lit.cfg.py passes, because a
# shader compiled with different flags is a different test.
src=$(if [ "$kind" = "llvm" ]; then hd_dep offload "$wt"; else printf '%s' "$wt"; fi)
tests="$prefix/share/hlsl-test-suite/test"
[ -d "$tests" ] || hd_die "$prefix has no installed tests; is this an offload-enabled build?"

jobs_arg=()
[ -z "$HD_OPT_JOBS" ] || jobs_arg=(--jobs "$HD_OPT_JOBS")
summary=""
for suite in $suites; do
    # What the suite decides regardless of the device, for lit's %if clauses:
    # which compiler, and which graphics API.
    args=()
    case "$suite" in
    clang-*) args+=(--feature Clang --not-feature DXC) ;;
    *) args+=(--feature DXC --not-feature Clang) ;;
    esac
    case "$suite" in
    *vk*) args+=(--feature Vulkan --not-feature DirectX --not-feature Metal) ;;
    *mtl*) args+=(--feature Metal --not-feature DirectX --not-feature Vulkan) ;;
    *) args+=(--feature DirectX --not-feature Vulkan --not-feature Metal) ;;
    esac
    case "$suite" in
    clang-*) compiler="$native_tools/clang-dxc" ;;
    *)
        [ -n "$dxc_dir" ] ||
            hd_die "the $suite suite compiles with dxc, and none was resolved;
       pass --dxc <worktree>, or ask for the clang-* suites only"
        compiler="$dxc_dir/dxc"
        ;;
    esac
    # SPIR-V for the Vulkan suites; Metal takes DXIL, like D3D12 does.
    case "$suite" in
    *vk*)
        args+=(--compiler-arg=-spirv --compiler-arg=-fspv-target-env=vulkan1.3)
        case "$suite" in
        clang-*) args+=(--compiler-arg=-fspv-extension=DXC) ;;
        esac
        ;;
    esac
    # The signature D3D12 refuses a shader for lacking. dxv is platform
    # agnostic, so signing here is signing for the target.
    case "$suite" in
    clang-*)
        [ -z "$dxc_dir" ] || args+=(--compiler-arg="--dxv-path=$dxc_dir")
        ;;
    esac

    hd_log "compiling the $suite shaders with $(basename "$compiler")"
    line=$(python3 "$HD_LIB_DIR/package/precompile-shaders.py" \
        --tests "$tests" \
        --out "$prefix/test/$suite" \
        --compiler "$compiler" \
        --split-file "$native_tools/split-file" \
        --manifest "$prefix/test/$suite/precompiled.json" \
        "${args[@]}" "${jobs_arg[@]}") ||
        hd_die "could not precompile the $suite shaders"
    printf '%s\n' "$line"
    summary="$summary$suite: $(printf '%s' "$line" | head -n 1)"$'\n'
done

# --- the report ---------------------------------------------------------------
{
    printf '# Offload tests, precompiled\n\n'
    # shellcheck disable=SC2016 # markdown backticks, not a subshell
    printf 'Packaged %s by `hlsl-precompile`.\n\n' "$(date -u '+%Y-%m-%d %H:%M UTC')"
    # shellcheck disable=SC2016 # markdown backticks, not a subshell
    printf '## Run them\n\n```\n'
    for s in $suites; do printf './bin/lit -v test/%s\n' "$s"; done
    # shellcheck disable=SC2016 # markdown backticks, not a subshell
    printf '```\n\n'
    # shellcheck disable=SC2016 # markdown backticks, not a subshell
    printf 'On Windows that is `bin\\lit.cmd -v test\\%s`. There is no compiler in this\n' "${suites%% *}"
    printf 'package and none is needed: every shader was compiled (and, for DXIL, signed)\n'
    printf 'before it was made, and each object sits at the path its test writes it to.\n'
    printf 'A GPU driver for the suite is the only thing this archive does not contain.\n\n'
    # shellcheck disable=SC2016 # markdown backticks, not a subshell
    # Running one test is the thing anyone does second, and the path to give
    # lit is the one in test/<suite>/ -- the execution tree -- not the one in
    # share/, which lit does not recognise as a test suite.
    # shellcheck disable=SC2016 # markdown backticks, not a subshell
    printf '## One test, or a few\n\n```\n'
    printf './bin/lit -v test/%s/Feature/HLSLLib/log2.32.test      # one test, by path\n' "${suites%% *}"
    printf './bin/lit -v test/%s/Feature/WaveOps                   # a directory of them\n' "${suites%% *}"
    printf './bin/lit -v --filter=log2 test/%s                     # by regular expression\n' "${suites%% *}"
    printf './bin/lit -va test/%s/Basic/Mandelbrot.test            # ... and show every command\n' "${suites%% *}"
    # shellcheck disable=SC2016 # markdown backticks, not a subshell
    printf '```\n\n'
    # shellcheck disable=SC2016 # markdown backticks, not a subshell
    printf 'The path is the one under `test/<suite>/`, which mirrors the test tree in\n'
    # shellcheck disable=SC2016 # markdown backticks, not a subshell
    printf '`share/hlsl-test-suite/test/`: lit maps the two, and it is the `test/<suite>/`\n'
    printf 'side that it recognises. On Windows use backslashes.\n\n'
    printf 'One note is expected, not a problem:\n\n'
    # shellcheck disable=SC2016 # markdown backticks, not a subshell
    printf -- '- `psutil not installed: running without a per-test timeout` -- `pip install\n'
    # shellcheck disable=SC2016 # markdown backticks, not a subshell
    printf '  psutil` on the test machine turns the timeout on.\n\n'
    printf 'Two of lit%ss own notes are suppressed by the packaged configuration: both\n' "'"
    printf 'are true of a package with no compiler in it, and neither is actionable --\n'
    # shellcheck disable=SC2016 # markdown backticks, not a subshell
    printf -- '`Did not find dxc_target ...` (the compile step is replayed instead) and, on\n'
    # shellcheck disable=SC2016 # markdown backticks, not a subshell
    printf -- 'Windows, `using lit tools: ...` (the GnuWin tools lit locates for RUN lines\n'
    printf 'that use them; no test in this suite does).\n\n'
    # shellcheck disable=SC2016 # markdown backticks, not a subshell
    printf '## What was compiled\n\n```\n%s```\n\n' "$summary"
    # shellcheck disable=SC2016 # markdown backticks, not a subshell
    printf 'The per-suite `test/<suite>/precompiled.json` lists every test, the objects\n'
    printf 'it got, and the reason for each test that was left uncompiled -- those still\n'
    printf 'run, and fail on the missing object, rather than being silently dropped.\n\n'
    printf '## Built from\n\n| what | value |\n|---|---|\n'
    hd_provenance "$wt" | while IFS=$'\t' read -r k v; do
        # shellcheck disable=SC2016 # markdown backticks, not a subshell
        printf -- '| %s | `%s` |\n' "$k" "$v"
    done
    # shellcheck disable=SC2016 # markdown backticks, not a subshell
    printf -- '| offload test sources | `%s` |\n' "$src"
} >"$prefix/PRECOMPILED.md"

# --- archive ------------------------------------------------------------------
if [ -n "${no_archive:-}" ]; then
    hd_log "left unarchived in $prefix (--no-archive)"
    printf '%-16s %s\n' "directory" "$prefix"
    printf '%-16s %s\n' "size" "$(du -sh "$prefix" | cut -f1)"
else
    hd_log "packaging $prefix"
    rm -f "$archive"
    if [ "$ext" = "zip" ]; then
        ( cd "$prefix" && hd_run cmake -E tar cf "$archive" --format=zip -- . )
    else
        ( cd "$prefix" && hd_run cmake -E tar czf "$archive" -- . )
    fi
    printf '%-16s %s\n' "archive" "$archive"
    printf '%-16s %s\n' "size" "$(du -h "$archive" | cut -f1)"
fi
printf '%-16s %s\n' "platform" "$(if hd_is_cross; then printf '%s' "$platform"; else printf 'native (%s)' "$label"; fi)"
printf '%-16s %s\n' "suites" "$suites"
printf '%-16s %s\n' "run it" "./bin/lit -v test/${suites%% *}"
printf '%-16s %s\n' "report" "PRECOMPILED.md"
