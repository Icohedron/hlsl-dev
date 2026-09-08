#!/usr/bin/env bash
# summary: Clone the submodules (shallow) into the workspace
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_DESC="Initialises the submodules with a shallow clone (--depth 2) to save time and
disk space. --recursive brings DirectXShaderCompiler's own nested submodules
(SPIRV-Tools, DirectX-Headers, ...) along."
hd_parse "$@"
hd_init_root

cd "$HD_ROOT"
git submodule update --init --recursive --depth 2
