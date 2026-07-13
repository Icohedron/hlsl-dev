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
import io
import json
import os
import pathlib
import re
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
    if status == "0":
        return False
    # e.g. "1", "2", "0xc0000005", "3221225477"
    return True


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
    from a value/image mismatch (miscompile) vs unknown.
    """
    cmds = _parse_lit_commands(block)
    # Focus on the first failing non-compiler command
    failing = [c for c in cmds if _status_is_failure(c["exit_status"]) and c["kind"] not in ("dxc", "clang_dxc")]

    driver_markers = (
        "device removed", "device lost", "dxgi_error", "vk_error_device_lost",
        "hung", "tdr", "access violation", "unhandled exception",
        "driver crashed", "d3d12: removing device",
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
            if any(re.search(m, payload) for m in miscompile_markers):
                return "runtime_miscompile"
        if c["kind"] == "filecheck":
            return "runtime_miscompile"

    # Fallback on whole-block text
    lower = block.lower()
    if any(m in lower for m in driver_markers):
        return "runtime_driver_error"
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
            blocks.append({"result": "FAIL", "suite": suite, "test": test, "block": "\n".join(body_lines)})
        i += 1

    # XPASS blocks come from the summary; the individual test doesn't emit a
    # detailed block. Collect XPASSes from the "Unexpectedly Passed Tests" list.
    xpass_re = re.compile(r"^\s*XPASS:\s+(\S+)\s+::\s+(\S+)")
    for line in lines:
        m = xpass_re.match(strip_ts(line))
        if m:
            blocks.append({"result": "XPASS", "suite": m.group(1), "test": m.group(2), "block": ""})
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
        "clause's GitHub issue (chosen by evaluating the clause's lit feature "
        "expression against the actual test-runner's feature set — see the row's "
        "`linked_issue` and `xfail_match`).",

    # Upgraded labels — applied by the cross-workflow pivot after the pass
    # matrix reveals the same test passes on at least one other workflow.
    "runtime_driver_suspected_miscompile":
        "Base label was `runtime_miscompile`, but the SAME shader binary produces the "
        "right result on some other workflows. Every workflow compiles the exact same "
        "shader from the exact same source, so a value mismatch that only appears on "
        "some runners is almost certainly a per-vendor driver bug — hence 'driver "
        "suspected'. See the row's `axes` for the pattern (e.g. gpu_pattern: NVIDIA-only).",
    "runtime_driver_confirmed":
        "Base label was `runtime_driver_error`. The same test passes on other workflows, "
        "which confirms the crash is specific to some subset of GPUs/APIs — a real "
        "driver bug, not a spec issue or harness flake. `axes` tells you which subset.",
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
    "api_backend_confirmed":
        "Base label was `runtime_driver_error`, upgraded on the same API-axis criterion. "
        "A crash that happens on every driver behind one API but on none behind another "
        "isn't a per-driver bug — it's the API-backend triggering something drivers "
        "reject.",
    "api_backend_suspected_unknown":
        "Base label was `runtime_unknown`, upgraded via API-axis attribution. The "
        "API/backend correlates but the specific failure mode wasn't parsed from the log.",
}


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


# Workflow names encode (host, API, GPU vendor, compiler, variant) in their
# display name, e.g.:
#   "Windows Vulkan AMD Clang"           -> host=x64, api=Vulkan, gpu=AMD
#   "Windows ARM64 D3D12 Warp DXC"       -> host=ARM64
#   "Windows D3D12 AMD Clang GBV"        -> variant=GBV
#   "Windows D3D12 Warp Preview Clang"   -> variant=Preview
#   "macOS Metal DXC"                    -> host=macOS
_API_TOKENS = ("D3D12", "Vulkan", "Metal", "Lavapipe")
_GPU_TOKENS = ("AMD", "NVIDIA", "Intel", "QC", "Warp", "Lavapipe", "Metal")
_VARIANT_TOKENS = ("GBV", "Preview")


def parse_workflow_axes(name: str) -> dict[str, str]:
    """
    Best-effort split of a workflow's display name into axes we can pivot on.
    Returns keys 'api', 'gpu', 'compiler', 'host', 'variant' (each 'none' or
    'unknown' if missing).
    """
    api = next((t for t in _API_TOKENS if re.search(rf"\b{t}\b", name, re.I)), "unknown")
    gpu = "unknown"
    for t in _GPU_TOKENS:
        if re.search(rf"\b{t}\b", name, re.I) and t != api:
            gpu = t
            break
    if gpu == "unknown" and api in ("Lavapipe", "Metal"):
        # Lavapipe is a software Vulkan impl and Metal has no vendor token;
        # bucket the API as its own "GPU" for divergence purposes.
        gpu = api
    compiler = "clang" if re.search(r"\bClang\b", name) else ("dxc" if re.search(r"\bDXC\b", name) else "unknown")
    if re.search(r"\bARM64\b", name, re.I):
        host = "ARM64"
    elif re.search(r"\bmacOS\b", name, re.I):
        host = "macOS"
    elif re.search(r"\bWindows\b", name, re.I):
        host = "x64"
    else:
        host = "unknown"
    variant = next((t for t in _VARIANT_TOKENS if re.search(rf"\b{t}\b", name, re.I)), "none")
    return {"api": api, "gpu": gpu, "compiler": compiler, "host": host, "variant": variant}


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
        if len(fvals) == 1 and fvals and (pvals - fvals):
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


def parse_xfail_clauses(text: str) -> list[dict]:
    """
    Return one dict per XFAIL clause: {expr, issue_url|None, line_no}.
    The issue URL is the one on the nearest preceding comment line (searched
    backward, stopping at a blank line or a non-comment line).
    """
    lines = text.splitlines()
    out: list[dict] = []
    for i, line in enumerate(lines):
        m = XFAIL_LINE_RE.match(line)
        if not m:
            continue
        expr = m.group("expr").strip()
        # Walk backward for a linked issue URL. Stop at blank or non-comment.
        url = None
        for j in range(i - 1, max(-1, i - 10), -1):
            prev = lines[j].rstrip()
            if not prev.strip():
                break
            if not prev.lstrip().startswith("#"):
                break
            u = ISSUE_URL_RE.search(prev)
            if u:
                url = u.group(0)
                break
        out.append({"expr": expr, "issue_url": url, "line_no": i + 1})
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
    # API — lit uses "DirectX" for D3D12; Vulkan/Metal keep their names;
    # Lavapipe is a software Vulkan implementation, so lit sees Vulkan.
    api_map = {"D3D12": "DirectX", "Vulkan": "Vulkan", "Lavapipe": "Vulkan", "Metal": "Metal"}
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
    # GPU vendor
    if ax["gpu"] in ("AMD", "NVIDIA", "Intel", "QC", "Warp"):
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
    ("QC",       "x64"):   set(),
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
        result["matched_expr"] = matched[0]["expr"]
    elif len(matched) > 1:
        # Multiple clauses fire — pick the one with an issue URL if unique,
        # else emit them all.
        with_url = [m for m in matched if m["issue_url"]]
        if len(with_url) == 1:
            result["issue_url"] = with_url[0]["issue_url"]
            result["matched_expr"] = with_url[0]["expr"]
            result["note"] = f"{len(matched)} XFAIL clauses matched; used the one with a linked issue"
        else:
            result["note"] = f"{len(matched)} XFAIL clauses matched — ambiguous"
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

    build_failed_without_tests = False
    if not has_test_stage_output(log_text):
        build_failed_without_tests = True

    if build_failed_without_tests:
        kind = classify_build_failure(log_text)
        result["category"] = "build_failure"
        result["detail"] = kind
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
    for b in blocks:
        entry = {"result": b["result"], "suite": b["suite"], "test": b["test"]}
        if b["result"] == "XPASS":
            testfile = find_test_file(otss_root, b["suite"], b["test"])
            if testfile is None:
                entry["classification"] = "xpass"
                entry["note"] = "test file not found on disk"
            else:
                entry["test_file"] = str(testfile.relative_to(otss_root))
                try:
                    text = testfile.read_text(errors="replace")
                except OSError:
                    text = ""
                match = match_xpass_to_issue(text, workflow_name, test_runner) if text else None
                entry["classification"] = "xpass"
                if match is None:
                    entry["note"] = "could not read test file"
                elif not match.get("issue_url"):
                    entry["xfail_match"] = {"note": match["note"], "matched_expr": match.get("matched_expr")}
                    entry["note"] = match["note"]
                else:
                    url = match["issue_url"]
                    u = ISSUE_URL_RE.search(url)
                    key = (u.group("owner"), u.group("repo"), int(u.group("num")))
                    if key not in issue_cache:
                        issue_cache[key] = fetch_issue_state(gh, *key)
                    st = issue_cache[key] or {}
                    entry["linked_issue"] = {
                        "url": url,
                        "state": st.get("state"),
                        "state_reason": st.get("state_reason"),
                        "title": st.get("title"),
                    }
                    entry["xfail_match"] = {
                        "matched_expr": match["matched_expr"],
                        "note": match.get("note"),
                        "features": match["features"],
                    }
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

            # Feed the pass matrix from every completed run.
            for (suite, test), res in extract_all_results(log_text).items():
                matrix[(suite, test)][wf["name"]] = res

            # Classify only failing runs.
            if run["conclusion"] == "failure":
                row.update(classify_run(log_text, gh, otss_root, issue_cache, wf["name"]))
        summary.append(row)

    # ---- cross-GPU pivot: refine runtime_miscompile / runtime_driver_error ----
    seen_div: set[tuple[str, str]] = set()
    divergences: list[dict] = []
    for r in summary:
        for t in r.get("tests") or []:
            key = (t["suite"], t["test"])
            per_wf = matrix.get(key, {})
            passes_on = sorted(w for w, res in per_wf.items() if res == "PASS")
            fails_on = sorted(w for w, res in per_wf.items() if res in ("FAIL", "XPASS"))
            t["passes_on"] = passes_on
            t["fails_on"] = fails_on
            if passes_on and fails_on and t.get("classification", "").startswith("runtime_"):
                axes = attribute_divergence(fails_on, passes_on)
                t["axes"] = axes
                # If failures line up cleanly on the API axis (e.g. all Vulkan
                # fail, D3D12/Metal pass), that's a backend/API issue rather
                # than a per-vendor driver bug.
                if "api_pattern" in axes and "gpu_pattern" not in axes:
                    if t["classification"] == "runtime_miscompile":
                        t["classification"] = "api_backend_suspected_miscompile"
                    elif t["classification"] == "runtime_driver_error":
                        t["classification"] = "api_backend_confirmed"
                    elif t["classification"] == "runtime_unknown":
                        t["classification"] = "api_backend_suspected_unknown"
                else:
                    if t["classification"] == "runtime_miscompile":
                        t["classification"] = "runtime_driver_suspected_miscompile"
                    elif t["classification"] == "runtime_driver_error":
                        t["classification"] = "runtime_driver_confirmed"
                    elif t["classification"] == "runtime_unknown":
                        t["classification"] = "runtime_driver_suspected_unknown"
                if key not in seen_div:
                    seen_div.add(key)
                    divergences.append({
                        "suite": t["suite"], "test": t["test"],
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
        md += ["## Classification legend", "",
               "Every row's `category` / `classification` column uses one of the labels below.",
               "",
               "| label (count) | meaning |",
               "|---|---|"]
        for label, n in used_labels.most_common():
            expl = CLASSIFICATION_LEGEND.get(label, "*(no legend entry — please add one to CLASSIFICATION_LEGEND)*")
            # collapse newlines/multiple spaces so it fits in a table cell
            expl = " ".join(expl.split())
            md.append(f"| `{label}` (×{n}) | {expl} |")
        md.append("")
        md += ["Axes referenced above: **api** (D3D12 / Vulkan / Metal / Lavapipe), "
               "**gpu** (AMD / NVIDIA / Intel / QC / Warp / Lavapipe / Metal), "
               "**compiler** (clang / dxc), **host** (x64 / ARM64 / macOS), "
               "**variant** (GBV / Preview / none). A divergence row's `axes` "
               "dict names the axis on which the failing set is homogeneous — "
               "e.g. `api_pattern: Vulkan-only` means every failing workflow is "
               "Vulkan and at least one non-Vulkan workflow passes.",
               ""]

    md += ["| workflow | conclusion | category | detail | # test failures | run |",
           "|---|---|---|---|---|---|"]
    for r in summary:
        cat = r.get("category") or ""
        det = r.get("detail") or ""
        n = len(r.get("tests") or [])
        md.append(f"| {r['workflow']} | {r['conclusion'] or r['status']} | {cat} | {det} | {n} | [run]({r['run_url']}) |")
    md.append("")

    if divergences:
        md += ["## Cross-workflow divergences (likely driver / API-backend bugs)", "",
               "Tests that fail on some workflows but pass on others. Same shader binary is",
               "produced on all backends, so divergence usually points at the runtime.",
               "The 'axis' column names the axis (api / gpu / compiler) on which the failure",
               "set is homogeneous — e.g. 'api: Vulkan-only' = all failing workflows are",
               "Vulkan, at least one non-Vulkan workflow passes.",
               "",
               "| test | classification | axis | fails on | passes on |",
               "|---|---|---|---|---|"]
        for d in divergences:
            axis_bits = [f"{k.replace('_pattern','')}: {v}" for k, v in (d.get("axes") or {}).items()]
            axis_str = "; ".join(axis_bits) or "-"
            md.append(f"| `{d['suite']} :: {d['test']}` | {d['classification']} | {axis_str} | "
                      f"{', '.join(d['fails_on'])} | {', '.join(d['passes_on'])} |")
        md.append("")

    for r in summary:
        tests = r.get("tests") or []
        if not tests:
            continue
        md += [f"## {r['workflow']}", "",
               "| result | test | classification | note |",
               "|---|---|---|---|"]
        for t in tests:
            note = []
            if "linked_issue" in t:
                li = t["linked_issue"]
                note.append(f"issue [{li.get('state')}]({li['url']}) — {li.get('title','')}")
            if t.get("note"):
                note.append(t["note"])
            if t.get("passes_on"):
                note.append(f"passes on: {', '.join(t['passes_on'])}")
            md.append(f"| {t['result']} | `{t['suite']} :: {t['test']}` | {t.get('classification','')} | {' • '.join(note)} |")
        md.append("")
    (out_dir / "summary.md").write_text("\n".join(md))

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
                w.writerow([r["workflow"], r["conclusion"] or r["status"], r.get("category",""), r.get("detail",""),
                            t["result"], t["suite"], t["test"], t.get("classification",""),
                            ";".join(t.get("passes_on") or []), ";".join(t.get("fails_on") or []),
                            li.get("url",""), li.get("state",""), r["run_url"]])

    print(f"\nReport: {out_dir}", file=sys.stderr)
    print(f"  summary.md / summary.json / summary.csv / divergences.json", file=sys.stderr)
    print(f"  {len(divergences)} cross-GPU divergences", file=sys.stderr)


if __name__ == "__main__":
    main()
