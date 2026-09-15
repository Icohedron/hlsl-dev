#!/usr/bin/env bash
# Self-test for the precompiled-package machinery: the shader precompiler and
# the stand-in that replays its verdict on the test machine.
#
# No compiler and no GPU: a fake split-file and a fake compiler stand in for
# the real ones, which is enough to pin down the decisions that matter --
# which tests are compiled, what happens to the ones the compiler rejects,
# and that a rejection travels to the target rather than turning into a pass.
set -eo pipefail
here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
root=$(mktemp -d); trap 'rm -rf "$root"' EXIT

fail=0
check() {
    if [ "$2" = "$3" ]; then
        echo "ok   $1"
    else
        echo "FAIL $1: got [$2] want [$3]"
        fail=1
    fi
}

# --- the stand-ins ------------------------------------------------------------
mkdir -p "$root/bin" "$root/tests/Feature" "$root/out"

# split-file, as the real one behaves: a directory of the test's parts.
cat >"$root/bin/split-file" <<'SH'
#!/bin/sh
mkdir -p "$2"
printf 'shader\n' >"$2/source.hlsl"
printf 'pipeline\n' >"$2/pipeline.yaml"
SH

# A compiler that writes its output file and *then* decides whether it is
# happy -- which is what clang does when the validator rejects the shader, and
# the reason "the object exists" cannot be read as "the compile passed".
cat >"$root/bin/compiler" <<'SH'
#!/bin/sh
out=""
prev=""
for arg in "$@"; do
    [ "$prev" = "-Fo" ] && out=$arg
    prev=$arg
done
[ -z "$out" ] || printf 'object\n' >"$out"
case "$*" in
*reject*) echo "error: this shader is rejected" >&2; exit 1 ;;
esac
echo "compiled $out"
SH
chmod +x "$root/bin/split-file" "$root/bin/compiler"

# --- the tests ----------------------------------------------------------------
write_test() { # write_test <relative path> <compile line>
    mkdir -p "$root/tests/$(dirname "$1")"
    {
        printf '#--- source.hlsl\n// a shader\n\n'
        printf '# RUN: split-file %%s %%t\n'
        printf '# RUN: %s\n' "$2"
        printf '# RUN: %%offloader %%t/pipeline.yaml %%t.o\n'
    } >"$root/tests/$1"
}
write_test "Feature/good.test"      '%dxc_target -T cs_6_5 -Fo %t.o %t/source.hlsl'
write_test "Feature/reject.test"    '%dxc_target -T cs_6_5 -Fo %t.o %t/reject.hlsl'
write_test "Feature/inside.test"    '%dxc_target -T cs_6_5 -Fo %t/shader.o %t/source.hlsl'
write_test "Feature/device.test"    '%dxc_target -T %if Int64 %{cs_6_6%} %else %{cs_6_0%} -Fo %t.o %t/source.hlsl'
write_test "Feature/compiler.test"  '%dxc_target -T %if Clang %{cs_6_0%} %else %{cs_6_9%} -Fo %t.o %t/source.hlsl'
# A test with no compile step at all: the offloader does all of its work.
mkdir -p "$root/tests/Tools"
printf '# RUN: %%offloader %%s\n' >"$root/tests/Tools/offloader-only.test"

out=$(python3 "$here/../package/precompile-shaders.py" \
    --tests "$root/tests" --out "$root/out" \
    --compiler "$root/bin/compiler" --split-file "$root/bin/split-file" \
    --manifest "$root/out/precompiled.json" \
    --feature Clang --not-feature DXC --jobs 2 2>&1)

manifest="$root/out/precompiled.json"
q() { python3 -c "import json,sys;d=json.load(open('$manifest'));print($1)"; }

check "precompile: the good test is compiled" \
    "$(q "'Feature/good.test' in d['compiled']")" "True"
check "precompile: and its object is there" \
    "$([ -f "$root/out/Feature/Output/good.test.tmp.o" ] && echo yes)" "yes"
check "precompile: the rejected test is compiled too (the verdict is the point)" \
    "$(q "'Feature/reject.test' in d['compiled']")" "True"
check "precompile: with the compiler's status recorded" \
    "$(q "d['objects']['Feature/Output/reject.test.tmp.o']['status']")" "1"
check "precompile: and its message" \
    "$(q "'rejected' in d['objects']['Feature/Output/reject.test.tmp.o']['output']")" "True"
# The object a rejected compile left behind must not travel: on the target it
# would be a file nothing reads, sitting next to a test that failed.
check "precompile: the rejected object is not shipped" \
    "$([ -e "$root/out/Feature/Output/reject.test.tmp.o" ] && echo shipped)" ""
check "precompile: -Fo inside %t is refused" \
    "$(q "'split-file recreates' in d['skipped']['Feature/inside.test']")" "True"
check "precompile: a device-dependent %if is left to the target" \
    "$(q "'%if' in d['skipped']['Feature/device.test']")" "True"
check "precompile: a compiler %if is answered here" \
    "$(q "'Feature/compiler.test' in d['compiled']")" "True"
check "precompile: a test with no compile step is reported, not failed" \
    "$(q "'no compile step' in d['skipped']['Tools/offloader-only.test']")" "True"
check "precompile: and the summary counts both" \
    "$(printf '%s' "$out" | sed -n '1s/.*(\([0-9]*\) of them.*/\1/p')" "1"

# --- the stand-in on the test machine ----------------------------------------
replay="$here/../package/precompiled-cc.py"
set +e
python3 "$replay" -T cs_6_5 -Fo "$root/out/Feature/Output/good.test.tmp.o" src >/dev/null 2>&1; good_status=$?
bad=$(python3 "$replay" -T cs_6_5 -Fo "$root/out/Feature/Output/reject.test.tmp.o" src 2>&1); bad_status=$?
missing=$(python3 "$replay" -T cs_6_5 -Fo "$root/out/Feature/Output/nothere.test.tmp.o" src 2>&1); missing_status=$?
set -e
check "replay: a compile that passed exits 0" "$good_status" "0"
check "replay: a compile that failed exits with its status" "$bad_status" "1"
check "replay: and says what the compiler said" "$(printf '%s' "$bad" | grep -c rejected)" "1"
check "replay: an object nobody compiled is an error, not a pass" "$missing_status" "2"
check "replay: naming the package's report" "$(printf '%s' "$missing" | grep -c PRECOMPILED.md)" "1"

exit "$fail"
