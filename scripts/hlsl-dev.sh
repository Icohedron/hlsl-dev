# shellcheck shell=bash
#
# hlsl-dev.sh -- worktree-aware resolution helpers shared by the task scripts
# in scripts/tasks/, which devenv exposes on PATH as `hlsl-<task>`.
#
# Every task that touches a checkout starts with:
#
#     source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"
#     hd_parse "$@"
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
# $HD_QUIET_WARNINGS silences the advisory ones: a task that asks the same
# question about the same worktree once per platform would otherwise repeat the
# same paragraph a screenful of times.
hd_warn() { [ -n "${HD_QUIET_WARNINGS:-}" ] || printf 'warning: %s\n' "$*" >&2; }
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

# hd_kind_named <name> -> llvm|dxc|offload|golden for a repository named by
# its kind ("llvm") or by its directory ("llvm-project"), 1 when it is
# neither. For tasks that act on a whole repository rather than one worktree.
hd_kind_named() {
    local k name=${1%/}
    for k in llvm dxc offload golden; do
        if [ "$name" = "$k" ] ||
            [ "$name" = "$(hd_kind_label "$k")" ] ||
            [ "$name" = "$(hd_repo_name "$k")" ]; then
            printf '%s\n' "$k"
            return 0
        fi
    done
    return 1
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

# The workspace root is the directory holding devenv.nix. Inside the developer
# environment devenv exports DEVENV_ROOT for exactly this purpose (and it is
# the project directory, not $PWD, so it stays correct in a subdirectory);
# HLSL_DEV_ROOT, set by enterShell, is kept as the overridable spelling.
hd_find_root() {
    if [ -n "${HLSL_DEV_ROOT:-}" ] && [ -f "$HLSL_DEV_ROOT/devenv.nix" ]; then
        printf '%s\n' "$HLSL_DEV_ROOT"
        return 0
    fi
    if [ -n "${DEVENV_ROOT:-}" ] && [ -f "$DEVENV_ROOT/devenv.nix" ]; then
        (cd "$DEVENV_ROOT" && pwd -P)
        return 0
    fi
    local d
    d=$(pwd -P)
    while [ "$d" != "/" ]; do
        if [ -f "$d/devenv.nix" ] && [ -f "$d/scripts/hlsl-dev.sh" ]; then
            printf '%s\n' "$d"
            return 0
        fi
        d=$(dirname "$d")
    done
    hd_die "cannot find the workspace root (devenv.nix); set HLSL_DEV_ROOT"
}

hd_state_dir() { printf '%s\n' "${HLSL_DEV_STATE:-$HD_ROOT/.hlsl-dev}"; }

# ---------------------------------------------------------------------------
# Command-line parsing
# ---------------------------------------------------------------------------
# The tasks are plain executables that devenv puts on PATH as `hlsl-<task>`,
# so option parsing lives here: one implementation, one spelling of each flag,
# and one place that documents them.
#
# A task declares what it accepts, then parses:
#
#     HD_TASK_ARGS="[target]"                # positional arguments, for --help
#     HD_TASK_DESC="Build the worktree ..."  # one or more lines
#     HD_TASK_OPTS="in= llvm= fresh"         # `name=` takes a value, `name` is a switch
#     hd_parse "$@"
#
# and afterwards reads each option from the variable of the same name ($in,
# $llvm, $fresh, ...) and the positional arguments from the HD_ARGV array. An
# option name is its flag with dashes turned into underscores, so --build-type
# sets $build_type. Every task also gets --help for free.

# The shared option catalogue: a flag means the same thing in every task, so
# every task's --help describes it the same way.
hd_opt_desc() {
    # shellcheck disable=SC2016 # `nix` is a literal option value, not an expansion
    case "$1" in
    in) printf 'Worktree to act on: path, directory name, suffix or branch (default: the current directory)' ;;
    llvm) printf 'llvm-project worktree to build/test against' ;;
    dxc) printf 'DirectXShaderCompiler worktree, a directory holding dxc/dxv, or `nix`' ;;
    offload) printf 'offload-test-suite worktree an llvm build includes as OffloadTest' ;;
    dist_prefix) printf 'Install prefix of an LLVM standalone distribution (e.g. an unpacked CI artifact)' ;;
    platform) printf 'Cross-compile for this platform instead of this machine (see '"'"'hlsl-cross'"'"')' ;;
    build_type) printf 'CMake build type: Debug, Release, RelWithDebInfo, MinSizeRel' ;;
    fresh) printf 'Start from scratch instead of reusing what is already there' ;;
    forget) printf 'Forget this worktree'"'"'s remembered dependencies before applying the flags' ;;
    dry_run) printf 'Print what would be built and configured, and stop' ;;
    no_auto) printf 'Fail instead of building a missing prerequisite (LLVM distribution, dxc)' ;;
    lit_args) printf 'Extra arguments for llvm-lit (default -v); use --lit-args=-x for a value starting with a dash' ;;
    out) printf 'Write the archive here instead of next to the build tree' ;;
    no_offload) printf 'Package only the compiler and lit tooling, leaving the offload test suite out' ;;
    jobs) printf 'Build this many targets at once (default: one per core)' ;;
    dist) printf 'Also act on the standalone distribution build and install prefix' ;;
    all) printf 'Act on every worktree of every repository' ;;
    all_build_dirs) printf 'Every build tree of the worktree whatever its name (build, build-container, the cross trees), not just this environment'"'"'s' ;;
    from) printf 'Worktree to seed a missing index from (default: any worktree of that repository that has one)' ;;
    restore_gitignore) printf 'Restore .gitignore exactly as git has it, and stop hiding it' ;;
    fetch) printf 'Fetch from origin afterwards' ;;
    since) printf 'Compare against this revision instead of looking at what is staged' ;;
    diff) printf 'Print the patch instead of a summary' ;;
    fix) printf 'Apply the change instead of reporting it' ;;
    install_hooks) printf 'Install the clang-format pre-commit hook in every checkout that has a .clang-format' ;;
    uninstall_hooks) printf 'Remove the hook again' ;;
    check_hooks) printf 'Exit non-zero when a hook is missing or out of date, and print nothing' ;;
    quiet) printf 'Say nothing when there is nothing to report' ;;
    *) printf '(no description)' ;;
    esac
}

# hd_opt_kind <name> -> value | switch | "" (not accepted by this task)
hd_opt_kind() {
    local entry
    for entry in ${HD_TASK_OPTS:-}; do
        case "$entry" in
        "$1=") printf 'value\n'; return 0 ;;
        "$1") printf 'switch\n'; return 0 ;;
        esac
    done
}

hd_usage() {
    local entry name flag override
    printf 'usage: %s%s%s\n' "$HD_TASK_NAME" \
        "${HD_TASK_OPTS:+ [options]}" "${HD_TASK_ARGS:+ $HD_TASK_ARGS}"
    [ -z "${HD_TASK_DESC:-}" ] || printf '\n%s\n' "$HD_TASK_DESC"
    if [ -n "${HD_TASK_OPTS:-}" ]; then
        printf '\noptions:\n'
        for entry in $HD_TASK_OPTS; do
            name=${entry%=}
            flag="--${name//_/-}"
            [ "$entry" = "$name" ] || flag="$flag <value>"
            # A task may say what a shared flag means *there* by setting
            # HD_DESC_<name>: --dry-run prints a build plan in one task and a
            # list of directories in another.
            override="HD_DESC_$name"
            printf '  %-24s %s\n' "$flag" "${!override:-$(hd_opt_desc "$name")}"
        done
    fi
    printf '  %-24s %s\n' "--help" "Show this message"
    [ -z "${HD_TASK_HELP:-}" ] || printf '\n%s\n' "$HD_TASK_HELP"
}

hd_parse() {
    local entry name kind

    # devenv's wrapper passes the command name in; a direct run of the file
    # falls back to its own name.
    HD_TASK_NAME=${HD_TASK_NAME:-$(basename "$0" .sh)}
    HD_ARGV=()

    # Start every declared option empty: a task reads plain variable names, and
    # a stray `mode`/`from`/`all` inherited from the caller's environment must
    # not be mistaken for a flag. The HLSL_* defaults are applied by hd_init.
    for entry in ${HD_TASK_OPTS:-}; do
        printf -v "${entry%=}" '%s' ''
    done

    while [ $# -gt 0 ]; do
        case "$1" in
        -h | --help)
            hd_usage
            exit 0
            ;;
        --)
            shift
            while [ $# -gt 0 ]; do
                HD_ARGV+=("$1")
                shift
            done
            ;;
        --*=*)
            name=${1%%=*}
            name=${name#--}
            name=${name//-/_}
            [ -n "$(hd_opt_kind "$name")" ] ||
                hd_die "unknown option '${1%%=*}' (try '$HD_TASK_NAME --help')"
            printf -v "$name" '%s' "${1#*=}"
            shift
            ;;
        --*)
            name=${1#--}
            name=${name//-/_}
            kind=$(hd_opt_kind "$name")
            case "$kind" in
            switch)
                printf -v "$name" '%s' 'true'
                shift
                ;;
            value)
                [ $# -ge 2 ] || hd_die "option '$1' needs a value"
                printf -v "$name" '%s' "$2"
                shift 2
                ;;
            *) hd_die "unknown option '$1' (try '$HD_TASK_NAME --help')" ;;
            esac
            ;;
        *)
            HD_ARGV+=("$1")
            shift
            ;;
        esac
    done
}

# hd_need_args <count> -- fail with the usage message when a required
# positional argument is missing.
hd_need_args() {
    [ "${#HD_ARGV[@]}" -ge "$1" ] && return 0
    hd_usage >&2
    exit 2
}

# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------
# Copies the parsed options into namespaced HD_OPT_* variables, applying the
# HLSL_* environment defaults, so that helper functions have a single place to
# look and task-local names cannot collide with them.

# Just the workspace root: for tasks that need the layout but not a build
# environment (vk-use, setup, update-submodules, ...).
hd_init_root() {
    HD_ROOT=$(hd_find_root)
    export HD_ROOT
}

hd_init() {
    hd_init_root

    HD_OPT_IN=${in:-${HLSL_WT:-}}
    HD_OPT_LLVM=${llvm:-${HLSL_LLVM:-}}
    HD_OPT_DXC=${dxc:-${HLSL_DXC:-}}
    HD_OPT_OFFLOAD=${offload:-${HLSL_OFFLOAD:-}}
    HD_OPT_GOLDEN=${HLSL_GOLDEN:-}
    HD_OPT_BUILD_DIR=${HLSL_BUILD_DIR:-}
    HD_OPT_BUILD_TYPE=${build_type:-${HLSL_BUILD_TYPE:-}}
    HD_OPT_DIST_PREFIX=${dist_prefix:-${HLSL_DIST_PREFIX:-}}
    HD_OPT_FRESH=${fresh:-}

    # Which machine the binaries are for. Empty means this one; anything else
    # moves the build directory, the pins and the flags (see hd_platform).
    HD_OPT_PLATFORM=${platform:-${HLSL_PLATFORM:-native}}
    hd_platform_check "$HD_OPT_PLATFORM"

    # How many compilations at once. Ninja's default is one per core, which is
    # the right answer on a workstation and the wrong one in a container with a
    # process limit (a build of 64 jobs x compiler x sccache client hits a
    # pids cgroup cap and dies as "posix_spawn: Resource temporarily
    # unavailable"). cmake --build --parallel is what it reaches.
    HD_OPT_JOBS=${jobs:-${HLSL_JOBS:-}}
    case "$HD_OPT_JOBS" in
    "" | *[!0-9]*) [ -z "$HD_OPT_JOBS" ] || hd_die "--jobs takes a number, not '$HD_OPT_JOBS'" ;;
    esac

    # A missing prerequisite is built rather than reported, unless the caller
    # says otherwise. --dry-run implies it: a plan never builds anything.
    HD_DRY_RUN=${dry_run:-}
    HD_AUTO=1
    if [ -n "${no_auto:-}" ] || [ "${HLSL_AUTO:-1}" = "0" ]; then HD_AUTO=""; fi

    if [ -z "${HLSL_CMAKE_FLAGS_LLVM:-}" ]; then
        hd_die "CMake flag templates are missing from the environment;
       enter the developer environment first ('devenv shell')"
    fi

    # Every task runs against the Vulkan driver chosen *now*, so switching it
    # takes effect on the next command instead of the next shell.
    hd_vk_export

    # enterShell mirrors this machine's system include directories into
    # C_INCLUDE_PATH/CPLUS_INCLUDE_PATH so that clang-tidy can find them, and
    # nixpkgs' cmake reads NIXPKGS_CMAKE_PREFIX_PATH -- the native profile --
    # as a search prefix for every find_package. Both are answers about *this*
    # machine: left in place, a cross build compiles the target's sources
    # against this machine's glibc headers and links this machine's libraries.
    # A cross build gets all of that from its toolchain file and nowhere else.
    if hd_is_cross; then
        unset C_INCLUDE_PATH CPLUS_INCLUDE_PATH
        unset NIXPKGS_CMAKE_PREFIX_PATH CMAKE_PREFIX_PATH
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
#
# hd_try_resolve is the same lookup without the verdict: it returns 1 instead
# of printing an error and exiting, for callers that have somewhere else to go.
hd_try_resolve() {
    local kind=$1 spec=$2 cand="" wt b
    [ -n "$spec" ] || return 1

    if [ -d "$spec" ]; then
        cand=$(hd_abs "$spec")
    elif [ -d "$HD_ROOT/$spec" ]; then
        cand=$(hd_abs "$HD_ROOT/$spec")
    fi
    if [ -n "$cand" ]; then
        [ "$(hd_kind "$cand")" = "$kind" ] || return 1
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

    return 1
}

hd_resolve() {
    local kind=$1 spec=$2 wt cand
    [ -n "$spec" ] || return 1

    if wt=$(hd_try_resolve "$kind" "$spec") && [ -n "$wt" ]; then
        printf '%s\n' "$wt"
        return 0
    fi

    # A directory that is there but is the wrong thing deserves to be said so.
    cand=""
    if [ -d "$spec" ]; then
        cand=$(hd_abs "$spec")
    elif [ -d "$HD_ROOT/$spec" ]; then
        cand=$(hd_abs "$HD_ROOT/$spec")
    fi
    [ -z "$cand" ] ||
        hd_die "$cand is not a $(hd_kind_label "$kind") checkout"

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
#
# A value that names something inside the workspace is stored relative to the
# workspace root, spelled "./<path>", because the same store is read through
# more than one path: the dev container mounts the workspace at
# /workspaces/<name> while the host has it wherever it cloned it, and an
# absolute pin written on one side resolves to nothing on the other. Values
# that are not workspace paths -- BUILD_TYPE, MODE, a dxc outside the tree,
# the literal "nix" -- are stored as they are.

# hd_pin_rel <value> -> the value as it is stored.
hd_pin_rel() {
    case "$1" in
    "$HD_ROOT") printf '.\n' ;;
    "$HD_ROOT"/*) printf './%s\n' "${1#"$HD_ROOT"/}" ;;
    *) printf '%s\n' "$1" ;;
    esac
}

# hd_pin_abs <value> -> the value as it is used.
hd_pin_abs() {
    case "$1" in
    .) printf '%s\n' "$HD_ROOT" ;;
    ./*) printf '%s/%s\n' "$HD_ROOT" "${1#./}" ;;
    *) printf '%s\n' "$1" ;;
    esac
}

hd_key() {
    local p=${1%/}
    p=${p#"$HD_ROOT"/}
    printf '%s\n' "$(printf '%s' "$p" | tr '/' '%')"
}

# A pin file belongs to one worktree *and* one platform: what a Windows build
# was configured with is not what the native one was. The native file is the
# fallback, so a `--llvm` said once still applies to every platform -- which
# checkout to build against does not change because the binaries do.
hd_pin_file() {
    printf '%s\n' "$(hd_state_dir)/pins/$(hd_key "$1")$(hd_platform_suffix | tr . @).env"
}

# hd_pin_get <worktree> <KEY>
hd_pin_get() {
    local f v
    f=$(hd_pin_file "$1")
    if [ ! -f "$f" ] || ! grep -q "^$2=" "$f"; then
        # Fall back to what the native build of this worktree remembers.
        f="$(hd_state_dir)/pins/$(hd_key "$1").env"
    fi
    [ -f "$f" ] || return 0
    v=$(sed -n "s/^$2=//p" "$f" | tail -n 1)
    [ -n "$v" ] || return 0
    hd_pin_abs "$v"
}

# hd_pin_set <worktree> <KEY> <value>   (empty value removes the pin)
#
# The other keys are rewritten too, which is what migrates a store written
# before pins were relative: one configure and the file is portable again.
hd_pin_set() {
    local f tmp line
    [ -z "${HD_DRY_RUN:-}" ] || return 0
    f=$(hd_pin_file "$1")
    mkdir -p "$(dirname "$f")"
    tmp=$(mktemp "$f.XXXXXX")
    if [ -f "$f" ]; then
        while IFS= read -r line; do
            case "$line" in
            "$2="* | '') continue ;;
            *=*) printf '%s=%s\n' "${line%%=*}" "$(hd_pin_rel "${line#*=}")" ;;
            *) printf '%s\n' "$line" ;;
            esac
        done <"$f" >"$tmp"
    fi
    [ -n "$3" ] && printf '%s=%s\n' "$2" "$(hd_pin_rel "$3")" >>"$tmp"
    mv "$tmp" "$f"
}

hd_pin_clear() {
    local f p
    f=$(hd_pin_file "$1")
    rm -f "$f"
    # Forgetting on this machine forgets what the cross builds of the same
    # worktree remember too: they are the same worktree, and a --forget that
    # left half the memory behind would be the confusing one.
    if ! hd_is_cross; then
        for p in $HD_ALL_PLATFORMS; do
            rm -f "$(hd_state_dir)/pins/$(hd_key "$1")@$p.env"
        done
    fi
}

# ---------------------------------------------------------------------------
# Workspace settings
# ---------------------------------------------------------------------------
# Choices that belong to the machine rather than to a checkout -- which Vulkan
# driver to run against, whether to build D3D12 support. They live in one
# key=value file next to the pins, so a change applies to the next command
# rather than to the next shell.

hd_settings_file() { printf '%s\n' "$(hd_state_dir)/settings.env"; }

hd_setting_get() {
    local f
    f=$(hd_settings_file)
    [ -f "$f" ] || return 0
    sed -n "s/^$1=//p" "$f" | tail -n 1
}

# hd_setting_set <KEY> <value>   (an empty value removes it)
hd_setting_set() {
    local f tmp
    [ -z "${HD_DRY_RUN:-}" ] || return 0
    f=$(hd_settings_file)
    mkdir -p "$(dirname "$f")"
    tmp=$(mktemp "$f.XXXXXX")
    if [ -f "$f" ]; then grep -v "^$1=" "$f" >"$tmp" || true; fi
    [ -n "$2" ] && printf '%s=%s\n' "$1" "$2" >>"$tmp"
    mv "$tmp" "$f"
}

# ---------------------------------------------------------------------------
# Vulkan driver (ICD)
# ---------------------------------------------------------------------------
# The vk / clang-vk suites execute SPIR-V, so the loader has to be pointed at a
# driver. It loads *every* manifest it can find and calls into each one from
# vkEnumeratePhysicalDevices, so one broken driver takes down the process --
# under WSL, Mesa's dzn segfaults there and no test can run. VK_DRIVER_FILES is
# the only lever that helps, because it replaces the discovery entirely, and
# offload-test-suite/test/lit.cfg.py already forwards it into the tests.
#
# One resolver serves all of it: hd_init exports the result for every task, and
# the shell hook exports it once via `hlsl-vk --export` so that a manual
# vulkaninfo or offloader run in the shell is covered too.

# hd_vk_driver -> the name in effect: $HLSL_VK_DRIVER, else the saved choice,
# else lavapipe (Mesa's CPU rasterizer: slow, but it works everywhere).
hd_vk_driver() {
    if [ -n "${HLSL_VK_DRIVER:-}" ]; then
        printf '%s\n' "$HLSL_VK_DRIVER"
        return 0
    fi
    printf '%s\n' "$(hd_setting_get VK_DRIVER)"
}

# hd_vk_icd -> the manifest to pin the loader to, empty to let it discover
# drivers itself ("system"). Unknown names resolve to a Mesa ICD by convention:
# radeon -> radeon_icd.<arch>.json.
hd_vk_icd() {
    local driver
    driver=$(hd_vk_driver)
    case "${driver:-lavapipe}" in
    system) ;;
    lavapipe | lvp) printf '%s/lvp_icd.%s.json\n' "${HLSL_VK_ICD_DIR:-}" "$(uname -m)" ;;
    /*) printf '%s\n' "$driver" ;;
    *) printf '%s/%s_icd.%s.json\n' "${HLSL_VK_ICD_DIR:-}" "$driver" "$(uname -m)" ;;
    esac
}

# Point the loader at that manifest, or take it out of the way for "system".
# VK_DRIVER_FILES is honoured by loader >= 1.3.207; VK_ICD_FILENAMES is the
# legacy name, kept for older loaders.
hd_vk_export() {
    local icd
    icd=$(hd_vk_icd)
    if [ -z "$icd" ]; then
        unset VK_DRIVER_FILES VK_ICD_FILENAMES
        return 0
    fi
    if [ ! -e "$icd" ]; then
        hd_warn "the Vulkan driver '$(hd_vk_driver)' resolves to a missing manifest
         ($icd); run 'hlsl-vk --list' to see what is available"
        return 0
    fi
    export VK_DRIVER_FILES="$icd"
    export VK_ICD_FILENAMES="$icd"
}

# ---------------------------------------------------------------------------
# Cross-compilation platforms
# ---------------------------------------------------------------------------
# `--platform <name>` (or $HLSL_PLATFORM) builds for another machine. Only
# clang and the offload test suite are the point of it: this is how a change is
# checked against the platforms HLSL ships on -- Windows above all -- without
# one of those machines.
#
# A platform is a name, a triple, an OS and an ABI, and everything else follows
# from those four:
#
#   build directory   <worktree>/build.<platform>, so a cross build never
#                     disturbs -- or is disturbed by -- the native one, and
#                     both can exist at once
#   pins              remembered per platform, falling back to the native ones
#                     (what an offload build builds against does not change
#                     because the binaries are for Windows)
#   flags             the native list plus HLSL_CMAKE_FLAGS_CROSS* (devenv.nix)
#   toolchain         scripts/cross/toolchains.nix, built on demand
#   tests             refused: the binaries do not run on this machine
#
# "native" is the name of the platform that is not a cross build at all, and
# is what every command means unless told otherwise.

# Every platform this workspace knows how to build for, whatever machine it is
# running on. One of them is usually the machine itself -- see hd_platforms.
HD_ALL_PLATFORMS="linux-arm64 linux-x64 windows-x64 windows-arm64"

# hd_host_platform -> the name of the platform this machine *is*, or empty when
# it is something the table does not cover (a Mac, say).
#
# It is what makes the workspace read the same on an x86-64 workstation and on
# an ARM laptop: "native" is this machine, and the platform that names it is
# not offered as a cross target, because building for yourself through a cross
# toolchain is a slower way to get the same binaries. $HLSL_HOST_PLATFORM
# overrides it -- for the self-tests, and for a machine whose uname says
# something unexpected.
hd_host_platform() {
    if [ -n "${HLSL_HOST_PLATFORM:-}" ]; then
        printf '%s\n' "$HLSL_HOST_PLATFORM"
        return 0
    fi
    case "$(uname -s)/$(uname -m)" in
    Linux/x86_64) printf 'linux-x64\n' ;;
    Linux/aarch64 | Linux/arm64) printf 'linux-arm64\n' ;;
    esac
}

# hd_platforms -> the platforms worth cross-compiling for here: all of them,
# less the one this machine already is.
hd_platforms() {
    local host p out=""
    host=$(hd_host_platform)
    for p in $HD_ALL_PLATFORMS; do
        [ "$p" = "$host" ] && continue
        out="$out${out:+ }$p"
    done
    printf '%s\n' "$out"
}


# hd_platform -> the platform in effect for this invocation.
hd_platform() { printf '%s\n' "${HD_OPT_PLATFORM:-native}"; }

# True when that is not this machine.
hd_is_cross() { [ "$(hd_platform)" != "native" ]; }

hd_platform_triple() {
    case "$1" in
    linux-arm64) printf 'aarch64-unknown-linux-gnu\n' ;;
    linux-x64) printf 'x86_64-unknown-linux-gnu\n' ;;
    windows-x64) printf 'x86_64-pc-windows-msvc\n' ;;
    windows-arm64) printf 'aarch64-pc-windows-msvc\n' ;;
    esac
}

# linux | windows -- which family of flags the build needs.
hd_platform_os() {
    case "$1" in
    linux-*) printf 'linux\n' ;;
    windows-*) printf 'windows\n' ;;
    esac
}

# msvc | gnu -- which ABI the binaries speak. Every Windows platform here is
# MSVC: D3D12 is reached through the Windows SDK's import libraries, so a
# GNU-ABI Windows build could carry neither the offload test suite nor DXC (see
# scripts/cross/toolchains.nix).
hd_platform_abi() {
    case "$1" in
    windows-*) printf 'msvc\n' ;;
    *) printf 'gnu\n' ;;
    esac
}

# hd_platform_check <name> -- accept it, or say what the names are.
hd_platform_check() {
    local p=$1 host
    [ "$p" = "native" ] && return 0
    host=$(hd_host_platform)
    if [ -n "$host" ] && [ "$p" = "$host" ]; then
        hd_die "'$p' is what this machine already is: build for it natively,
       without --platform. (A cross toolchain aimed at the host would produce
       the same binaries more slowly, in a second build tree.)"
    fi
    case " $(hd_platforms) " in
    *" $p "*) ;;
    *) hd_die "unknown platform '$p'; expected native or one of:
       $(hd_platforms)
       'hlsl-cross' describes each one and what it needs." ;;
    esac
    [ -n "${HLSL_CMAKE_FLAGS_CROSS:-}" ] || hd_die "this environment has no cross-compilation flags;
       leave and re-enter the developer environment ('direnv reload', or exit
       and 'devenv shell') so devenv.nix is evaluated again"
    [ -n "${HLSL_NIXPKGS_PATH:-}" ] || hd_die "\$HLSL_NIXPKGS_PATH is not set; re-enter the developer environment"
}

# The directory suffix a platform adds to build trees. Native adds nothing, so
# the tree a plain `hlsl-build` uses keeps the name it has always had.
hd_platform_suffix() {
    hd_is_cross || return 0
    printf '.%s\n' "$(hd_platform)"
}

# --- the Visual Studio licence ---------------------------------------------
# nixpkgs has Microsoft's headers and import libraries (windows.sdk, an xwin
# splat of the official packages) but will not build them until the licence at
# https://visualstudio.microsoft.com/license-terms/mt644918/ has been accepted.
# That is a decision for the person building, so it is asked for once and kept
# with the workspace's other choices; $HLSL_MSVC_LICENSE=accepted does it for
# one command (a CI job, say).
hd_msvc_license_accepted() {
    [ "${HLSL_MSVC_LICENSE:-}" = "accepted" ] && return 0
    [ "$(hd_setting_get MSVC_LICENSE)" = "accepted" ]
}

# hd_nixpkgs_path -> the pinned nixpkgs the cross toolchains are built from.
#
# devenv exports it as a plain string, which means nothing in the store refers
# to it and a garbage collection is free to remove the source it names -- and
# does, sooner or later, since only evaluation ever needed it. The revision is
# also written down in devenv.lock, so rather than failing on a path that is no
# longer there, the same revision is fetched again (from the binary cache, in
# seconds). Rooting it instead would mean keeping a second copy of the whole
# nixpkgs tree for the sake of a directory this workspace reads twice a month.
hd_nixpkgs_path() {
    local rev path
    if [ -n "${HLSL_NIXPKGS_PATH:-}" ] && [ -d "$HLSL_NIXPKGS_PATH" ]; then
        printf '%s\n' "$HLSL_NIXPKGS_PATH"
        return 0
    fi
    [ -f "$HD_ROOT/devenv.lock" ] ||
        hd_die "the pinned nixpkgs ($HLSL_NIXPKGS_PATH) is not in the store and
       there is no devenv.lock to say which revision it was"

    rev=$(python3 -c '
import json, sys
nodes = json.load(open(sys.argv[1]))["nodes"]
locked = nodes.get("nixpkgs", {}).get("locked", {})
print(locked.get("rev", ""))
' "$HD_ROOT/devenv.lock" 2>/dev/null) || rev=""
    [ -n "$rev" ] || hd_die "could not read the nixpkgs revision from devenv.lock"

    hd_log "the pinned nixpkgs is no longer in the store (garbage collected); fetching $rev again"
    path=$(nix flake prefetch --json "github:NixOS/nixpkgs/$rev" 2>/dev/null |
        python3 -c 'import json,sys; print(json.load(sys.stdin)["storePath"])' 2>/dev/null) || path=""
    [ -n "$path" ] && [ -d "$path" ] ||
        hd_die "could not fetch nixpkgs $rev; re-enter the developer environment and try again"
    printf '%s\n' "$path"
}

# hd_toolchain_file <platform> -> the CMake toolchain file for it, building it
# if this workspace has not built it yet.
#
# The result is a store path, kept as a symlink in .hlsl-dev/toolchains/<name>:
# that is the cache (the second configure never calls nix), and it is a GC root
# (a nix-collect-garbage does not silently remove a toolchain a build tree was
# configured against).
hd_toolchain_file() {
    local p=$1 link nixpkgs args=()
    link="$(hd_state_dir)/toolchains/$p"
    if [ -f "$link/toolchain.cmake" ]; then
        printf '%s\n' "$link/toolchain.cmake"
        return 0
    fi

    if [ "$(hd_platform_abi "$p")" = "msvc" ] && ! hd_msvc_license_accepted; then
        hd_die "$p needs Microsoft's SDK (headers and import libraries), which
       nixpkgs will not build until its licence has been accepted. Read
       https://visualstudio.microsoft.com/license-terms/mt644918/ and, if you
       agree, run:  hlsl-cross --accept-msvc-license"
    fi

    if [ -n "${HD_DRY_RUN:-}" ]; then
        hd_log "would build the $p toolchain (nix-build scripts/cross/toolchains.nix)"
        printf '%s\n' "$link/toolchain.cmake"
        return 0
    fi

    hd_log "building the $p cross toolchain (once; the MSVC SDK is a large download)"
    nixpkgs=$(hd_nixpkgs_path) || return 1
    mkdir -p "$(dirname "$link")"
    args=(
        "$HD_ROOT/scripts/cross/toolchains.nix"
        --argstr nixpkgs "$nixpkgs"
        --argstr platform "$p"
        -o "$link"
    )
    hd_msvc_license_accepted && args+=(--arg acceptMsvcLicense true)
    hd_run nix-build "${args[@]}" >/dev/null ||
        hd_die "could not build the $p toolchain; see 'hlsl-cross' for what it needs"
    [ -f "$link/toolchain.cmake" ] ||
        hd_die "the $p toolchain was built but $link/toolchain.cmake is not there"
    printf '%s\n' "$link/toolchain.cmake"
}

# hd_materialise_symlinks <dir> -- replace every symlink under <dir> with a
# copy of what it points at.
#
# LLVM installs its driver aliases as symlinks (clang-dxc.exe -> clang.exe),
# which is right on the machine that built them and wrong in an archive bound
# for Windows: a .zip carries the link, Windows extracts something that is not
# an executable, and running it fails with "The operation was canceled by the
# user" -- a message that says nothing about the cause. A real Windows install
# of LLVM has copies there, so this is what that layout looks like.
hd_materialise_symlinks() {
    local dir=$1 link target
    while IFS= read -r link; do
        [ -n "$link" ] || continue
        target=$(readlink -f "$link") || continue
        [ -e "$target" ] || {
            hd_warn "dropping $link: it points at $target, which is not here"
            rm -f "$link"
            continue
        }
        rm -f "$link"
        cp -a "$target" "$link"
    done <<< "$(find "$dir" -type l)"
}

# ---------------------------------------------------------------------------
# A runnable prefix: what leaves this machine
# ---------------------------------------------------------------------------
# `hlsl-package` and `hlsl-repro` both need the same thing -- a directory that
# runs the offload suite on another machine -- and where its parts come from
# depends on which checkout is being packaged:
#
#   llvm-project        one build tree holds everything: LLVM's
#                       install-distribution provides clang and lit's tooling,
#                       OffloadTest's two install targets the offloader and
#                       the tests.
#   offload-test-suite  a standalone build has *only* the suite's own targets
#                       (install-distribution is LLVM's and does not exist
#                       here). clang, FileCheck, split-file and the resource
#                       headers come from the LLVM distribution it was built
#                       against, which is on disk precisely because the build
#                       links against it.

# hd_install_targets <kind> -> the install targets that fill <build>/install.
hd_install_targets() {
    case "$1" in
    llvm) printf 'install-distribution install-offload-tools install-offload-test-suite\n' ;;
    offload) printf 'install-offload-tools install-offload-test-suite\n' ;;
    *) hd_die "nothing to package in a $(hd_kind_label "$1") checkout" ;;
    esac
}

# hd_stage_prefix <worktree> <dest> -- assemble the prefix in <dest>.
#
# Staged rather than archived in place for three reasons: an offload build's
# prefix is two prefixes merged, a Windows archive may not contain the
# symlinks LLVM installs, and neither of those is a thing to do to the install
# directory someone might still be testing against.
hd_stage_prefix() {
    local wt=$1 dest=$2 kind build prefix llvm dist
    kind=$(hd_kind "$wt")
    build=$(hd_build_dir "$wt")
    prefix="$build/install"

    rm -rf "$dest"
    mkdir -p "$dest"

    if [ "$kind" = "offload" ]; then
        llvm=$(hd_dep llvm "$wt")
        dist=$(hd_dist_prefix "$llvm")
        [ -x "$dist/bin/clang-dxc" ] || [ -x "$dist/bin/clang-dxc.exe" ] ||
            hd_die "the LLVM distribution this suite builds against has no clang-dxc
       ($dist). Refresh it with 'hlsl-dist --in $(basename "$llvm")'."
        hd_log "taking the compiler and lit tooling from $dist"
        # Only what a test run needs: the tools and the resource headers. The
        # distribution also carries LLVM's libraries and headers, which are
        # for *building* the suite and are hundreds of megabytes.
        mkdir -p "$dest/bin"
        cp -a "$dist/bin/." "$dest/bin/"
        if [ -d "$dist/lib/clang" ]; then
            mkdir -p "$dest/lib"
            cp -a "$dist/lib/clang" "$dest/lib/"
        fi
    fi

    [ -d "$prefix" ] || hd_die "nothing installed in $prefix"
    cp -a "$prefix/." "$dest/"

    # A .zip cannot carry LLVM's driver symlinks to Windows: what comes out the
    # other side is not an executable, and the error it produces
    # ("The operation was canceled by the user") says nothing about why.
    if [ "$(hd_platform_os "$(hd_platform)")" = "windows" ]; then
        hd_materialise_symlinks "$dest"
    fi
}

# ---------------------------------------------------------------------------
# Host tools for a cross build
# ---------------------------------------------------------------------------
# A cross build of LLVM has to *run* llvm-tblgen, clang-tblgen and a couple of
# generators, and the ones it builds are for the target. LLVM_NATIVE_TOOL_DIR
# is where it looks for host copies instead; this builds them, once per llvm
# worktree, into <worktree>/build-native-tools -- shared by every platform,
# like build-dist is, because they are plain host binaries.
#
# It is a small build (no tests, no benchmarks, one target, Release) but not a
# free one: ~10 minutes cold, seconds when sccache has seen the sources.

# The tablegens every cross build of LLVM has to run, and the generators the
# clang-tools-extra half of the HLSL cache adds. The second list is optional:
# which generators exist moves between LLVM revisions, and a cross build that
# does not need one must not fail here because this workspace asked for it.
HD_NATIVE_TOOLS="llvm-min-tblgen llvm-tblgen clang-tblgen"
HD_NATIVE_TOOLS_OPTIONAL="clang-tidy-confusable-chars-gen clang-pseudo-gen"

hd_native_tools_dir() { printf '%s/build-native-tools\n' "$1"; }

# hd_ensure_native_tools <llvm worktree> -> the directory holding them.
hd_ensure_native_tools() {
    local wt=$1 build target parallel=()
    build=$(hd_native_tools_dir "$wt")
    if [ -x "$build/bin/llvm-tblgen" ] && [ -x "$build/bin/clang-tblgen" ]; then
        printf '%s\n' "$build/bin"
        return 0
    fi

    hd_provide "the host tablegens for $(basename "$wt") -- a cross build cannot run its own" || return 1
    if [ -z "${HD_DRY_RUN:-}" ]; then
        # Everything below writes to stderr: this function's *stdout* is the
        # directory it resolved, and the caller reads it with $(...).
        {
            export HD_LLVM_SRC=$wt
            export HD_INSTALL_PREFIX="$build/install"
            hd_prepare_build_dir "$build"
            hd_lock "$build"
            hd_cmake_flags HLSL_CMAKE_FLAGS_NATIVE_TOOLS
            hd_run cmake -S "$wt/llvm" -B "$build" "${HD_FLAGS[@]}" ||
                hd_die "could not configure the host tools build in $build"
            hd_parallel_args parallel
            # shellcheck disable=SC2086 # a list of target names; splitting is the point
            hd_run cmake --build "$build" "${parallel[@]}" --target $HD_NATIVE_TOOLS ||
                hd_die "could not build the host tools in $build"
            for target in $HD_NATIVE_TOOLS_OPTIONAL; do
                hd_run cmake --build "$build" "${parallel[@]}" --target "$target" >/dev/null 2>&1 ||
                    hd_warn "this llvm-project has no $target; carrying on without it"
            done
        } >&2
        [ -x "$build/bin/llvm-tblgen" ] ||
            hd_die "$build was built but has no llvm-tblgen"
    fi
    printf '%s\n' "$build/bin"
}

# ---------------------------------------------------------------------------
# D3D12
# ---------------------------------------------------------------------------
# offload-test-suite detects D3D12 at configure time and there is no switch of
# its own: on Windows through find_package(D3D12), on Linux only under WSL,
# where find_package(D3D12_WSL) picks up the host driver from /usr/lib/wsl/lib
# plus the two static libraries DirectX-Headers ships. Whatever it finds
# decides whether the d3d12 / clang-d3d12 suites exist in the build tree.
#
# CMake's own CMAKE_DISABLE_FIND_PACKAGE_<name> is the supported way to take
# that decision back, so "off" is expressed with those, and "on" spells them
# out as OFF so a build tree that was configured off can be turned back on
# without a fresh configure.

hd_d3d12() {
    local saved
    saved=$(hd_setting_get D3D12)
    printf '%s\n' "${saved:-on}"
}

# True when the platform can offer D3D12 at all (WSL's driver, or Windows).
hd_d3d12_available() {
    case "$(uname -s)" in
    *NT* | MINGW* | MSYS* | CYGWIN*) return 0 ;;
    esac
    [ -e "/usr/lib/wsl/lib/libd3d12.so" ]
}

# The flags every configure passes, so the choice is never left to whatever the
# last configure happened to cache. A cross build answers for the platform it
# is building for, not for this machine: Windows has D3D12 whatever this
# machine is, and an aarch64 Linux binary has no WSL driver to find.
hd_d3d12_flags() {
    local off=OFF
    case "$(hd_platform)" in
    native) [ "$(hd_d3d12)" = "off" ] && off=ON ;;
    windows-*) off=OFF ;;
    *) off=ON ;;
    esac
    printf '%s\n' "-DCMAKE_DISABLE_FIND_PACKAGE_D3D12=$off"
    printf '%s\n' "-DCMAKE_DISABLE_FIND_PACKAGE_D3D12_WSL=$off"
}

# ---------------------------------------------------------------------------
# Dependency resolution
# ---------------------------------------------------------------------------
# Resolution order for "which <kind> checkout should <worktree> build against":
#
#   1. --llvm / --dxc / --offload / --golden on the command line
#   2. $HLSL_LLVM / $HLSL_DXC / $HLSL_OFFLOAD / $HLSL_GOLDEN in the environment
#   3. a pin recorded by `hlsl-link` or by the last successful `hlsl-configure`
#   4. a worktree of that repository checked out on the *same branch name*
#   5. the submodule checkout in the workspace root
#
# 1 and 2 are said out loud, so a spec that resolves to nothing is an error.
# A pin is only a memory of an earlier command: if what it names has been
# removed since -- or was never there, as with a store written against another
# path -- it is dropped with a warning and the search goes on, rather than
# every command in that checkout failing until someone runs --forget.

hd_dep() {
    local kind=$1 from=${2:-} spec="" wt br

    case "$kind" in
    llvm) spec=$HD_OPT_LLVM ;;
    dxc) spec=$HD_OPT_DXC ;;
    offload) spec=$HD_OPT_OFFLOAD ;;
    golden) spec=$HD_OPT_GOLDEN ;;
    esac

    if [ -n "$spec" ]; then
        hd_resolve "$kind" "$spec"
        return
    fi

    if [ -n "$from" ]; then
        spec=$(hd_pin_get "$from" "$(printf '%s' "$kind" | tr '[:lower:]' '[:upper:]')")
        if [ -n "$spec" ]; then
            if wt=$(hd_try_resolve "$kind" "$spec") && [ -n "$wt" ]; then
                printf '%s\n' "$wt"
                return 0
            fi
            hd_warn "$(basename "$from") is pinned to the $(hd_kind_label "$kind") checkout
         '$spec', which is not there; ignoring the pin. Point it somewhere else
         with 'hlsl-configure --$kind <worktree>', or drop it with
         'hlsl-configure --forget'."
        fi
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

    local base
    base="$HD_ROOT/$(hd_repo_name "$kind")"
    [ -d "$base" ] ||
        hd_die "no $(hd_kind_label "$kind") checkout found; run 'hlsl-setup'"
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
# $HLSL_BUILD_DIR overrides the first one for the *target* worktree, and
# $HLSL_BUILD_DIR_NAME renames it for *every* worktree. Both are session
# variables rather than flags on purpose: every command in that session then
# agrees on where the build tree is, with nothing to remember between them.
#
# The name is what an environment that must not share build trees with another
# one sets -- the dev container does, because a tree configured under WSL links
# a D3D12 driver that is not there. Renaming it rather than pointing at one
# directory keeps every command consistent: a build, the `built` column of
# `hlsl-ls` and the prerequisite checks all look in the same place, per
# worktree. The distribution build (build-dist) is deliberately not renamed:
# it is plain LLVM, so it is worth sharing.

hd_build_dir() {
    local wt=$1
    if [ -n "$HD_OPT_BUILD_DIR" ] && [ "$wt" = "${HD_WT:-}" ]; then
        case "$HD_OPT_BUILD_DIR" in
        /*) printf '%s\n' "${HD_OPT_BUILD_DIR%/}" ;;
        *) printf '%s\n' "$wt/${HD_OPT_BUILD_DIR%/}" ;;
        esac
        return 0
    fi
    printf '%s/%s%s\n' "$wt" "${HLSL_BUILD_DIR_NAME:-build}" "$(hd_platform_suffix)"
}

# The distribution build is per platform as well: an offload build for Windows
# links against LLVM libraries for Windows. The native one keeps its name.
hd_dist_build_dir() { printf '%s/build-dist%s\n' "$1" "$(hd_platform_suffix)"; }

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
    # A pinned prefix that has not been installed yet is normal -- it is
    # installed on demand -- but one whose parent does not exist either is a
    # memory of a directory that is not on this machine (or not at this path).
    if [ -n "$pinned" ] && [ ! -e "$pinned" ] && [ ! -d "$(dirname "$pinned")" ]; then
        hd_warn "$(basename "$1") is pinned to the LLVM distribution
         '$pinned', which is not there; using its own instead."
        pinned=""
    fi
    if [ -n "$pinned" ]; then
        printf '%s\n' "$pinned"
    else
        printf '%s/install\n' "$(hd_dist_build_dir "$1")"
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

# hd_cache_get <build dir> <variable> -> cached value (empty if unset)
hd_cache_get() {
    [ -f "$1/CMakeCache.txt" ] || return 0
    sed -n "s|^$2:[^=]*=||p" "$1/CMakeCache.txt" | tail -n 1
}

# ---------------------------------------------------------------------------
# DXC binary directory
# ---------------------------------------------------------------------------
# --dxc accepts a worktree spec, a directory containing dxc/dxv, or the literal
# "nix" for the prebuilt compiler from the environment.
#
# hd_dxc_bin_dir <from worktree> [ensure]
#
# With "ensure", a worktree that was asked for by name but has not been built
# is built here instead of being reported: the caller is about to need dxc, and
# the command it would otherwise print is the one we would run anyway. Callers
# that only *report* (hlsl-info, hlsl-ls) leave it off, so they never build.

hd_dxc_bin_dir() {
    local from=${1:-} ensure=${2:-} spec=$HD_OPT_DXC explicit=1 wt bin

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
        [ -n "$ensure" ] ||
            hd_die "$wt has no built dxc ($bin/dxc);
       build it first:  hlsl-build --in $(basename "$wt")"
        hd_provide "dxc for $(basename "$wt")" || return 1
        if [ -n "${HD_DRY_RUN:-}" ]; then
            printf '%s\n' "$bin"
            return 0
        fi
        ( HD_WT=$wt; hd_build "$wt" ) >&2 || hd_die "could not build dxc in $wt"
        [ -x "$bin/dxc" ] || hd_die "$wt built, but $bin/dxc still does not exist"
        printf '%s\n' "$bin"
        return 0
    fi
    [ -n "${HLSL_DXC_PREBUILT_DIR:-}" ] ||
        hd_die "no dxc available: neither $bin/dxc nor a dev-shell dxc exists"
    hd_warn "$(basename "$wt") has no built dxc yet, using the dev shell's prebuilt one
         (build it with 'hlsl-build --in $(basename "$wt")', or pass --dxc <worktree>)"
    printf '%s\n' "$HLSL_DXC_PREBUILT_DIR"
}

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
# A checkout cannot always be built from itself: a standalone offload build
# needs an installed LLVM distribution, and running its suites needs a dxc
# binary. Both used to stop with the exact command to type next; they are built
# instead, because typing back what we just printed is not a decision.
#
#   --dry-run   report the plan and build nothing
#   --no-auto   restore the old behaviour: fail, and say what is missing
#               (also $HLSL_AUTO=0, for a whole session)

# hd_provide <what> -- true when the caller should go ahead and build it.
hd_provide() {
    if [ -n "${HD_DRY_RUN:-}" ]; then
        hd_log "would build: $1"
        return 0
    fi
    if [ -z "${HD_AUTO:-}" ]; then
        hd_die "missing prerequisite: $1
       build it first, or drop --no-auto / \$HLSL_AUTO=0 to have it built here"
    fi
    hd_log "missing prerequisite: $1 -- building it now"
    return 0
}

# hd_ensure_dist <llvm worktree> -> the distribution prefix, installing it if
# it is not there yet. `hlsl-dist` stays the way to refresh an existing one
# after a Clang change; nothing here decides that an install is out of date.
hd_ensure_dist() {
    local llvm=$1 dist
    dist=$(hd_dist_prefix "$llvm")
    if [ ! -f "$dist/lib/cmake/llvm/LLVMConfig.cmake" ]; then
        if [ -n "${HD_OPT_DIST_PREFIX:-}" ]; then
            hd_die "no LLVM distribution at $dist (--dist-prefix / \$HLSL_DIST_PREFIX)"
        fi
        hd_provide "the LLVM distribution for $(basename "$llvm") -- the expensive one" || return 1
        if [ -z "${HD_DRY_RUN:-}" ]; then
            # >&2 for the same reason as hd_ensure_native_tools: the caller
            # reads the prefix from this function's stdout, and a cmake run
            # would otherwise end up in the middle of it.
            ( HD_WT=$llvm; hd_dist "$llvm" ) >&2 || hd_die "could not build the LLVM distribution for $llvm"
            [ -f "$dist/lib/cmake/llvm/LLVMConfig.cmake" ] ||
                hd_die "$llvm installed a distribution, but $dist has no LLVMConfig.cmake"
        fi
    fi
    printf '%s\n' "$dist"
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
       cd into one, or pass --in <worktree> (see 'hlsl-ls')"
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
    hd_die "no worktree matches '$spec' (see 'hlsl-ls')"
}

# ---------------------------------------------------------------------------
# Housekeeping: git excludes and build locks
# ---------------------------------------------------------------------------

# Keep `git status` clean in worktrees whose upstream .gitignore does not cover
# our artifact directories (offload-test-suite in particular). info/exclude is
# per-clone and never committed, so this is invisible to the upstream repo.
hd_git_exclude() {
    local common f pat
    [ -z "${HD_DRY_RUN:-}" ] || return 0
    common=$(git -C "$1" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || return 0
    f="$common/info/exclude"
    mkdir -p "$(dirname "$f")" 2>/dev/null || return 0
    [ -f "$f" ] || : >"$f"
    for pat in '/build*/' '/install*/' '/compile_commands.json' \
        '/.codegraph/' '/.codegraph-*/' '/codegraph.json'; do
        grep -qxF "$pat" "$f" 2>/dev/null || printf '%s\n' "$pat" >>"$f"
    done
}

# hd_link_cdb <worktree> [build dir] -- keep <worktree>/compile_commands.json
# pointing at a compilation database an editor can actually find.
#
# clangd looks for compile_commands.json beside the file it is editing, in the
# parent directories, and in a `build/` subdirectory of each -- and nowhere
# else. That is fine while the build directory is called `build`, but the dev
# container sets $HLSL_BUILD_DIR_NAME=build-container, so a worktree only ever
# built in there has its database in a directory no editor looks in: open the
# same tree from the host and clangd has no flags, no index and no navigation.
# A symlink at the root of the worktree is found by both, whatever the build
# directory is called, and git never sees it (hd_git_exclude above lists it).
#
# The link follows the tree most recently configured or built: the two
# databases describe the same sources and differ only in what the environment
# detected (D3D12 under WSL, for instance), so either serves an editor, and
# "the one I last worked in" is the least surprising answer. A real file is
# left alone -- that one is the developer's own.
hd_link_cdb() {
    local wt=$1 build=${2:-} link target cand newest=""
    [ -z "${HD_DRY_RUN:-}" ] || return 0
    # A cross build's database describes another machine's compiler and
    # headers; pointing the editor at it would make clangd diagnose the wrong
    # platform. The native tree keeps the link.
    ! hd_is_cross || return 0
    link="$wt/compile_commands.json"
    [ ! -e "$link" ] || [ -L "$link" ] || return 0

    if [ -n "$build" ] && [ -f "$build/compile_commands.json" ]; then
        target="$build/compile_commands.json"
    else
        # Whichever build directory of this worktree has the newest database:
        # what heals a worktree the *other* environment configured.
        for cand in "$wt"/build*/compile_commands.json; do
            [ -f "$cand" ] || continue
            case "$cand" in
            # Never a cross tree's: same reason as above.
            "$wt"/build.* | "$wt"/build-dist*) continue ;;
            esac
            [ -z "$newest" ] || [ "$cand" -nt "$newest" ] || continue
            newest=$cand
        done
        target=$newest
    fi

    if [ -z "$target" ]; then
        # Nothing to point at (a fresh or just-cleaned worktree): do not leave
        # a dangling link behind for clangd to trip over.
        [ -L "$link" ] && [ ! -e "$link" ] && rm -f "$link"
        return 0
    fi
    ln -sfn "${target#"$wt"/}" "$link" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# clang-format pre-commit hook
# ---------------------------------------------------------------------------
# A warning, not a gate: `git clang-format` reformats only the lines a commit
# touches, so the hook reports what it would change and lets the commit
# through. Upstream reviewers ask for formatted diffs; a workspace that blocks
# commits over it would be making a policy these repositories do not have.
#
# It is installed per *clone*, in the common git directory, so every `wt`
# worktree of a submodule shares one hook, and nothing lands in the checkout
# where it could be committed by accident. `hlsl-format` installs, removes and
# runs it; the hlsl:hooks task keeps it in place.

HD_HOOK_MARKER="hlsl-dev clang-format hook"

# hd_hook_path <worktree> -> the pre-commit hook shared by every worktree of
# that clone (empty if the directory is not a git checkout).
hd_hook_path() {
    local common
    common=$(git -C "$1" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || return 0
    printf '%s/hooks/pre-commit\n' "$common"
}

# The hook is a stub: it works out where the workspace is, sets up just enough
# to find its tools -- a commit from an editor or a bare terminal is not in the
# developer environment -- and hands over to the task, which is where the logic
# lives and can be edited without reinstalling anything.
#
# It finds the workspace by walking up from itself rather than having the path
# written in. The same clone is reached through different paths by different
# machines -- /workspaces/... inside the dev container, somewhere else on the
# host, and they share these files through the bind mount -- and a hook with a
# path baked in gets rewritten by whichever one ran last, forever.
hd_hook_body() {
    cat <<EOF
#!/usr/bin/env bash
# $HD_HOOK_MARKER
EOF
    cat <<'EOF'
#
# Warns when the staged changes do not match .clang-format. It never blocks a
# commit. Remove it with 'hlsl-format --uninstall-hooks', or just delete it.
root=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd -P)
while [ "$root" != "/" ]; do
    [ -f "$root/devenv.nix" ] && [ -x "$root/scripts/tasks/format.sh" ] && break
    root=$(dirname "$root")
done
[ "$root" != "/" ] || exit 0   # not in the workspace any more; nothing to say
export HLSL_DEV_ROOT="$root"
export PATH="$root/.devenv/profile/bin:$PATH"
# --quiet: a hook that speaks on every clean commit is a hook people remove.
HD_TASK_NAME=hlsl-format "$root/scripts/tasks/format.sh" --quiet || true
exit 0
EOF
}

# 0 when the hook is ours and current, 1 when it is missing or out of date,
# 2 when something else owns the file and we must not touch it.
hd_hook_ok() {
    local hook
    hook=$(hd_hook_path "$1")
    [ -n "$hook" ] || return 0
    [ -f "$hook" ] || return 1
    grep -qF "$HD_HOOK_MARKER" "$hook" 2>/dev/null || return 2
    [ "$(cat "$hook")" = "$(hd_hook_body)" ] || return 1
    return 0
}

hd_hook_install() {
    local hook rc=0
    hook=$(hd_hook_path "$1")
    [ -n "$hook" ] || return 0
    hd_hook_ok "$1" || rc=$?
    case "$rc" in
    0) return 0 ;;
    2)
        hd_warn "$(basename "$1") already has a pre-commit hook that is not ours; leaving it alone
         ($hook)"
        return 2
        ;;
    esac
    [ -z "${HD_DRY_RUN:-}" ] || { hd_log "would install $hook"; return 0; }
    mkdir -p "$(dirname "$hook")"
    hd_hook_body >"$hook"
    chmod +x "$hook"
    return 1
}

hd_hook_remove() {
    local hook
    hook=$(hd_hook_path "$1")
    [ -n "$hook" ] && [ -f "$hook" ] || return 0
    grep -qF "$HD_HOOK_MARKER" "$hook" 2>/dev/null || return 2
    [ -z "${HD_DRY_RUN:-}" ] || { hd_log "would remove $hook"; return 0; }
    rm -f "$hook"
    return 1
}

# Every clone the hook applies to: the repositories with a .clang-format, one
# entry per clone rather than per worktree, since they share the hook.
hd_hook_repos() {
    local kind wt common seen=""
    for kind in llvm dxc offload; do
        while IFS= read -r wt; do
            [ -n "$wt" ] || continue
            [ -f "$wt/.clang-format" ] || continue
            common=$(git -C "$wt" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || continue
            case " $seen " in
            *" $common "*) continue ;;
            esac
            seen="$seen $common"
            printf '%s\n' "$wt"
        done <<< "$(hd_worktrees "$kind")"
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
# `hlsl-codegraph --restore-gitignore` in that worktree, redo the git
# operation, and run `hlsl-codegraph` again to put the block back.
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
# 'hlsl-codegraph'; 'hlsl-codegraph --restore-gitignore' takes it back out.
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
# Set HD_OPT_FRESH (--fresh) to rebuild from scratch instead of seeding.
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

# Serialise concurrent task invocations that target the same build directory.
# Different worktrees have different build directories, so agents working in
# parallel never wait on each other. Re-locking a directory this process
# already holds is a no-op (flock would otherwise deadlock against itself).
hd_lock() {
    local dir=$1 lock
    [ -z "${HD_DRY_RUN:-}" ] || return 0
    command -v flock >/dev/null 2>&1 || return 0
    case " ${HD_LOCKED:-} " in
    *" $dir "*) return 0 ;;
    esac
    mkdir -p "$(hd_state_dir)/locks"
    lock="$(hd_state_dir)/locks/$(hd_key "$dir").lock"
    exec {HD_LOCK_FD}>"$lock"
    if ! flock -n "$HD_LOCK_FD"; then
        hd_log "waiting for another invocation to release $dir"
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
# compile) keeps the flock alive long after the task has exited, and the next
# invocation then blocks on a lock nobody is using. Only this shell needs to
# hold the lock, so hand children a clean set of descriptors.
hd_run() {
    local fd redirs=""
    if [ -n "${HD_DRY_RUN:-}" ]; then
        hd_log "would run: $*"
        return 0
    fi
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
# devenv.nix exports the flag lists with $HD_* placeholders left unexpanded;
# they are filled in here once the paths above have been resolved.

hd_no_spaces() {
    case "$2" in
    *[[:space:]]*) hd_die "$1 contains whitespace ('$2'), which CMake flag expansion cannot represent" ;;
    esac
}

# hd_expand_flags <template> -> flags, one per line
hd_expand_flags() {
    local t=$1
    # CMake spells a list with semicolons (LLVM_ENABLE_PROJECTS), which a
    # template cannot contain literally: this expands with eval, and a bare
    # semicolon there would end the command. ${HD_SEMI} is the way to write
    # one -- it is substituted *after* the line has been parsed, so it can only
    # ever be a character in an argument, never a separator.
    # shellcheck disable=SC2034 # read by the eval below
    local HD_SEMI=';'
    # shellcheck disable=SC2016 # matching the literal placeholder syntax
    case "$t" in
    *'`'* | *'$('* | *';'*) hd_die "refusing to expand a CMake flag template containing shell metacharacters" ;;
    esac
    eval "printf '%s\n' $t"
}

# hd_read_flags <template> <label> -- append its expansion to HD_FLAGS.
#
# The expansion runs in a subshell (it has to: it is a command substitution),
# so an hd_die inside it ends *that* shell and nothing else. Without this check
# a rejected template would leave the list silently empty and cmake would be
# run with no flags at all -- which configures something, just not what was
# asked for.
hd_read_flags() {
    local t=$1 label=$2 line before=${#HD_FLAGS[@]}
    [ -n "$t" ] || return 0
    while IFS= read -r line; do
        [ -n "$line" ] && HD_FLAGS+=("$line")
    done <<< "$(hd_expand_flags "$t")"
    [ "${#HD_FLAGS[@]}" -gt "$before" ] ||
        hd_die "the $label flag template expanded to nothing (see the error above)"
}

# hd_cmake_flags <template var name> -> populates the HD_FLAGS array
# The read loops in this file feed from a here-string rather than a process
# substitution: some sandboxed/container shells (toolbox, seccomp-mediated
# shells) do not resolve /dev/fd/<n>, and bash then fails `< <(...)` with
# "/dev/fd/63: No such file or directory". A here-string keeps the loop in the
# current shell (so assignments survive) without needing /dev/fd.
hd_cmake_flags() {
    HD_FLAGS=()
    hd_read_flags "${!1}" "$1"
}

# hd_add_flags <template var name>... -- append more expanded templates to the
# HD_FLAGS array that hd_cmake_flags started. cmake takes the last -D of a
# repeated variable, so what is appended here overrules the native answer.
hd_add_flags() {
    local name
    for name in "$@"; do
        hd_read_flags "${!name:-}" "$name"
    done
}

# hd_cross_flags <kind: llvm|offload|dxc> -- append everything a cross build
# adds to the native flag list. Nothing at all when building for this machine,
# which is what keeps the native path exactly as it was.
#
# The order is: what every cross build needs, then what its OS needs, then what
# its ABI needs, and last what a build of LLVM itself needs. Each may overrule
# the one before -- that is how, for instance, "link with lld" is off for the
# GNU toolchains and back on for MSVC, where clang-cl has no other linker here.
hd_cross_flags() {
    local kind=$1 p os
    hd_is_cross || return 0
    p=$(hd_platform)
    os=$(hd_platform_os "$p")

    HD_TOOLCHAIN_FILE=$(hd_toolchain_file "$p") || exit 1
    export HD_TOOLCHAIN_FILE
    HD_TARGET_TRIPLE=$(hd_platform_triple "$p")
    export HD_TARGET_TRIPLE
    hd_no_spaces "toolchain file" "$HD_TOOLCHAIN_FILE"

    hd_add_flags HLSL_CMAKE_FLAGS_CROSS
    case "$os" in
    linux) hd_add_flags HLSL_CMAKE_FLAGS_CROSS_LINUX ;;
    windows) hd_add_flags HLSL_CMAKE_FLAGS_CROSS_WINDOWS ;;
    esac
    [ "$kind" = "llvm" ] && hd_add_flags HLSL_CMAKE_FLAGS_CROSS_LLVM
    if [ "$kind" = "dxc" ]; then
        hd_add_flags HLSL_CMAKE_FLAGS_CROSS_DXC
        # The DIA SDK only a Windows machine has. Pass it on when this
        # workspace has been told where it is; DXC's configure says clearly
        # enough what is missing when it has not.
        if [ -n "${HLSL_DIA_SDK:-}" ]; then
            HD_DIA_SDK=$HLSL_DIA_SDK
            export HD_DIA_SDK
            hd_no_spaces "DIA SDK directory" "$HD_DIA_SDK"
            hd_add_flags HLSL_CMAKE_FLAGS_CROSS_DXC_DIA
        fi
    fi
    return 0
}

# What the cross build is producing, for the report a configure prints.
hd_cross_report() {
    hd_is_cross || return 0
    hd_report_deps \
        "platform" "$(hd_platform)" \
        "triple" "$(hd_platform_triple "$(hd_platform)")" \
        "toolchain" "${HD_TOOLCHAIN_FILE:-}"
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
    if [ -n "${HD_DRY_RUN:-}" ]; then
        [ -n "$HD_OPT_FRESH" ] && [ -d "$build" ] && hd_log "would remove $build (--fresh)"
        return 0
    fi
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
    local wt=$1 build offload golden dxcbin flag
    build=$(hd_build_dir "$wt")
    offload=$(hd_dep offload "$wt")
    golden=$(hd_dep golden "$wt")
    # dxc is only ever *run* -- by the offload tests -- so a cross configure
    # takes whatever dxc is already here and never builds one: the binaries it
    # would build could not run these tests anyway.
    if hd_is_cross; then
        dxcbin=$(hd_dxc_bin_dir "$wt")
    else
        dxcbin=$(hd_dxc_bin_dir "$wt" ensure)
    fi

    export HD_LLVM_SRC=$wt
    export HD_OFFLOAD_SRC=$offload
    export HD_GOLDEN_DIR=$golden
    export HD_DXC_BIN_DIR=$dxcbin
    export HD_INSTALL_PREFIX="$build/install"
    HD_BUILD_TYPE=$(hd_build_type "$wt")
    export HD_BUILD_TYPE
    if hd_is_cross; then
        HD_NATIVE_TOOL_DIR=$(hd_ensure_native_tools "$wt") || return 1
        export HD_NATIVE_TOOL_DIR
        hd_no_spaces "host tools directory" "$HD_NATIVE_TOOL_DIR"
    fi
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
    while IFS= read -r flag; do HD_FLAGS+=("$flag"); done <<< "$(hd_d3d12_flags)"
    hd_cross_flags llvm
    hd_cross_report
    hd_run cmake -S "$wt/llvm" -B "$build" "${HD_FLAGS[@]}"
    hd_link_cdb "$wt" "$build"

    hd_record "$wt" BUILD_TYPE "$HD_BUILD_TYPE" OFFLOAD "$offload" DXC "$dxcbin"
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
    hd_cross_flags dxc
    hd_cross_report
    hd_run cmake -S "$wt" -B "$build" "${HD_FLAGS[@]}"
    hd_link_cdb "$wt" "$build"

    hd_record "$wt" BUILD_TYPE "$HD_BUILD_TYPE"
}

# hd_configure_offload <offload worktree>
#
# The suite is the top-level CMake project and links against an installed LLVM
# distribution (see hd_dist): configures in seconds, builds in a couple of
# minutes, and any number of offload worktrees can share one distribution.
#
# To build the suite *inside* an llvm build tree instead -- the in-tree layout
# upstream CI uses -- configure that llvm worktree against these sources:
#
#     hlsl-configure --in llvm-project.my-feature --offload offload-test-suite.mine
hd_configure_offload() {
    local wt=$1 build llvm dist golden dxcbin flag
    llvm=$(hd_dep llvm "$wt")
    dist=$(hd_ensure_dist "$llvm")
    build=$(hd_build_dir "$wt")
    golden=$(hd_dep golden "$wt")
    if hd_is_cross; then
        dxcbin=$(hd_dxc_bin_dir "$wt")
    else
        dxcbin=$(hd_dxc_bin_dir "$wt" ensure)
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
    while IFS= read -r flag; do HD_FLAGS+=("$flag"); done <<< "$(hd_d3d12_flags)"
    hd_cross_flags offload
    hd_cross_report
    hd_run cmake -S "$wt" -B "$build" "${HD_FLAGS[@]}"
    hd_link_cdb "$wt" "$build"

    hd_record "$wt" BUILD_TYPE "$HD_BUILD_TYPE" LLVM "$llvm" DXC "$dxcbin"
    if [ -n "${HD_OPT_DIST_PREFIX:-}" ]; then
        hd_pin_set "$llvm" DIST_PREFIX "$dist"
    fi
}

# hd_configure <worktree> -- dispatches on the kind of checkout.
hd_configure() {
    [ -z "${forget:-}" ] || hd_pin_clear "$1"
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
    local wt=$1 build prefix offload parallel=()
    build=$(hd_dist_build_dir "$wt")
    prefix=$(hd_dist_prefix "$wt")
    offload=$(hd_dep offload "$wt")

    export HD_LLVM_SRC=$wt
    export HD_OFFLOAD_SRC=$offload
    export HD_INSTALL_PREFIX=$prefix
    HD_BUILD_TYPE=${HD_OPT_BUILD_TYPE:-$(hd_cache_get "$build" CMAKE_BUILD_TYPE)}
    export HD_BUILD_TYPE=${HD_BUILD_TYPE:-Release}
    if hd_is_cross; then
        HD_NATIVE_TOOL_DIR=$(hd_ensure_native_tools "$wt") || return 1
        export HD_NATIVE_TOOL_DIR
    fi
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
    hd_cross_flags llvm
    hd_cross_report
    hd_run cmake -S "$wt/llvm" -B "$build" "${HD_FLAGS[@]}"
    hd_parallel_args parallel
    hd_run cmake --build "$build" "${parallel[@]}" --target install-distribution

    hd_pin_set "$wt" DIST_PREFIX "$prefix"
    hd_log "installed distribution: $prefix"
}

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

# Configure <worktree> unless it already is. Worktrees configured earlier in
# this process are remembered: hlsl-test asks for one and then hands over to
# hd_build, which asks again, and a dry run would otherwise report the same
# configure twice (it creates nothing for the second check to find).
hd_ensure_configured() {
    local wt=$1 build
    case " ${HD_CONFIGURED:-} " in
    *" $wt "*) return 0 ;;
    esac
    HD_CONFIGURED="${HD_CONFIGURED:-} $wt"
    build=$(hd_build_dir "$wt")
    if [ ! -f "$build/build.ninja" ] && [ ! -f "$build/Makefile" ]; then
        hd_configure "$wt"
    fi
}

# hd_parallel_args <array name> -- fill it with `--parallel N`, or nothing when
# the default (one job per core) is wanted. Every `cmake --build` in this file
# goes through it, so --jobs means the same thing in a build, a distribution
# install and the host tools build.
hd_parallel_args() {
    local -n _out=$1
    _out=()
    [ -z "${HD_OPT_JOBS:-}" ] || _out=(--parallel "$HD_OPT_JOBS")
}

# hd_build <worktree> [target...]
#
# More than one target is passed straight through: `cmake --build --target`
# has taken a list since CMake 3.15, and Ninja builds them in one invocation,
# which is both faster and safer than a task loop -- one configure, one lock,
# one dependency graph. Empty arguments are dropped rather than forwarded as
# an empty target name, so a caller composing a name that may come out blank
# ends up at the default target instead of a cmake error.
hd_build() {
    local wt=$1 build target targets=()
    shift
    for target in "$@"; do
        [ -n "$target" ] && targets+=("$target")
    done
    hd_ensure_configured "$wt"
    build=$(hd_build_dir "$wt")
    hd_link_cdb "$wt" "$build"
    hd_lock "$build"
    local parallel=()
    hd_parallel_args parallel
    if [ "${#targets[@]}" -gt 0 ]; then
        hd_log "building ${targets[*]} in $build"
        hd_run cmake --build "$build" "${parallel[@]}" --target "${targets[@]}"
    else
        hd_log "building in $build"
        hd_run cmake --build "$build" "${parallel[@]}"
    fi
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

# shellcheck disable=SC2034 # read by the task scripts that source this file
HD_SUITES="d3d12 vk mtl warp-d3d12 clang-d3d12 clang-vk clang-mtl clang-warp-d3d12"

# hd_test_root <worktree> <build dir> -> directory holding the per-suite lit
# trees. An llvm build tree carries them under tools/OffloadTest; a standalone
# suite build has them at its top level.
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
    hd_require_native "run tests"
    local lit="$build/bin/llvm-lit"
    [ -x "$lit" ] || hd_die "$lit not found; run 'hlsl-build' first"
    hd_log "$lit $*"
    hd_run "$lit" "$@"
}

# hd_require_native <what> -- stop a task that would execute what was built.
# A cross build produces binaries for another machine; running them here is not
# something to attempt and report as a test failure.
hd_require_native() {
    hd_is_cross || return 0
    hd_die "cannot $1 for '$(hd_platform)': those binaries are for
       $(hd_platform_triple "$(hd_platform)"), not for this machine.
       Build them here (hlsl-build --platform $(hd_platform)) and run the tests
       on the target, or drop --platform to work natively."
}
