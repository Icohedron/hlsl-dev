#!/usr/bin/env bash
# summary: Show, list or switch the Vulkan driver the tests run against
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_ARGS="[driver]"
HD_TASK_DESC="The vk and clang-vk suites execute SPIR-V, so they need a Vulkan driver (an
'ICD'). This is the one knob for choosing it.

  hlsl-vk              what the tests will run against, and what that exposes
  hlsl-vk --list       the drivers this machine offers
  hlsl-vk lavapipe     switch; it applies to the next command, not the next shell

<driver> is 'system' (let the loader discover drivers itself -- use this on a
machine with a working native driver), 'lavapipe' (Mesa's CPU rasterizer: slow,
but it works everywhere, and the default), a Mesa ICD short name from --list,
or an absolute path to an ICD manifest.

The choice is remembered in the workspace, outside every checkout. To override
it for one command, set \$HLSL_VK_DRIVER; to bypass all of it, set the loader's
own \$VK_DRIVER_FILES -- lit forwards that into the tests as well."
HD_TASK_OPTS="list export"
hd_parse "$@"
hd_init_root

# --export is what the shell hook evaluates on entry, so that a plain
# vulkaninfo or offloader in the shell is pinned to the same driver the tasks
# use. Printing rather than exporting keeps one resolver for both.
if [ -n "${export:-}" ]; then
    icd=$(hd_vk_icd)
    if [ -n "$icd" ] && [ -e "$icd" ]; then
        printf 'export VK_DRIVER_FILES=%s\n' "$icd"
        printf 'export VK_ICD_FILENAMES=%s\n' "$icd"
    else
        printf 'unset VK_DRIVER_FILES VK_ICD_FILENAMES\n'
    fi
    exit 0
fi

if [ -n "${list:-}" ]; then
    [ -n "${HLSL_VK_ICD_DIR:-}" ] ||
        hd_die "not in the developer environment; run 'devenv shell' first"
    echo "system      let the Vulkan loader discover drivers itself (real GPU)"
    echo "lavapipe    Mesa's CPU rasterizer - slow, but always works (default)"
    echo
    echo "Mesa ICDs in $HLSL_VK_ICD_DIR:"
    find "$HLSL_VK_ICD_DIR" -maxdepth 1 -name "*_icd.$(uname -m).json" -printf '%f\n' |
        sed -e 's/_icd\..*\.json$//' -e 's/^/    /' | sort
    echo
    echo "An absolute path to any ICD manifest is also accepted."
    exit 0
fi

# --- switch -----------------------------------------------------------------
if [ "${#HD_ARGV[@]}" -gt 0 ]; then
    driver=${HD_ARGV[0]}
    hd_setting_set VK_DRIVER "$driver"
    echo "Vulkan driver: $driver"
    if [ -n "${HLSL_VK_DRIVER:-}" ] && [ "$HLSL_VK_DRIVER" != "$driver" ]; then
        hd_warn "\$HLSL_VK_DRIVER='$HLSL_VK_DRIVER' is set in this environment and
         overrides the saved choice; unset it for this to take effect"
    fi
fi

# --- status -----------------------------------------------------------------
driver=$(hd_vk_driver)
icd=$(hd_vk_icd)
printf '%-16s %s\n' "driver" "${driver:-lavapipe (default)}"
if [ -z "$icd" ]; then
    printf '%-16s %s\n' "manifest" "<none> -> the loader discovers drivers itself"
elif [ -e "$icd" ]; then
    printf '%-16s %s\n' "manifest" "$icd"
else
    printf '%-16s %s\n' "manifest" "$icd (missing -- see 'hlsl-vk --list')"
fi
printf '%-16s %s\n' "settings" "$(hd_settings_file)"

if command -v vulkaninfo >/dev/null 2>&1; then
    echo
    hd_vk_export
    vulkaninfo --summary 2>/dev/null | sed -n '/^Devices:/,$p' ||
        hd_warn "vulkaninfo could not enumerate any device with this driver"
fi
