#!/usr/bin/env bash
# summary: Triage a report produced by offloader-monitor
set -eo pipefail
# shellcheck source=../../scripts/hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/hlsl-dev.sh"

HD_TASK_ARGS="<report>"
HD_TASK_DESC="Triages a report produced by monitor_failures.py (offloader-monitor). Writes
triage artifacts under <report>/triage/:

  * build / shader-compile failures -- bounds the first-faulting commit range
    in llvm-project (clang) or DirectXShaderCompiler (dxc) by comparing across
    the report history (no building required), then narrows to the culprit.
  * suspected driver / API-backend failures -- writes an evidence report from
    the cross-workflow pass/fail split.
  * suspected miscompiles -- compiles the shader to DXIL locally and reasons
    about it statically (never runs the offload test suite / a GPU).

Reasoning-heavy steps use an agent (pi -p) when available; pass --no-agent via
\$TRIAGE_ARGS to emit prompts instead. Offline; no GitHub token needed. Run it
on the report offloader-monitor just wrote; the more history under reports/,
the tighter the commit ranges.

<report> is the report directory, the one containing summary.json."
hd_parse "$@"
hd_need_args 1
hd_init_root

report=$(cd "${HD_ARGV[0]}" && pwd -P) ||
    hd_die "no such report directory: ${HD_ARGV[0]}"

cd "$HD_ROOT/offloader-scripts"
# shellcheck disable=SC2086 # TRIAGE_ARGS is an intentional flag list
python3 triage_report.py "$report" ${TRIAGE_ARGS:-}
