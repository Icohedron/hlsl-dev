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

Everything the tests need is inside, and the shaders are already compiled: the
offloader, lit and its tooling, the golden images, the test data and one
object file per test, built here by the compiler under test. What the machine
has to supply is a graphics driver -- attach the archive to the bug, and
whoever picks it up runs one command, on any platform, with no compiler and no
DXC.

The shader sources come too, under shaders/, with the exact command line that
produced each object: whoever reads the report can recompile it with their own
build and see whether the failure moves."
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
# Which install targets exist depends on the layout: a standalone offload build
# has no install-distribution (that is LLVM's), and its compiler comes from the
# distribution it was built against. hd_stage_prefix knows both.
# The compiler that produces the objects is *this* machine's, even when the
# reproducer is for another: DXIL and SPIR-V are machine-independent, and so is
# dxv's signature. Resolved before the build, so a missing one stops this in
# seconds rather than after the install targets have run.
llvmwt=$(if [ "$kind" = "llvm" ]; then printf '%s' "$wt"; else hd_dep llvm "$wt"; fi)
native_tools=$( HD_OPT_PLATFORM=native hd_ensure_dist "$llvmwt" )/bin
dxc_dir=$( HD_OPT_PLATFORM=native hd_dxc_bin_dir "$wt" ensure ) || dxc_dir=""
case "$suite" in
clang-*) native_compiler="$native_tools/clang-dxc" ;;
*)
    [ -n "$dxc_dir" ] ||
        hd_die "the $suite suite compiles with dxc, and none was resolved;
       pass --dxc <worktree>, or use a clang-* suite"
    native_compiler="$dxc_dir/dxc"
    ;;
esac
[ -x "$native_compiler" ] ||
    hd_die "$native_compiler is not there; the shaders are compiled on this machine
       ('hlsl-dist' installs the distribution that provides clang-dxc)"

# shellcheck disable=SC2046 # a list of target names; splitting is the point
hd_build "$wt" $(hd_install_targets "$kind")
prefix="$build/repro-prefix"

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

hd_stage_prefix "$wt" "$prefix"
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

# clang's resource headers are for compiling, which happens on the machine that
# made this archive, not on the one that runs it.
rm -rf "$repro/lib/clang" "$repro/include"

# No compiler: the objects are already built, and shipping one invites someone
# to use it -- which would be a different test from the one that failed.
keep="offloader api-query imgdiff FileCheck not split-file obj2yaml lit"
dropped=""
for f in "$repro"/bin/*; do
    [ -e "$f" ] || continue
    base=$(basename "$f")
    case " $keep " in
    *" ${base%.exe} "*) continue ;;
    esac
    # A directory here is a runtime the tools load rather than a tool of its
    # own -- bin/D3D12 is the Agility SDK, which offloader.exe picks up from
    # beside itself -- so it stays.
    [ -d "$f" ] && continue
    dropped="$dropped ${base}"
    rm -f "$f"
done
# -L as well: clang-dxc is a symlink to the clang just removed, and -e reports
# a dangling link as absent, so rm never runs for it.
for f in "$repro"/bin/*; do
    [ -L "$f" ] && [ ! -e "$f" ] && rm -f "$f"
done
[ -z "$dropped" ] || hd_log "left out (nothing in these tests runs them):$dropped"
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

# --- the suite, configured, with the shaders already compiled -----------------
# The same relocatable configuration hlsl-precompile writes, cut down to the
# one suite these tests run as, with the compile step replaced by the stand-in
# that replays what the compiler said here.
staged=$(HD_PRECOMPILED=1 hd_stage_suites "$wt" "$repro")
for s in $staged; do
    [ "$s" = "$suite" ] || rm -rf "${repro:?}/test/$s"
done
[ -d "$repro/test/$suite" ] ||
    hd_die "this build has no $suite suite to package (it configured: $staged)"
hd_stage_python "$repro" "$llvmwt"
cp -a "$HD_LIB_DIR/package/precompiled-cc.py" "$repro/bin/precompiled-cc.py"

only=()
for rel in "${tests[@]}"; do only+=(--only "$rel"); done
# What lit.cfg.py would pass the compiler for this suite, and what the suite
# decides for lit's %if clauses. Kept apart: the printed compile line in the
# report wants the flags, not the features.
flags=()
case "$suite" in
*vk*)
    flags+=(--compiler-arg=-spirv --compiler-arg=-fspv-target-env=vulkan1.3)
    case "$suite" in clang-*) flags+=(--compiler-arg=-fspv-extension=DXC) ;; esac
    ;;
esac
case "$suite" in
clang-*) [ -z "$dxc_dir" ] || flags+=(--compiler-arg="--dxv-path=$dxc_dir") ;;
esac
feat=() true_feat=()
case "$suite" in
clang-*) feat+=(--feature Clang --not-feature DXC); true_feat+=(--feature Clang) ;;
*) feat+=(--feature DXC --not-feature Clang); true_feat+=(--feature DXC) ;;
esac
case "$suite" in
*vk*)
    feat+=(--feature Vulkan --not-feature DirectX --not-feature Metal)
    true_feat+=(--feature Vulkan)
    ;;
*mtl*)
    feat+=(--feature Metal --not-feature DirectX --not-feature Vulkan)
    true_feat+=(--feature Metal)
    ;;
*)
    feat+=(--feature DirectX --not-feature Vulkan --not-feature Metal)
    true_feat+=(--feature DirectX)
    ;;
esac

hd_log "compiling the shaders with $(basename "$native_compiler")"
python3 "$HD_LIB_DIR/package/precompile-shaders.py" \
    --tests "$repro/share/hlsl-test-suite/test" \
    --out "$repro/test/$suite" \
    --compiler "$native_compiler" \
    --split-file "$native_tools/split-file" \
    --manifest "$repro/test/$suite/precompiled.json" \
    "${only[@]}" "${flags[@]}" "${feat[@]}" ||
    hd_die "could not compile the shaders of these tests"

# --- the shader, as a file someone can read and recompile ---------------------
# split-file has already pulled the parts out of each .test file to compile
# them; they are copied out of the run directory (which lit recreates) into
# shaders/, where they are for reading, and for recompiling with a different
# build to see whether the failure moves.
compile_cmds=()
# The printed compile line is for a reader on another machine: this machine's
# dxv path means nothing there, and a placeholder says what to supply.
print_flags=()
for flag in "${flags[@]}"; do
    case "$flag" in
    --compiler-arg=--dxv-path=*) print_flags+=(--compiler-arg="--dxv-path=<directory holding dxv>") ;;
    *) print_flags+=("$flag") ;;
    esac
done

for rel in "${tests[@]}"; do
    stem=${rel%.*}
    split_dir="$repro/test/$suite/$(dirname "$rel")/Output/$(basename "$rel").tmp"
    mkdir -p "$repro/shaders/$stem"
    [ -d "$split_dir" ] && cp -a "$split_dir/." "$repro/shaders/$stem/"
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        compile_cmds+=("$stem|$line")
    done < <(python3 "$HD_LIB_DIR/package/compile-lines.py" "$suite_src/$rel" \
        --compiler "$(basename "$native_compiler")" --stem "$stem" \
        "${print_flags[@]}" "${true_feat[@]}")
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

# hd_expand_run <test file> -> the RUN lines with lit's substitutions applied,
# or nothing at all when one of them is not understood.
expand_run() {
    local file=$1 line out tool marker unknown=""
    while IFS= read -r line; do
        line=${line#*RUN:}
        line=${line# }
        out=$line
        # The compile step already happened, here: the line becomes a note,
        # so the script reads like the test and runs like the package.
        out=${out//\%dxc_target_lib/echo \[precompiled\]}
        out=${out//\%dxc_target/echo \[precompiled\]}
        out=${out//\%offloader/\$BIN\/offloader $offloader_flags}
        out=${out//\%goldenimage_dir/\$GOLDEN}
        # lit substitutes these whether the test writes %imgdiff or plain
        # imgdiff (ToolSubst matches the bare name), so both spellings have to
        # be expanded -- via a marker, because replacing the bare name after
        # the %-form would also rewrite what the %-form just produced.
        for tool in imgdiff FileCheck api-query obj2yaml split-file not; do
            marker="@@${tool}@@"
            out=${out//"%$tool"/$marker}
            out=${out//"$tool "/"$marker "}
            out=${out//"$marker"/"\$BIN/$tool"}
        done
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
# The test's own run directory in this archive: %t.o beside it is the object
# compiled when the archive was made, so it is not removed here.
T=\$ROOT/test/$suite/$(dirname "$rel")/Output/$(basename "$rel").tmp
rm -rf "\$T"
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
            # cmd.exe wants backslashes in the path it sets.
            windir=$(printf '%s' "$(dirname "$rel")" | tr "/" "\\\\")
            printf 'set T=%%ROOT%%test\\%s\\%s\\Output\\%s.tmp\r\n' \
                "$suite" "$windir" "$(basename "$rel")"
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
the offloader, lit and its tooling, the golden images, the test data, and the
shaders *already compiled* by the compiler under test. No compiler, no DXC, no
CMake, no checkout and no Python packages are needed on the machine that runs
it -- only a graphics driver.

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

    if [ "$(hd_platform_os "$platform")" = "windows" ]; then
        lit_cmd="bin\\lit.cmd -v test\\$suite"
        nolit="run-nolit.cmd"
        runner="run.cmd"
    else
        lit_cmd="./bin/lit -v test/$suite"
        nolit="run-nolit.sh"
        runner="./run.sh"
    fi

    cat <<EOF

## Run it

\`\`\`
$lit_cmd
\`\`\`

Nothing to configure and nothing to install: the lit configuration in
\`test/$suite/\` is written out, every path in it is relative to this archive,
lit itself is in \`bin/\`, and the shaders are already compiled. \`$runner\` is
the same command if you would rather not type it. A graphics driver is the
only thing not in here.

One test at a time, when there are several in here:

\`\`\`
./bin/lit -v test/$suite/<dir>/<name>.test     # by path, under test/$suite/
./bin/lit -v --filter=<regex> test/$suite      # by name
./bin/lit -va test/$suite                      # every command, in full
\`\`\`

The path to give lit is the one under \`test/$suite/\`, which mirrors
\`share/hlsl-test-suite/test/\`; lit maps the two and recognises that side.
\`Did not find dxc_target\` in the output is expected -- there is no compiler
here, and the compile step is replayed from what it said when this was made.

EOF

    if [ "${#nolit_tests[@]}" -gt 0 ]; then
        cat <<EOF
### Without lit, without Python

\`\`\`
./$nolit
\`\`\`

The tests' own RUN lines, with lit's substitutions applied and the compile step
already done, against the binaries in this archive. lit is what CI runs and
therefore the authority on feature detection and XFAILs; if the two ever
disagree, lit is right and that is worth saying in the report.

EOF
        if [ "${#nolit_skipped[@]}" -gt 0 ]; then
            printf 'Not in %s (a substitution it does not expand): ' "$nolit"
            # shellcheck disable=SC2016 # markdown backticks
            printf '`%s` ' "${nolit_skipped[@]}"
            printf -- '-- use lit for those.\n\n'
        fi
    fi

    cat <<EOF
## The shaders, and how to compile them

Each test's shader sources are in \`shaders/<test>/\`, exactly as \`split-file\`
pulled them out of the .test file, and the object each produced is in
\`test/$suite/.../Output/\`, where lit looks for it. These are the command lines
that produced them, as run on the machine that made this archive:

\`\`\`
EOF
    for entry in "${compile_cmds[@]}"; do
        printf '# %s\n%s\n' "${entry%%|*}" "${entry#*|}"
    done
    cat <<EOF
\`\`\`

To try a different compiler, run one of those with yours and put the object
where the test expects it -- \`test/$suite/<dir>/Output/<name>.tmp.o\`. Nothing
else changes: the stand-in that replaces the compiler
(\`bin/precompiled-cc.py\`) replays what the compiler said here and never looks
at the object, so overwriting one is enough.

EOF

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
# The suite is already configured and the shaders are already compiled;
# anything passed here goes to lit (-a for a failure's full output,
# --time-tests, --timeout=N).
set -eu
cd "\$(dirname "\$(readlink -f "\$0")")"
exec ./bin/lit -v "\$@" "test/$suite"
EOF
chmod +x "$repro/run.sh"

{
    printf '@echo off\r\n'
    printf 'REM Generated by hlsl-repro: runs %s as the %s suite.\r\n' "$name" "$suite"
    printf 'REM The suite is already configured and the shaders are already compiled;\r\n'
    printf 'REM arguments here go to lit.\r\n'
    printf 'cd /d "%%~dp0"\r\n'
    printf 'call bin\\lit.cmd -v %%* test\\%s\r\n' "$suite"
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
printf '%-16s %s\n' "shaders" "precompiled with $(basename "$native_compiler"); sources in shaders/"
printf '%-16s %s\n' "run it" "./run.sh   (or ./bin/lit -v test/$suite)"
printf '%-16s %s\n' "report" "REPRO.md (fill in 'What happened here')"
