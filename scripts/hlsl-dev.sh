# shellcheck shell=bash
#
# hlsl-dev.sh -- worktree-aware resolution helpers shared by maskfile.md tasks.
#
# Every task that touches a checkout starts with:
#
#     source "$MASKFILE_DIR/scripts/hlsl-dev.sh"
#     hd_init
#
# The library answers three questions for a task:
#
#   1. *Which checkout am I acting on?*  -- the "target worktree", taken from
#      `--in` / $HLSL_WT, or discovered by walking up from $PWD. This is what
#      makes the tasks work unchanged from inside any `wt` worktree.
#   2. *Which checkouts does it depend on?* -- e.g. an offload-test-suite build
#      needs an llvm-project worktree (headers, lit, an installed distribution)
#      and a DXC binary directory. See hd_dep().
#   3. *Where do the build artifacts live?* -- always inside the worktree, so
#      that parallel agents in different worktrees never share a build dir and
#      `wt remove` takes the artifacts with it. See hd_build_dir().
#
# Nothing here hard-codes a path under the workspace root: worktrees are found
# through `git worktree list`, so they can live anywhere.

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

hd_log()  { printf '==> %s\n' "$*" >&2; }
hd_warn() { printf 'warning: %s\n' "$*" >&2; }
hd_die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Repository identity
# ---------------------------------------------------------------------------
# A "kind" is one of: llvm, dxc, offload, golden. Kinds are detected from the
# contents of a directory rather than its name, so renamed or relocated
# worktrees still resolve.

hd_repo_name() {
    case "$1" in
    llvm) printf '%s\n' "${HLSL_REPO_LLVM:-llvm-project}" ;;
    dxc) printf '%s\n' "${HLSL_REPO_DXC:-DirectXShaderCompiler}" ;;
    offload) printf '%s\n' "${HLSL_REPO_OFFLOAD:-offload-test-suite}" ;;
    golden) printf '%s\n' "${HLSL_REPO_GOLDEN:-offload-golden-images}" ;;
    *) hd_die "unknown repository kind '$1'" ;;
    esac
}

hd_kind_label() {
    case "$1" in
    llvm) printf 'llvm-project\n' ;;
    dxc) printf 'DirectXShaderCompiler\n' ;;
    offload) printf 'offload-test-suite\n' ;;
    golden) printf 'offload-golden-images\n' ;;
    esac
}

# hd_kind <dir> -> llvm|dxc|offload|golden (empty if the directory is none of
# them). Ordered so that DXC (an LLVM 3.7 fork) is matched before llvm-project.
hd_kind() {
    local d=$1
    [ -n "$d" ] && [ -d "$d" ] || return 0
    if [ -f "$d/cmake/caches/PredefinedParams.cmake" ] && [ -d "$d/tools/clang" ]; then
        printf 'dxc\n'
    elif [ -d "$d/tools/offloader" ] && [ -d "$d/lib/API" ]; then
        printf 'offload\n'
    elif [ -f "$d/llvm/CMakeLists.txt" ] && [ -d "$d/clang" ]; then
        printf 'llvm\n'
    elif [ -d "$d/hlsl" ] && [ -f "$d/README.md" ] && [ ! -f "$d/CMakeLists.txt" ]; then
        printf 'golden\n'
    fi
}

# ---------------------------------------------------------------------------
# Workspace root
# ---------------------------------------------------------------------------

hd_find_root() {
    if [ -n "${HLSL_DEV_ROOT:-}" ] && [ -f "$HLSL_DEV_ROOT/maskfile.md" ]; then
        printf '%s\n' "$HLSL_DEV_ROOT"
        return 0
    fi
    if [ -n "${MASKFILE_DIR:-}" ] && [ -f "$MASKFILE_DIR/maskfile.md" ]; then
        (cd "$MASKFILE_DIR" && pwd -P)
        return 0
    fi
    local d
    d=$(pwd -P)
    while [ "$d" != "/" ]; do
        if [ -f "$d/maskfile.md" ] && [ -f "$d/flake.nix" ]; then
            printf '%s\n' "$d"
            return 0
        fi
        d=$(dirname "$d")
    done
    hd_die "cannot find the workspace root (maskfile.md + flake.nix); set HLSL_DEV_ROOT"
}

hd_state_dir() { printf '%s\n' "${HLSL_DEV_STATE:-$HD_ROOT/.hlsl-dev}"; }

# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------
# Copies the option variables that `mask` injects into the task environment
# into namespaced HD_OPT_* variables, so that helper functions have a single
# place to look and task-local names cannot collide with them.

hd_init() {
    HD_ROOT=$(hd_find_root)
    export HD_ROOT

    HD_OPT_IN=${in:-${HLSL_WT:-}}
    HD_OPT_LLVM=${llvm:-${HLSL_LLVM:-}}
    HD_OPT_DXC=${dxc:-${HLSL_DXC:-}}
    HD_OPT_OFFLOAD=${offload:-${HLSL_OFFLOAD:-}}
    HD_OPT_GOLDEN=${golden:-${HLSL_GOLDEN:-}}
    HD_OPT_MODE=${mode:-${HLSL_MODE:-}}
    HD_OPT_BUILD_DIR=${build_dir:-${HLSL_BUILD_DIR:-}}
    HD_OPT_BUILD_TYPE=${build_type:-${HLSL_BUILD_TYPE:-}}
    HD_OPT_DIST_PREFIX=${dist_prefix:-${HLSL_DIST_PREFIX:-}}
    HD_OPT_FRESH=${fresh:-}

    case "${HD_OPT_MODE:-}" in
    "" | standalone | integrated) ;;
    *) hd_die "--mode must be 'standalone' or 'integrated' (got '$HD_OPT_MODE')" ;;
    esac

    if [ -z "${HLSL_CMAKE_FLAGS_LLVM:-}" ]; then
        hd_die "CMake flag templates are missing from the environment;
       enter the dev shell first ('nix develop', or 'direnv allow')"
    fi
}

# ---------------------------------------------------------------------------
# Worktree discovery
# ---------------------------------------------------------------------------

hd_abs() { (cd "$1" 2>/dev/null && pwd -P); }

# hd_worktrees <kind> -> one absolute path per line.
#
# Sources, in order: the submodule checkout in the workspace root, every
# worktree git knows about (these can live anywhere on disk), and any sibling
# directory in the workspace root whose contents match the kind (covers plain
# clones that are not registered as worktrees).
hd_worktrees() {
    local kind=$1 base d
    base="$HD_ROOT/$(hd_repo_name "$kind")"
    {
        printf '%s\n' "$base"
        if [ -e "$base/.git" ]; then
            git -C "$base" worktree list --porcelain 2>/dev/null |
                sed -n 's/^worktree //p'
        fi
        for d in "$base"*; do
            [ -d "$d" ] && printf '%s\n' "$d"
        done
    } | {
        while IFS= read -r d; do
            [ -n "$d" ] || continue
            d=$(hd_abs "$d") || continue
            [ -n "$d" ] || continue
            [ "$(hd_kind "$d")" = "$kind" ] && printf '%s\n' "$d"
        done
    } | awk '!seen[$0]++'
}

hd_branch() {
    local b
    b=$(git -C "$1" rev-parse --abbrev-ref HEAD 2>/dev/null) || return 0
    [ "$b" = "HEAD" ] && b="(detached)"
    printf '%s\n' "$b"
}

# hd_wt_from <dir> -> nearest enclosing worktree root, empty if there is none.
hd_wt_from() {
    local d
    d=$(hd_abs "$1") || return 0
    while [ -n "$d" ] && [ "$d" != "/" ]; do
        if [ -n "$(hd_kind "$d")" ]; then
            printf '%s\n' "$d"
            return 0
        fi
        d=$(dirname "$d")
    done
}

# hd_resolve <kind> <spec> -> absolute worktree path.
#
# A spec is an absolute or relative path, a path relative to the workspace
# root, a directory name ("llvm-project.texture-store"), a bare suffix
# ("texture-store"), or a branch name.
hd_resolve() {
    local kind=$1 spec=$2 cand="" wt b
    [ -n "$spec" ] || return 1

    if [ -d "$spec" ]; then
        cand=$(hd_abs "$spec")
    elif [ -d "$HD_ROOT/$spec" ]; then
        cand=$(hd_abs "$HD_ROOT/$spec")
    fi
    if [ -n "$cand" ]; then
        [ "$(hd_kind "$cand")" = "$kind" ] ||
            hd_die "$cand is not a $(hd_kind_label "$kind") checkout"
        printf '%s\n' "$cand"
        return 0
    fi

    while IFS= read -r wt; do
        [ -n "$wt" ] || continue
        b=$(basename "$wt")
        if [ "$b" = "$spec" ] ||
            [ "$b" = "$(hd_repo_name "$kind").$spec" ] ||
            [ "$(hd_branch "$wt")" = "$spec" ]; then
            printf '%s\n' "$wt"
            return 0
        fi
    done <<< "$(hd_worktrees "$kind")"

    printf 'error: no %s worktree matches '\''%s'\''. Known worktrees:\n' \
        "$(hd_kind_label "$kind")" "$spec" >&2
    hd_worktrees "$kind" | sed 's|^|         |' >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Per-worktree state (pins)
# ---------------------------------------------------------------------------
# Cross-worktree choices are remembered outside the checkouts, in
# $HD_ROOT/.hlsl-dev/pins/<key>.env, so that `git status` inside a worktree
# stays clean and two agents never write to the same file.

hd_key() {
    local p=${1%/}
    p=${p#"$HD_ROOT"/}
    printf '%s\n' "$(printf '%s' "$p" | tr '/' '%')"
}

hd_pin_file() { printf '%s\n' "$(hd_state_dir)/pins/$(hd_key "$1").env"; }

# hd_pin_get <worktree> <KEY>
hd_pin_get() {
    local f
    f=$(hd_pin_file "$1")
    [ -f "$f" ] || return 0
    sed -n "s/^$2=//p" "$f" | tail -n 1
}

# hd_pin_set <worktree> <KEY> <value>   (empty value removes the pin)
hd_pin_set() {
    local f tmp
    f=$(hd_pin_file "$1")
    mkdir -p "$(dirname "$f")"
    tmp=$(mktemp "$f.XXXXXX")
    if [ -f "$f" ]; then grep -v "^$2=" "$f" >"$tmp" || true; fi
    [ -n "$3" ] && printf '%s=%s\n' "$2" "$3" >>"$tmp"
    mv "$tmp" "$f"
}

hd_pin_clear() {
    local f
    f=$(hd_pin_file "$1")
    rm -f "$f"
}

# ---------------------------------------------------------------------------
# Dependency resolution
# ---------------------------------------------------------------------------
# Resolution order for "which <kind> checkout should <worktree> build against":
#
#   1. --llvm / --dxc / --offload / --golden on the command line
#   2. $HLSL_LLVM / $HLSL_DXC / $HLSL_OFFLOAD / $HLSL_GOLDEN in the environment
#   3. a pin recorded by `mask link` or by the last successful `mask configure`
#   4. a worktree of that repository checked out on the *same branch name*
#   5. the submodule checkout in the workspace root

hd_dep() {
    local kind=$1 from=${2:-} spec="" wt br

    case "$kind" in
    llvm) spec=$HD_OPT_LLVM ;;
    dxc) spec=$HD_OPT_DXC ;;
    offload) spec=$HD_OPT_OFFLOAD ;;
    golden) spec=$HD_OPT_GOLDEN ;;
    esac

    if [ -z "$spec" ] && [ -n "$from" ]; then
        spec=$(hd_pin_get "$from" "$(printf '%s' "$kind" | tr '[:lower:]' '[:upper:]')")
    fi
    if [ -n "$spec" ]; then
        hd_resolve "$kind" "$spec"
        return
    fi

    if [ -n "$from" ]; then
        br=$(hd_branch "$from")
        if [ -n "$br" ] && [ "$br" != "(detached)" ]; then
            while IFS= read -r wt; do
                [ -n "$wt" ] || continue
                if [ "$(hd_branch "$wt")" = "$br" ]; then
                    printf '%s\n' "$wt"
                    return 0
                fi
            done <<< "$(hd_worktrees "$kind")"
        fi
    fi

    local base="$HD_ROOT/$(hd_repo_name "$kind")"
    [ -d "$base" ] ||
        hd_die "no $(hd_kind_label "$kind") checkout found; run 'mask setup'"
    hd_abs "$base"
}

# ---------------------------------------------------------------------------
# Build directories
# ---------------------------------------------------------------------------
# Artifacts live inside the worktree they belong to:
#
#   <worktree>/build                normal build tree
#   <llvm worktree>/build-dist      standalone LLVM distribution build
#   <llvm worktree>/build-dist/install
#                                   the prefix offload standalone builds consume
#
# --build-dir (or $HLSL_BUILD_DIR) overrides the first one for the *target*
# worktree only; the choice is pinned so that dependent worktrees can find it.

hd_build_dir() {
    local wt=$1 pinned
    if [ -n "$HD_OPT_BUILD_DIR" ] && [ "$wt" = "${HD_WT:-}" ]; then
        case "$HD_OPT_BUILD_DIR" in
        /*) printf '%s\n' "${HD_OPT_BUILD_DIR%/}" ;;
        *) printf '%s\n' "$wt/${HD_OPT_BUILD_DIR%/}" ;;
        esac
        return 0
    fi
    pinned=$(hd_pin_get "$wt" BUILD_DIR)
    if [ -n "$pinned" ]; then
        printf '%s\n' "$pinned"
        return 0
    fi
    printf '%s/%s\n' "$wt" "${HLSL_BUILD_DIR_NAME:-build}"
}

hd_dist_build_dir() { printf '%s/build-dist\n' "$1"; }

# The install prefix of the standalone LLVM distribution belonging to an
# llvm-project worktree. --dist-prefix (or $HLSL_DIST_PREFIX) points at a
# prefix built elsewhere -- a shared one, or an unpacked CI artifact.
hd_dist_prefix() {
    local pinned
    if [ -n "${HD_OPT_DIST_PREFIX:-}" ]; then
        hd_abs "$HD_OPT_DIST_PREFIX" 2>/dev/null ||
            printf '%s\n' "${HD_OPT_DIST_PREFIX%/}"
        return 0
    fi
    pinned=$(hd_pin_get "$1" DIST_PREFIX)
    if [ -n "$pinned" ]; then
        printf '%s\n' "$pinned"
    else
        printf '%s/build-dist/install\n' "$1"
    fi
}

hd_build_type() {
    local wt=$1 cached pinned
    if [ -n "$HD_OPT_BUILD_TYPE" ]; then
        printf '%s\n' "$HD_OPT_BUILD_TYPE"
        return 0
    fi
    pinned=$(hd_pin_get "$wt" BUILD_TYPE)
    if [ -n "$pinned" ]; then
        printf '%s\n' "$pinned"
        return 0
    fi
    cached=$(hd_cache_get "$(hd_build_dir "$wt")" CMAKE_BUILD_TYPE)
    printf '%s\n' "${cached:-RelWithDebInfo}"
}

# hd_mode <offload worktree> -> standalone|integrated
hd_mode() {
    local pinned
    if [ -n "$HD_OPT_MODE" ]; then
        printf '%s\n' "$HD_OPT_MODE"
        return 0
    fi
    pinned=$(hd_pin_get "$1" MODE)
    printf '%s\n' "${pinned:-standalone}"
}

# hd_cache_get <build dir> <variable> -> cached value (empty if unset)
hd_cache_get() {
    [ -f "$1/CMakeCache.txt" ] || return 0
    sed -n "s|^$2:[^=]*=||p" "$1/CMakeCache.txt" | tail -n 1
}

# ---------------------------------------------------------------------------
# DXC binary directory
# ---------------------------------------------------------------------------
# --dxc accepts a worktree spec, a directory containing dxc/dxv, or the literal
# "nix" for the prebuilt compiler from the dev shell.

hd_dxc_bin_dir() {
    local from=${1:-} spec=$HD_OPT_DXC explicit=1 wt bin

    if [ -z "$spec" ] && [ -n "$from" ]; then
        spec=$(hd_pin_get "$from" DXC)
    fi
    [ -n "$spec" ] || explicit=0

    case "$spec" in
    nix | system | prebuilt)
        [ -n "${HLSL_DXC_PREBUILT_DIR:-}" ] ||
            hd_die "the dev shell did not provide a prebuilt dxc"
        printf '%s\n' "$HLSL_DXC_PREBUILT_DIR"
        return 0
        ;;
    esac
    if [ -n "$spec" ] && [ -x "$spec/dxc" ]; then
        hd_abs "$spec"
        return 0
    fi

    wt=$(hd_dep dxc "$from")
    bin="$(hd_build_dir "$wt")/bin"
    if [ -x "$bin/dxc" ]; then
        printf '%s\n' "$bin"
        return 0
    fi
    if [ "$explicit" = 1 ]; then
        hd_die "$wt has no built dxc ($bin/dxc);
       build it first:  mask build --in $(basename "$wt")"
    fi
    [ -n "${HLSL_DXC_PREBUILT_DIR:-}" ] ||
        hd_die "no dxc available: neither $bin/dxc nor a dev-shell dxc exists"
    hd_warn "$(basename "$wt") has no built dxc yet, using the dev shell's prebuilt one
         (build it with 'mask build --in $(basename "$wt")', or pass --dxc <worktree>)"
    printf '%s\n' "$HLSL_DXC_PREBUILT_DIR"
}

# ---------------------------------------------------------------------------
# Target worktree
# ---------------------------------------------------------------------------

# hd_target [expected kind ...] -> the worktree the task acts on.
hd_target() {
    local wt kind k ok
    if [ -n "$HD_OPT_IN" ]; then
        wt=$(hd_resolve_any "$HD_OPT_IN")
    else
        wt=$(hd_wt_from "$PWD")
        [ -n "$wt" ] || hd_die "not inside a known worktree.
       cd into one, or pass --in <worktree> (see 'mask ls')"
    fi
    kind=$(hd_kind "$wt")
    if [ "$#" -gt 0 ]; then
        ok=0
        for k in "$@"; do [ "$k" = "$kind" ] && ok=1; done
        if [ "$ok" != 1 ]; then
            local expected=""
            for k in "$@"; do expected="$expected $(hd_kind_label "$k")"; done
            hd_die "this task does not apply to $wt ($(hd_kind_label "$kind"));
       expected a checkout of:$expected"
        fi
    fi
    printf '%s\n' "$wt"
}

# hd_resolve_any <spec> -> worktree of whichever kind matches.
hd_resolve_any() {
    local spec=$1 cand kind wt b
    if [ -d "$spec" ]; then cand=$(hd_abs "$spec"); elif [ -d "$HD_ROOT/$spec" ]; then cand=$(hd_abs "$HD_ROOT/$spec"); fi
    if [ -n "${cand:-}" ]; then
        kind=$(hd_kind "$cand")
        [ -n "$kind" ] || hd_die "$cand is not an llvm-project, DirectXShaderCompiler or offload-test-suite checkout"
        printf '%s\n' "$cand"
        return 0
    fi
    for kind in llvm dxc offload; do
        while IFS= read -r wt; do
            [ -n "$wt" ] || continue
            b=$(basename "$wt")
            if [ "$b" = "$spec" ] ||
                [ "$b" = "$(hd_repo_name "$kind").$spec" ] ||
                [ "$(hd_branch "$wt")" = "$spec" ]; then
                printf '%s\n' "$wt"
                return 0
            fi
        done <<< "$(hd_worktrees "$kind")"
    done
    hd_die "no worktree matches '$spec' (see 'mask ls')"
}

# ---------------------------------------------------------------------------
# Housekeeping: git excludes and build locks
# ---------------------------------------------------------------------------

# Keep `git status` clean in worktrees whose upstream .gitignore does not cover
# our artifact directories (offload-test-suite in particular). info/exclude is
# per-clone and never committed, so this is invisible to the upstream repo.
hd_git_exclude() {
    local common f pat
    common=$(git -C "$1" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || return 0
    f="$common/info/exclude"
    mkdir -p "$(dirname "$f")" 2>/dev/null || return 0
    [ -f "$f" ] || : >"$f"
    for pat in '/build*/' '/install*/' '/compile_commands.json' \
        '/.codegraph/' '/.codegraph-*/' '/codegraph.json'; do
        grep -qxF "$pat" "$f" 2>/dev/null || printf '%s\n' "$pat" >>"$f"
    done
}

# ---------------------------------------------------------------------------
# CodeGraph index
# ---------------------------------------------------------------------------
# One index per worktree, kept out of git by hd_git_exclude above.
#
# The index (.codegraph/codegraph.db) stores project-root-RELATIVE paths and
# records no absolute root, so it is portable between checkouts of the same
# repository -- a fresh worktree can start from a copy of another worktree's
# database and then re-parse only the files its branch actually changed.
#
# It is NOT shareable in place: the database is a live SQLite WAL file that the
# indexer rewrites to match the tree it sits in, so a symlink shared by two
# worktrees on different branches would thrash the index and fight over the
# daemon lock. Copy-then-sync gets the cheap start without that.

hd_codegraph_dir() { printf '%s\n' "$1/.codegraph"; }

# Which parts of a checkout to index, one shared pattern list per repository
# kind (scripts/codegraph-<kind>.json), copied into the worktree as
# codegraph.json. Only the subtrees we actually work in reach the index: for
# llvm-project that is clang/ and llvm/ minus their lit test corpora, ~11k files
# instead of ~116k; for DXC it drops the two test corpora and external/; for the
# offload suite it drops third-party/.
hd_codegraph_template() { printf '%s/scripts/codegraph-%s.json\n' "$HD_ROOT" "$1"; }

hd_codegraph_config() {
    local wt=$1 template
    template=$(hd_codegraph_template "$(hd_kind "$wt")")
    [ -f "$template" ] || hd_die "no CodeGraph scope for $wt (expected $template)"
    cmp -s "$template" "$wt/codegraph.json" || cp "$template" "$wt/codegraph.json"
}

# CodeGraph ignores any directory called "target" (the Rust build directory) or
# "coverage" by default, case-INsensitively, and that built-in list beats both
# `exclude` and `include` in codegraph.json. In llvm-project that silently drops
# all 2,954 files of llvm/lib/Target -- the DirectX and SPIR-V backends
# included; in DXC it drops include/llvm/Target. The one lever that overrides a
# built-in default is a negation in the project root's .gitignore, which
# CodeGraph merges *after* its own patterns. The offload test suite has no such
# directory, so its .gitignore is left untouched (see hd_codegraph_needs_block).
#
# .gitignore is a tracked upstream file, so the block below is marked
# skip-worktree: it stays on disk for CodeGraph, out of `git status`, and out of
# any commit. The one thing it costs: a checkout/rebase/pull that wants to
# change .gitignore itself refuses to run ("local changes would be overwritten",
# or "Entry '.gitignore' not uptodate"). Then run
# `mask codegraph --restore-gitignore` in that worktree, redo the git
# operation, and run `mask codegraph` again to put the block back.
HD_CG_MARK_BEGIN='# >>> codegraph: local index scope, not committed >>>'
HD_CG_MARK_END='# <<< codegraph <<<'

hd_sed_escape() { printf '%s\n' "$1" | sed 's/[.[\*^$\/]/\\&/g'; }

# Does this repository kind own source under a directory CodeGraph ignores by
# default? llvm-project (llvm/lib/Target, llvm/include/llvm/Target, the coverage
# libraries) and DXC (include/llvm/Target) do; the offload test suite does not,
# so its committed .gitignore is never touched.
hd_codegraph_needs_block() {
    case "$1" in
    llvm | dxc) return 0 ;;
    *) return 1 ;;
    esac
}

# The block's exact text, so an outdated one is rewritten rather than kept.
hd_codegraph_block() {
    cat <<EOF
$HD_CG_MARK_BEGIN
# CodeGraph's built-in ignore list drops every directory called "target" (the
# Rust build dir) or "coverage", case-insensitively. Here that hides
# lib/Target and include/llvm/Target -- in llvm-project the DirectX and SPIR-V
# backends included -- along with their unittests and the coverage libraries.
# Only a root .gitignore negation overrides a built-in default. Managed by
# 'mask codegraph'; 'mask codegraph --restore-gitignore' takes it back out.
!**/Target/
!**/Target/**
!**/Coverage/
!**/Coverage/**
$HD_CG_MARK_END
EOF
}

hd_codegraph_gitignore() {
    local wt=$1 f current
    f="$wt/.gitignore"
    hd_codegraph_needs_block "$(hd_kind "$wt")" || return 0
    [ -f "$f" ] || return 0
    current=$(sed -n "/^$(hd_sed_escape "$HD_CG_MARK_BEGIN")\$/,/^$(hd_sed_escape "$HD_CG_MARK_END")\$/p" "$f")
    if [ "$current" != "$(hd_codegraph_block)" ]; then
        hd_codegraph_ungitignore "$wt"
        hd_log "re-including Target/ sources in $wt/.gitignore (kept local with skip-worktree)"
        printf '\n%s\n' "$(hd_codegraph_block)" >>"$f"
    fi
    git -C "$wt" update-index --skip-worktree .gitignore 2>/dev/null || true
}

hd_codegraph_ungitignore() {
    local wt=$1 f
    f="$wt/.gitignore"
    git -C "$wt" update-index --no-skip-worktree .gitignore 2>/dev/null || true
    { [ -f "$f" ] && grep -qxF "$HD_CG_MARK_BEGIN" "$f"; } || return 0
    hd_log "removing the codegraph block from $wt/.gitignore"
    sed -i "/^$(hd_sed_escape "$HD_CG_MARK_BEGIN")\$/,/^$(hd_sed_escape "$HD_CG_MARK_END")\$/d" "$f"
    # Collapse the blank line the block was appended after.
    sed -i -e :a -e '/^\n*$/{$d;N;ba' -e '}' "$f"
}

# hd_codegraph_donor <worktree> -> another worktree OF THE SAME REPOSITORY with
# an index to seed from, preferring the submodule checkout, then the largest
# (most complete) database. Never crosses repositories: the file paths in an
# index only mean anything inside the repository they came from.
hd_codegraph_donor() {
    local self=$1 kind base wt db best="" bestsize=0 size
    kind=$(hd_kind "$self")
    base="$HD_ROOT/$(hd_repo_name "$kind")"
    if [ "$self" != "$base" ] && [ -f "$(hd_codegraph_dir "$base")/codegraph.db" ]; then
        printf '%s\n' "$base"
        return 0
    fi
    while IFS= read -r wt; do
        [ -n "$wt" ] && [ "$wt" != "$self" ] || continue
        db="$(hd_codegraph_dir "$wt")/codegraph.db"
        [ -f "$db" ] || continue
        size=$(stat -c %s "$db" 2>/dev/null || echo 0)
        if [ "$size" -gt "$bestsize" ]; then best=$wt; bestsize=$size; fi
    done <<< "$(hd_worktrees "$kind")"
    # Never fail: "nothing to seed from" is an ordinary answer (the first index
    # of a repository), and the caller runs under `set -e`.
    [ -n "$best" ] && printf '%s\n' "$best"
    return 0
}

# hd_codegraph <worktree> [donor] -- build or refresh the index of a worktree.
# Set HD_OPT_FRESH (mask --fresh) to rebuild from scratch instead of seeding.
hd_codegraph() {
    local wt=$1 donor=${2:-} dir db
    command -v codegraph >/dev/null 2>&1 ||
        hd_die "codegraph is not on PATH (see https://github.com/colbymchenry/codegraph)"

    dir=$(hd_codegraph_dir "$wt")
    db="$dir/codegraph.db"

    hd_git_exclude "$wt"
    hd_codegraph_config "$wt"
    hd_codegraph_gitignore "$wt"

    if [ -n "${HD_OPT_FRESH:-}" ]; then
        hd_log "rebuilding the index of $wt from scratch"
        rm -rf "$dir"
        hd_run codegraph init "$wt"
        return
    fi

    if [ ! -f "$db" ]; then
        if [ -n "$donor" ] && [ "$(hd_kind "$donor")" != "$(hd_kind "$wt")" ]; then
            hd_die "cannot seed $(hd_kind_label "$(hd_kind "$wt")") from $donor;
       an index only means anything inside the repository it came from"
        fi
        [ -n "$donor" ] || donor=$(hd_codegraph_donor "$wt")
        if [ -n "$donor" ]; then
            hd_log "seeding the index of $wt from $donor"
            mkdir -p "$dir"
            cp "$(hd_codegraph_dir "$donor")/codegraph.db" "$db"
            rm -f "$dir/codegraph.db-wal" "$dir/codegraph.db-shm"
        else
            hd_log "no existing index to seed from; indexing $wt from scratch"
            hd_run codegraph init "$wt"
            return
        fi
    fi

    rm -f "$dir/codegraph.lock"
    # A seeded index whose donor sat on a widely diverged branch can overflow
    # the syncer on the first (very large) batch; the retry resumes from what
    # it committed, and a full index is the backstop.
    if ! hd_run codegraph sync "$wt"; then
        hd_warn "sync failed; retrying"
        rm -f "$dir/codegraph.lock"
        if ! hd_run codegraph sync "$wt"; then
            hd_warn "sync failed twice; rebuilding from scratch"
            rm -rf "$dir"
            hd_run codegraph init "$wt"
        fi
    fi
}

# Serialise concurrent mask invocations that target the same build directory.
# Different worktrees have different build directories, so agents working in
# parallel never wait on each other. Re-locking a directory this process
# already holds is a no-op (flock would otherwise deadlock against itself).
hd_lock() {
    local dir=$1 lock
    command -v flock >/dev/null 2>&1 || return 0
    case " ${HD_LOCKED:-} " in
    *" $dir "*) return 0 ;;
    esac
    mkdir -p "$(hd_state_dir)/locks"
    lock="$(hd_state_dir)/locks/$(hd_key "$dir").lock"
    exec {HD_LOCK_FD}>"$lock"
    if ! flock -n "$HD_LOCK_FD"; then
        hd_log "waiting for another mask invocation to release $dir"
        flock -w "${HLSL_LOCK_TIMEOUT:-7200}" "$HD_LOCK_FD" ||
            hd_die "timed out waiting for the build lock on $dir"
    fi
    HD_LOCKED="${HD_LOCKED:-} $dir"
    HD_LOCK_FDS="${HD_LOCK_FDS:-} $HD_LOCK_FD"
}

# hd_run <command> [args...] -- run an external command with the build-lock
# file descriptors closed.
#
# `exec {fd}>lock` descriptors are not close-on-exec, so every process the
# build spawns inherits them. That is harmless for short-lived children, but a
# daemon that survives the build (sccache's server, started by the first
# compile) keeps the flock alive long after mask has exited, and the next
# invocation then blocks on a lock nobody is using. Only this shell needs to
# hold the lock, so hand children a clean set of descriptors.
hd_run() {
    local fd redirs=""
    if [ -z "${HD_LOCK_FDS:-}" ]; then
        "$@"
        return
    fi
    for fd in $HD_LOCK_FDS; do
        redirs="$redirs $fd>&-"
    done
    eval '"$@"'"$redirs"
}

# ---------------------------------------------------------------------------
# CMake flag templates
# ---------------------------------------------------------------------------
# flake.nix exports the flag lists with $HD_* placeholders left unexpanded;
# they are filled in here once the paths above have been resolved.

hd_no_spaces() {
    case "$2" in
    *[[:space:]]*) hd_die "$1 contains whitespace ('$2'), which CMake flag expansion cannot represent" ;;
    esac
}

# hd_expand_flags <template> -> flags, one per line
hd_expand_flags() {
    local t=$1
    case "$t" in
    *'`'* | *'$('* | *';'*) hd_die "refusing to expand a CMake flag template containing shell metacharacters" ;;
    esac
    eval "printf '%s\n' $t"
}

# hd_cmake_flags <template var name> -> populates the HD_FLAGS array
# The read loops in this file feed from a here-string rather than a process
# substitution: some sandboxed/container shells (toolbox, seccomp-mediated
# shells) do not resolve /dev/fd/<n>, and bash then fails `< <(...)` with
# "/dev/fd/63: No such file or directory". A here-string keeps the loop in the
# current shell (so assignments survive) without needing /dev/fd.
hd_cmake_flags() {
    local line
    HD_FLAGS=()
    while IFS= read -r line; do
        [ -n "$line" ] && HD_FLAGS+=("$line")
    done <<< "$(hd_expand_flags "${!1}")"
}

# ---------------------------------------------------------------------------
# Configure
# ---------------------------------------------------------------------------

hd_report_deps() {
    local k v
    while [ "$#" -gt 1 ]; do
        k=$1
        v=$2
        shift 2
        printf '    %-14s %s\n' "$k" "$v" >&2
    done
}

# Record what a configure resolved to, so later build/test invocations in this
# worktree reuse it without repeating the flags.
hd_record() {
    local wt=$1
    shift
    while [ "$#" -gt 1 ]; do
        hd_pin_set "$wt" "$1" "$2"
        shift 2
    done
}

hd_prepare_build_dir() {
    local build=$1
    if [ -n "$HD_OPT_FRESH" ] && [ -d "$build" ]; then
        hd_log "removing $build (--fresh)"
        rm -rf "$build"
    fi
    mkdir -p "$build"
}

# hd_configure_llvm <llvm worktree>
#
# The integrated build: LLVM + Clang + OffloadTest as an external project, i.e.
# the tree that provides check-clang, check-llvm and the check-hlsl-* suites.
hd_configure_llvm() {
    local wt=$1 build offload golden dxcbin
    build=$(hd_build_dir "$wt")
    offload=$(hd_dep offload "$wt")
    golden=$(hd_dep golden "$wt")
    dxcbin=$(hd_dxc_bin_dir "$wt")

    export HD_LLVM_SRC=$wt
    export HD_OFFLOAD_SRC=$offload
    export HD_GOLDEN_DIR=$golden
    export HD_DXC_BIN_DIR=$dxcbin
    export HD_INSTALL_PREFIX="$build/install"
    HD_BUILD_TYPE=$(hd_build_type "$wt")
    export HD_BUILD_TYPE
    hd_no_spaces "llvm worktree" "$wt"
    hd_no_spaces "offload-test-suite worktree" "$offload"
    hd_no_spaces "build directory" "$build"

    hd_log "configuring llvm-project: $wt"
    hd_report_deps \
        "build dir" "$build" \
        "build type" "$HD_BUILD_TYPE" \
        "offload" "$offload" \
        "golden" "$golden" \
        "dxc" "$dxcbin"

    hd_git_exclude "$wt"
    hd_prepare_build_dir "$build"
    hd_lock "$build"
    hd_cmake_flags HLSL_CMAKE_FLAGS_LLVM
    hd_run cmake -S "$wt/llvm" -B "$build" "${HD_FLAGS[@]}"

    hd_record "$wt" BUILD_DIR "$build" BUILD_TYPE "$HD_BUILD_TYPE" \
        OFFLOAD "$offload" GOLDEN "$golden" DXC "$dxcbin"
}

# hd_configure_dxc <dxc worktree>
hd_configure_dxc() {
    local wt=$1 build
    build=$(hd_build_dir "$wt")
    export HD_DXC_SRC=$wt
    HD_BUILD_TYPE=$(hd_build_type "$wt")
    export HD_BUILD_TYPE
    hd_no_spaces "DXC worktree" "$wt"
    hd_no_spaces "build directory" "$build"

    hd_log "configuring DirectXShaderCompiler: $wt"
    hd_report_deps "build dir" "$build" "build type" "$HD_BUILD_TYPE"

    hd_git_exclude "$wt"
    hd_prepare_build_dir "$build"
    hd_lock "$build"
    hd_cmake_flags HLSL_CMAKE_FLAGS_DXC
    hd_run cmake -S "$wt" -B "$build" "${HD_FLAGS[@]}"

    hd_record "$wt" BUILD_DIR "$build" BUILD_TYPE "$HD_BUILD_TYPE"
}

# hd_configure_offload <offload worktree>
#
# standalone: this worktree is the top-level CMake project and links against an
#             already-installed LLVM distribution (see hd_dist). Configures in
#             seconds and builds in a couple of minutes.
# integrated: no build of its own -- the llvm worktree's build tree is
#             configured to pull this source directory in as OffloadTest.
hd_configure_offload() {
    local wt=$1 mode build llvm dist golden dxcbin
    mode=$(hd_mode "$wt")
    llvm=$(hd_dep llvm "$wt")

    if [ "$mode" = "integrated" ]; then
        hd_log "offload worktree $wt is in integrated mode; configuring $llvm against it"
        HD_OPT_OFFLOAD=$wt
        hd_pin_set "$wt" MODE integrated
        hd_pin_set "$wt" LLVM "$llvm"
        hd_configure_llvm "$llvm"
        return
    fi

    build=$(hd_build_dir "$wt")
    dist=$(hd_dist_prefix "$llvm")
    golden=$(hd_dep golden "$wt")
    dxcbin=$(hd_dxc_bin_dir "$wt")

    if [ ! -f "$dist/lib/cmake/llvm/LLVMConfig.cmake" ]; then
        hd_die "no LLVM distribution installed for $(basename "$llvm") (looked in $dist).
       Build one with:  mask dist --in $(basename "$llvm")
       or point this worktree at another one:  mask configure --llvm <worktree>"
    fi

    export HD_OFFLOAD_SRC=$wt
    export HD_LLVM_SRC=$llvm
    export HD_LLVM_CMAKE_DIR="$dist/lib/cmake/llvm"
    export HD_GOLDEN_DIR=$golden
    export HD_DXC_BIN_DIR=$dxcbin
    export HD_INSTALL_PREFIX="$build/install"
    HD_BUILD_TYPE=$(hd_build_type "$wt")
    export HD_BUILD_TYPE
    hd_no_spaces "offload-test-suite worktree" "$wt"
    hd_no_spaces "llvm worktree" "$llvm"
    hd_no_spaces "build directory" "$build"

    hd_log "configuring offload-test-suite (standalone): $wt"
    hd_report_deps \
        "build dir" "$build" \
        "build type" "$HD_BUILD_TYPE" \
        "llvm src" "$llvm" \
        "llvm dist" "$dist" \
        "golden" "$golden" \
        "dxc" "$dxcbin"

    hd_git_exclude "$wt"
    hd_prepare_build_dir "$build"
    hd_lock "$build"
    hd_cmake_flags HLSL_CMAKE_FLAGS_OFFLOAD
    hd_run cmake -S "$wt" -B "$build" "${HD_FLAGS[@]}"

    hd_record "$wt" BUILD_DIR "$build" BUILD_TYPE "$HD_BUILD_TYPE" MODE standalone \
        LLVM "$llvm" GOLDEN "$golden" DXC "$dxcbin"
    if [ -n "${HD_OPT_DIST_PREFIX:-}" ]; then
        hd_pin_set "$llvm" DIST_PREFIX "$dist"
    fi
}

# hd_configure <worktree> -- dispatches on the kind of checkout.
hd_configure() {
    case "$(hd_kind "$1")" in
    llvm) hd_configure_llvm "$1" ;;
    dxc) hd_configure_dxc "$1" ;;
    offload) hd_configure_offload "$1" ;;
    *) hd_die "don't know how to configure $1" ;;
    esac
}

# hd_dist <llvm worktree> -- configure + build + install the LLVM half of the
# standalone offload distribution (clang, lit tools, LLVM libraries and CMake
# exports). Shared by every offload worktree that points at this llvm worktree.
hd_dist() {
    local wt=$1 build prefix offload
    build=$(hd_dist_build_dir "$wt")
    prefix=$(hd_dist_prefix "$wt")
    offload=$(hd_dep offload "$wt")

    export HD_LLVM_SRC=$wt
    export HD_OFFLOAD_SRC=$offload
    export HD_INSTALL_PREFIX=$prefix
    HD_BUILD_TYPE=${HD_OPT_BUILD_TYPE:-$(hd_cache_get "$build" CMAKE_BUILD_TYPE)}
    export HD_BUILD_TYPE=${HD_BUILD_TYPE:-Release}
    hd_no_spaces "llvm worktree" "$wt"
    hd_no_spaces "install prefix" "$prefix"

    hd_log "building the standalone LLVM distribution for $wt"
    hd_report_deps \
        "build dir" "$build" \
        "build type" "$HD_BUILD_TYPE" \
        "prefix" "$prefix" \
        "cache from" "$offload/cmake/caches/StandaloneDistribution.cmake"

    hd_git_exclude "$wt"
    hd_prepare_build_dir "$build"
    hd_lock "$build"
    hd_cmake_flags HLSL_CMAKE_FLAGS_LLVM_DIST
    hd_run cmake -S "$wt/llvm" -B "$build" "${HD_FLAGS[@]}"
    hd_run cmake --build "$build" --target install-distribution

    hd_pin_set "$wt" DIST_PREFIX "$prefix"
    hd_log "installed distribution: $prefix"
}

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

# hd_effective_build <worktree> -> the build directory that actually compiles
# this worktree's sources. Everything is built in place except an
# offload-test-suite worktree in integrated mode, which is built by the llvm
# worktree that includes it.
hd_effective_build() {
    local wt=$1 llvm
    if [ "$(hd_kind "$wt")" = "offload" ] && [ "$(hd_mode "$wt")" = "integrated" ]; then
        llvm=$(hd_dep llvm "$wt")
        hd_build_dir "$llvm"
        return 0
    fi
    hd_build_dir "$wt"
}

hd_ensure_configured() {
    local wt=$1 build
    build=$(hd_effective_build "$wt")
    if [ ! -f "$build/build.ninja" ] && [ ! -f "$build/Makefile" ]; then
        hd_configure "$wt"
    fi
}

# ---------------------------------------------------------------------------
# Sandbox symlink guard
# ---------------------------------------------------------------------------
#
# Many build steps publish a tool into <build>/bin as a symlink and recreate
# it with `cmake -E create_symlink` / `cmake -E cmake_symlink_executable`:
# the spirv-tools wrappers and dxil-dis (always-run custom targets), and every
# llvm_add_tool_symlink alias such as bin/clang -> clang-24, bin/llvm-strip,
# bin/llvm-readelf. All of them unlink the destination before recreating it.
#
# Inside a sandboxed agent shell (pi's landstrip) unlink() on a *symlink*
# deletes the file the link points at and leaves the link behind. The build
# therefore destroys the binary it just published and then dies with
#
#     CMake Error: failed to create symbolic link '.../bin/spirv-as': File exists
#     CMake Error: cmake_symlink_executable: System Error: File exists
#
# For clang that means the 1.4 GB clang-24 is deleted immediately after being
# linked, and every retry deletes it again. For the spirv tools it also leaves
# the tree wedged, because the ExternalProject stamps still claim the tools
# are built. Creating a symlink whose name does not exist yet is unaffected,
# so clearing the destinations before the build keeps the normal build graph
# -- SPIRVTools ExternalProject included -- completely intact.
#
# Deleting the link itself is only safe once it dangles, so a live link is
# cleared by moving its target aside, deleting the now-dangling link, and
# moving the target back. Anything the build did not recreate is restored
# afterwards, so a partial build can never leave a tool alias missing.

# Extra fragile symlinks outside <build>/bin, relative to the build directory.
# Everything directly under <build>/bin is discovered automatically.
HD_FRAGILE_SYMLINKS="lib/libpng.a"
HD_CLEARED_SYMLINKS=()

# True when unlink() on a symlink destroys the target instead of the link,
# i.e. when we are running under a sandbox that rewrites path syscalls.
hd_unlink_follows_symlinks() {
    local d rc=1
    d=$(mktemp -d) || return 1
    : >"$d/target"
    ln -s "$d/target" "$d/link"
    rm -f "$d/link" 2>/dev/null
    [ -e "$d/target" ] || rc=0
    rm -rf "$d" 2>/dev/null
    return $rc
}

# Every build-created symlink the next build may republish. Anything directly
# under bin/ qualifies: those are all tool aliases produced by
# llvm_add_tool_symlink / cmake_symlink_executable / create_symlink.
hd_fragile_symlinks() {
    local build=$1 rel
    find "$build/bin" -maxdepth 1 -type l -print 2>/dev/null
    for rel in $HD_FRAGILE_SYMLINKS; do
        [ -L "$build/$rel" ] && printf '%s\n' "$build/$rel"
    done
    return 0
}

hd_clear_fragile_symlinks() {
    local build=$1 link text target tmp list
    HD_CLEARED_SYMLINKS=()
    hd_unlink_follows_symlinks || return 0
    # A pipeline would run the loop in a subshell and lose the array, and
    # process substitution needs /dev/fd, which a sandbox may not provide.
    list=$(hd_fragile_symlinks "$build")
    [ -n "$list" ] || return 0
    while IFS= read -r link; do
        [ -L "$link" ] || continue
        text=$(readlink "$link") || continue
        if [ -e "$link" ]; then
            target=$(readlink -f "$link") || continue
            # Only touch links whose target we own. A link into a read-only
            # tree such as /nix/store can be neither moved nor deleted here.
            case $target in
            "$build"/*) ;;
            *) continue ;;
            esac
            # Make the link dangle first: renaming a regular file is safe,
            # deleting a dangling link is safe, deleting a live one is not.
            tmp=$target.hd-symlink-guard.$$
            mv "$target" "$tmp" 2>/dev/null || continue
            rm -f "$link"
            mv "$tmp" "$target"
        else
            rm -f "$link" || continue
        fi
        HD_CLEARED_SYMLINKS+=("$link"$'\t'"$text")
    done <<<"$list"
    [ "${#HD_CLEARED_SYMLINKS[@]}" -gt 0 ] &&
        hd_log "cleared ${#HD_CLEARED_SYMLINKS[@]} sandbox-fragile symlink(s) under $build"
    return 0
}

# Recreate anything the build did not republish itself, so that building an
# unrelated target cannot leave a tool alias missing.
hd_restore_fragile_symlinks() {
    local entry link text resolved
    for entry in ${HD_CLEARED_SYMLINKS+"${HD_CLEARED_SYMLINKS[@]}"}; do
        link=${entry%%$'\t'*}
        text=${entry#*$'\t'}
        if [ -e "$link" ] || [ -L "$link" ]; then continue; fi
        case $text in
        /*) resolved=$text ;;
        *) resolved=$(dirname "$link")/$text ;;
        esac
        [ -e "$resolved" ] || continue
        ln -s "$text" "$link" 2>/dev/null
    done
    HD_CLEARED_SYMLINKS=()
}
# hd_build <worktree> [target...]
hd_build() {
    local wt=$1 build rc=0
    shift
    hd_ensure_configured "$wt"
    build=$(hd_effective_build "$wt")
    hd_lock "$build"
    hd_clear_fragile_symlinks "$build"
    if [ "$#" -gt 0 ] && [ -n "$1" ]; then
        hd_log "building $* in $build"
        hd_run cmake --build "$build" --target "$@" || rc=$?
    else
        hd_log "building in $build"
        hd_run cmake --build "$build" || rc=$?
    fi
    hd_restore_fragile_symlinks "$build"
    return $rc
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

HD_SUITES="d3d12 vk mtl warp-d3d12 clang-d3d12 clang-vk clang-mtl clang-warp-d3d12"

# hd_test_root <worktree> <build dir> -> directory holding the per-suite lit
# trees, which differs between the integrated and standalone layouts.
hd_test_root() {
    if [ -d "$2/tools/OffloadTest/test" ]; then
        printf '%s\n' "$2/tools/OffloadTest/test"
    else
        printf '%s\n' "$2/test"
    fi
}

# hd_suite_src <suite dir in the build tree> -> the offload-test-suite source
# tree that suite was configured from, as recorded in its lit.site.cfg.py.
# Only the suite directories exist in a build tree; the tests themselves are
# read from the source tree, and lit maps a build-tree path onto them.
hd_suite_src() {
    [ -f "$1/lit.site.cfg.py" ] || return 0
    sed -n 's|^config.offloadtest_src_root = path(r"\(.*\)")|\1|p' "$1/lit.site.cfg.py" | tail -n 1
}

# Switch an already-configured build tree to a different dxc without a full
# reconfigure of everything else. DXC_EXECUTABLE / DXV_EXECUTABLE are
# find_program caches and SUPPORTS_SPIRV is probed by running dxc, so all three
# have to be refreshed together.
hd_sync_dxc() {
    local build=$1 dxcbin=$2 current
    current=$(hd_cache_get "$build" DXC_DIR)
    [ "$current" = "$dxcbin" ] && return 0
    hd_log "switching this build tree to dxc from $dxcbin (was ${current:-unset})"
    hd_run cmake -S "$(hd_cache_get "$build" CMAKE_HOME_DIRECTORY)" -B "$build" \
        -DDXC_DIR="$dxcbin" \
        -DDXC_EXECUTABLE="$dxcbin/dxc" \
        -DDXV_EXECUTABLE="$dxcbin/dxv" \
        -USUPPORTS_SPIRV >/dev/null
}

# hd_lit <build dir> [args...]
hd_lit() {
    local build=$1
    shift
    local lit="$build/bin/llvm-lit"
    [ -x "$lit" ] || hd_die "$lit not found; run 'mask build' first"
    hd_log "$lit $*"
    hd_run "$lit" "$@"
}
