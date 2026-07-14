# offloader-scripts

Tooling for monitoring — and triaging — the scheduled CI workflows of
[`llvm/offload-test-suite`](https://github.com/llvm/offload-test-suite).

Two stdlib-only Python tools work together:

| tool | what it does |
|---|---|
| **`monitor_failures.py`** | Surveys the latest scheduled run of every workflow, downloads + classifies the failing logs, and writes a timestamped **report**. |
| **`triage_report.py`** | Consumes a report (and the history of prior reports) and produces **triage** artifacts: first-faulting commit ranges, driver/API evidence writeups, and static DXIL analysis of suspected miscompiles. |

Both are offline-friendly: only the monitor's log download needs a GitHub
token, and triage never needs one.

---

## Workflow at a glance

```
   (scheduled CI on llvm/offload-test-suite)
                    │
                    ▼
        mask monitor            ──►  reports/<UTC-ts>/summary.{json,md,csv}
   (classify red workflows)          reports/<UTC-ts>/logs/*.log.gz
                    │
                    ▼
        mask triage <report>    ──►  reports/<UTC-ts>/triage/<item>.md
   (root-cause each failure)          reports/<UTC-ts>/triage/{triage.json,README.md}
```

Run the monitor on a cadence (e.g. daily). Each report is a snapshot; triage
compares a report against **the accumulated history of earlier reports**, so
the more reports you keep, the tighter the commit ranges it can derive — with
no compiler builds required.

---

## Setup

- **Python 3.10+**, standard library only. No pip dependencies.
- **GitHub token** (monitor only): export `GH_TOKEN` or `GITHUB_TOKEN` with
  public-repo read scope. A local `.env` in this directory is a convenient
  place to keep it.
- **Sibling checkouts** (triage only, for commit-range resolution and DXIL):
  `../llvm-project`, `../DirectXShaderCompiler`, `../offload-test-suite`. These
  are shallow by default; triage unshallows on demand (see below).
- **`dxc`** on `PATH` (triage DXIL analysis). Any DXC that emits `-Fc`
  disassembly works; override with `--dxc-bin`.
- **`pi`** on `PATH` (optional): enables the agentic reasoning steps. Without
  it, use `--no-agent`.

Tasks are defined in [`maskfile.md`](./maskfile.md) via
[mask](https://github.com/jacobdeichert/mask); run `mask` with no args to list
them.

---

## Monitoring — `monitor_failures.py`

```bash
mask monitor            # full: fetch failing + successful logs, build divergence pivot
mask monitor fast       # --no-pass-matrix: failing logs only (~half the downloads)
mask monitor status     # --skip-logs: which workflows are red today (no classification)

# equivalently:
python3 monitor_failures.py [--skip-logs] [--no-pass-matrix] [--otss-root DIR] [--out-root DIR]
```

For each active workflow it fetches the latest scheduled run; if the run isn't
`success`, it downloads the logs, parses them, and classifies each failure.

**Report layout** — `reports/<UTC-timestamp>/`:

| file | contents |
|---|---|
| `summary.json` | machine-readable per-workflow rows, including `commits` (built llvm/dxc/offload SHAs) and per-test `classification` |
| `summary.md` | human summary table (with a **build (llvm / dxc)** column) + cross-workflow divergence pivot |
| `summary.csv` | flat per-test rows |
| `divergences.json` | tests that fail on some workflows but pass on others |
| `legend.json` | explanation of every classification label |
| `logs/<workflow>.log.gz` | raw combined log (gzipped) |
| `logs/<workflow>.failures.txt` | extracted FAIL/XPASS blocks |

**Classification tree** (best-effort, log-driven):

- `build_failure` — `clang_llvm` (building llvm-project / clang / clang-dxc),
  `dxc` (building DirectXShaderCompiler), or `other` (infra / checkout / cmake).
- `test_failure` — `shader_compile_dxc` / `shader_compile_clang_dxc` (compiler
  failed to build a shader), or `runtime_*` (shader compiled; failure at
  execute/verify: `driver_error` | `miscompile` | `unknown`).
- `xpass` — an XFAIL test that unexpectedly passed (with linked GitHub issue).
- `runtime_driver_suspected_*` / `api_backend_suspected_*` — cross-workflow
  divergence upgrades: the same binary passes on some backends and fails on
  others. Labels stay **suspected**; divergence alone is not proof.

> Lavapipe is treated as a GPU (a software rasterizer running the Vulkan path),
> never as an API backend.

---

## Triaging — `triage_report.py`

```bash
mask triage reports/<UTC-ts>          # triage the given report
TRIAGE_ARGS="--no-agent" mask triage reports/<UTC-ts>

# equivalently:
python3 triage_report.py <report> [--no-agent] [--no-fetch-history] [--max N] \
    [--dxc-bin dxc] [--agent-model M] [--agent-timeout S] \
    [--otss-root DIR] [--llvm-root DIR] [--dxc-root DIR]
```

Triage routes each failure to one of three strategies by its classification:

### 1. Commit bisect — build & shader-compile failures
`build_failure`, `shader_compile_dxc`, `shader_compile_clang_dxc`.

Every scheduled run records the exact compiler commits it built. The monitor
now captures these in `summary.json` (`row["commits"]`), so triage bounds
*when* a failure first appeared **without building anything**: the last report
where the test/build passed and the first where it failed pin a `good..bad`
range in `llvm-project` (clang / clang-dxc) or `DirectXShaderCompiler` (dxc).
The range is then narrowed to the first faulting commit by reading the diffs in
the local checkout (agentically, when `pi` is available). If a SHA can't be
resolved locally, a GitHub `/compare/` URL is emitted instead.

### 2. Evidence report — suspected driver / API-backend failures
`runtime_driver_suspected_*`, `api_backend_suspected_*`.

The same shader binary runs on every workflow, so a pass/fail split across
GPUs/APIs is evidence about *where* the bug lives. Triage marshals that split
plus the failure excerpt into a writeup of the driver/backend-specific
behaviour — with a theory when the evidence supports one, and the
test-spec-bug alternative flagged when it can't be ruled out.

### 3. DXIL analysis — suspected miscompiles
`*_miscompile`.

Triage locates the test, compiles its shader(s) to DXIL locally
(`dxc -T <profile> -Fc`), and reasons statically from the **disassembly**.
Multi-shader graphics tests (`vertex.hlsl` + `pixel.hlsl`) are each matched to
their RUN compile line. **The offload test suite is never run** — the
environment is assumed to have no GPU / software renderer, so miscompiles are
interpreted from the DXIL alone.

**Triage layout** — `reports/<UTC-ts>/triage/`:

| file | contents |
|---|---|
| `README.md` | index of triaged items with their strategy |
| `triage.json` | machine-readable results (ranges, compiled shaders, etc.) |
| `<strategy>__<workflow>__<suite>__<test>.md` | one writeup per failure |

**Full git history.** Sibling repos are shallow clones. Triage unshallows them
on demand (`mask fetch-history <repo>`, with a direct `git fetch --unshallow`
fallback) to resolve commit ranges. Pass `--no-fetch-history` to skip this and
rely on `/compare/` URLs.

**Agent vs headless.** Deterministic evidence gathering is stdlib-only and
works fully headless. The reasoning-heavy steps delegate to an agent
(`pi -p`) when available; with `--no-agent` the assembled evidence plus a
ready-to-use prompt are written for you to run later.

---

## Testing

Offline; no token needed.

```bash
mask test
# or:
python3 -m unittest discover -s tests -v
```

`tests/test_monitor_failures.py` pins the monitor's parsers/classifiers against
checked-in log excerpts under `tests/fixtures/`; `tests/test_triage_report.py`
covers the triager's deterministic logic (commit extraction, repo dispatch,
split-file / RUN-line parsing, commit-range bounding).

---

## Conventions

- Python: standard library only where feasible; no pip deps.
- Follow surrounding style; keep changes minimal and targeted.
- `reports/` is generated output and is gitignored.
