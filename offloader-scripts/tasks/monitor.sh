#!/usr/bin/env bash
# summary: Survey the llvm/offload-test-suite scheduled workflows
set -eo pipefail
# shellcheck source=../../scripts/hlsl-dev.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/hlsl-dev.sh"

HD_TASK_ARGS="[full|fast|status]"
HD_TASK_DESC="Runs monitor_failures.py -- surveys the latest completed scheduled run of every
llvm/offload-test-suite workflow, classifies failures, and writes
offloader-scripts/reports/<UTC-timestamp>/summary.{md,json,csv} plus
divergences.json and legend.json.

Needs a GitHub token; public-repo read scope is enough. It takes one from
\$GH_TOKEN or \$GITHUB_TOKEN if the environment has it -- a dev container
forwards the host's, and CI sets its own -- and otherwise asks secretspec for
it, which keeps the value out of the environment, out of devenv.nix and out of
the Nix store:

  secretspec config global init    # choose a provider, once per machine
  secretspec set GH_TOKEN          # keyring, 1Password, ... whatever it is

  full (default)  downloads logs for failing *and* successful runs, builds the
                  cross-workflow pass matrix and emits the divergence pivot
  fast            --no-pass-matrix: only failing-run logs (about half the
                  downloads), but the pivot section stays empty
  status          --skip-logs: status only, no downloads and no classification"
hd_parse "$@"
hd_init_root

cd "$HD_ROOT/offloader-scripts"

case "${HD_ARGV[0]:-full}" in
status) args=(--skip-logs) ;;
fast)   args=(--no-pass-matrix) ;;
full)   args=() ;;
*)      hd_die "unknown mode: ${HD_ARGV[0]} (want: full | fast | status)" ;;
esac

# A token already in the environment wins: that is the dev container forwarding
# the host's, or CI providing its own, and going around it would be surprising.
# Otherwise hand the run to secretspec, if it has somewhere to get one from --
# the probe is there because `secretspec run` fails outright on a machine with
# no provider configured, which must not stop anyone running the monitor with a
# token they exported by hand. Failing that, monitor_failures.py says what is
# missing better than we could here.
if [ -z "${GH_TOKEN:-}${GITHUB_TOKEN:-}" ] &&
    command -v secretspec >/dev/null 2>&1 &&
    secretspec run -- true >/dev/null 2>&1; then
    hd_log "taking the GitHub token from secretspec"
    exec secretspec run -- python3 monitor_failures.py "${args[@]}"
fi

exec python3 monitor_failures.py "${args[@]}"
