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
            elif "offloader" in low:
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
    # Pipeline/PSO creation was rejected by the runtime. The shader compiled to
    # DXIL/SPIR-V fine, but Create*PipelineState / vkCreate*Pipelines failed.
    # This is distinct from both a driver crash (the process survives and
    # reports a clean error) and a miscompile (nothing ever executed). Depending
    # on the HRESULT/VkResult it's usually a backend/driver rejection or a
    # malformed pipeline spec in the test itself.
    pipeline_markers = (
        "failed to create pso",
        "failed to create pipeline",
        "failed to create compute pipeline",
        "failed to create graphics pipeline",
    )
    miscompile_markers = (
        "mismatch", "verify failed", "verification failed",
        "image mismatch", "golden image", "expected .* actual", "diff:",
        "filecheck error",
    )

    # If offloader crashed with an NT status (0xC0000005 etc.) treat as driver crash.
    for c in failing:
        if c["kind"] == "offloader" and c["exit_status"].startswith(("0x", "-")):
            return "runtime_driver_error"
        if c["kind"] == "offloader":
            # Non-zero decimal exit — inspect payload
            payload = (c["stdout"] + "\n" + c["stderr"]).lower()
            if any(m in payload for m in driver_markers):
                return "runtime_driver_error"
            if any(m in payload for m in pipeline_markers):
                return "runtime_pipeline_error"
            if any(re.search(m, payload) for m in miscompile_markers):
                return "runtime_miscompile"
        if c["kind"] == "filecheck":
            return "runtime_miscompile"

    # Fallback on whole-block text
    lower = block.lower()
    if any(m in lower for m in driver_markers):
        return "runtime_driver_error"
    if any(m in lower for m in pipeline_markers):
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
    "compiler_suspected_miscompile":
        "Base label was `runtime_miscompile`, and the failing set aligns cleanly on the "
        "**compiler axis** (all failures use one compiler — DXC or clang-dxc — and at "
        "least one workflow using the other compiler passes) *without* aligning on any "
        "GPU vendor or API. Every workflow runs the exact same test from the exact same "
        "source; the only difference is which compiler produced the shader, so a value "
        "mismatch confined to one compiler points at that compiler's frontend/codegen "
        "(e.g. DXC vs clang-dxc DXIL differences) rather than a driver or backend. "
        "Still 'suspected': an under-specified test input can also diverge. See `axes` "
        "for the pattern (e.g. compiler_pattern: clang-only).",
    "compiler_suspected_crash":
        "Base label was `runtime_driver_error`, upgraded on the compiler axis (all "
        "failures share one compiler, at least one workflow on the other compiler "
        "passes, with no per-GPU or per-API alignment). A crash confined to shaders "
        "from one compiler points at that compiler emitting something the runtime "
        "rejects — but, as with the other suspected labels, it can equally be a "
        "test-spec issue only that compiler's output surfaces. Compiler-specific crash, "
        "root cause unconfirmed (compiler bug OR test-spec bug).",
    "compiler_suspected_unknown":
        "Base label was `runtime_unknown`, upgraded via compiler-axis attribution. The "
        "compiler (DXC vs clang-dxc) correlates but the specific failure mode wasn't "
        "parsed from the log.",
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


def _compact_wf_list(names: list[str]) -> str:
    """Comma-join workflow names as compact slugs, prefixed with the count."""
    if not names:
        return "-"
    return f"{len(names)}: " + ", ".join(compact_workflow(n) for n in names)


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
        if len(fvals) == 1 and (pvals - fvals):
            out[f"{axis}_pattern"] = f"{next(iter(fvals))}-only"
    return out


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
         Intel-Gen-*, AppleM<N>). When `runner_name` is provided (parsed
         from the workflow log's `Runner name:` line, which reveals the
         *actual* physical machine the test job ran on), we key on that
         hostname via `_RUNNER_FEATURES`. This is authoritative because it
         reflects reality even when the test runner differs from the build
         runner in SplitBuild workflows. When `runner_name` is None (e.g.
         we haven't fetched the log yet), we fall back to a static guess
         keyed on the workflow's (gpu, host) axes.
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
    # Hardware features — from the actual test runner if we have it, else
    # from a static (gpu, host) guess. The `_RUNNER_FEATURES` path is the
    # source of truth; `_BUILDER_FEATURES` exists only for calls that don't
    # have a log yet (e.g. divergence-attribution helpers that just want
    # a rough feature set from the display name).
    if runner_name and runner_name in _RUNNER_FEATURES:
        feats |= _RUNNER_FEATURES[runner_name]
    else:
        feats |= _BUILDER_FEATURES.get((ax["gpu"], ax["host"]), set())
    return feats


# Actual test-runner hostnames -> CPU/GPU-derived lit features.
# Sourced from `Runner name:` lines in workflow logs, cross-referenced with
# offload-test-suite/docs/CI.md. This is the source of truth for XFAIL
# evaluation; the (gpu, host) fallback below is used only when we don't
# have a log yet.
_RUNNER_FEATURES: dict[str, set[str]] = {
    # AMD builder: Ryzen 7 9700X (Zen 5, has AVX-512), Radeon RX 9070
    "HLSLPC-AMD01":    {"AVX512"},
    # Intel builder: Threadripper 3970X (Zen 2, no AVX-512), Arc Pro B50
    # (Xe HPG => Intel Gen11-14/Xe => Intel-Gen-Current)
    "HLSLPC-INTEL01":  {"Intel-Gen-Current"},
    # NVIDIA builder: i5-14400F (Raptor Lake refresh; consumer Intel disabled
    # AVX-512 from 12th gen on), RTX 5070 — no extra features
    "HLSLPC-NVIDIA01": set(),
    # QC builder: Snapdragon X Plus (ARM), Adreno X1-85 — no extra features
    "HLSLPC-QC01":     set(),
    # macOS builder: Apple M4 Pro
    "HLSLPC-APPLE01":  {"AppleM4"},
}


# Fallback (gpu, host) -> features table for calls that don't yet have a log.
# Same information as `_RUNNER_FEATURES` but keyed on the workflow's
# name-derived axes. This is a coincidence-based static mapping: today the
# `Windows AMD *` workflows all test on HLSLPC-AMD01, so keying on gpu=AMD
# reaches the same feature set. If a workflow ever tests on a different
# machine than its GPU vendor suggests, `_RUNNER_FEATURES` (used by the
# main classifier path) is still correct because it inspects the log.
_BUILDER_FEATURES: dict[tuple[str, str], set[str]] = {
    ("AMD",      "x64"):   {"AVX512"},
    ("Intel",    "x64"):   {"Intel-Gen-Current"},
    ("Warp",     "x64"):   set(),
    ("NVIDIA",   "x64"):   set(),
    # Lavapipe-x64 (llvmpipe software Vulkan) runs on the AMD builder
    # (HLSLPC-AMD01, Ryzen 7 9700X) so it inherits that host's AVX512.
    ("Lavapipe", "x64"):   {"AVX512"},
    # QC (Snapdragon) is ARM64-only — there is no (QC, x64) configuration.
    ("Warp",     "ARM64"): set(),
    ("Lavapipe", "ARM64"): set(),
    ("QC",       "ARM64"): set(),
    ("Metal",    "macOS"): {"AppleM4"},
}


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


def _git(repo_root: pathlib.Path, *args: str, timeout: int = 60):
    """Run a git command in repo_root; return CompletedProcess or None on error."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True, timeout=timeout,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None


def git_commit_available(repo_root: pathlib.Path, commit: str) -> bool:
    """
    True if `commit` is a resolvable commit object in repo_root. If it's missing
    (shallow clone), make one best-effort attempt to fetch just that commit
    (works against servers that allow fetch-by-sha; harmless otherwise). Cached.
    """
    key = (str(repo_root), commit)
    if key in _GIT_COMMIT_AVAILABLE:
        return _GIT_COMMIT_AVAILABLE[key]
    r = _git(repo_root, "cat-file", "-e", f"{commit}^{{commit}}")
    ok = r is not None and r.returncode == 0
    if not ok:
        fr = _git(repo_root, "fetch", "--quiet", "--depth", "1", "origin", commit, timeout=120)
        if fr is not None and fr.returncode == 0:
            r2 = _git(repo_root, "cat-file", "-e", f"{commit}^{{commit}}")
            ok = r2 is not None and r2.returncode == 0
    _GIT_COMMIT_AVAILABLE[key] = ok
    return ok


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
# The prefix names the tightest aligning axis: `runtime_driver_suspected`
# (per-vendor GPU split), `api_backend_suspected` (clean API-axis split), or
# `compiler_suspected` (DXC vs clang-dxc split); see CLASSIFICATION_LEGEND.
_RUNTIME_UPGRADE_SUFFIX = {
    "runtime_miscompile": "miscompile",
    "runtime_driver_error": "crash",
    "runtime_unknown": "unknown",
}


def divergence_suspect_prefix(axes: dict) -> str:
    """
    Choose the suspected-layer prefix for a cross-workflow divergence from its
    attributed axes. More specific axes win: a per-vendor GPU split is checked
    before an API split, which is checked before a compiler split; anything else
    is treated as environment-dependent. Returns one of
    'runtime_driver_suspected' | 'api_backend_suspected' | 'compiler_suspected'.
    """
    if "gpu_pattern" in axes:
        return "runtime_driver_suspected"
    if "api_pattern" in axes:
        return "api_backend_suspected"
    if "compiler_pattern" in axes:
        return "compiler_suspected"
    return "runtime_driver_suspected"


def _fmt_commits(commits: dict) -> str:
    """Compact `llvm <sha> · dxc <sha>` for the summary table (empty if unknown)."""
    bits = []
    if commits.get("llvm-project"):
        bits.append(f"llvm `{commits['llvm-project'][:9]}`")
    if commits.get("directxshadercompiler"):
        bits.append(f"dxc `{commits['directxshadercompiler'][:9]}`")
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
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  margin:1.5rem;color:#1f2328;background:#fff;line-height:1.45}
h1{font-size:1.5rem} h2{font-size:1.15rem;margin-top:1.6rem}
h1,h2{border-bottom:1px solid #d0d7de;padding-bottom:.3rem}
a{color:#0969da;text-decoration:none} a:hover{text-decoration:underline}
.meta{color:#656d76;font-size:13px;margin:.2rem 0 1rem}
table{border-collapse:collapse;width:100%;margin:.5rem 0 1rem;font-size:13px}
th,td{border:1px solid #d0d7de;padding:5px 8px;text-align:left;vertical-align:top}
th{background:#f6f8fa;position:sticky;top:0;z-index:1}
tbody tr:nth-child(even){background:#f9fafb}
td.test{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
  white-space:nowrap;max-width:34ch;overflow:hidden;text-overflow:ellipsis}
td.note{max-width:44ch;color:#57606a;font-size:12px}
.muted{color:#656d76;font-size:12px}
.chip{display:inline-block;padding:1px 7px;border-radius:2em;font-size:11px;
  font-weight:600;color:#fff;white-space:nowrap}
.badge{display:inline-block;padding:0 6px;border-radius:2em;font-size:11px;
  font-weight:600;text-decoration:none;margin:1px 3px 1px 0;white-space:nowrap;
  border:1px solid transparent}
.b-open{background:#dafbe1;color:#1a7f37;border-color:#1a7f37}
.b-fixed{background:#fbefff;color:#8250df;border-color:#8250df}
.b-wontfix{background:#eaeef2;color:#57606a;border-color:#8c959f}
.b-unknown{background:#fff;color:#57606a;border-color:#d0d7de}
.res-FAIL{color:#cf222e;font-weight:700}
.res-XPASS{color:#0969da;font-weight:700}
.ok{color:#1a7f37;font-weight:600}.bad{color:#cf222e;font-weight:600}
details{margin:.3rem 0}summary{cursor:pointer;font-weight:600}
#filter{margin:.6rem 0;padding:6px 10px;width:min(360px,90%);font-size:13px;
  border:1px solid #d0d7de;border-radius:6px}
.count{color:#656d76;font-weight:400;font-size:12px}
"""

_HTML_JS = """
function filt(v){v=v.toLowerCase();
  document.querySelectorAll('tr.f').forEach(function(r){
    r.style.display=r.textContent.toLowerCase().indexOf(v)>=0?'':'none';});}
"""


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
    bits = []
    if t.get("note"):
        bits.append(html.escape(t["note"]))
    if t.get("passes_on"):
        bits.append('<span class="muted">passes on '
                    + html.escape(_compact_wf_list(t["passes_on"])) + "</span>")
    return "<br>".join(bits) or '<span class="muted">\u2014</span>'


def render_html_report(run_ts: str, summary: list[dict], divergences: list[dict],
                       used_labels: Counter) -> str:
    esc = html.escape
    h: list[str] = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        f"<title>offload-test-suite report {esc(run_ts)}</title>",
        f"<style>{_HTML_CSS}</style>",
        "</head><body>",
        f"<h1>offload-test-suite scheduled-workflow report</h1>",
        f'<div class=meta>{esc(run_ts)} \u00b7 repo <code>{esc(REPO)}</code> '
        f"\u00b7 {len(summary)} scheduled workflows</div>",
    ]

    # Legend (collapsible).
    if used_labels:
        h.append("<details><summary>Classification legend "
                 f'<span class=count>({len(used_labels)} labels in this report)</span></summary>')
        h.append("<table><thead><tr><th>label</th><th>count</th><th>meaning</th></tr></thead><tbody>")
        for label, n in used_labels.most_common():
            expl = " ".join(CLASSIFICATION_LEGEND.get(label, "(no legend entry)").split())
            h.append(f"<tr><td>{_html_chip(label)}</td><td>{n}</td><td>{esc(expl)}</td></tr>")
        h.append("</tbody></table></details>")

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
             "<th>detail</th><th># fails</th><th>build (llvm / dxc)</th><th>run</th></tr></thead><tbody>")
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

    # Divergences.
    if divergences:
        h.append("<h2>Cross-workflow divergences "
                 '<span class=count>(GPU/API/compiler-specific failures)</span></h2>')
        h.append("<table><thead><tr><th>test</th><th>classification</th><th>axis</th>"
                 "<th>fails on</th><th>passes on</th></tr></thead><tbody>")
        for d in divergences:
            axis = "; ".join(f"{k.replace('_pattern','')}: {v}"
                             for k, v in (d.get("axes") or {}).items()) or "\u2014"
            h.append(
                f'<tr class=f><td class=test>{esc(d["test"])}</td>'
                f"<td>{_html_chip(d['classification'])}</td>"
                f"<td>{esc(axis)}</td>"
                f'<td class=note>{esc(_compact_wf_list(d["fails_on"]))}</td>'
                f'<td class=note>{esc(_compact_wf_list(d["passes_on"]))}</td></tr>')
        h.append("</tbody></table>")

    # Per-workflow failure detail (collapsible), with a shared filter box.
    workflows_with_tests = [r for r in summary if r.get("tests")]
    if workflows_with_tests:
        h.append("<h2>Failures by workflow</h2>")
        h.append('<input id=filter placeholder="filter tests / classification / issue\u2026" '
                 'oninput="filt(this.value)">')
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
    args = ap.parse_args()

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
                # Pick the axis that most tightly explains the divergence and
                # attribute the suspected layer accordingly:
                #   * gpu-aligned      -> per-vendor driver (runtime_driver_suspected)
                #   * api-aligned      -> API/backend layer  (api_backend_suspected)
                #   * compiler-aligned -> DXC vs clang-dxc frontend/codegen
                #                         (compiler_suspected) — same shader source,
                #                         same runtime, only the compiler differs
                #   * none of the above -> environment-dependent (runtime_driver_suspected)
                # More specific axes win: a per-vendor split is checked before an
                # API split, which is checked before a compiler split, so
                # compiler_suspected fires only for a clean compiler-only pattern
                # (heterogeneous api and gpu). Note: a GPU/API/compiler-specific
                # failure is not proof of a defect in that layer — the test itself
                # may specify pipeline inputs/outputs in a way only some
                # configurations reject — so these upgrades stay 'suspected',
                # never 'confirmed'.
                suffix = _RUNTIME_UPGRADE_SUFFIX.get(t["classification"])
                if suffix:
                    prefix = divergence_suspect_prefix(axes)
                    t["classification"] = f"{prefix}_{suffix}"
                if key not in seen_div:
                    seen_div.add(key)
                    divergences.append({
                        "suite": normalize_suite(t["suite"]), "test": t["test"],
                        "classification": t["classification"],
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

    md += ["| workflow | conclusion | category | detail | # test failures | build (llvm / dxc) | run |",
           "|---|---|---|---|---|---|---|"]
    for r in summary:
        cat = r.get("category") or ""
        det = r.get("detail") or ""
        n = len(r.get("tests") or [])
        build = _fmt_commits(r.get("commits") or {})
        md.append(f"| {r['workflow']} | {r['conclusion'] or r['status']} | {cat} | {det} | {n} | {build} | [run]({r['run_url']}) |")
    md.append("")

    if divergences:
        md += ["## Cross-workflow divergences (GPU/API/compiler-specific failures)", "",
               "Tests that fail on some workflows but pass on others. The same test source",
               "runs everywhere, so divergence points at something configuration-specific:",
               "a per-vendor driver bug, an API/backend bug, a compiler (DXC vs clang-dxc)",
               "codegen difference, or a test that specifies pipeline inputs/outputs in a way",
               "only some configurations reject. On the gpu/api axes the shader binary is",
               "identical across the diverging workflows; on the compiler axis the binary",
               "differs (DXC vs clang-dxc), which is itself the suspected cause. Labels stay",
               "'suspected'; none of this alone confirms a defect.",
               "The 'axis' column names the axis (api / gpu / compiler) on which the failure",
               "set is homogeneous — e.g. 'api: Vulkan-only' = all failing workflows are",
               "Vulkan, at least one non-Vulkan workflow passes. "
               "The 'fails on' / 'passes on' columns use compact `gpu/api/compiler` "
               "slugs (with the count) instead of full workflow names.",
               "",
               "| test | classification | axis | fails on | passes on |",
               "|---|---|---|---|---|"]
        for d in divergences:
            axis_bits = [f"{k.replace('_pattern','')}: {v}" for k, v in (d.get("axes") or {}).items()]
            axis_str = "; ".join(axis_bits) or "-"
            md.append(f"| `{d['test']}` | {d['classification']} | {axis_str} | "
                      f"{_compact_wf_list(d['fails_on'])} | {_compact_wf_list(d['passes_on'])} |")
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
            if t.get("passes_on"):
                ncell.append(f"passes on {_compact_wf_list(t['passes_on'])}")
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
    print(f"  {len(divergences)} cross-GPU divergences", file=sys.stderr)


if __name__ == "__main__":
    main()
