#!/usr/bin/env bash
# summary: Package named offload tests into a standalone reproducer for a bug report
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_ARGS="<test>..."
HD_TASK_DESC="Packages one or more offload tests, the tools that run them and a record of
where they came from into a single archive that reproduces a failure on a
machine with no compiler, no CMake and no checkout:

  hlsl-repro Feature/HLSLLib/log2.32.test
  hlsl-repro --suite clang-d3d12 Feature/HLSLLib/log2.32.test Feature/HLSLLib/exp2.32.test
  hlsl-repro --platform windows-x64 --suite vk Feature/Basic/DescriptorTable.test

A test is named the way lit names it -- relative to the suite's test/
directory -- or as a path to the .test file in the offload-test-suite checkout.

What comes out is the same install prefix 'hlsl-package' produces, with the
test tree cut down to the tests named, plus:

  REPRO.md   what this is, what built it (the exact revisions of every
             checkout), the platform, the suite, the RUN lines of each test,
             and the two commands that run it
  run.sh     configure the suite and run exactly those tests
  run.cmd    the same, for a Windows target

Everything the tests need is inside: the offloader, the compiler, lit's
tooling, the golden images and the test data. What the machine has to supply
is a graphics driver and Python with lit and pyyaml -- attach the archive to
the bug, and whoever picks it up runs one command.

For the DXC-compiled suites (d3d12, vk, mtl) the reproducer also needs a dxc
for that machine: package one with
'hlsl-package --in DirectXShaderCompiler' and say so in the report, or use a
clang-* suite, where the compiler in the archive is the one under test."
HD_TASK_OPTS="in= platform= suite= jobs= out= dry_run no_auto"
hd_parse "$@"
hd_need_args 1
hd_init

wt=$(hd_target llvm offload)
HD_WT=$wt
kind=$(hd_kind "$wt")
build=$(hd_build_dir "$wt")
platform=$(hd_platform)

# Where the tests are read from, and which suite they are to be run as.
if [ "$kind" = "llvm" ]; then
    src=$(hd_dep offload "$wt")
else
    src=$wt
fi
suite=${suite:-}
if [ -z "$suite" ]; then
    case "$(hd_platform_os "$platform")" in
    windows) suite="clang-d3d12" ;;
    *) suite="clang-vk" ;;
    esac
fi
case " $HD_SUITES " in
*" $suite "*) ;;
*) hd_die "unknown suite '$suite'; expected one of: $HD_SUITES" ;;
esac

# --- which tests --------------------------------------------------------------
# A name is what lit calls the test (Feature/HLSLLib/log2.32.test), or any path
# to the file; both end up relative to the suite's test/ directory, which is
# how the archive stores them.
tests=()
for arg in "${HD_ARGV[@]}"; do
    rel=$arg
    case "$rel" in
    "$src"/test/*) rel=${rel#"$src"/test/} ;;
    /*) hd_die "$arg is not inside $src/test" ;;
    test/*) rel=${rel#test/} ;;
    esac
    # Also accept a path relative to the current directory, which is what tab
    # completion produces when standing in the suite.
    if [ ! -e "$src/test/$rel" ] && [ -e "$PWD/$arg" ]; then
        abs=$(cd "$(dirname "$PWD/$arg")" && pwd -P)/$(basename "$arg")
        case "$abs" in
        "$src"/test/*) rel=${abs#"$src"/test/} ;;
        esac
    fi
    [ -f "$src/test/$rel" ] ||
        hd_die "no such test: $arg
       expected a path under $src/test, e.g. Feature/HLSLLib/log2.32.test"
    tests+=("$rel")
done

# --- build what the tests run against -----------------------------------------
hd_build "$wt" install-distribution install-offload-tools install-offload-test-suite
prefix="$build/install"

if hd_is_cross; then
    label=$platform
else
    label="$(uname -m)-$(uname -s | tr '[:upper:]' '[:lower:]')"
fi
# Named after the first test, so several reproducers in a build tree do not
# overwrite each other: Feature/HLSLLib/log2.32.test -> log2.32
name=$(basename "${tests[0]}" .test)
[ "${#tests[@]}" -eq 1 ] || name="$name+$((${#tests[@]} - 1))"
repro="$build/repro/$name"

case "$(hd_platform_os "$platform")" in
windows) ext="zip" ;;
*) ext="tar.gz" ;;
esac
archive=${out:-$build/hlsl-repro-$name-$label.$ext}
case "$archive" in
/*) ;;
*) archive="$PWD/$archive" ;;
esac

hd_log "packaging ${#tests[@]} test(s) as $suite for $platform"
hd_report_deps \
    "tests" "${tests[*]}" \
    "suite" "$suite" \
    "from" "$prefix" \
    "archive" "$archive"

if [ -n "${HD_DRY_RUN:-}" ]; then
    hd_log "would assemble $repro and archive it"
    exit 0
fi

[ -d "$prefix/share/hlsl-test-suite" ] ||
    hd_die "$prefix has no installed test suite; is this an offload-enabled build?"

# --- assemble -----------------------------------------------------------------
rm -rf "$repro"
mkdir -p "$repro/share/hlsl-test-suite/test"
cp -a "$prefix/bin" "$repro/bin"
[ -d "$prefix/lib" ] && cp -a "$prefix/lib" "$repro/lib"

# LLVM's driver aliases are symlinks (clang-dxc.exe -> clang.exe). A .zip
# carries the link and Windows extracts something that will not run, so make
# them real files first -- and only then drop what these tests never invoke.
# The order matters: pruning first would break the link that is about to become
# the compiler.
hd_materialise_symlinks "$repro/bin"

case "$suite" in
clang-*) compiler="clang-dxc" ;;
*) compiler="dxc" ;;
esac
keep="offloader api-query imgdiff FileCheck not split-file obj2yaml $compiler"
dropped=""
for f in "$repro"/bin/*; do
    [ -e "$f" ] || continue
    base=$(basename "$f")
    case " $keep " in
    *" ${base%.exe} "*) continue ;;
    esac
    dropped="$dropped ${base}"
    rm -f "$f"
done
[ -z "$dropped" ] || hd_log "left out (nothing in these tests runs them):$dropped"
for f in configure-test-suite.py lit.site.cfg.py.in requirements.txt; do
    [ -e "$prefix/share/hlsl-test-suite/$f" ] &&
        cp -a "$prefix/share/hlsl-test-suite/$f" "$repro/share/hlsl-test-suite/"
done
# The golden images are a dozen files and a test that needs one fails
# confusingly without it; take them all.
[ -d "$prefix/share/hlsl-test-suite/golden-images" ] &&
    cp -a "$prefix/share/hlsl-test-suite/golden-images" "$repro/share/hlsl-test-suite/"

suite_src="$prefix/share/hlsl-test-suite/test"
cp -a "$suite_src/lit.cfg.py" "$repro/share/hlsl-test-suite/test/" 2>/dev/null || true
[ -e "$suite_src/requirements.txt" ] &&
    cp -a "$suite_src/requirements.txt" "$repro/share/hlsl-test-suite/test/"

for rel in "${tests[@]}"; do
    mkdir -p "$repro/share/hlsl-test-suite/test/$(dirname "$rel")"
    cp -a "$suite_src/$rel" "$repro/share/hlsl-test-suite/test/$rel"
    # lit reads a lit.local.cfg from every directory on the way down; a test
    # copied without them is configured differently from the one that failed.
    dir=""
    for part in $(printf '%s\n' "$rel" | tr '/' '\n'); do
        dir="${dir:+$dir/}$part"
        [ -f "$suite_src/$dir/lit.local.cfg" ] || continue
        cp -a "$suite_src/$dir/lit.local.cfg" "$repro/share/hlsl-test-suite/test/$dir/"
    done
done

# --- the same tests, without lit ----------------------------------------------
# lit is the authority -- it is what CI runs -- but it is also Python, pip and
# two packages on a machine whose only job is to reproduce a failure. A test's
# RUN lines are three commands against binaries that are already in this
# archive, so they are expanded here as well: `run-nolit.sh` needs nothing but
# the archive itself.
#
# The substitutions are the ones offload-test-suite's lit.cfg.py makes, and
# their values come from the same suite flags. A test using anything not in
# this list is left out of the script rather than guessed at, and REPRO.md says
# which -- a reproducer that runs something subtly different from the suite is
# worse than one that says "use lit for this one".

# What the suite implies, from lit.cfg.py: Vulkan compiles to SPIR-V, warp runs
# the software rasterizer, and the debug layer is on unless turned off.
compiler_flags=""
offloader_flags="-debug-layer"
case "$suite" in
*vk* | *mtl*) compiler_flags="-spirv -fspv-target-env=vulkan1.3" ;;
esac
case "$suite" in
clang-*vk*) compiler_flags="$compiler_flags -fspv-extension=DXC" ;;
esac
case "$suite" in
*warp*) offloader_flags="$offloader_flags -warp" ;;
esac
case "$suite" in
clang-*) compiler_tool="clang-dxc" ;;
*) compiler_tool="dxc" ;;
esac

# hd_expand_run <test file> -> the RUN lines with lit's substitutions applied,
# or nothing at all when one of them is not understood.
expand_run() {
    local file=$1 line out unknown=""
    while IFS= read -r line; do
        line=${line#*RUN:}
        line=${line# }
        out=$line
        out=${out//\%dxc_target_lib/\$BIN\/$compiler_tool $compiler_flags}
        out=${out//\%dxc_target/\$BIN\/$compiler_tool $compiler_flags}
        out=${out//\%offloader/\$BIN\/offloader $offloader_flags}
        out=${out//\%imgdiff/\$BIN\/imgdiff}
        out=${out//\%FileCheck/\$BIN\/FileCheck}
        out=${out//\%api-query/\$BIN\/api-query}
        out=${out//\%goldenimage_dir/\$GOLDEN}
        out=${out//\%split-file/\$BIN\/split-file}
        out=${out//split-file /\$BIN\/split-file }
        out=${out//\%basename_t/\$(basename \$T)}
        out=${out//\%t/\$T}
        out=${out//\%s/\$TEST}
        case "$out" in
        *%*) unknown="yes" ;;
        esac
        printf '%s\n' "$out"
    done < <(grep -E '^[#/]+ *RUN:' "$file")
    [ -z "$unknown" ] || return 1
}

nolit_tests=()
nolit_skipped=()
for rel in "${tests[@]}"; do
    if expand_run "$suite_src/$rel" >/dev/null 2>&1; then
        nolit_tests+=("$rel")
    else
        nolit_skipped+=("$rel")
    fi
done

if [ "${#nolit_tests[@]}" -gt 0 ] && [ "$(hd_platform_os "$platform")" != "windows" ]; then
    {
        cat <<EOF
#!/usr/bin/env bash
# Generated by hlsl-repro: the RUN lines of ${#nolit_tests[@]} test(s) as the
# $suite suite would run them, with lit's substitutions already applied.
# Needs nothing but this archive -- no Python, no lit, no pip.
set -eu
ROOT=\$(cd "\$(dirname "\$0")" && pwd)
BIN=\$ROOT/bin
GOLDEN=\$ROOT/share/hlsl-test-suite/golden-images
WORK=\${TMPDIR:-/tmp}/hlsl-repro.\$\$
mkdir -p "\$WORK"
trap 'rm -rf "\$WORK"' EXIT
fail=0
EOF
        for rel in "${nolit_tests[@]}"; do
            cat <<EOF

echo "=== $rel"
TEST=\$ROOT/share/hlsl-test-suite/test/$rel
T=\$WORK/$(basename "$rel" .test)
rm -rf "\$T" "\$T".o
(
set -x
EOF
            expand_run "$suite_src/$rel"
            cat <<EOF
) || { echo "FAILED: $rel"; fail=1; }
EOF
        done
        cat <<'EOF'

if [ "$fail" = 0 ]; then echo "all tests passed"; else echo "see FAILED above"; fi
exit "$fail"
EOF
    } >"$repro/run-nolit.sh"
    chmod +x "$repro/run-nolit.sh"

fi

# The same commands for cmd.exe, when the binaries are Windows ones.
if [ "${#nolit_tests[@]}" -gt 0 ] && [ "$(hd_platform_os "$platform")" = "windows" ]; then
    {
        printf '@echo off\r\n'
        printf 'REM Generated by hlsl-repro: %s as the %s suite, without lit.\r\n' "$name" "$suite"
        printf 'setlocal\r\n'
        printf 'set ROOT=%%~dp0\r\n'
        printf 'set BIN=%%ROOT%%bin\r\n'
        printf 'set GOLDEN=%%ROOT%%share/hlsl-test-suite/golden-images\r\n'
        printf 'set WORK=%%TEMP%%\\hlsl-repro\r\n'
        printf 'if not exist "%%WORK%%" mkdir "%%WORK%%"\r\n'
        for rel in "${nolit_tests[@]}"; do
            printf 'echo === %s\r\n' "$rel"
            printf 'set TEST=%%ROOT%%share/hlsl-test-suite/test/%s\r\n' "$rel"
            printf 'set T=%%WORK%%\\%s\r\n' "$(basename "$rel" .test)"
            # shellcheck disable=SC2016 # $BIN and friends are the *generated*
            # script's variables; this rewrites them into cmd.exe's spelling
            expand_run "$suite_src/$rel" |
                sed -e 's|\$BIN/\([a-zA-Z0-9_-]*\)|%BIN%\\\1.exe|g' \
                    -e 's|\$GOLDEN|%GOLDEN%|g' \
                    -e 's|\$TEST|%TEST%|g' -e 's|\$T\b|%T%|g' |
                while IFS= read -r line; do
                    printf '%s || exit /b 1\r\n' "$line"
                done
        done
        printf 'echo all tests passed\r\n'
    } >"$repro/run-nolit.cmd"
fi

# --- the report ---------------------------------------------------------------
rev() { # rev <checkout> -> "branch @ short-sha (dirty)"
    local d=$1 b s dirty=""
    [ -d "$d" ] || { printf 'not present\n'; return 0; }
    b=$(hd_branch "$d")
    s=$(git -C "$d" rev-parse --short HEAD 2>/dev/null) || s="(no commit)"
    git -C "$d" diff --quiet 2>/dev/null || dirty=" + uncommitted changes"
    printf '%s @ %s%s\n' "$b" "$s" "$dirty"
}

llvm=$(hd_dep llvm "$wt" 2>/dev/null || true)
[ "$kind" = "llvm" ] && llvm=$wt
dxcbin=$(hd_dxc_bin_dir "$wt" 2>/dev/null || true)

llvm_rev=$(rev "$llvm")
offload_rev=$(rev "$src")
golden_rev=$(rev "$(hd_dep golden "$wt" 2>/dev/null || echo '')")
build_type=$(hd_build_type "$wt")
stamp=$(date -u '+%Y-%m-%d %H:%M UTC')

{
    cat <<EOF
# Offload test reproducer: $name

Packaged $stamp by \`hlsl-repro\`.

## What this is

A standalone copy of ${#tests[@]} offload test(s) and everything that runs them:
the offloader, the compiler under test, lit's tooling, the golden images and
the test data. No compiler, CMake or checkout is needed on the machine that
runs it.

## Tests

EOF
    # shellcheck disable=SC2016 # backticks are markdown here, not a subshell
    for rel in "${tests[@]}"; do printf -- '- `%s`\n' "$rel"; done

    cat <<EOF

## Built from

| what | value |
|---|---|
| platform | \`$platform\` |
| suite | \`$suite\` |
| build type | \`$build_type\` |
| llvm-project | $llvm_rev |
| offload-test-suite | $offload_rev |
| golden images | $golden_rev |
| dxc used to build | \`${dxcbin:-unknown}\` |
EOF
    # shellcheck disable=SC2016 # as above: markdown backticks
    if [ "$platform" = "native" ]; then
        printf -- '| vulkan driver | `%s` |\n' "$(hd_vk_driver)"
        printf -- '| d3d12 | `%s` |\n' "$(hd_d3d12)"
    fi

    if [ "${#nolit_tests[@]}" -gt 0 ]; then
        if [ "$(hd_platform_os "$platform")" = "windows" ]; then nolit="run-nolit.cmd"; else nolit="run-nolit.sh"; fi
        cat <<EOF

## Run it

\`\`\`
./$nolit
\`\`\`

Needs nothing but this archive and a graphics driver: it is the tests' own RUN
lines with lit's substitutions already applied, against the binaries in
\`bin/\`. No Python, no pip, no lit.

### The same thing through lit

For the exact configuration CI uses -- feature detection, XFAIL handling, the
whole lit report -- there is also:

\`\`\`
./run.sh            # run.cmd on Windows
\`\`\`

(\`$nolit\` is what this task expanded; lit is what decides it in CI. If the
two ever disagree, lit is right and that is worth saying in the report.)

which is:

\`\`\`
python share/hlsl-test-suite/configure-test-suite.py --suite $suite
lit -v share/hlsl-test-suite/run/test/$suite
\`\`\`

and that one does need Python 3.6+ with \`pip install lit pyyaml\`.
EOF
        if [ "${#nolit_skipped[@]}" -gt 0 ]; then
            printf '\nNot in run-nolit.sh (a substitution it does not expand): '
            # shellcheck disable=SC2016 # markdown backticks
            printf '`%s` ' "${nolit_skipped[@]}"
            printf -- '-- use the lit path for those.\n'
        fi
    else
        cat <<EOF

## Run it

\`\`\`
./run.sh            # run.cmd on Windows
\`\`\`

which is:

\`\`\`
python share/hlsl-test-suite/configure-test-suite.py --suite $suite
lit -v share/hlsl-test-suite/run/test/$suite
\`\`\`

Prerequisites: Python 3.6+, \`pip install lit pyyaml\`, and a graphics driver
for the suite. (These tests use lit substitutions that hlsl-repro does not
expand by itself, so there is no Python-free script for them.)
EOF
    fi
    case "$suite" in
    clang-*) ;;
    *)
        cat <<EOF

This is a DXC-compiled suite, so it also needs a \`dxc\` built for that machine:
pass \`--dxc-path <path to dxc>\` to configure-test-suite.py.
EOF
        ;;
    esac

    printf '\n## What the tests do\n\n'
    for rel in "${tests[@]}"; do
        printf '### %s\n\n```\n' "$rel"
        grep -E '^[#/]+ *RUN:' "$suite_src/$rel" || printf '(no RUN lines)\n'
        printf '```\n\n'
    done

    cat <<'EOF'
## What happened here

<!-- Replace this with the failure: the lit output, the machine, the driver
     version, and what you expected instead. -->
EOF
} >"$repro/REPRO.md"

# --- the run scripts ----------------------------------------------------------
# Heredoc quoted where it matters: everything below is the *reproducer's*
# script, and its $1 and $@ belong to whoever runs it, not to this task.
cat >"$repro/run.sh" <<EOF
#!/usr/bin/env bash
# Generated by hlsl-repro: runs $name as the $suite suite.
# An argument overrides the suite; anything after it is passed to
# configure-test-suite.py (--dxc-path ..., for one).
set -eu
cd "\$(dirname "\$(readlink -f "\$0")")"
suite=\${1:-$suite}
python3 share/hlsl-test-suite/configure-test-suite.py --suite "\$suite" "\${@:2}"
exec python3 -m lit -v share/hlsl-test-suite/run/test/"\$suite"
EOF
chmod +x "$repro/run.sh"

{
    printf '@echo off\r\n'
    printf 'REM Generated by hlsl-repro: runs %s as the %s suite.\r\n' "$name" "$suite"
    printf 'cd /d "%%~dp0"\r\n'
    printf 'set SUITE=%%1\r\n'
    printf 'if "%%SUITE%%"=="" set SUITE=%s\r\n' "$suite"
    printf 'python share\\hlsl-test-suite\\configure-test-suite.py --suite %%SUITE%% || exit /b 1\r\n'
    printf 'python -m lit -v share\\hlsl-test-suite\\run\\test\\%%SUITE%%\r\n'
} >"$repro/run.cmd"

# --- archive ------------------------------------------------------------------
hd_log "packaging $repro"
rm -f "$archive"
if [ "$ext" = "zip" ]; then
    ( cd "$repro" && hd_run cmake -E tar cf "$archive" --format=zip -- . )
else
    ( cd "$repro" && hd_run cmake -E tar czf "$archive" -- . )
fi

printf '%-16s %s\n' "archive" "$archive"
printf '%-16s %s\n' "size" "$(du -h "$archive" | cut -f1)"
printf '%-16s %s\n' "tests" "${tests[*]}"
printf '%-16s %s\n' "suite" "$suite"
printf '%-16s %s\n' "report" "REPRO.md (fill in 'What happened here')"
