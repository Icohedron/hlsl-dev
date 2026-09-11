#!/usr/bin/env bash
# summary: Package a build into an archive to take to another machine
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_DESC="Packages a build for the 'split build / test' flow from offload-test-suite's
docs/offload-distribution.md -- build here, run there, which is the point of a
cross build. What it packages depends on the checkout you are standing in:

  llvm-project            the tools, the clang resource headers, the tests and
                          the golden images (install-distribution,
                          install-offload-tools, install-offload-test-suite)
                          -> hlsl-<platform>.zip / .tar.gz
  DirectXShaderCompiler   dxc, dxv and the libraries they load
                          -> hlsl-dxc-<platform>.zip / .tar.gz

Both prefixes are needed on the test runner, and they are deliberately two
archives: Clang's HLSL headers and DXC's would collide in one tree.

  hlsl-package --platform windows-x64                      # the suite
  hlsl-package --platform windows-x64 --in DirectXShaderCompiler   # its dxc
  hlsl-package                                             # this machine

The archive is .zip for a Windows platform and .tar.gz otherwise, written next
to the build tree so it is never part of the checkout; --out puts it elsewhere.

On the target machine, unpack both and point the suite at the dxc prefix:

  python share/hlsl-test-suite/configure-test-suite.py --dxc-path <dxc-dist>/bin/dxc"
HD_TASK_OPTS="in= platform= jobs= out= dry_run no_auto"
hd_parse "$@"
hd_init

wt=$(hd_target llvm dxc)
HD_WT=$wt
kind=$(hd_kind "$wt")
build=$(hd_build_dir "$wt")
platform=$(hd_platform)

case "$(hd_platform_os "$platform")" in
windows) ext="zip" ;;
*) ext="tar.gz" ;;
esac

# What to call the thing. A cross archive is named after its platform; a native
# one after the machine it was built on, because "native" says nothing once the
# file has been copied somewhere (hlsl-x86_64-linux.tar.gz does).
if hd_is_cross; then
    label=$platform
else
    label="$(uname -m)-$(uname -s | tr '[:upper:]' '[:lower:]')"
fi

# --- what to build, and what ends up in the archive -------------------------
if [ "$kind" = "llvm" ]; then
    # LLVM has install targets for exactly this, and they run the build first,
    # so there is no separate "build everything" step to remember.
    hd_build "$wt" install-distribution install-offload-tools install-offload-test-suite
    prefix="$build/install"
    default_archive="$build/hlsl-$label.$ext"
else
    # DXC has no install target that produces this: `ninja install` walks every
    # cmake_install.cmake (including LLVM tools that the default target never
    # built) and fails, install-dxc covers a subset, dxv has no install target
    # at all and dxil is a prebuilt signing library with no install rule. The
    # documented answer is to copy the handful of files out of the build tree,
    # which is what this does.
    hd_build "$wt" dxc dxv dxcompiler
    prefix="$build/dxc-dist"
    default_archive="$build/hlsl-dxc-$label.$ext"

    # Everything the test runner loads, by the layout in
    # docs/offload-distribution.md. Windows keeps the DLLs beside the
    # executables so the app-directory search finds them without $PATH; on
    # Linux the binaries have a RUNPATH of ../lib. A file that this build does
    # not produce is skipped rather than fatal: dxil is Windows-only (and
    # unsigned builds have none), and PDBs exist only in a config that emits
    # them.
    if [ "$(hd_platform_os "$platform")" = "windows" ] ||
        { [ "$platform" = "native" ] && case "$(uname -s)" in *NT* | MINGW* | MSYS* | CYGWIN*) true ;; *) false ;; esac; }; then
        bin_files="dxc.exe dxv.exe dxcompiler.dll dxil.dll dxc.pdb dxv.pdb dxcompiler.pdb dxil.pdb"
        lib_files="dxcompiler.lib dxil.lib"
    else
        bin_files="dxc dxv"
        lib_files="libdxcompiler.so libdxcompiler.dylib libdxil.so libdxil.dylib"
    fi

    if [ -z "${HD_DRY_RUN:-}" ]; then
        rm -rf "$prefix"
        mkdir -p "$prefix/bin" "$prefix/lib"
        missing=""
        # -L, because `bin/dxc` in a build tree is a symlink to the
        # versioned `dxc-3.7` beside it: copying the link would put a dangling
        # one in the archive, and the file it points at is not in the list.
        for f in $bin_files; do
            if [ -e "$build/bin/$f" ]; then
                cp -aL "$build/bin/$f" "$prefix/bin/"
            else
                missing="$missing bin/$f"
            fi
        done
        for f in $lib_files; do
            if [ -e "$build/lib/$f" ]; then
                cp -aL "$build/lib/$f" "$prefix/lib/"
            else
                missing="$missing lib/$f"
            fi
        done
        [ -n "$(ls -A "$prefix/bin")" ] ||
            hd_die "nothing to package: $build/bin has no dxc"
        [ -z "$missing" ] ||
            hd_log "not in this build, left out:$missing"
    else
        hd_log "would copy dxc, dxv and their libraries into $prefix"
    fi
fi

archive=${out:-$default_archive}
case "$archive" in
/*) ;;
*) archive="$PWD/$archive" ;;
esac

if [ -z "${HD_DRY_RUN:-}" ] && [ ! -d "$prefix" ]; then
    hd_die "nothing was installed into $prefix"
fi

hd_log "packaging $prefix"
rm -f "$archive"
# cmake's own archiver, so this needs no zip/tar of a particular flavour on
# PATH and produces the same layout everywhere.
if [ -n "${HD_DRY_RUN:-}" ]; then
    hd_run cmake -E tar cf "$archive" -- .
    exit 0
fi
if [ "$ext" = "zip" ]; then
    ( cd "$prefix" && hd_run cmake -E tar cf "$archive" --format=zip -- . )
else
    ( cd "$prefix" && hd_run cmake -E tar czf "$archive" -- . )
fi

if [ -z "${HD_DRY_RUN:-}" ]; then
    printf '%-16s %s\n' "archive" "$archive"
    printf '%-16s %s\n' "size" "$(du -h "$archive" | cut -f1)"
    if hd_is_cross; then
        printf '%-16s %s\n' "platform" "$platform"
    else
        printf '%-16s %s\n' "platform" "native ($label)"
    fi
    if [ "$kind" = "llvm" ]; then
        printf '%-16s %s\n' "contents" "bin/ lib/clang/<ver>/include/ share/hlsl-test-suite/"
    else
        printf '%-16s %s\n' "contents" "$(cd "$prefix" && find bin lib -type f | sort | tr '\n' ' ')"
    fi
fi
