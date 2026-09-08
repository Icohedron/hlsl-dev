#!/usr/bin/env bash
# summary: Start the sccache server with no idle timeout
set -eo pipefail
# shellcheck source=../hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../hlsl-dev.sh"

HD_TASK_DESC="Starts the sccache server with no timeout. Convenient for agents running bash
in sandboxes where it is inappropriate or disallowed for them to start the
sccache server themselves."
hd_parse "$@"

SCCACHE_IDLE_TIMEOUT=0 sccache --start-server
