#!/usr/bin/env bash
# summary: Delete built binaries the targets you use do not depend on
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_ARGS="[target]..."
HD_TASK_DESC="Removes executables from a build directory that the targets you actually use
do not depend on, and leaves everything else -- objects, libraries, CMake
state, the install prefix -- alone. A build that wandered off leaves gigabytes
of binaries no HLSL work links, runs or tests: a bare 'ninja', or a
'check-llvm', which drags in llvm-test-depends and with it the whole LLVM tool
zoo and the Kaleidoscope examples. This takes that space back without a
reconfigure or a rebuild of anything you kept.

What to keep is derived from the build graph itself, so it follows the tree
instead of a hard-coded list. Named targets replace the default set:

  hlsl-trim                 keep what check-hlsl and check-clang need
  hlsl-trim check-hlsl      HLSL only -- the clang unit tests go too
  hlsl-trim check-hlsl-clang-vk clang check-clang-unit
                            or any other combination of targets
  hlsl-trim --dry-run       list what would go, remove nothing

Nothing is lost that a link step cannot make again: the next build asking for
one of these targets rebuilds it. Only ELF files are considered, so llvm-lit
and the other scripts in bin/ stay put whatever the graph says."
HD_TASK_OPTS="in= platform= dry_run"
hd_parse "$@"
hd_init

wt=$(hd_target)
HD_WT=$wt
build=$(hd_build_dir "$wt")
[ -f "$build/build.ninja" ] ||
    hd_die "$build is not a configured Ninja build directory -- nothing to trim"

# Without arguments, keep what this kind of checkout is normally asked for.
# check-clang is in the llvm default because HLSL work lives in clang/test as
# much as in the offload suites, and its lit tests reach for LLVM tools that
# check-hlsl alone does not.
targets=("${HD_ARGV[@]}")
if [ "${#targets[@]}" -eq 0 ]; then
    case "$(hd_kind "$wt")" in
    llvm) targets=(check-hlsl check-clang) ;;
    offload) targets=(check-hlsl) ;;
    *) hd_die "no default target list for a $(hd_kind "$wt") build -- name the targets to keep" ;;
    esac
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# The keep set: every node reachable from the targets. `ninja -t graph` labels
# built files with their build-relative path, which is exactly how the scan
# below addresses them. (`-t inputs` will not do: it lists sources only, and a
# binary is never the input of the thing that builds it.)
found=""
for t in "${targets[@]}"; do
    if ninja -C "$build" -t graph "$t" >"$tmp/graph" 2>"$tmp/err"; then
        sed -n 's/^.*\[label="\([^"]*\)".*$/\1/p' "$tmp/graph" >>"$tmp/keep"
        found=1
    else
        hd_warn "no target '$t' in $build -- ignoring it"
    fi
done
[ -n "$found" ] || hd_die "none of those targets exist in $build; nothing would be kept"

declare -A keep=()
while IFS= read -r line; do keep["$line"]=1; done <"$tmp/keep"
hd_log "keeping what ${targets[*]} needs: ${#keep[@]} nodes"

# Candidates: executable regular files in the build tree, minus the install
# prefix (a finished artifact, not a build product) and CMake's own scratch.
total=0
count=0
while IFS= read -r -d '' f; do
    rel=${f#"$build"/}
    [ -z "${keep[$rel]:-}" ] || continue

    # ELF only. bin/llvm-lit and friends are scripts, are not in anybody's
    # graph, and are not ours to remove.
    magic=""
    LC_ALL=C IFS= read -r -N 4 magic <"$f" 2>/dev/null || true
    [ "$magic" = $'\x7fELF' ] || continue

    size=$(stat -c %s -- "$f" 2>/dev/null || echo 0)
    total=$((total + size))
    count=$((count + 1))
    printf '%s\t%s\n' "$size" "$rel" >>"$tmp/doomed"
done < <(find "$build" \
    -path "$build/install" -prune -o \
    -name CMakeFiles -prune -o \
    -type f -perm -u+x -print0)

if [ "$count" -eq 0 ]; then
    hd_log "nothing to trim in $build"
    exit 0
fi

sort -rn "$tmp/doomed" | while IFS=$'\t' read -r size rel; do
    printf '  %8s  %s\n' "$(numfmt --to=iec --format='%.1f' "$size")" "$rel"
done

human=$(numfmt --to=iec --format='%.1f' "$total")
if [ -n "${HD_DRY_RUN:-}" ]; then
    hd_log "would remove $count binaries from $build ($human)"
    exit 0
fi

# A concurrent build of this directory may be linking one of these right now.
hd_lock "$build"
while IFS=$'\t' read -r _ rel; do
    rm -f -- "$build/$rel"
done <"$tmp/doomed"

# Aliases of something that has just gone (llvm-ranlib -> llvm-ar, libLTO.so
# -> libLTO.so.24.0git, and so on).
dangling=0
while IFS= read -r -d '' link; do
    rm -f -- "$link"
    dangling=$((dangling + 1))
done < <(find "$build/bin" "$build/lib" -maxdepth 1 -xtype l -print0 2>/dev/null)

hd_log "removed $count binaries from $build ($human)"
[ "$dangling" -eq 0 ] || hd_log "and $dangling symlink(s) left pointing at them"
