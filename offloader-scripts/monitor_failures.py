#!/usr/bin/env python3
"""
Monitor scheduled-workflow failures on llvm/offload-test-suite.

For each active workflow that has scheduled runs, fetch the latest scheduled run.
If it's not "success", download its logs, parse them, and classify each failure.

Classification tree (best-effort, log-driven):

  build_failure
      clang_llvm    - failure while building llvm-project / clang / clang-dxc
      dxc           - failure while building DirectXShaderCompiler
      other         - infra / checkout / cmake / device-setup step failed
  test_failure
      shader_compile_dxc         - dxc failed to compile a shader for a test
      shader_compile_clang_dxc   - clang-dxc failed to compile a shader
      runtime                    - shader compiled; failure was at execute/verify
        (subclass: driver_error | miscompile | unknown)
      xpass                      - test expected to fail but passed
        (with linked GitHub issue lookup from the test file)

The tool writes:
  offloader-scripts/reports/<UTC-timestamp>/summary.{json,md,csv}
  offloader-scripts/reports/<UTC-timestamp>/logs/<workflow>.log     (raw combined log, gzipped)
  offloader-scripts/reports/<UTC-timestamp>/logs/<workflow>.failures.txt  (extracted FAIL/XPASS blocks)

Auth: reads a token from $GH_TOKEN or $GITHUB_TOKEN.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import html
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict
from typing import Any

REPO = "llvm/offload-test-suite"
API = "https://api.github.com"


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


TOKEN_ENV_VARS = ("GH_TOKEN", "GITHUB_TOKEN")


def load_token() -> str:
    for env in TOKEN_ENV_VARS:
        val = os.environ.get(env)
        if val:
            return val.strip()
    raise SystemExit(
        "No GitHub token in environment. Set $GH_TOKEN or $GITHUB_TOKEN "
        "(public-repo read scope is sufficient) before running."
    )


class GH:
    def __init__(self, token: str):
        self.token = token

    def _req(self, url: str, accept: str = "application/vnd.github+json") -> urllib.request.Request:
        return urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": accept,
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "hlsl-monitor/0.1",
            },
        )

    def get_json(self, path: str, **params) -> Any:
        url = f"{API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(self._req(url)) as r:
            return json.loads(r.read())

    def get_bytes(self, url: str) -> bytes:
        # Follow redirects (log archive is served from Azure Blob).
        # GitHub's /logs endpoint 302s to a signed URL that rejects a custom
        # Accept header, so use the default JSON accept.
        req = self._req(url)
        with urllib.request.urlopen(req) as r:
            return r.read()


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------


# Path fragments that tell us which build we're in.
LLVM_BUILD_PATH_RE = re.compile(r"llvm-project|/llvm[/-]|clang-dxc|(?:^|/)clang(?:$|/)|(?:^|/)llvm(?:$|/)", re.I)
DXC_BUILD_PATH_RE = re.compile(r"DirectXShaderCompiler|(?:^|/)dxc(?:$|/)", re.I)

# lit result markers
LIT_RESULT_RE = re.compile(
    r"^(?P<result>FAIL|XPASS|UNRESOLVED|TIMEOUT|LIT_UNRESOLVED):\s+(?P<suite>[\w\-.]+)\s+::\s+(?P<test>\S+)"
)
LIT_TESTCASE_HEADER_RE = re.compile(
    r"TEST\s+'([\w\-.]+)\s+::\s+(\S+)'\s+FAILED"
)
# `lit -v` prints "PASS: suite :: test (N of M)" for every pass, plus XFAIL/UNSUPPORTED.
LIT_STATUS_LINE_RE = re.compile(
    r"^(?P<result>PASS|FAIL|XFAIL|XPASS|UNSUPPORTED|UNRESOLVED|TIMEOUT|SKIPPED):\s+"
    r"(?P<suite>[\w\-.]+)\s+::\s+(?P<test>\S+)"
)

# GitHub Actions log line prefix (timestamp)
GHA_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s+")

# GitHub Actions in-file group markers
GHA_GROUP_START = re.compile(r"##\[group\](.*)$")
GHA_GROUP_END = re.compile(r"##\[endgroup\]")
GHA_STEP_START = re.compile(r"##\[section\]Starting: (.+)$")


def strip_ts(line: str) -> str:
    return GHA_TS_RE.sub("", line)


def _parse_lit_commands(block: str) -> list[dict]:
    """
    Parse the lit failure block into a list of executed-command records:

        {"cmd": "...full command line...",
         "stdout": "...", "stderr": "...",
         "exit_status": "0"|"1"|"0xc0000005"|... (str), "kind": "dxc"|"clang_dxc"|"offloader"|"filecheck"|"other"}

    lit's output looks like:
        # RUN: at line 84
        <expanded command>
        # executed command: '...'
        # .---command stdout------------
        # | ...
        # `-----------------------------
        # .---command stderr------------
        # | ...
        # `-----------------------------
        # error: command failed with exit status: N     <-- only if it failed
    """
    lines = block.splitlines()
    cmds: list[dict] = []
    cur: dict | None = None
    section: str | None = None  # 'stdout' or 'stderr'
    for line in lines:
        # Detect an "executed command:" line — canonical form.
        m = re.match(r"^# executed command:\s+(.*)$", line)
        if m:
            if cur is not None:
                cmds.append(cur)
            cmd = m.group(1)
            kind = "other"
            low = cmd.lower()
            if re.search(r"[\\/]clang[-_]?dxc(?:\.exe)?['\"]?", low) or "clang_dxc" in low or "clang-dxc" in low:
                kind = "clang_dxc"
            elif re.search(r"[\\/]dxc(?:\.exe)?['\"]?", low):
                kind = "dxc"
            elif "offloader" in low or "gpu-exec" in low:
                kind = "offloader"
            elif "filecheck" in low:
                kind = "filecheck"
            elif "split-file" in low:
                kind = "split_file"
            cur = {"cmd": cmd, "stdout": [], "stderr": [], "exit_status": "0", "kind": kind}
            section = None
            continue
        if cur is None:
            continue
        if line.startswith("# .---command stdout"):
            section = "stdout"
            continue
        if line.startswith("# .---command stderr"):
            section = "stderr"
            continue
        if line.startswith("# `---"):
            section = None
            continue
        if section and line.startswith("# | "):
            cur[section].append(line[4:])
            continue
        # Exit-status footer for a failed command
        m = re.match(r"^# error: command failed with exit status:\s+(\S+)", line)
        if m:
            cur["exit_status"] = m.group(1)
            continue
    if cur is not None:
        cmds.append(cur)
    # normalise stdout/stderr to strings
    for c in cmds:
        c["stdout"] = "\n".join(c["stdout"])
        c["stderr"] = "\n".join(c["stderr"])
    return cmds


def _status_is_failure(status: str) -> bool:
    # Any non-"0" exit is a failure, e.g. "1", "0xc0000005", "3221225477".
    return status != "0"


def classify_shader_compile(block: str) -> str | None:
    """
    Return 'shader_compile_dxc' or 'shader_compile_clang_dxc' iff the dxc /
    clang-dxc invocation in this failure block itself exited non-zero.
    Otherwise None (failure was in a later step: offloader / filecheck / etc.).
    """
    cmds = _parse_lit_commands(block)
    if not cmds:
        return None
    for c in cmds:
        if c["kind"] in ("dxc", "clang_dxc") and _status_is_failure(c["exit_status"]):
            return f"shader_compile_{c['kind']}"
    return None

# Runtime rejected pipeline/PSO creation. Matches the offloader's messages
# across backends: D3D12 "Failed to create PSO." / "Failed to create graphics
# PSO." / "Failed to create mesh shader PSO." and Vulkan "Failed to create
# compute pipeline." / "Failed to create graphics pipeline." (the optional words
# between "create" and "pso/pipeline" cover the graphics/mesh-shader variants).
_PIPELINE_MARKER_RE = re.compile(r"failed to create (?:\w+ )*(?:pso|pipeline)\b")

# The offloader tool (tools/offloader, internally "gpu-exec") prefixes every
# error it emits with "gpu-exec: error: " — an unambiguous marker that a failure
# came from the GPU-execution stage (i.e. is runtime-related), never the
# compiler or FileCheck.
_GPU_EXEC_ERROR = "gpu-exec: error:"

def classify_runtime(block: str) -> str:
    """
    Called when the dxc/clang-dxc step succeeded but a later step failed.
    Distinguish driver crash (offloader access violation, TDR, device lost)
    from a pipeline-state creation failure, a value/image mismatch
    (miscompile), or unknown.
    """
    cmds = _parse_lit_commands(block)
    # Focus on the first failing non-compiler command
    failing = [c for c in cmds if _status_is_failure(c["exit_status"]) and c["kind"] not in ("dxc", "clang_dxc")]

    driver_markers = (
        "device removed", "device lost", "dxgi_error", "vk_error_device_lost",
        "hung", "tdr", "access violation", "unhandled exception",
        "driver crashed", "d3d12: removing device",
    )
    # Pipeline/PSO creation was rejected by the runtime — detected via
    # _PIPELINE_MARKER_RE (covers D3D12 "Failed to create [graphics/mesh shader]
    # PSO." and Vulkan "Failed to create compute/graphics pipeline."). The shader
    # compiled fine but Create*PipelineState / vkCreate*Pipelines failed; this is
    # distinct from a driver crash (process survives, clean error) and a
    # miscompile (nothing ever executed). Usually a backend/driver rejection or a
    # malformed pipeline spec in the test itself.
    miscompile_markers = (
        "mismatch", "verify failed", "verification failed",
        "image mismatch", "golden image", "expected .* actual", "diff:",
        "filecheck error",
    )

    # If offloader crashed with an NT status (0xC0000005 etc.) treat as driver
    # crash. A command is offloader output if its kind says so OR its payload
    # carries the gpu-exec error prefix (robust to command-kind misdetection).
    for c in failing:
        payload = (c["stdout"] + "\n" + c["stderr"]).lower()
        is_offloader = c["kind"] == "offloader" or _GPU_EXEC_ERROR in payload
        if is_offloader and c["exit_status"].startswith(("0x", "-")):
            return "runtime_driver_error"
        if is_offloader:
            if any(m in payload for m in driver_markers):
                return "runtime_driver_error"
            if _PIPELINE_MARKER_RE.search(payload):
                return "runtime_pipeline_error"
            if any(re.search(m, payload) for m in miscompile_markers):
                return "runtime_miscompile"
        if c["kind"] == "filecheck":
            return "runtime_miscompile"

    # Fallback on whole-block text
    lower = block.lower()
    if any(m in lower for m in driver_markers):
        return "runtime_driver_error"
    if _PIPELINE_MARKER_RE.search(lower):
        return "runtime_pipeline_error"
    if any(re.search(m, lower) for m in miscompile_markers):
        return "runtime_miscompile"
    return "runtime_unknown"


def extract_failure_blocks(log_text: str) -> list[dict]:
    """
    Split the combined log into per-test FAILED / XPASS blocks.

    lit prints a delimited block per failing test:

        ******************** TEST 'suite :: path/name.test' FAILED ********************
        <content>
        ********************

    Returns a list of dicts: {result, suite, test, block}.
    """
    lines = log_text.splitlines()

    # Collect XPASS test keys first. lit counts an unexpected pass as a suite
    # failure, so the SAME test also emits a `TEST '...' FAILED` banner. Its
    # canonical result is XPASS, not FAIL — we must not also record it as a
    # runtime failure, or it shows up twice (once xpass, once runtime_*).
    xpass_re = re.compile(r"^\s*XPASS:\s+(\S+)\s+::\s+(\S+)")
    xpass_keys: set[tuple[str, str]] = set()
    for line in lines:
        m = xpass_re.match(strip_ts(line))
        if m:
            xpass_keys.add((m.group(1), m.group(2)))

    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        line = strip_ts(lines[i])
        m = LIT_TESTCASE_HEADER_RE.search(line)
        if m:
            suite, test = m.group(1), m.group(2)
            # accumulate until closing row of stars
            body_lines = []
            i += 1
            while i < len(lines):
                nxt = strip_ts(lines[i])
                if re.match(r"^\*{20,}\s*$", nxt) and body_lines:
                    break
                body_lines.append(nxt)
                i += 1
            # Skip the FAILED banner emitted for an unexpected pass; the XPASS
            # entry below is the authoritative record for this test.
            if (suite, test) not in xpass_keys:
                blocks.append({"result": "FAIL", "suite": suite, "test": test, "block": "\n".join(body_lines)})
        i += 1

    # XPASS blocks come from the summary; the individual test doesn't emit a
    # detailed block. Collect XPASSes from the "Unexpectedly Passed Tests" list.
    for suite, test in sorted(xpass_keys):
        blocks.append({"result": "XPASS", "suite": suite, "test": test, "block": ""})
    return blocks


BUILD_FAIL_MARKERS = [
    ("cmake", re.compile(r"CMake Error", re.I)),
    ("compile", re.compile(r"\berror C\d{4}\b|\bfatal error C\d{4}\b|\berror:.*(?:no such file|cannot find|undefined)", re.I)),
    ("link", re.compile(r"LNK\d{4}|undefined reference|ld returned")),
    ("ninja", re.compile(r"ninja: build stopped", re.I)),
    ("msbuild", re.compile(r"Build FAILED\.", re.I)),
]


def classify_build_failure(log_text: str) -> str:
    """
    Heuristic: scan lines around build-error markers for path context to guess
    whether the failing build was clang/llvm, dxc, or infra/other.
    """
    lines = log_text.splitlines()
    votes = Counter()
    for idx, raw in enumerate(lines):
        line = strip_ts(raw)
        for _kind, rx in BUILD_FAIL_MARKERS:
            if rx.search(line):
                # look at surrounding 20 lines for path context
                lo, hi = max(0, idx - 20), min(len(lines), idx + 5)
                ctx = "\n".join(strip_ts(l) for l in lines[lo:hi])
                if DXC_BUILD_PATH_RE.search(ctx):
                    votes["dxc"] += 1
                elif LLVM_BUILD_PATH_RE.search(ctx):
                    votes["clang_llvm"] += 1
                else:
                    votes["other"] += 1
                break
    if not votes:
        return "unknown"
    return votes.most_common(1)[0][0]


def has_test_stage_output(log_text: str) -> bool:
    return (
        "Testing:" in log_text
        or "PASS:" in log_text
        or "FAIL:" in log_text
        or "Total Discovered Tests" in log_text
        or "check-hlsl" in log_text.lower()
    )


# ---------------------------------------------------------------------------
# Classification labels — human-readable explanations.
#
# Two-tier scheme:
#   * Base labels are emitted by `classify_run` from a single failing log.
#   * Upgraded labels are emitted by the cross-workflow pivot in `main` once
#     it can compare a failing test against its results on other workflows;
#     they refine "runtime_*" bases when the failure aligns cleanly on the
#     api / gpu / compiler / host / variant axis.
#
# Rendered as a table at the top of every summary.md so readers don't need
# to reverse-engineer the labels from source.
# ---------------------------------------------------------------------------


CLASSIFICATION_LEGEND: dict[str, str] = {
    # Build-stage failures (job never entered lit).
    "build_failure":
        "Job failed before running any tests (cmake/compile/link/ninja/msbuild error). "
        "`detail` says which subtree the error was in: clang_llvm | dxc | other | unknown.",

    # Test-stage failures — base categories (no cross-workflow data used).
    "shader_compile_dxc":
        "The `dxc.exe` invocation in the failing test's block exited non-zero — "
        "DXC couldn't compile the shader. Not the runtime's fault.",
    "shader_compile_clang_dxc":
        "The `clang-dxc.exe` invocation in the failing test's block exited non-zero — "
        "clang-dxc couldn't compile the shader. Not the runtime's fault.",
    "runtime_driver_error":
        "Shader compiled OK, but a later step (typically `offloader.exe`) crashed with "
        "an NT status like 0xC0000005, hit a device-lost / TDR / access-violation "
        "marker, or otherwise reported the driver had gone away.",
    "runtime_pipeline_error":
        "Shader compiled OK, but the runtime rejected pipeline-state creation "
        "(D3D12 `Failed to create PSO.`, Vulkan `Failed to create compute/graphics "
        "pipeline.`). The offloader reported a clean non-zero error rather than "
        "crashing, and nothing ever executed. Usually a backend/driver rejection of "
        "the compiled shader OR a malformed pipeline spec in the test itself "
        "(root signature / bindings). Distinct from runtime_driver_error (crash) and "
        "runtime_miscompile (ran, wrong result).",
    "runtime_miscompile":
        "Shader compiled OK, the pipeline ran to completion, but output values or "
        "images didn't match the golden reference (FileCheck failed, verify failed, "
        "or an 'expected X actual Y' diff). Points at wrong codegen or wrong runtime "
        "execution.",
    "runtime_unknown":
        "Shader compiled OK, a later step exited non-zero, but no driver-crash or "
        "value-mismatch signal was found in the log. Fell through both classifiers.",
    "xpass":
        "Test was marked XFAIL but passed. Report also links the specific XFAIL "
        "clause's GitHub issue(s) (chosen by evaluating the clause's lit feature "
        "expression against the actual test-runner's feature set — see the row's "
        "`linked_issue`, `linked_issues`, and `xfail_match`).",

    # Upgraded labels — applied by the cross-workflow pivot after the pass
    # matrix reveals the same test passes on at least one other workflow.
    "runtime_driver_suspected_miscompile":
        "Base label was `runtime_miscompile`, but the SAME shader binary produces the "
        "right result on some other workflows. Every workflow compiles the exact same "
        "shader from the exact same source, so a value mismatch that only appears on some "
        "runners strongly suggests a per-vendor driver/codegen bug — hence 'driver "
        "suspected'. Not a certainty, though: uninitialized/under-specified test inputs, "
        "data races, or a tolerance the test sets too tight can also make correct output "
        "differ across hardware. See the row's `axes` for the pattern (e.g. "
        "gpu_pattern: NVIDIA-only).",
    "runtime_driver_suspected_crash":
        "Base label was `runtime_driver_error`. The same test passes on other workflows, "
        "so the crash is specific to some subset of GPUs/APIs. That narrows it to a "
        "runtime-environment-dependent fault, but does NOT confirm a driver defect: a "
        "malformed pipeline spec in the test itself (wrong resource bindings, buffer "
        "sizes, root signature, or uninitialized/under-specified inputs) can be tolerated "
        "by lenient drivers yet rejected or crashed by stricter ones or a validation "
        "layer. Treat as 'GPU/API-specific crash, root cause unconfirmed' — could be a "
        "driver bug OR a test-authoring bug. `axes` tells you which subset diverges.",
    "runtime_driver_suspected_unknown":
        "Base label was `runtime_unknown`. The test passes elsewhere, so the failure is "
        "at least *runtime-dependent* — but we couldn't pinpoint driver-crash vs "
        "miscompile from the log. Manual inspection needed; `axes` narrows the search.",
    "api_backend_suspected_miscompile":
        "Base label was `runtime_miscompile`, and the failing set aligns cleanly on the "
        "**API axis** (all failures share one API, at least one workflow on a different "
        "API passes) *without* aligning on any GPU vendor. That points at the "
        "API/backend layer — e.g. the Vulkan SPIRV codegen path — rather than a "
        "per-vendor driver. Common example: `Texture2D.Sample` fails on all Vulkan "
        "drivers and passes on D3D12.",
    "api_backend_suspected_crash":
        "Base label was `runtime_driver_error`, upgraded on the API-axis criterion (all "
        "failures share one API, at least one workflow on a different API passes, with no "
        "per-GPU-vendor alignment). A crash confined to one API backend points at that "
        "backend's codegen/runtime path rather than a single vendor's driver — but, as "
        "with `runtime_driver_suspected_crash`, it can equally be the test specifying "
        "pipeline inputs/outputs in a way only that backend rejects. API-specific crash, "
        "root cause unconfirmed (backend bug OR test-spec bug).",
    "api_backend_suspected_unknown":
        "Base label was `runtime_unknown`, upgraded via API-axis attribution. The "
        "API/backend correlates but the specific failure mode wasn't parsed from the log.",
    "api_backend_suspected_unknown":
        "Base label was `runtime_unknown`, upgraded via API-axis attribution. The "
        "API/backend correlates but the specific failure mode wasn't parsed from the log.",
    # NOTE: there is deliberately no `compiler_suspected_*` label. A failing set
    # that aligns on the compiler axis is surfaced via the `compiler_pattern`
    # axis (see `attribute_divergence` / the axis legend) but is NOT upgraded to
    # a fault-implying classification — clang-dxc and DXC emit different DXIL, so
    # a compiler-aligned divergence is different-output, not a proven compiler
    # bug. See `divergence_suspect_prefix` for the rationale.
}


# The workflow summary table's `category` column: the stage at which the run
# failed. Rendered as its own legend so a reader knows what `test_failure` vs
# `build_failure` mean without inferring it from the per-test classifications.
CATEGORY_LEGEND: dict[str, str] = {
    "build_failure":
        "The run failed before any test executed — no lit test-stage output was "
        "produced (cmake/compile/link/ninja/msbuild error, or an infra/checkout/"
        "device-setup step). The `detail` column says which subtree. There are no "
        "per-test rows for this workflow.",
    "test_failure":
        "The run reached the lit test stage and at least one test FAILED or XPASSed. "
        "Each failing test gets a row under 'Failures by workflow' with its own "
        "`classification`; the `detail` column is normally empty (see the per-test "
        "rows) unless no per-test block could be parsed (`unknown_no_blocks`).",
}


# The workflow summary table's `detail` column: refines `category`. For
# build_failure it names the failing subtree; for test_failure it only appears
# when the per-test blocks couldn't be parsed.
DETAIL_LEGEND: dict[str, str] = {
    # build_failure subtree (from classify_build_failure)
    "clang_llvm":
        "Build error was in the llvm-project / clang / clang-dxc subtree.",
    "dxc":
        "Build error was in DirectXShaderCompiler.",
    "other":
        "Build error was in infra / checkout / cmake / device-setup — not a "
        "compiler subtree.",
    "unknown":
        "A build-error marker fired but no surrounding path context identified "
        "the subtree.",
    # test_failure detail
    "unknown_no_blocks":
        "The test stage ran but no per-test FAIL/XPASS block could be parsed — "
        "e.g. a build error that still emitted a partial testing marker, or an "
        "infra flake. No per-test classification is available for this run.",
}


# lit suite names encode the platform as a suffix on a shared base name:
#   OffloadTest-vk, OffloadTest-clang-vk, OffloadTest-d3d12,
#   OffloadTest-warp-d3d12, OffloadTest-clang-warp-d3d12, OffloadTest-mtl, ...
# (config.name = "OffloadTest-" + <suite>, where <suite> is one of the platform
#  targets in offload-test-suite/test/CMakeLists.txt: [clang-][warp-]<api>.)
#
# The SAME test file (e.g. `Basic/simple.test`) is run under every platform's
# suite, so `OffloadTest-vk :: Basic/simple.test` and
# `OffloadTest-warp-d3d12 :: Basic/simple.test` are the same test, differing only
# in the workflow/runner. To compare a test across workflows we must key on its
# platform-independent identity: the base suite (platform suffix stripped) + the
# test path. The platform tokens the suffix carried (vk / d3d12 / warp / ...)
# are already recovered as pivot axes from the workflow display name
# (parse_workflow_axes), so dropping them from the key loses no information.
_SUITE_PLATFORM_SUFFIX_RE = re.compile(
    r"-(?:clang-)?(?:warp-)?(?:vk|vulkan|d3d12|directx|mtl|metal)$", re.I
)


def normalize_suite(suite: str) -> str:
    """
    Strip the platform-specific suffix from a lit suite name so the same test
    file compares as one identity across workflows. `OffloadTest-clang-warp-d3d12`
    and `OffloadTest-vk` both normalise to `OffloadTest`. Suite names without a
    recognised platform suffix (e.g. `OffloadTest-Unit`) are returned unchanged.
    """
    return _SUITE_PLATFORM_SUFFIX_RE.sub("", suite)


def normalize_test_key(suite: str, test: str) -> tuple[str, str]:
    """Platform-independent (base_suite, test_path) identity for a lit test."""
    return (normalize_suite(suite), test)


def extract_all_results(log_text: str) -> dict[tuple[str, str], str]:
    """
    Scan the entire log for lit status lines and return a
    {(suite, test): result} map (last-wins if a test appears more than once,
    which shouldn't happen in a single run).
    """
    results: dict[tuple[str, str], str] = {}
    for raw in log_text.splitlines():
        m = LIT_STATUS_LINE_RE.match(strip_ts(raw))
        if m:
            results[(m.group("suite"), m.group("test"))] = m.group("result")
    return results


# Each scheduled run checks out the compiler repos it builds; the checkout logs
# them as `Syncing repository: <owner>/<repo>` followed by `HEAD is now at <sha>`.
# Recording these in the summary means downstream triage never has to re-open the
# logs just to learn which llvm-project / DXC commit produced a failure.
_REPO_DECL_RE = re.compile(r"(?:Syncing repository|repository):\s*(\S+/\S+)\s*$")
_HEAD_AT_RE = re.compile(r"HEAD is now at ([0-9a-f]{7,40})\b")


def extract_built_commits(log_text: str) -> dict[str, str]:
    """
    Map repo-dir-key -> built commit sha (e.g. {'llvm-project': 'f60650c77',
    'directxshadercompiler': 'dc3e6c48', 'offload-test-suite': 'bda4d3e'}), by
    pairing each repository declaration with the next `HEAD is now at <sha>`.
    First sha wins per repo (fetch + checkout log it twice, identically).
    """
    commits: dict[str, str] = {}
    pending: str | None = None
    for raw in log_text.splitlines():
        line = strip_ts(raw)
        m = _REPO_DECL_RE.search(line)
        if m:
            pending = m.group(1).split("/")[-1].lower()
            continue
        h = _HEAD_AT_RE.search(line)
        if h and pending:
            commits.setdefault(pending, h.group(1))
            pending = None
    return commits


# Workflow names encode (host, API, GPU vendor, compiler, variant) in their
# display name, e.g.:
#   "Windows Vulkan AMD Clang"           -> host=x64, api=Vulkan, gpu=AMD
#   "Windows ARM64 D3D12 Warp DXC"       -> host=ARM64
#   "Windows D3D12 AMD Clang GBV"        -> variant=GBV
#   "Windows D3D12 Warp Preview Clang"   -> variant=Preview
#   "macOS Metal DXC"                    -> host=macOS
_API_TOKENS = ("D3D12", "Vulkan", "Metal")
_GPU_TOKENS = ("AMD", "NVIDIA", "Intel", "QC", "Warp", "Lavapipe", "Metal")
_VARIANT_TOKENS = ("GBV", "Preview")


def parse_workflow_axes(name: str) -> dict[str, str]:
    """
    Best-effort split of a workflow's display name into axes we can pivot on.
    Returns keys 'api', 'gpu', 'compiler', 'host', 'variant' (each 'none' or
    'unknown' if missing).
    """
    def has(token: str, ci: bool = True) -> bool:
        """True if `token` appears in the name as a whole word."""
        return re.search(rf"\b{token}\b", name, re.I if ci else 0) is not None

    # Lavapipe is a software Vulkan renderer, not an API and not vendor
    # hardware. When it appears it *is* the device ("GPU") and its API is
    # Vulkan (lit sees the llvmpipe device -> API=Vulkan, feature `Lavapipe`).
    # Any vendor token in the name (e.g. "AMD") is just the physical builder
    # host the software renderer runs on, not the device under test.
    if has("Lavapipe"):
        api = "Vulkan"
        gpu = "Lavapipe"
    else:
        api = next((t for t in _API_TOKENS if has(t)), "unknown")
        gpu = next((t for t in _GPU_TOKENS if has(t) and t != api), "unknown")
        if gpu == "unknown" and api == "Metal":
            # Metal carries no separate vendor token; bucket the API as its
            # own "GPU" for divergence purposes.
            gpu = api

    # Clang/DXC/QC have fixed casing in real names, so they match
    # case-sensitively; the remaining tokens are matched case-insensitively.
    if has("Clang", ci=False):
        compiler = "clang"
    elif has("DXC", ci=False):
        compiler = "dxc"
    else:
        compiler = "unknown"

    if has("ARM64"):
        host = "ARM64"
    elif has("macOS"):
        host = "macOS"
    elif has("QC", ci=False):
        # Qualcomm boards (Snapdragon X Plus) are ARM64-only; the display name
        # doesn't carry an ARM64 token, but the host always is (RUNNER_ARCH=ARM64).
        # Keyed on the QC *name* token, not the gpu axis, so a Lavapipe run on a
        # Qualcomm board (gpu=Lavapipe) is still recognised as ARM64.
        host = "ARM64"
    elif has("Windows"):
        host = "x64"
    else:
        host = "unknown"

    variant = next((t for t in _VARIANT_TOKENS if has(t)), "none")
    return {"api": api, "gpu": gpu, "compiler": compiler, "host": host, "variant": variant}


def compact_workflow(name: str) -> str:
    """
    Short, unambiguous slug for a workflow display name, for dense table cells.
    Builds `<gpu>/<api>/<compiler>[/<host>][/<variant>]` from the parsed axes,
    dropping unknown / duplicate tokens (Metal reports gpu==api, so it collapses
    to one). Host is shown only when it's notable (ARM64 / macOS); x64 is the
    common default and omitted. Examples:
      "Windows Vulkan AMD DXC"        -> "AMD/Vulkan/DXC"
      "macOS Metal DXC"               -> "Metal/DXC/macOS"
      "Windows ARM64 D3D12 Warp DXC"  -> "Warp/D3D12/DXC/ARM64"
      "Windows D3D12 AMD Clang GBV"   -> "AMD/D3D12/Clang/GBV"
    Falls back to the raw name if nothing parses.
    """
    ax = parse_workflow_axes(name)
    parts: list[str] = []

    def add(v: str) -> None:
        if v and v not in ("unknown", "none") and v not in parts:
            parts.append(v)

    add(ax["gpu"])
    add(ax["api"])
    add({"clang": "Clang", "dxc": "DXC"}.get(ax["compiler"], ""))
    if ax["host"] in ("ARM64", "macOS"):
        add(ax["host"])
    add(ax["variant"])
    return "/".join(parts) or name


def _fail_mode(cls: str) -> str:
    """Short failure mode from a (possibly upgraded) classification: the
    miscompile / crash / unknown suffix, else the label itself."""
    for m in ("miscompile", "crash", "unknown"):
        if cls.endswith("_" + m):
            return m
    return cls


def _md_wf_list(names: list[str], annotate: dict[str, str] | None = None) -> str:
    """Comma-join full workflow names, prefixed with the count (markdown cells).
    Full names (not compact slugs) so they're easy to match against the workflow
    table and the section headers. When `annotate` is given, each name is
    suffixed with its per-workflow value, e.g. `Windows Lavapipe AMD DXC
    (crash)`."""
    if not names:
        return "-"
    def one(n: str) -> str:
        return f"{n} ({annotate[n]})" if annotate and n in annotate else n
    return f"{len(names)}: " + ", ".join(one(n) for n in names)


def _md_pass_groups(passes_on: list[str], axes: dict) -> str:
    """Passing workflows for a markdown cell, grouped by the divergence's primary
    axis value when there is one (e.g. `api D3D12: 2: A, B`), else a flat list."""
    if not passes_on:
        return "-"
    groups = group_passes_by_axis(passes_on, axes)
    if groups is None:
        return _md_wf_list(passes_on)
    dim = primary_axis_dim(axes)
    return "<br>".join(f"**{dim} {value}** — {_md_wf_list(wfs)}" for value, wfs in groups)


def _truncate(text: str, limit: int = 60) -> str:
    """Collapse whitespace and clip to `limit` chars (for table cells)."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


# --- Linked-issue rendering shared by the markdown and HTML reports ----------

def _issue_records(t: dict) -> list[dict]:
    """Every linked-issue dict for a test entry, primary first, de-duped by url."""
    out: list[dict] = []
    seen: set[str] = set()
    li = t.get("linked_issue")
    for rec in ([li] if li else []) + (t.get("linked_issues") or []):
        u = rec.get("url")
        if u and u not in seen:
            seen.add(u)
            out.append(rec)
    return out


def _issue_number(url: str) -> str:
    m = ISSUE_URL_RE.search(url or "")
    return f"#{m.group('num')}" if m else "issue"


def _issue_emoji(state: str | None, state_reason: str | None = None) -> str:
    """Compact status glyph: open / completed-closed / not-planned-closed."""
    s = (state or "").lower()
    if s == "open":
        return "\U0001F7E2"          # green circle
    if s == "closed":
        # completed (fixed) vs not_planned (won't-fix) get different glyphs.
        return "\U0001F7E3" if (state_reason or "").lower() == "completed" else "\u26AB"
    return "\u26AA"                  # white circle: unknown / unfetched


def attribute_divergence(fails_on: list[str], passes_on: list[str]) -> dict:
    """
    Given the workflow-name lists, figure out whether the failure pattern
    lines up cleanly with one axis (api / gpu / compiler / host / variant).

    Returns dict with any of {api_pattern, gpu_pattern, compiler_pattern,
    host_pattern, variant_pattern}, each value being e.g. 'Vulkan-only' /
    'AMD-only' / 'GBV-only' when the failure set is exactly one axis value and
    passes cover >=1 other. 'unknown' and (for variant) 'none' are excluded
    from the value set so they don't dilute a pattern.

    The compiler axis is held to a stricter bar. GPU/API/host/variant describe
    *runtime* behaviour, which legitimately varies per configuration (a driver
    bug can be Vulkan-only on NVIDIA yet fine on that same NVIDIA card under
    D3D12), so partial passes within the failing value are expected. The
    compiler, by contrast, is config-independent: one compiler build emits the
    same DXIL regardless of which GPU/API later runs it, so if any workflow
    using the suspected compiler *passes*, the compiler can't be the
    differentiator (the real cause is a GPU/API/driver the failing set didn't
    cleanly isolate, or a per-workflow toolchain version skew). In that case we
    do NOT emit `compiler_pattern`, so a `clang-only` axis never appears while
    clang is demonstrably passing elsewhere.

    Note: even when `compiler_pattern` *is* emitted (a clean split — every clang
    workflow failed, DXC passes), it is reported only as a factual correlation.
    It is never upgraded to a fault-implying classification (see
    `divergence_suspect_prefix`): clang-dxc and DXC emit different DXIL, so a
    compiler split is different-output, not a proven compiler bug. Genuine
    compiler regressions are confirmed by triage_report.py's commit bounding.
    """
    fa = [parse_workflow_axes(w) for w in fails_on]
    pa = [parse_workflow_axes(w) for w in passes_on]
    # 'unknown' means we couldn't parse the axis — never pattern-worthy on
    # either side. 'none' means "no variant applied" — it's not a real value
    # to build a pattern from (we don't want "none-only") but it IS a valid
    # passing state that establishes the pattern (e.g. failers all have
    # variant=GBV, passers all have variant=none -> GBV-only).
    out: dict[str, str] = {}
    for axis in ("api", "gpu", "compiler", "host", "variant"):
        drop_from_fails = {"unknown", "none"} if axis == "variant" else {"unknown"}
        fvals = {x[axis] for x in fa} - drop_from_fails
        pvals = {x[axis] for x in pa} - {"unknown"}
        if len(fvals) != 1 or not (pvals - fvals):
            continue
        # Compiler-only: require a clean partition — the failing compiler must
        # not also appear among the passers (see docstring). Any passing
        # workflow of that compiler means the compiler isn't the differentiator.
        if axis == "compiler" and (fvals & pvals):
            continue
        out[f"{axis}_pattern"] = f"{next(iter(fvals))}-only"
    return out


# Priority for choosing which single axis to group the passing workflows on in
# the test-failure summary: the most specific runtime layer first.
_AXIS_PRIORITY = ("gpu", "api", "compiler", "host", "variant")


def primary_axis_dim(axes: dict) -> str | None:
    """The dimension (gpu/api/…) to summarise a divergence on, or None. When a
    failing set aligns on several axes at once, the most specific one wins."""
    present = {k[:-len("_pattern")] for k in axes if k.endswith("_pattern")}
    return next((d for d in _AXIS_PRIORITY if d in present), None)


def group_passes_by_axis(passes_on: list[str], axes: dict) -> list[tuple[str, list[str]]] | None:
    """Group the passing workflows by their value on the divergence's primary
    axis, so the pass side reads as a direct contrast to the (homogeneous)
    failing value on that same axis — e.g. fails on `api: Vulkan-only`, passes
    grouped by api into D3D12 / Metal. Returns an ordered
    [(axis_value, [workflows])] list (largest group first), or None when there
    is no single axis to group on (the caller then lists all passes flat).
    """
    dim = primary_axis_dim(axes)
    if dim is None or not passes_on:
        return None
    groups: dict[str, list[str]] = {}
    for w in passes_on:
        groups.setdefault(parse_workflow_axes(w).get(dim) or "unknown", []).append(w)
    return [(v, sorted(ws))
            for v, ws in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))]


def test_failure_rows(entry: dict) -> list[dict]:
    """Split one cross-workflow failure entry into per-(test, classification)
    display rows. Failing workflows are grouped by their assigned
    classification; the aggregate axis and passing set are shared by every row
    of the test (the axis is attributed over the whole failing set, which is
    more meaningful than re-deriving it from a one-workflow subset). Returns
    rows as {classification, fails_on} sorted by classification.
    """
    by_cls: dict[str, list[str]] = {}
    for w, cls in (entry.get("fail_classifications") or {}).items():
        by_cls.setdefault(cls or "unknown", []).append(w)
    if not by_cls:  # defensive: fall back to the aggregate view
        for c in entry.get("classifications") or ["unknown"]:
            by_cls.setdefault(c, list(entry.get("fails_on") or []))
    return [{"classification": c, "fails_on": sorted(ws)} for c, ws in sorted(by_cls.items())]


# ---------------------------------------------------------------------------
# XPASS: look up linked GitHub issue in the local test file
#
# offload-test-suite convention: each XFAIL line is preceded by a comment
# carrying the linked bug URL. The XFAIL body is a lit feature-set expression:
#
#     # Bug https://github.com/llvm/llvm-project/issues/156775
#     # XFAIL: Vulkan && Clang
#     # Bug https://github.com/llvm/offload-test-suite/issues/525
#     # XFAIL: NV && Clang && DirectX
#
# When a test XPASSes on a particular workflow we want the issue tied to the
# XFAIL clause that actually matched that workflow's lit feature set, not
# just "the first URL in the file".
# ---------------------------------------------------------------------------


ISSUE_URL_RE = re.compile(r"https?://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/issues/(?P<num>\d+)")
XFAIL_LINE_RE = re.compile(r"^\s*#?\s*XFAIL:\s*(?P<expr>.+?)\s*$")

# Other lit directive lines that mark the boundary of a *different* clause's
# comment block. When walking backward from an XFAIL to find its linked
# issue(s) we must stop here, otherwise a description belonging to the
# preceding clause (or a bare RUN/UNSUPPORTED line) would bleed into this one.
# Matched case-sensitively: real lit directives are uppercase, so prose like
# `# Unsupported: <url>` or `# Requires review` reads as description, not a
# boundary.
LIT_DIRECTIVE_RE = re.compile(
    r"^\s*#?\s*(?:RUN|XFAIL|UNSUPPORTED|REQUIRES|ALLOW_RETRIES|DEFINE|REDEFINE|COM):"
)

# How far back (in comment lines) to scan for a clause's linked issue, and how
# many blank lines we tolerate inside the comment block before giving up.
_XFAIL_MAX_LOOKBACK = 15
_XFAIL_MAX_BLANK_RUN = 2


def parse_xfail_clauses(text: str) -> list[dict]:
    """
    Return one dict per XFAIL clause:
        {expr, issue_url|None, issue_urls, line_no}

    ``issue_urls`` is the list of every distinct GitHub issue URL linked in the
    clause's preceding comment block, in source order (top-to-bottom).
    ``issue_url`` is kept for backward compatibility and is the *nearest*
    preceding URL (the one closest to the XFAIL line) or None.

    The comment block is the run of ``#`` comment lines immediately above the
    XFAIL line. The backward walk:
      * tolerates up to ``_XFAIL_MAX_BLANK_RUN`` consecutive blank lines, so a
        multi-paragraph rationale split by a blank line is still searched;
      * stops at a non-comment (code) line;
      * stops at another lit directive line (XFAIL/UNSUPPORTED/RUN/...), which
        marks the boundary of a neighbouring clause's comment block;
      * scans at most ``_XFAIL_MAX_LOOKBACK`` comment lines back.
    This collects issue links from multi-line descriptions and multiple bugs
    per clause, without stealing links that belong to an adjacent clause.
    """
    lines = text.splitlines()
    out: list[dict] = []
    for i, line in enumerate(lines):
        m = XFAIL_LINE_RE.match(line)
        if not m:
            continue
        expr = m.group("expr").strip()
        # Walk backward through the comment block, building the issue list in
        # source order (top-to-bottom). Each line's own URLs are already
        # left-to-right; since we walk upward we prepend each line's list.
        issue_urls: list[str] = []
        seen: set[str] = set()
        nearest_url: str | None = None
        blank_run = 0
        comment_lines_seen = 0
        j = i - 1
        while j >= 0:
            prev = lines[j].rstrip()
            stripped = prev.strip()
            if not stripped:
                blank_run += 1
                if blank_run > _XFAIL_MAX_BLANK_RUN:
                    break
                j -= 1
                continue
            if not stripped.startswith("#"):
                break
            # Boundary: reached the previous clause's own directive line.
            if LIT_DIRECTIVE_RE.match(prev):
                break
            blank_run = 0
            comment_lines_seen += 1
            if comment_lines_seen > _XFAIL_MAX_LOOKBACK:
                break
            line_urls = [u.group(0) for u in ISSUE_URL_RE.finditer(prev)]
            if line_urls and nearest_url is None:
                # Nearest = first (leftmost) URL on the closest linking line,
                # matching the historical single-URL behaviour.
                nearest_url = line_urls[0]
            # Prepend this (higher) line's URLs, de-duplicating.
            for url in reversed(line_urls):
                if url not in seen:
                    seen.add(url)
                    issue_urls.insert(0, url)
            j -= 1
        out.append({
            "expr": expr,
            "issue_url": nearest_url,  # nearest linking line, leftmost (back-compat)
            "issue_urls": issue_urls,
            "line_no": i + 1,
        })
    return out


# Boolean lit XFAIL expression parser + evaluator.
# Grammar (recursive descent): or := and ('||' and)* ; and := unary ('&&' unary)* ;
# unary := '!' unary | '(' or ')' | IDENT ;  IDENT: [A-Za-z0-9_.-]+  ('*' = wildcard).
_XFAIL_TOKEN_RE = re.compile(r"\s*(\|\||&&|!|\(|\)|[\w.-]+)")


def _tokenize_xfail(expr: str) -> list[str]:
    toks, i = [], 0
    while i < len(expr):
        m = _XFAIL_TOKEN_RE.match(expr, i)
        if not m:
            raise ValueError(f"bad XFAIL expr near {expr[i:]!r}")
        toks.append(m.group(1))
        i = m.end()
    return toks


def _eval_xfail(expr: str, features: set[str]) -> bool:
    """
    Evaluate a lit XFAIL boolean expression against a feature set.
    Bare identifiers evaluate to True iff the feature is present.
    Wildcards are not supported here — lit's `*` (match anything) shows up
    as `XFAIL: *` and always fires; we handle that specially in the caller.
    """
    tokens = _tokenize_xfail(expr)
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def consume(t):
        nonlocal pos
        if peek() != t:
            raise ValueError(f"expected {t!r} at token {pos}: {tokens}")
        pos += 1

    def parse_or():
        left = parse_and()
        while peek() == "||":
            consume("||")
            left = left | parse_and()
        return left

    def parse_and():
        left = parse_unary()
        while peek() == "&&":
            consume("&&")
            left = left & parse_unary()
        return left

    def parse_unary():
        nonlocal pos
        if peek() == "!":
            consume("!")
            return not parse_unary()
        if peek() == "(":
            consume("(")
            v = parse_or()
            consume(")")
            return v
        ident = tokens[pos]
        pos += 1
        return ident in features

    return parse_or()


def workflow_features(name: str, runner_name: str | None = None) -> set[str]:
    """
    Best-effort translation of a workflow display name into the lit
    `available_features` set that would be visible when evaluating XFAIL
    clauses on that runner.

    Feature sources (merged):
      1. Name-token features — API / compiler / GPU / host, derivable from
         the display string alone.
      2. Runner-hardware features — CPU/GPU-model-derived (AVX512,
         Intel-Gen-*, AppleM<N>), read from the runners.json table keyed on
         the physical machine (HLSLPC-*). When `runner_name` is provided
         (parsed from the workflow log's `Runner name:` line, the *actual*
         machine the test job ran on) we key on it directly; this is
         authoritative even when the test runner differs from the build runner
         in SplitBuild workflows. When `runner_name` is None (e.g. we haven't
         fetched the log yet) we resolve the workflow's (gpu, host) axes to the
         machine that runs them via `_BUILDER_HOSTS`. Software renderers
         (WARP / Lavapipe) get the host's CPU features but not its GPU ones.
    """
    ax = parse_workflow_axes(name)
    feats: set[str] = set()
    # API — lit uses "DirectX" for D3D12; Vulkan/Metal keep their names.
    # (A Lavapipe workflow already reports api=Vulkan since llvmpipe is a
    # software Vulkan device.)
    api_map = {"D3D12": "DirectX", "Vulkan": "Vulkan", "Metal": "Metal"}
    if ax["api"] in api_map:
        feats.add(api_map[ax["api"]])
    # Compiler token in test files is "Clang"/"DXC" (capitalised).
    if ax["compiler"] == "clang":
        feats.add("Clang")
        # Some tests key on the composite "Clang-Vulkan" feature.
        if api_map.get(ax["api"]) == "Vulkan":
            feats.add("Clang-Vulkan")
    elif ax["compiler"] == "dxc":
        feats.add("DXC")
    # GPU vendor / device. lit derives these from the selected device's
    # description: NVIDIA -> NV, Warp -> WARP, and "Lavapipe" for the llvmpipe
    # software renderer (no vendor-hardware token is added for Lavapipe).
    if ax["gpu"] in ("AMD", "NVIDIA", "Intel", "QC", "Warp", "Lavapipe"):
        feats.add({"NVIDIA": "NV", "Warp": "WARP"}.get(ax["gpu"], ax["gpu"]))
    # Host / OS
    if ax["host"] == "macOS":
        feats.add("Darwin")
    elif ax["host"] in ("x64", "ARM64"):
        feats.add("Windows")
        feats.add(ax["host"])  # x64 / ARM64 also appear as arch features
    # Hardware features, keyed on the physical runner (HLSLPC-*). Prefer the
    # actual test runner from the log; otherwise resolve the workflow's axes to
    # the machine that runs them. The device (gpu axis) selects that device's own
    # features — e.g. WARP / Lavapipe on a box carry none of its discrete GPU's.
    host = runner_name if runner_name in _RUNNER_CPU_FEATURES else \
        _BUILDER_HOSTS.get((ax["gpu"], ax["host"]))
    if host:
        feats |= _host_hw_features(host, ax["gpu"])
    return feats


# Hardware-derived lit features live in an external, human-editable data file
# (runners.json) so adding a runner or updating hardware needs no code change.
# Features belong to physical machines (HLSLPC-*), never to GPU/API axes:
#   _RUNNER_CPU_FEATURES[host]  - apply to every job on the machine (incl. WARP/
#                                 Lavapipe software renderers).
#   _RUNNER_GPU_FEATURES[host]  - apply only when the device under test is the
#                                 machine's own GPU (excluded for software
#                                 renderers).
#   _BUILDER_HOSTS[(gpu, host)] - which machine a workflow's axes run on, used
#                                 to pick the host when a log's runner name
#                                 isn't available yet.
_RUNNERS_FILE = pathlib.Path(__file__).with_name("runners.json")


def _infer_host_arch(cpu: str, explicit: str | None) -> str:
    """
    The workflow `host` axis for a machine. Explicit wins; otherwise inferred
    from the CPU description: Apple Silicon -> macOS, Arm parts -> ARM64, else
    x64. (The host arch is a property of the machine's CPU, so runners.json
    doesn't repeat it per device.)
    """
    if explicit:
        return explicit
    c = (cpu or "").lower()
    if "apple" in c:
        return "macOS"
    if any(t in c for t in ("snapdragon", "arm64", "aarch64", "ampere")):
        return "ARM64"
    return "x64"


def _load_runner_table(path: pathlib.Path):
    """Parse runners.json into (cpu_features, gpu_features, builder_hosts).

    cpu_features is keyed by machine (HLSLPC-*); gpu_features is keyed by
    (machine, gpu-token) so each device on a machine — its physical GPU and any
    software renderer — carries its own features. builder_hosts maps a workflow's
    (gpu, host) axes to the machine that runs it, pairing each machine's `gpus`
    with its CPU-inferred host arch.
    """
    cpu: dict[str, set[str]] = {}
    gpu: dict[tuple[str, str], set[str]] = {}
    builder_hosts: dict[tuple[str, str], str] = {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        print(f"warning: could not load {path.name} ({e}); "
              "hardware feature inference disabled", file=sys.stderr)
        return cpu, gpu, builder_hosts
    for host, spec in (data.get("runners") or {}).items():
        cpu[host] = set(spec.get("cpu_features") or [])
        arch = _infer_host_arch(spec.get("cpu", ""), spec.get("host"))
        gpus = spec.get("gpus") or {}
        # Accept a bare list (features unknown -> none) as well as the map form.
        if isinstance(gpus, list):
            gpus = {g: [] for g in gpus}
        for g, gfeats in gpus.items():
            gpu[(host, g)] = set(gfeats or [])
            builder_hosts[(g, arch)] = host
    return cpu, gpu, builder_hosts


_RUNNER_CPU_FEATURES, _RUNNER_GPU_FEATURES, _BUILDER_HOSTS = _load_runner_table(_RUNNERS_FILE)


def _host_hw_features(host: str, gpu: str) -> set[str]:
    """
    Hardware lit features for testing device `gpu` on machine `host`: the
    machine's CPU features plus that specific device's own GPU features (so a
    WARP / Lavapipe run gets the CPU features but not the discrete GPU's).
    """
    feats = set(_RUNNER_CPU_FEATURES.get(host, set()))
    feats |= _RUNNER_GPU_FEATURES.get((host, gpu), set())
    return feats


# Runner-name parser. GitHub Actions prints `Runner name: 'HLSLPC-AMD01'`
# once per job. In SplitBuild workflows the build job's line appears before
# the test job's line in the combined log, so the last occurrence is the
# test runner — which is what XFAIL evaluation needs.
_RUNNER_NAME_RE = re.compile(r"Runner name:\s*'([^']+)'")


def extract_runner_names(log_text: str) -> dict[str, str | None]:
    """
    Return {'build': <name or None>, 'test': <name or None>}.

    In SplitBuild workflows the archive contains two per-job logs whose
    section headers include the job name (e.g. `===== 0_Windows-D3D12-AMD-DXC
    _ test.txt =====` and `===== 1_..._build.txt =====`). We scan for
    `Runner name:` lines and attribute each to the enclosing section — the
    section header carrying `test` vs `build` in its filename decides
    which is which.

    For non-split workflows (e.g. macOS, which has a single combined job)
    there's only one `Runner name:`; we report it as 'test' since that's
    the machine lit ran on.
    """
    # Find each Runner-name occurrence, then look backward for the nearest
    # section header of the form `===== <idx>_<jobname> _ <role>.txt =====`.
    result: dict[str, str | None] = {"build": None, "test": None}
    for m in _RUNNER_NAME_RE.finditer(log_text):
        header = _find_preceding_header(log_text, m.start())
        role = _role_from_header(header)  # 'build', 'test', or None
        name = m.group(1)
        if role is None:
            # Ambiguous: no header, put it in 'test' as the safer default
            # (single-runner logs).
            if result["test"] is None:
                result["test"] = name
            continue
        if result[role] is None:
            result[role] = name
    # Non-SplitBuild workflows (macOS) run a single 'build' job that also
    # runs the tests. The runner that hosted 'build' is therefore the test
    # runner too — fill in 'test' if it's unset.
    if result["test"] is None and result["build"] is not None:
        result["test"] = result["build"]
    return result


_SECTION_HEADER_RE = re.compile(r"^=====\s+(.+?)\s+=====\s*$", re.M)
_HEADER_ROLE_RE = re.compile(r"\b(test|build)\.txt\b", re.I)


def _find_preceding_header(text: str, pos: int) -> str | None:
    header = None
    for m in _SECTION_HEADER_RE.finditer(text, 0, pos):
        header = m.group(1)
    return header


def _role_from_header(header: str | None) -> str | None:
    if not header:
        return None
    m = _HEADER_ROLE_RE.search(header)
    return m.group(1).lower() if m else None


def match_xpass_to_issue(
    text: str,
    workflow_name: str,
    runner_name: str | None = None,
) -> dict:
    """
    Return {matched_expr, issue_url, all_clauses, features, note}.

    Picks the XFAIL clause whose feature expression evaluates true against
    the workflow's inferable feature set. If evaluation of a clause depends
    on features we can't infer from the workflow name alone, we mark that
    clause as 'inconclusive' and only pick a match if exactly one clause
    is definitively true.

    Pass `runner_name` (from `extract_runner_names(log_text)['test']`) to
    key hardware features on the actual test-runner hostname rather than
    guessing from the workflow name — necessary when the test job runs on
    a different machine than the workflow name would suggest.
    """
    clauses = parse_xfail_clauses(text)
    features = workflow_features(workflow_name, runner_name)
    # Feature vocabulary this runner can definitively answer for. A clause
    # whose identifiers all sit in this set is decidable; a clause with
    # identifiers outside it may reference runtime-only state we can't infer
    # (SM_6_N caps, Intel-Memory-Coherence-Issue-226, specific driver names,
    # Wave-lane-size features, etc.) and is treated as inconclusive when it
    # would otherwise evaluate False.
    INFERABLE = features | {
        "DirectX", "Vulkan", "Metal",
        "Clang", "DXC", "Clang-Vulkan",
        "NV", "AMD", "Intel", "QC", "WARP",
        "Darwin", "Windows", "x64", "x86", "ARM64",
        # Hardware-derived features we CAN answer for (present on some
        # builders, absent on others — both cases decidable now that we have
        # the builder table).
        "AVX512", "Intel-Gen-Current", "Intel-Gen-10",
        "AppleM1", "AppleM2", "AppleM3", "AppleM4", "AppleM5",
    }

    def clause_result(expr: str) -> tuple[bool | None, list[str]]:
        # A bare '*' clause always fires.
        if expr.strip() == "*":
            return True, []
        try:
            toks = _tokenize_xfail(expr)
        except ValueError:
            return None, [f"parse error: {expr!r}"]
        idents = [t for t in toks if re.match(r"[\w.-]+$", t) and t not in ("&&", "||", "!")]
        unknown = [t for t in idents if t not in INFERABLE]
        if unknown:
            # If every unknown feature is definitely-absent from the workflow
            # (we know x64 workflow lacks ARM64 etc.), still try — we assume
            # unknown identifiers evaluate False (safe over-approximation for
            # detecting definite-True clauses).
            try:
                val = _eval_xfail(expr, features)
                return (val if val else None), unknown  # True is trusted; False becomes inconclusive
            except ValueError as e:
                return None, [str(e)]
        try:
            return _eval_xfail(expr, features), []
        except ValueError as e:
            return None, [str(e)]

    matched: list[dict] = []
    inconclusive: list[dict] = []
    for c in clauses:
        v, unknown = clause_result(c["expr"])
        entry = {**c, "unknown_features": unknown}
        if v is True:
            matched.append(entry)
        elif v is None:
            inconclusive.append(entry)

    result: dict = {
        "features": sorted(features),
        "all_clauses": clauses,
        "matched": matched,
        "inconclusive": inconclusive,
        "note": None,
    }
    if len(matched) == 1:
        result["issue_url"] = matched[0]["issue_url"]
        result["issue_urls"] = matched[0].get("issue_urls") or []
        result["matched_expr"] = matched[0]["expr"]
    elif len(matched) > 1:
        # Multiple XFAIL clauses legitimately fire for this runner (e.g. a
        # Vulkan+Clang+AMD run matches both `Clang && Vulkan` and `AMD`). An
        # XPASS means every one of those expected-failure conditions passed, so
        # each linked bug is a candidate. If exactly one matched clause carries
        # an issue the attribution is unambiguous; otherwise report ALL matched
        # clauses' issues (with their statuses) rather than dropping them.
        with_url = [c for c in matched if c["issue_url"]]
        if len(with_url) == 1:
            result["issue_url"] = with_url[0]["issue_url"]
            result["issue_urls"] = with_url[0].get("issue_urls") or []
            result["matched_expr"] = with_url[0]["expr"]
            result["note"] = f"{len(matched)} XFAIL clauses matched; used the only one with a linked issue"
        elif with_url:
            # Aggregate every matched clause's issues, in source order, de-duped.
            all_urls: list[str] = []
            seen: set[str] = set()
            for c in with_url:
                for u in (c.get("issue_urls") or [c["issue_url"]]):
                    if u and u not in seen:
                        seen.add(u)
                        all_urls.append(u)
            result["issue_url"] = all_urls[0]
            result["issue_urls"] = all_urls
            result["matched_expr"] = "; ".join(c["expr"] for c in with_url)
            result["ambiguous"] = True
            result["note"] = (
                f"ambiguous — {len(with_url)} XFAIL clauses matched "
                f"({', '.join(c['expr'] for c in with_url)}); all linked issues reported"
            )
        else:
            result["note"] = f"{len(matched)} XFAIL clauses matched, none linked to an issue"
    else:
        # No definitively-matching clause.
        if inconclusive:
            result["note"] = (
                f"no clause matched with inferable features {sorted(features)}; "
                f"{len(inconclusive)} clause(s) reference runtime-only features"
            )
        else:
            result["note"] = "no XFAIL clause in the test file matches this workflow"
    return result


def find_test_file(repo_root: pathlib.Path, suite: str, relpath: str) -> pathlib.Path | None:
    # lit prints paths with forward slashes but the on-disk copy is a normal
    # tree under the repo root. suite doesn't map 1:1 to a directory in this
    # repo, so try a couple of candidates.
    candidates = [
        repo_root / relpath,
        repo_root / "test" / relpath,
        repo_root / "Test" / relpath,
    ]
    for c in candidates:
        if c.exists():
            return c
    # Fallback: search
    name = pathlib.Path(relpath).name
    for hit in repo_root.rglob(name):
        return hit
    return None

# --- Reading test files at the commit the CI run actually tested -------------
#
# The XPASS -> issue matcher parses XFAIL clauses out of the test file. If we
# read the file from the current working tree, clauses added or removed since
# the run executed corrupt the match (e.g. a `# XFAIL: Clang && Vulkan` clause
# added after the run makes a Vulkan+Clang XPASS look like it matches two
# clauses -> "ambiguous", when at run time only `# XFAIL: AMD` existed). Each
# scheduled run logs the offload-test-suite commit it built (extract_built_
# commits), so we read the file content as it existed at *that* commit via
# `git show <sha>:<path>`, without mutating the working tree. Falls back to the
# working tree when the commit isn't available locally (shallow clone).

# (repo_root, commit) -> bool: is the commit object present locally?
_GIT_COMMIT_AVAILABLE: dict[tuple[str, str], bool] = {}
# Repos we've already deep-fetched / unshallowed this run (do it at most once).
_GIT_DEEPENED: set[str] = set()

# How hard read_test_file_for_run may work to obtain a run's commit:
#   "targeted"  - best-effort `git fetch --depth 1 origin <sha>` (default)
#   "unshallow" - also unshallow / full-fetch the repo when the sha is still
#                 missing, so the exact tested revision is always available
#   "off"       - never fetch; read whatever the working tree has
_GIT_FETCH_MODE = "targeted"


def set_git_fetch_mode(mode: str) -> None:
    """Set the module-wide fetch policy ('off' | 'targeted' | 'unshallow')."""
    global _GIT_FETCH_MODE
    _GIT_FETCH_MODE = mode


def _git(repo_root: pathlib.Path, *args: str, timeout: int = 60):
    """Run a git command in repo_root; return CompletedProcess or None on error."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True, timeout=timeout,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None


def _git_resolves(repo_root: pathlib.Path, commit: str) -> bool:
    r = _git(repo_root, "cat-file", "-e", f"{commit}^{{commit}}")
    return r is not None and r.returncode == 0


def _git_is_shallow(repo_root: pathlib.Path) -> bool:
    r = _git(repo_root, "rev-parse", "--is-shallow-repository")
    return r is not None and r.returncode == 0 and r.stdout.decode().strip() == "true"


def _git_deepen(repo_root: pathlib.Path) -> None:
    """
    Fetch full history once per repo: `--unshallow` for a shallow clone, else a
    plain fetch to pull commits not yet present. Cached so it runs at most once.
    """
    key = str(repo_root)
    if key in _GIT_DEEPENED:
        return
    _GIT_DEEPENED.add(key)
    if _git_is_shallow(repo_root):
        print(f"  unshallowing {repo_root.name} to obtain tested commits\u2026", file=sys.stderr)
        _git(repo_root, "fetch", "--quiet", "--unshallow", "--tags", timeout=900)
    else:
        print(f"  fetching {repo_root.name} history\u2026", file=sys.stderr)
        _git(repo_root, "fetch", "--quiet", "--tags", timeout=900)


def git_commit_available(repo_root: pathlib.Path, commit: str) -> bool:
    """
    True if `commit` is a resolvable commit object in repo_root. When it's
    missing, the fetch policy (set_git_fetch_mode) decides how hard to try:
    a targeted by-sha fetch, and — in 'unshallow' mode — a full unshallow/fetch
    of the repo. Results are cached per (repo, commit).
    """
    key = (str(repo_root), commit)
    if key in _GIT_COMMIT_AVAILABLE:
        return _GIT_COMMIT_AVAILABLE[key]

    def _finish(ok: bool) -> bool:
        _GIT_COMMIT_AVAILABLE[key] = ok
        return ok

    if _git_resolves(repo_root, commit):
        return _finish(True)
    if _GIT_FETCH_MODE == "off":
        return _finish(False)

    # A targeted by-sha fetch only works for a full 40-char sha the server
    # allows fetching (allowReachableSHA1InWant). Run logs record ABBREVIATED
    # SHAs (`HEAD is now at <short>`), which git can't fetch by ref — those can
    # only be resolved by having the history locally (deepen), so skip the
    # doomed fetch for them.
    if len(commit) >= 40:
        _git(repo_root, "fetch", "--quiet", "--depth", "1", "origin", commit, timeout=120)
        if _git_resolves(repo_root, commit):
            return _finish(True)

    # Last resort: pull full history, then re-check.
    if _GIT_FETCH_MODE == "unshallow":
        _git_deepen(repo_root)
        if _git_resolves(repo_root, commit):
            return _finish(True)
    return _finish(False)


def git_show_file(repo_root: pathlib.Path, commit: str, gitpath: str) -> str | None:
    """Return the text of `gitpath` at `commit`, or None if it can't be read."""
    r = _git(repo_root, "show", f"{commit}:{gitpath}")
    if r is not None and r.returncode == 0:
        return r.stdout.decode("utf-8", errors="replace")
    return None


def read_test_file_for_run(
    otss_root: pathlib.Path, suite: str, relpath: str, commit: str | None
) -> tuple[str | None, str | None, str | None]:
    """
    Return (text, rel_path, source) for a test file, preferring its content at the
    CI run's offload-test-suite `commit` so XFAIL clauses match what actually ran.
    `source` is the short commit sha when the git blob was used, or 'worktree'
    when we fell back to the checked-out file (its clauses may be newer/older than
    the run). Locates the on-disk path first to derive the repo-relative git path.
    """
    p = find_test_file(otss_root, suite, relpath)
    if p is None:
        return None, None, None
    rel = p.relative_to(otss_root)
    if commit and git_commit_available(otss_root, commit):
        text = git_show_file(otss_root, commit, rel.as_posix())
        if text is not None:
            return text, str(rel), commit[:9]
    try:
        return p.read_text(errors="replace"), str(rel), "worktree"
    except OSError:
        return None, str(rel), None

def fetch_issue_state(gh: GH, owner: str, repo: str, number: int) -> dict | None:
    try:
        return gh.get_json(f"/repos/{owner}/{repo}/issues/{number}")
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def fetch_workflows(gh: GH) -> list[dict]:
    return gh.get_json(f"/repos/{REPO}/actions/workflows", per_page=100)["workflows"]


def latest_scheduled_run(gh: GH, workflow_id: int) -> dict | None:
    """
    Latest *completed* scheduled run — filtered server-side with status=completed
    so we never pick up a queued/in_progress run (whose logs aren't ready and
    which would show up as blanks in the pass matrix, degrading the cross-
    workflow pivot). 'completed' still includes cancelled/timed_out; those
    remain classified as non-actionable rows without log downloads.
    """
    data = gh.get_json(
        f"/repos/{REPO}/actions/workflows/{workflow_id}/runs",
        event="schedule",
        status="completed",
        per_page=1,
    )
    runs = data.get("workflow_runs", [])
    return runs[0] if runs else None


def download_run_logs(gh: GH, run_id: int) -> bytes:
    # This endpoint 302s to a signed archive URL.
    url = f"{API}/repos/{REPO}/actions/runs/{run_id}/logs"
    return gh.get_bytes(url)


def combined_log_text(zip_bytes: bytes) -> str:
    """
    GitHub packs run logs as a zip with:
      <idx>_<jobname>.txt         - full per-job combined log (what we want)
      <jobname>/<step>.txt        - per-step logs (only 'system.txt' is here
                                    in the current archive format, which is
                                    just runner metadata)
    We concatenate the top-level per-job files. If a run only has subdir logs,
    fall back to those.
    """
    buf = io.BytesIO(zip_bytes)
    parts = []
    with zipfile.ZipFile(buf) as zf:
        names = zf.namelist()
        top_level = [n for n in names if "/" not in n and n.endswith(".txt")]
        if top_level:
            chosen = sorted(top_level)
        else:
            chosen = sorted(n for n in names if n.endswith(".txt"))
        for n in chosen:
            try:
                parts.append(f"\n===== {n} =====\n")
                parts.append(zf.read(n).decode("utf-8", errors="replace"))
            except KeyError:
                continue
    return "".join(parts)


def classify_run(log_text: str, gh: GH, otss_root: pathlib.Path, issue_cache: dict, workflow_name: str) -> dict:
    """
    Return a dict describing the run failure(s).
    """
    result: dict = {"category": None, "detail": None, "tests": []}

    # Parse runner names once — passed into XPASS matching so hardware features
    # come from the actual test-runner hostname (which can differ from the
    # build-runner in SplitBuild workflows).
    runners = extract_runner_names(log_text)
    result["runners"] = runners
    test_runner = runners.get("test")

    # No test-stage output at all -> the job failed during build/infra.
    if not has_test_stage_output(log_text):
        result["category"] = "build_failure"
        result["detail"] = classify_build_failure(log_text)
        return result

    # Otherwise we made it into testing. Extract failure blocks.
    blocks = extract_failure_blocks(log_text)
    if not blocks:
        # Test stage ran but no per-test FAIL block matched. Could be a build
        # failure that still emitted a partial testing marker, or infra flake.
        result["category"] = "test_failure"
        result["detail"] = "unknown_no_blocks"
        return result

    result["category"] = "test_failure"
    # Commit the run built — used to read each XFAIL test file as it existed then.
    otss_commit = extract_built_commits(log_text).get("offload-test-suite")
    for b in blocks:
        entry = {"result": b["result"], "suite": b["suite"], "test": b["test"]}
        if b["result"] == "XPASS":
            text, test_rel, src = read_test_file_for_run(
                otss_root, b["suite"], b["test"], otss_commit
            )
            if not text:
                entry["classification"] = "xpass"
                entry["note"] = (
                    "test file not found on disk" if test_rel is None
                    else "could not read test file"
                )
            else:
                entry["test_file"] = test_rel
                entry["test_file_source"] = src  # commit sha, or 'worktree' fallback
                match = match_xpass_to_issue(text, workflow_name, test_runner)
                entry["classification"] = "xpass"
                if match is None:
                    entry["note"] = "could not read test file"
                elif not match.get("issue_url"):
                    entry["xfail_match"] = {"note": match["note"], "matched_expr": match.get("matched_expr")}
                    entry["note"] = match["note"]
                else:
                    # Fetch state for every linked issue on the matched clause.
                    # `issue_url` (nearest) stays the primary for back-compat;
                    # `issue_urls` carries the full set in source order.
                    urls = match.get("issue_urls") or [match["issue_url"]]

                    def _fetch(url: str) -> dict:
                        u = ISSUE_URL_RE.search(url)
                        key = (u.group("owner"), u.group("repo"), int(u.group("num")))
                        if key not in issue_cache:
                            issue_cache[key] = fetch_issue_state(gh, *key)
                        st = issue_cache[key] or {}
                        return {
                            "url": url,
                            "state": st.get("state"),
                            "state_reason": st.get("state_reason"),
                            "title": st.get("title"),
                        }

                    linked = [_fetch(u) for u in urls]
                    primary = next(
                        (li for li in linked if li["url"] == match["issue_url"]),
                        linked[0],
                    )
                    entry["linked_issue"] = primary
                    if len(linked) > 1:
                        entry["linked_issues"] = linked
                    entry["xfail_match"] = {
                        "matched_expr": match["matched_expr"],
                        "note": match.get("note"),
                        "features": match["features"],
                    }
                    # Surface an ambiguous / multi-clause match in the summary
                    # note so it's visible next to the linked issue statuses.
                    if match.get("note"):
                        entry["note"] = match["note"]
                # If we couldn't read the file at the run's commit, the XFAIL
                # clauses may not reflect what actually ran — flag it.
                if src == "worktree" and otss_commit:
                    warn = (f"XFAIL read from working tree; run commit "
                            f"{otss_commit[:9]} unavailable, clauses may be stale")
                    entry["note"] = f"{entry['note']} • {warn}" if entry.get("note") else warn
        else:
            compile_kind = classify_shader_compile(b["block"])
            if compile_kind:
                entry["classification"] = compile_kind
            else:
                entry["classification"] = classify_runtime(b["block"])
        # Truncate block for report
        entry["excerpt"] = "\n".join(b["block"].splitlines()[:40])
        result["tests"].append(entry)
    return result


# Base runtime label -> suffix used when the cross-workflow pivot upgrades it.
# The prefix names the tightest aligning runtime layer: `runtime_driver_suspected`
# (per-vendor GPU split) or `api_backend_suspected` (clean API-axis split); see
# CLASSIFICATION_LEGEND. A compiler-axis split is reported as an axis but not
# upgraded (no fault prefix) — see `divergence_suspect_prefix`.
_RUNTIME_UPGRADE_SUFFIX = {
    "runtime_miscompile": "miscompile",
    "runtime_driver_error": "crash",
    "runtime_unknown": "unknown",
}


def divergence_suspect_prefix(axes: dict) -> str | None:
    """
    Choose the suspected-layer prefix for a cross-workflow divergence from its
    attributed axes, or None when the divergence should NOT be upgraded to a
    fault-implying label.

    GPU and API divergences run the *same* shader binary everywhere, so a
    result that differs across runners localizes the fault to that runtime
    layer — hence `runtime_driver_suspected` (per-vendor GPU split) and
    `api_backend_suspected` (API split); more specific GPU wins over API.

    The compiler axis is deliberately NOT upgraded. clang-dxc and DXC emit
    *different* DXIL from the same source, so a compiler-aligned divergence only
    means the two compilers produced different output — and different output is
    not necessarily *wrong* output. Minting a `compiler_suspected_miscompile`
    label would point the finger at clang/DXC, which the divergence alone can't
    establish (the golden may encode a test-spec assumption only one compiler's
    perfectly valid codegen happens to satisfy). So we return None: the
    `compiler_pattern` axis chip still surfaces the correlation as a *fact*, but
    the classification stays the neutral base symptom label rather than blaming
    a compiler. A genuine compiler regression is confirmed instead by
    triage_report.py's commit bounding, not asserted from the pass matrix.

    Returns 'runtime_driver_suspected' | 'api_backend_suspected' | None.
    """
    if "gpu_pattern" in axes:
        return "runtime_driver_suspected"
    if "api_pattern" in axes:
        return "api_backend_suspected"
    if "compiler_pattern" in axes:
        return None            # compiler divergence is reported, never blamed
    return "runtime_driver_suspected"


def _fmt_commits(commits: dict) -> str:
    """Compact `llvm <sha> · dxc <sha> · offload <sha>` for the summary table
    (each part omitted if that repo's commit is unknown)."""
    bits = []
    if commits.get("llvm-project"):
        bits.append(f"llvm `{commits['llvm-project'][:9]}`")
    if commits.get("directxshadercompiler"):
        bits.append(f"dxc `{commits['directxshadercompiler'][:9]}`")
    if commits.get("offload-test-suite"):
        bits.append(f"offload `{commits['offload-test-suite'][:9]}`")
    return " · ".join(bits)


# ---------------------------------------------------------------------------
# HTML report
#
# A single self-contained file (inline CSS/JS, no external requests) meant to be
# opened locally or served via GitHub Pages — GitHub does NOT render committed
# .html in the repo file view, so summary.md stays the on-GitHub surface and
# this is the richer local view. Wrapping cells, colour-coded chips, issue
# status badges with the title on hover, and a live text filter.
# ---------------------------------------------------------------------------

_HTML_CSS = """
:root{
  --bg:#fff; --fg:#1f2328; --border:#d0d7de; --th-bg:#f6f8fa; --row-alt:#f9fafb;
  --link:#0969da; --muted:#656d76;
  --danger:#cf222e; --accent:#0969da; --success:#1a7f37;
  --bo-bg:#dafbe1; --bo-fg:#1a7f37; --bo-bd:#1a7f37;
  --bf-bg:#fbefff; --bf-fg:#8250df; --bf-bd:#8250df;
  --bw-bg:#eaeef2; --bw-fg:#57606a; --bw-bd:#8c959f;
  --bu-bg:#fff; --bu-fg:#57606a; --bu-bd:#d0d7de;
  --ax-api:#0969da; --ax-gpu:#1a7f37; --ax-compiler:#8250df; --ax-host:#bc4c00; --ax-variant:#6e7781;
}
:root[data-theme="dark"]{
  --bg:#0d1117; --fg:#e6edf3; --border:#30363d; --th-bg:#161b22; --row-alt:#161b22;
  --link:#4493f8; --muted:#8b949e;
  --danger:#ff7b72; --accent:#4493f8; --success:#3fb950;
  --bo-bg:#12261c; --bo-fg:#3fb950; --bo-bd:#238636;
  --bf-bg:#2b1a3d; --bf-fg:#bc8cff; --bf-bd:#8957e5;
  --bw-bg:#21262d; --bw-fg:#8b949e; --bw-bd:#484f58;
  --bu-bg:#0d1117; --bu-fg:#8b949e; --bu-bd:#30363d;
  --ax-api:#4493f8; --ax-gpu:#3fb950; --ax-compiler:#bc8cff; --ax-host:#e3903c; --ax-variant:#8b949e;
}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  margin:0 1.5rem 1.5rem;padding-top:3.4rem;color:var(--fg);background:var(--bg);line-height:1.45}
h1{font-size:1.5rem} h2{font-size:1.15rem;margin-top:1.6rem}
h1,h2{border-bottom:1px solid var(--border);padding-bottom:.3rem}
a{color:var(--link);text-decoration:none} a:hover{text-decoration:underline}
.meta{color:var(--muted);font-size:13px;margin:.2rem 0 1rem}
table{border-collapse:collapse;width:100%;margin:.5rem 0 1rem;font-size:13px}
th,td{border:1px solid var(--border);padding:5px 8px;text-align:left;vertical-align:top}
th{background:var(--th-bg);position:sticky;top:46px;z-index:1}
tbody tr:nth-child(even){background:var(--row-alt)}
td.test{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
  white-space:nowrap;max-width:34ch;overflow:hidden;text-overflow:ellipsis}
td.note{max-width:44ch;color:var(--muted);font-size:12px}
.passgrp{margin:2px 0}.passgrp+.passgrp{margin-top:3px;padding-top:3px;border-top:1px dashed var(--border)}
.muted{color:var(--muted);font-size:12px}
.chip{display:inline-block;padding:1px 7px;border-radius:2em;font-size:11px;
  font-weight:600;color:#fff;white-space:nowrap}
.badge{display:inline-block;padding:0 6px;border-radius:2em;font-size:11px;
  font-weight:600;text-decoration:none;margin:1px 3px 1px 0;white-space:nowrap;
  border:1px solid transparent}
.b-open{background:var(--bo-bg);color:var(--bo-fg);border-color:var(--bo-bd)}
.b-fixed{background:var(--bf-bg);color:var(--bf-fg);border-color:var(--bf-bd)}
.b-wontfix{background:var(--bw-bg);color:var(--bw-fg);border-color:var(--bw-bd)}
.b-unknown{background:var(--bu-bg);color:var(--bu-fg);border-color:var(--bu-bd)}
.res-FAIL{color:var(--danger);font-weight:700}
.res-XPASS{color:var(--accent);font-weight:700}
.ok{color:var(--success);font-weight:600}.bad{color:var(--danger);font-weight:600}
.axis{display:inline-block;padding:1px 8px;border-radius:2em;font-size:11px;
  margin:1px 3px 1px 0;white-space:nowrap;background:var(--th-bg);border:1px solid var(--border)}
.axis .k{color:var(--muted)} .axis .v{font-weight:700}
.ax-api{border-color:var(--ax-api)} .ax-api .v{color:var(--ax-api)}
.ax-gpu{border-color:var(--ax-gpu)} .ax-gpu .v{color:var(--ax-gpu)}
.ax-compiler{border-color:var(--ax-compiler)} .ax-compiler .v{color:var(--ax-compiler)}
.ax-host{border-color:var(--ax-host)} .ax-host .v{color:var(--ax-host)}
.ax-variant{border-color:var(--ax-variant)} .ax-variant .v{color:var(--ax-variant)}
.wfcount{color:var(--muted);font-size:11px;font-weight:700;margin-right:6px}
.wfmode{color:var(--muted);font-size:10px;font-style:italic}
.wf{display:inline-block;padding:0 6px;margin:1px 3px 1px 0;border-radius:5px;
  font-size:11px;
  background:var(--th-bg);border:1px solid var(--border);white-space:nowrap}
details{margin:.3rem 0}summary{cursor:pointer;font-weight:600}
#toolbar{position:fixed;top:0;left:0;right:0;z-index:6;display:flex;align-items:center;
  gap:12px;padding:7px 14px;background:var(--th-bg);border-bottom:1px solid var(--border);
  box-shadow:0 1px 5px rgba(0,0,0,.12)}
#toolbar .title{font-weight:700;font-size:15px;white-space:nowrap}
#toolbar .sub{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#toolbar .spacer{flex:1 1 auto}
#filter{padding:5px 9px;width:210px;font-size:13px;
  border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg)}
.count{color:var(--muted);font-weight:400;font-size:12px}
#themeBtn{padding:5px 11px;font-size:13px;white-space:nowrap;
  border:1px solid var(--border);border-radius:6px;background:var(--th-bg);color:var(--fg);
  cursor:pointer}
"""

_HTML_JS = """
function filt(v){v=v.toLowerCase();
  document.querySelectorAll('tr.f').forEach(function(r){
    r.style.display=r.textContent.toLowerCase().indexOf(v)>=0?'':'none';});}
function _syncThemeBtn(){
  var t=document.documentElement.getAttribute('data-theme')||'light';
  var b=document.getElementById('themeBtn');
  if(b)b.textContent=(t==='dark')?'☀️ Light':'🌙 Dark';}
function toggleTheme(){
  var c=(document.documentElement.getAttribute('data-theme')==='dark')?'light':'dark';
  document.documentElement.setAttribute('data-theme',c);
  try{localStorage.setItem('otss-theme',c);}catch(e){}
  _syncThemeBtn();}
_syncThemeBtn();
"""

# Runs in <head> before the body paints so the stored/preferred theme is applied
# with no flash of the wrong colour scheme.
_HTML_THEME_INIT = (
    "<script>(function(){try{var t=localStorage.getItem('otss-theme');"
    "if(!t)t=(window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)')"
    ".matches)?'dark':'light';"
    "document.documentElement.setAttribute('data-theme',t);}catch(e){}})();</script>"
)


def _label_color(label: str) -> str:
    l = label or ""
    if "miscompile" in l:
        return "#8250df"
    if "crash" in l or "driver_error" in l:
        return "#cf222e"
    if "pipeline" in l:
        return "#bf8700"
    if "shader_compile" in l:
        return "#953800"
    if l == "build_failure":
        return "#cf222e"
    if l == "xpass":
        return "#0969da"
    return "#57606a"  # unknown / other


# Axis dimension -> css class + the value set it can take (also used to render a
# colour key in the legend).
_AXIS_CLASS = {"api": "ax-api", "gpu": "ax-gpu", "compiler": "ax-compiler",
               "host": "ax-host", "variant": "ax-variant"}
_AXIS_VALUES = [
    ("api", "D3D12 / Vulkan / Metal"),
    ("gpu", "AMD / NVIDIA / Intel / QC / Warp / Lavapipe / Metal"),
    ("compiler", "clang / dxc"),
    ("host", "x64 / ARM64 / macOS"),
    ("variant", "GBV / Preview / none"),
]


def _html_axis_chip(dim: str, value: str) -> str:
    cls = _AXIS_CLASS.get(dim, "ax-variant")
    return (f'<span class="axis {cls}"><span class=k>{html.escape(dim)}</span> '
            f'<span class=v>{html.escape(str(value))}</span></span>')


def _html_axis_chips(axes: dict) -> str:
    if not axes:
        return '<span class="muted">\u2014</span>'
    return "".join(_html_axis_chip(k.replace("_pattern", ""), v) for k, v in axes.items())


def _html_axis_legend() -> str:
    chips = "".join(_html_axis_chip(dim, vals) for dim, vals in _AXIS_VALUES)
    return (
        '<p class=muted><b>Axes.</b> Each test failure summary row is tagged with the axis '
        "(dimension) on which its failing set is homogeneous while at least one "
        "workflow on a different value passes. The dimensions and their values:</p>"
        f"<p>{chips}</p>"
        "<p class=muted>e.g. <code>api: Vulkan-only</code> means every failing "
        "workflow is Vulkan and at least one non-Vulkan workflow passes.</p>"
    )


def _html_wf_list(names: list[str], annotate: dict[str, str] | None = None) -> str:
    """Render workflow names as individual pills (full names), count-badged, so
    the members of a fails-on / passes-on set are visually separate and clear.
    When `annotate` is given, each pill shows its per-workflow value (e.g. the
    failure mode) as a small muted suffix."""
    if not names:
        return '<span class="muted">\u2014</span>'
    pills = []
    for n in names:
        label = html.escape(n)
        if annotate and n in annotate:
            label += f' <span class=wfmode>{html.escape(annotate[n])}</span>'
        pills.append(f'<span class=wf>{label}</span>')
    return f'<span class=wfcount>{len(names)}\u00d7</span>' + "".join(pills)


def _html_chip(label: str) -> str:
    if not label:
        return ""
    return f'<span class="chip" style="background:{_label_color(label)}">{html.escape(label)}</span>'


def _html_issue_badges(t: dict) -> str:
    recs = _issue_records(t)
    if not recs:
        return '<span class="muted">\u2014</span>'
    out = []
    for r in recs:
        state = (r.get("state") or "").lower()
        reason = (r.get("state_reason") or "").lower()
        cls = ("b-open" if state == "open"
               else "b-fixed" if reason == "completed"
               else "b-wontfix" if state == "closed"
               else "b-unknown")
        label = f"{_issue_number(r['url'])} {state or '?'}"
        tip = html.escape(r.get("title") or "", quote=True)
        out.append(f'<a class="badge {cls}" href="{html.escape(r["url"])}" '
                   f'title="{tip}" target="_blank">{html.escape(label)}</a>')
    return "".join(out)


def _html_note(t: dict) -> str:
    # Only the XFAIL/diagnostic note here. The cross-workflow "passes on" list is
    # intentionally omitted from the per-workflow failure table (it's low-signal
    # and wide); the Cross-workflow divergences section carries it where it's the
    # actual point.
    if t.get("note"):
        return html.escape(t["note"])
    return '<span class="muted">\u2014</span>'


def _html_pass_groups(passes_on: list[str], axes: dict) -> str:
    """Render the passing workflows, grouped by the divergence's primary axis
    value when there is one (each group headed by the axis-value chip), else a
    flat pill list."""
    if not passes_on:
        return '<span class="muted">\u2014</span>'
    groups = group_passes_by_axis(passes_on, axes)
    if groups is None:
        return _html_wf_list(passes_on)
    dim = primary_axis_dim(axes)
    return "".join(
        f'<div class=passgrp>{_html_axis_chip(dim, value)} {_html_wf_list(wfs)}</div>'
        for value, wfs in groups
    )


def render_html_report(run_ts: str, summary: list[dict], divergences: list[dict],
                       used_labels: Counter) -> str:
    esc = html.escape
    # A fixed full-width top bar carries the report title + timestamp on the left
    # and the theme toggle plus (when there are any failures) the test filter on
    # the right, so all stay visible without scrolling back up.
    has_failures = any(r.get("tests") for r in summary)
    filter_input = ('<input id=filter placeholder="filter tests / classification / issue\u2026" '
                    'oninput="filt(this.value)">') if has_failures else ""
    toolbar = (
        "<div id=toolbar>"
        '<span class=title>offload-test-suite report</span>'
        f'<span class=sub>{esc(run_ts)} \u00b7 {esc(REPO)} \u00b7 {len(summary)} workflows</span>'
        "<span class=spacer></span>"
        f"{filter_input}"
        '<button id=themeBtn onclick="toggleTheme()">Dark</button>'
        "</div>"
    )
    h: list[str] = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        f"<title>offload-test-suite report {esc(run_ts)}</title>",
        _HTML_THEME_INIT,
        f"<style>{_HTML_CSS}</style>",
        "</head><body>",
        toolbar,
    ]

    # Legend (collapsible).
    if used_labels:
        h.append("<details><summary>Classification legend "
                 f'<span class=count>({len(used_labels)} labels in this report)</span></summary>')
        h.append("<table><thead><tr><th>label</th><th>count</th><th>meaning</th></tr></thead><tbody>")
        for label, n in used_labels.most_common():
            expl = " ".join(CLASSIFICATION_LEGEND.get(label, "(no legend entry)").split())
            h.append(f"<tr><td>{_html_chip(label)}</td><td>{n}</td><td>{esc(expl)}</td></tr>")
        h.append("</tbody></table>")
        h.append(_html_axis_legend())
        h.append("</details>")

    # Category / detail column legend (collapsible), values that appear.
    used_cat: Counter = Counter()
    used_det: Counter = Counter()
    for r in summary:
        if r.get("category"):
            used_cat[r["category"]] += 1
        if r.get("detail"):
            used_det[r["detail"]] += 1
    if used_cat or used_det:
        h.append("<details><summary>Category / detail column legend</summary>")
        h.append("<table><thead><tr><th>column</th><th>value</th><th>count</th>"
                 "<th>meaning</th></tr></thead><tbody>")
        for c, n in used_cat.most_common():
            expl = " ".join(CATEGORY_LEGEND.get(c, "(no legend entry)").split())
            h.append(f"<tr><td>category</td><td><code>{esc(c)}</code></td>"
                     f"<td>{n}</td><td>{esc(expl)}</td></tr>")
        for d, n in used_det.most_common():
            expl = " ".join(DETAIL_LEGEND.get(d, "(no legend entry)").split())
            h.append(f"<tr><td>detail</td><td><code>{esc(d)}</code></td>"
                     f"<td>{n}</td><td>{esc(expl)}</td></tr>")
        h.append("</tbody></table></details>")
    # Per-workflow summary table.
    h.append("<h2>Workflows</h2>")
    h.append("<table><thead><tr><th>workflow</th><th>conclusion</th><th>category</th>"
             "<th>detail</th><th># fails</th><th>tested commits (llvm / dxc / offload)</th><th>run</th></tr></thead><tbody>")
    for r in summary:
        concl = r.get("conclusion") or r.get("status") or ""
        ccls = "ok" if concl == "success" else "bad" if concl == "failure" else "muted"
        n = len(r.get("tests") or [])
        build = esc(_fmt_commits(r.get("commits") or {})).replace("`", "")
        h.append(
            f"<tr><td>{esc(r['workflow'])}</td>"
            f'<td class="{ccls}">{esc(concl)}</td>'
            f"<td>{esc(r.get('category') or '')}</td>"
            f"<td>{esc(r.get('detail') or '')}</td>"
            f"<td>{n or ''}</td><td>{build}</td>"
            f'<td><a href="{esc(r["run_url"])}" target=_blank>run</a></td></tr>')
    h.append("</tbody></table>")

    # Test failure summary: one row per (test, classification), naming the
    # aligning axis, the workflows it fails on, and — grouped by that axis — the
    # workflows it passes on.
    if divergences:
        n_rows = sum(len(test_failure_rows(d)) for d in divergences)
        h.append("<h2>Test failure summary "
                 f'<span class=count>({n_rows} test/classification rows · '
                 'fails on some workflows, passes on others)</span></h2>')
        h.append("<table><thead><tr><th>test</th><th>classification</th><th>axis</th>"
                 "<th>fails on</th><th>passes on</th></tr></thead><tbody>")
        for d in divergences:
            axes = d.get("axes") or {}
            axis_cell = _html_axis_chips(axes)
            passes_cell = _html_pass_groups(d.get("passes_on") or [], axes)
            for row in test_failure_rows(d):
                h.append(
                    f'<tr class=f><td class=test>{esc(d["test"])}</td>'
                    f'<td>{_html_chip(row["classification"])}</td>'
                    f"<td>{axis_cell}</td>"
                    f'<td class=note>{_html_wf_list(row["fails_on"])}</td>'
                    f'<td class=note>{passes_cell}</td></tr>')
        h.append("</tbody></table>")

    # Per-workflow failure detail (collapsible), with a shared filter box.
    workflows_with_tests = [r for r in summary if r.get("tests")]
    if workflows_with_tests:
        h.append("<h2>Failures by workflow</h2>")
        for r in workflows_with_tests:
            tests = r["tests"]
            h.append(f"<details open><summary>{esc(r['workflow'])} "
                     f'<span class=count>\u2014 {len(tests)} failure(s)</span></summary>')
            h.append(f'<div class=meta><a href="{esc(r["run_url"])}" target=_blank>run \u2197</a></div>')
            h.append("<table><thead><tr><th>result</th><th>test</th><th>classification</th>"
                     "<th>issues</th><th>notes</th></tr></thead><tbody>")
            for t in tests:
                res = t.get("result", "")
                h.append(
                    f'<tr class=f><td class="res-{esc(res)}">{esc(res)}</td>'
                    f"<td class=test>{esc(t['test'])}</td>"
                    f"<td>{_html_chip(t.get('classification',''))}</td>"
                    f"<td>{_html_issue_badges(t)}</td>"
                    f'<td class=note>{_html_note(t)}</td></tr>')
            h.append("</tbody></table></details>")

    h.append(f"<script>{_HTML_JS}</script>")
    h.append("</body></html>")
    return "\n".join(h)

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--otss-root", default=str(pathlib.Path(__file__).resolve().parent.parent / "offload-test-suite"))
    ap.add_argument("--out-root", default=str(pathlib.Path(__file__).resolve().parent / "reports"))
    ap.add_argument("--skip-logs", action="store_true", help="don't download logs, only list run statuses")
    ap.add_argument("--no-pass-matrix", action="store_true",
                    help="don't download logs for successful runs (skips cross-GPU divergence analysis)")
    ap.add_argument("--no-unshallow", action="store_true",
                    help="don't unshallow to obtain a run's offload-test-suite commit; only try a "
                         "targeted by-sha fetch. Note: logs record abbreviated SHAs, which a shallow "
                         "clone usually can't resolve, so XFAIL matching may fall back to the working "
                         "tree (possibly-stale clauses)")
    ap.add_argument("--no-git-fetch", action="store_true",
                    help="never fetch commits; read XFAIL test files from the working tree as-is")
    args = ap.parse_args()

    # Default: unshallow when needed. Run logs only carry abbreviated commit
    # SHAs (`HEAD is now at <short>`), which git can't fetch by ref on a shallow
    # clone; fetching full history is the only reliable way to resolve them, so
    # XFAIL clauses are read from the exact tested revision. It's a one-time cost
    # per repo (subsequent commits resolve locally).
    set_git_fetch_mode("off" if args.no_git_fetch else "targeted" if args.no_unshallow else "unshallow")

    otss_root = pathlib.Path(args.otss_root).resolve()
    if not otss_root.exists():
        raise SystemExit(f"offload-test-suite root not found: {otss_root}")

    gh = GH(load_token())
    workflows = fetch_workflows(gh)
    print(f"[{len(workflows)}] total workflows in repo", file=sys.stderr)

    # UTC timestamp, second precision — the monitor may be run more than
    # once per day and each run should get its own report directory.
    run_ts = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = pathlib.Path(args.out_root) / run_ts
    (out_dir / "logs").mkdir(parents=True, exist_ok=True)

    summary: list[dict] = []
    issue_cache: dict = {}
    # pass matrix: {(suite, test): {workflow_name: result}}
    matrix: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)

    for wf in workflows:
        if wf["state"] != "active":
            continue
        run = latest_scheduled_run(gh, wf["id"])
        if run is None:
            continue
        row = {
            "workflow": wf["name"],
            "workflow_path": wf["path"],
            "run_id": run["id"],
            "run_url": run["html_url"],
            "conclusion": run["conclusion"],
            "status": run["status"],
            "created_at": run["created_at"],
            "head_sha": run["head_sha"],
        }
        print(f"  {row['conclusion'] or row['status']:12s}  {wf['name']}", file=sys.stderr)

        # Download logs for BOTH failures (to classify) and successes
        # (to build the cross-GPU pass matrix). Skip in-progress/queued/cancelled.
        want_log = (
            not args.skip_logs
            and run["conclusion"] in ("failure", "success")
            and not (run["conclusion"] == "success" and args.no_pass_matrix)
        )
        if want_log:
            try:
                zip_bytes = download_run_logs(gh, run["id"])
            except urllib.error.HTTPError as e:
                row["log_error"] = f"HTTP {e.code}"
                summary.append(row)
                continue
            log_text = combined_log_text(zip_bytes)
            log_path = out_dir / "logs" / (wf["path"].replace("/", "_") + ".log.gz")
            with gzip.open(log_path, "wt", encoding="utf-8") as f:
                f.write(log_text)
            row["log_file"] = str(log_path.relative_to(out_dir))
            row["commits"] = extract_built_commits(log_text)

            # Feed the pass matrix from every completed run. Key on the
            # platform-independent (base_suite, test) identity so the same test
            # file is grouped across workflows (e.g. OffloadTest-vk and
            # OffloadTest-warp-d3d12 both fold into OffloadTest for comparison).
            for (suite, test), res in extract_all_results(log_text).items():
                matrix[normalize_test_key(suite, test)][wf["name"]] = res

            # Classify only failing runs.
            if run["conclusion"] == "failure":
                row.update(classify_run(log_text, gh, otss_root, issue_cache, wf["name"]))
        summary.append(row)

    # ---- cross-GPU pivot: refine runtime_miscompile / runtime_driver_error ----
    # A test's failure MODE (miscompile / crash / unknown) can differ per
    # workflow — e.g. a driver crash on one GPU but a value mismatch on others.
    # Only the axis-derived *prefix* (runtime_driver_suspected / ...) is shared.
    # Capture each failing workflow's base classification up front so the
    # divergence row can report the per-workflow breakdown rather than one
    # arbitrary label.
    base_modes: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    for r in summary:
        for t in r.get("tests") or []:
            base_modes[normalize_test_key(t["suite"], t["test"])][r["workflow"]] = \
                t.get("classification", "")

    seen_div: set[tuple[str, str]] = set()
    divergences: list[dict] = []
    for r in summary:
        for t in r.get("tests") or []:
            key = normalize_test_key(t["suite"], t["test"])
            per_wf = matrix.get(key, {})
            passes_on = sorted(w for w, res in per_wf.items() if res == "PASS")
            fails_on = sorted(w for w, res in per_wf.items() if res in ("FAIL", "XPASS"))
            t["passes_on"] = passes_on
            t["fails_on"] = fails_on
            if passes_on and fails_on and t.get("classification", "").startswith("runtime_"):
                axes = attribute_divergence(fails_on, passes_on)
                t["axes"] = axes
                # Attribute the suspected runtime layer from the tightest axis:
                #   * gpu-aligned -> per-vendor driver (runtime_driver_suspected)
                #   * api-aligned -> API/backend layer  (api_backend_suspected)
                #   * compiler-aligned -> NOT upgraded; clang-dxc/DXC emit
                #     different DXIL, so a compiler split is different-output,
                #     not proof of a compiler fault. The compiler_pattern axis
                #     is still recorded (a fact); the label stays neutral.
                #   * none of the above -> environment-dependent
                #     (runtime_driver_suspected)
                # More specific axes win (gpu before api). A GPU/API-specific
                # failure is still not proof of a defect in that layer — the
                # test itself may specify pipeline I/O in a way only some
                # configurations reject — so upgrades stay 'suspected', never
                # 'confirmed'.
                prefix = divergence_suspect_prefix(axes)
                suffix = _RUNTIME_UPGRADE_SUFFIX.get(t["classification"])
                if prefix and suffix:
                    t["classification"] = f"{prefix}_{suffix}"
                if key not in seen_div:
                    seen_div.add(key)
                    # Per-workflow classification. When a runtime layer was
                    # attributed (gpu/api) the per-workflow failure mode (suffix)
                    # carries the shared prefix; otherwise (e.g. compiler-only or
                    # no clean axis) each workflow keeps its neutral base label.
                    fail_cls: dict[str, str] = {}
                    for w in fails_on:
                        base = base_modes[key].get(w, "")
                        sfx = _RUNTIME_UPGRADE_SUFFIX.get(base)
                        fail_cls[w] = (f"{prefix}_{sfx}" if (prefix and sfx)
                                       else (base or "unknown"))
                    divergences.append({
                        "suite": normalize_suite(t["suite"]), "test": t["test"],
                        "classifications": sorted(set(fail_cls.values())),
                        "fail_classifications": fail_cls,
                        "axes": axes,
                        "fails_on": fails_on, "passes_on": passes_on,
                    })

    # ---- write outputs ----
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    (out_dir / "divergences.json").write_text(json.dumps(divergences, indent=2))
    (out_dir / "legend.json").write_text(json.dumps(CLASSIFICATION_LEGEND, indent=2))

    # markdown
    md = [f"# offload-test-suite scheduled-workflow report — {run_ts}", "",
          f"Repo: `{REPO}`  •  {len(summary)} scheduled workflows", ""]

    # Which labels actually appear in this report? Only legend those, ordered
    # by frequency (most common first) so the top of the page answers
    # "what does the label on the biggest cluster mean?" first.
    used_labels: Counter = Counter()
    for r in summary:
        if r.get("category") == "build_failure":
            used_labels["build_failure"] += 1
        for t in r.get("tests") or []:
            c = t.get("classification")
            if c:
                used_labels[c] += 1

    if used_labels:
        md += ["<details>",
               f"<summary>Classification legend ({len(used_labels)} labels in this report)</summary>",
               "",
               "| label (count) | meaning |",
               "|---|---|"]
        for label, n in used_labels.most_common():
            expl = CLASSIFICATION_LEGEND.get(label, "*(no legend entry — please add one to CLASSIFICATION_LEGEND)*")
            # collapse newlines/multiple spaces so it fits in a table cell
            expl = " ".join(expl.split())
            md.append(f"| `{label}` (×{n}) | {expl} |")
        md += ["",
               "Axes: **api** (D3D12 / Vulkan / Metal), "
               "**gpu** (AMD / NVIDIA / Intel / QC / Warp / Lavapipe / Metal), "
               "**compiler** (clang / dxc), **host** (x64 / ARM64 / macOS), "
               "**variant** (GBV / Preview / none). A divergence row's `axes` "
               "dict names the axis on which the failing set is homogeneous — "
               "e.g. `api_pattern: Vulkan-only` means every failing workflow is "
               "Vulkan and at least one non-Vulkan workflow passes.",
               "",
               "</details>",
               ""]

    # Legend for the workflow table's `category` / `detail` columns (only the
    # values that actually appear).
    used_cat: Counter = Counter()
    used_det: Counter = Counter()
    for r in summary:
        if r.get("category"):
            used_cat[r["category"]] += 1
        if r.get("detail"):
            used_det[r["detail"]] += 1
    if used_cat or used_det:
        md += ["<details>",
               "<summary>Category / detail column legend</summary>",
               "",
               "`category` — the stage at which the run failed:",
               "",
               "| category (count) | meaning |",
               "|---|---|"]
        for c, n in used_cat.most_common():
            expl = " ".join(CATEGORY_LEGEND.get(c, "*(no legend entry — add one to CATEGORY_LEGEND)*").split())
            md.append(f"| `{c}` (×{n}) | {expl} |")
        if used_det:
            md += ["",
                   "`detail` — refines the category (build subtree, or why no per-test rows):",
                   "",
                   "| detail (count) | meaning |",
                   "|---|---|"]
            for d, n in used_det.most_common():
                expl = " ".join(DETAIL_LEGEND.get(d, "*(no legend entry — add one to DETAIL_LEGEND)*").split())
                md.append(f"| `{d}` (×{n}) | {expl} |")
        md += ["", "</details>", ""]

    md += ["| workflow | conclusion | category | detail | # test failures | tested commits (llvm / dxc / offload) | run |",
           "|---|---|---|---|---|---|---|"]
    for r in summary:
        cat = r.get("category") or ""
        det = r.get("detail") or ""
        n = len(r.get("tests") or [])
        build = _fmt_commits(r.get("commits") or {})
        md.append(f"| {r['workflow']} | {r['conclusion'] or r['status']} | {cat} | {det} | {n} | {build} | [run]({r['run_url']}) |")
    md.append("")

    if divergences:
        md += ["## Test failure summary", "",
               "Tests that fail on some workflows but pass on others, one row per",
               "(test, classification). The same test source runs everywhere, so the",
               "split points at something configuration-specific: a per-vendor driver",
               "bug, an API/backend bug, a compiler (DXC vs clang-dxc) codegen",
               "difference, or a test that specifies pipeline inputs/outputs in a way",
               "only some configurations reject. The **axis** column names the axis on",
               "which the failing set is homogeneous (e.g. `api: Vulkan-only` = every",
               "failing workflow is Vulkan); the **passes on** column groups the passing",
               "workflows by that same axis so the contrast is explicit. On gpu/api the",
               "shader binary is identical across workflows, so the label is upgraded to",
               "a 'suspected' driver/backend layer (never 'confirmed'); a compiler-axis",
               "split is reported as an axis only, without blaming the compiler.",
               "",
               "| test | classification | axis | fails on | passes on |",
               "|---|---|---|---|---|"]
        for d in divergences:
            axes = d.get("axes") or {}
            axis_bits = [f"{k.replace('_pattern','')}: {v}" for k, v in axes.items()]
            axis_str = "; ".join(axis_bits) or "-"
            passes_str = _md_pass_groups(d.get("passes_on") or [], axes)
            for row in test_failure_rows(d):
                md.append(f"| `{d['test']}` | `{row['classification']}` | {axis_str} | "
                          f"{_md_wf_list(row['fails_on'])} | {passes_str} |")
        md.append("")

    tested = [r for r in summary if r.get("tests")]
    if tested:
        md += ["## Failures by workflow", ""]
    for r in tested:
        tests = r["tests"]
        md += ["<details open>",
               f"<summary><b>{r['workflow']}</b> — {len(tests)} failure(s)</summary>",
               "",
               f"[run]({r['run_url']})",
               ""]
        md += ["| result | test | classification | issues | notes |",
               "|---|---|---|---|---|"]
        for t in tests:
            recs = _issue_records(t)
            issues = "<br>".join(
                f"{_issue_emoji(x.get('state'), x.get('state_reason'))} "
                f"[{_issue_number(x['url'])}]({x['url']})"
                for x in recs
            ) or "—"
            ncell = []
            if t.get("note"):
                # Escape pipes so an XFAIL expr like `AMD || NVIDIA` in the note
                # can't break the markdown table cell.
                ncell.append(t["note"].replace("|", "\\|"))
            # "passes on" is intentionally omitted here (low-signal, wide); the
            # Cross-workflow divergences section carries it where it matters.
            notes = "<br>".join(ncell) or "—"
            md.append(f"| {t['result']} | `{t['test']}` | "
                      f"{t.get('classification','')} | {issues} | {notes} |")
        md += ["", "</details>", ""]
    (out_dir / "summary.md").write_text("\n".join(md))

    # html (self-contained rich view for local / GitHub Pages)
    (out_dir / "summary.html").write_text(
        render_html_report(run_ts, summary, divergences, used_labels))
    # csv
    with (out_dir / "summary.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["workflow", "conclusion", "category", "detail", "result", "suite", "test",
                    "classification", "passes_on", "fails_on", "linked_issue_url", "linked_issue_state", "run_url"])
        for r in summary:
            tests = r.get("tests") or []
            if not tests:
                w.writerow([r["workflow"], r["conclusion"] or r["status"], r.get("category",""), r.get("detail",""),
                            "", "", "", "", "", "", "", "", r["run_url"]])
            for t in tests:
                li = t.get("linked_issue") or {}
                all_urls = [d["url"] for d in (t.get("linked_issues") or ([li] if li else []))]
                w.writerow([r["workflow"], r["conclusion"] or r["status"], r.get("category",""), r.get("detail",""),
                            t["result"], t["suite"], t["test"], t.get("classification",""),
                            ";".join(t.get("passes_on") or []), ";".join(t.get("fails_on") or []),
                            ";".join(all_urls), li.get("state",""), r["run_url"]])

    print(f"\nReport: {out_dir}", file=sys.stderr)
    print(f"  summary.md / summary.html / summary.json / summary.csv / divergences.json", file=sys.stderr)
    print(f"  {len(divergences)} tests fail-on-some/pass-on-others (test failure summary)", file=sys.stderr)


if __name__ == "__main__":
    main()
