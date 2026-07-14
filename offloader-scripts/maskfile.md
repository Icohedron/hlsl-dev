# Tasks for offloader-scripts/

Tooling for monitoring the scheduled workflows in `llvm/offload-test-suite`.
Run from this directory (or via `mask --maskfile offloader-scripts/maskfile.md ...`).

## test
Runs the unit-test suite for `monitor_failures.py`. Offline; no GitHub token
needed.

```bash
python3 -m unittest discover -s tests -v
```

## monitor [mode]
Runs `monitor_failures.py` — surveys the latest completed scheduled run of
every `llvm/offload-test-suite` workflow, classifies failures, and writes
`reports/<UTC-timestamp>/summary.{md,json,csv}` plus `divergences.json` and
`legend.json`.

Requires a GitHub token exported as `$GH_TOKEN` or `$GITHUB_TOKEN`. Only
public-repo read scope is needed.

**OPTIONS**
* mode: Optional. One of:
  * (unset) — full run: downloads logs for both failing and successful runs;
    builds the cross-workflow pass matrix and emits the divergence pivot.
  * `fast` — passes `--no-pass-matrix` so only failing-run logs are fetched.
    Faster (~half the log downloads) but the pivot section will be empty.
  * `status` — passes `--skip-logs` for a status-only survey (no downloads,
    no classification). Useful for a quick "which workflows are red today".

```bash
case "${mode:-full}" in
  status)  python3 monitor_failures.py --skip-logs ;;
  fast)    python3 monitor_failures.py --no-pass-matrix ;;
  full|"") python3 monitor_failures.py ;;
  *) echo "unknown mode: $mode (want: full | fast | status)" >&2; exit 2 ;;
esac
```

## triage (report)
Triages a report produced by `monitor_failures.py` (`mask monitor`). Writes
triage artifacts under `<report>/triage/`:

* **build / shader-compile failures** — bounds the first-faulting commit range
  in `llvm-project` (clang / clang-dxc) or `DirectXShaderCompiler` (dxc) by
  comparing across the report history (no building required), then narrows to
  the culprit commit.
* **suspected driver / API-backend failures** — writes an evidence report from
  the cross-workflow pass/fail split.
* **suspected miscompiles** — compiles the shader to DXIL locally and reasons
  about it statically (never runs the offload test suite / a GPU).

Reasoning-heavy steps use an agent (`pi -p`) when available; pass `--no-agent`
(via env `TRIAGE_ARGS`) to emit prompts instead. Offline; no GitHub token needed.
Run after `mask monitor` on the freshly written report; the more history under
`reports/`, the tighter the commit ranges.

**OPTIONS**
* report (required): path to a report directory (the one containing `summary.json`).

```bash
python3 triage_report.py "$report" ${TRIAGE_ARGS:-}
```
