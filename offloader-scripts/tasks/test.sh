#!/usr/bin/env bash
# summary: Run the offloader-scripts unit tests
set -eo pipefail
# shellcheck source=../../scripts/hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/hlsl-dev.sh"

HD_TASK_DESC="Runs the unit-test suite for monitor_failures.py. Offline; no GitHub token
needed."
hd_parse "$@"
hd_init_root

cd "$HD_ROOT/offloader-scripts"
python3 -m unittest discover -s tests -v
