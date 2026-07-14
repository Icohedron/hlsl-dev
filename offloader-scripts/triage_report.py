#!/usr/bin/env python3
"""
triage_report.py — triage a monitor_failures.py report.

Consumes one report directory produced by monitor_failures.py (and, when
available, its sibling report *history* under the same reports/ root) and
writes triage artifacts under <report>/triage/.

Triage strategies, dispatched by category/classification:

  * build_failure / shader_compile_dxc / shader_compile_clang_dxc
        -> COMMIT BISECT. Each scheduled run records, in its logs, the exact
           compiler commits it built ("Syncing repository: X" -> "HEAD is now
           at <sha>"). Because the monitor runs on a schedule, the report
           history lets us bound *when* a failure first appeared: the last
           report where the test/build passed and the first where it failed
           pin a good..bad range in llvm-project (clang / clang-dxc) or
           DirectXShaderCompiler (dxc) — with no building required. The range
           is then (optionally, agentically) narrowed to the first faulting
           commit by reading the diffs in the local checkout.

  * runtime_driver_suspected_* / api_backend_suspected_*
        -> EVIDENCE REPORT. The same shader binary is produced for every
           workflow, so a pass/fail split across GPUs/APIs is evidence about
           where the bug lives. We marshal that split + the failure excerpt
           into a writeup of the driver/backend-specific behaviour, with a
           theory when the evidence supports one (and the test-spec-bug
           alternative flagged when it can't be ruled out).

  * *_miscompile
        -> DXIL ANALYSIS. We locate the shader, compile it to DXIL locally,
           and speculate from the *disassembly*. The offload test suite is
           never run (the environment may have no GPU / software renderer):
           miscompiles are reasoned about by interpreting the DXIL statically.

Deterministic evidence gathering is stdlib-only and works fully headless. The
reasoning-heavy steps delegate to an agent (`pi -p`) when one is available;
with --no-agent the assembled evidence + a ready-to-use prompt are written for
later use instead.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import subprocess
import sys
import pathlib
from dataclasses import dataclass, field
from typing import Any

# Reuse the monitor's parsers so triage speaks the exact same vocabulary.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import monitor_failures as mf  # noqa: E402

# ---------------------------------------------------------------------------
# Repo topology
# ---------------------------------------------------------------------------

WORKSPACE = pathlib.Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class RepoCfg:
    dirname: str          # on-disk sibling checkout name / commits-map key
    slug: str             # github owner/repo for compare URLs


# repo dir basename (lowercased) -> config. Keys match how checkout logs name
# the repositories ("Syncing repository: llvm/llvm-project").
LLVM = RepoCfg("llvm-project", "llvm/llvm-project")
DXC = RepoCfg("DirectXShaderCompiler", "microsoft/DirectXShaderCompiler")
# (repo dir basename lowercased == commits-map key, e.g. "llvm-project", "directxshadercompiler")


def repo_for_failure(category: str | None, detail: str | None,
                     classification: str | None) -> RepoCfg | None:
    """Which source repo owns a build / shader-compile failure."""
    if category == "build_failure":
        return DXC if detail == "dxc" else LLVM  # clang_llvm / other / unknown
    if classification == "shader_compile_dxc":
        return DXC
    if classification == "shader_compile_clang_dxc":
        return LLVM
    return None


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Built compiler commits
# ---------------------------------------------------------------------------

# Newer reports store these per row (summary.json -> row["commits"]); older ones
# don't, so we can still recover them from the raw logs. The parser is canonical
# in monitor_failures so both tools agree on the format.
extract_built_commits = mf.extract_built_commits


# ---------------------------------------------------------------------------
# Report history model
# ---------------------------------------------------------------------------

_REPORT_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")


class Snapshot:
    """One report directory; lazily decompresses per-workflow logs."""

    def __init__(self, report_dir: pathlib.Path):
        self.dir = report_dir
        self.ts = report_dir.name
        rows = json.loads((report_dir / "summary.json").read_text())
        self.rows: list[dict] = rows
        self._by_wf = {r["workflow"]: r for r in rows}
        self._commits: dict[str, dict[str, str]] = {}
        self._results: dict[str, dict[tuple[str, str], str]] = {}
        self._logcache: dict[str, str | None] = {}

    def run_row(self, workflow: str) -> dict | None:
        return self._by_wf.get(workflow)

    def _log(self, workflow: str) -> str | None:
        if workflow in self._logcache:
            return self._logcache[workflow]
        row = self._by_wf.get(workflow)
        text: str | None = None
        if row and row.get("log_file"):
            path = self.dir / row["log_file"]
            if path.exists():
                try:
                    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except OSError:
                    text = None
        self._logcache[workflow] = text
        return text

    def commits(self, workflow: str) -> dict[str, str]:
        if workflow not in self._commits:
            row = self._by_wf.get(workflow) or {}
            stored = row.get("commits")
            if stored:
                self._commits[workflow] = stored          # recorded by the monitor
            else:
                text = self._log(workflow)                # older report: reparse logs
                self._commits[workflow] = extract_built_commits(text) if text else {}
        return self._commits[workflow]

    def results(self, workflow: str) -> dict[tuple[str, str], str]:
        if workflow not in self._results:
            text = self._log(workflow)
            self._results[workflow] = mf.extract_all_results(text) if text else {}
        return self._results[workflow]

    def test_result(self, workflow: str, suite: str, test: str) -> str | None:
        return self.results(workflow).get((suite, test))

    def is_build_failure(self, workflow: str) -> bool:
        row = self._by_wf.get(workflow)
        return bool(row and row.get("category") == "build_failure")

    def is_run_success(self, workflow: str) -> bool:
        row = self._by_wf.get(workflow)
        return bool(row and row.get("conclusion") == "success")


@dataclass
class Range:
    repo: RepoCfg | None
    good_sha: str | None
    bad_sha: str | None
    good_ts: str | None
    bad_ts: str | None
    note: str = ""

    @property
    def bounded(self) -> bool:
        return bool(self.repo and self.good_sha and self.bad_sha)

    @property
    def compare_url(self) -> str | None:
        if self.repo and self.good_sha and self.bad_sha:
            return f"https://github.com/{self.repo.slug}/compare/{self.good_sha}...{self.bad_sha}"
        return None


class History:
    def __init__(self, snaps: list[Snapshot], target_idx: int):
        self.snaps = snaps
        self.target_idx = target_idx

    def _bound(self, repo: RepoCfg | None, workflow: str,
               is_good, is_bad) -> Range:
        """Generic newest->oldest walk to find last-good / first-bad snapshots."""
        snaps = self.snaps
        bad_idx = self.target_idx
        good_idx: int | None = None
        for j in range(self.target_idx - 1, -1, -1):
            if is_good(snaps[j], workflow):
                good_idx = j
                break
            if is_bad(snaps[j], workflow):
                bad_idx = j
            else:
                break  # absent / ambiguous — stop rather than overreach

        key = repo.dirname.lower() if repo else None

        def sha_at(idx: int | None) -> str | None:
            if idx is None or not key:
                return None
            return snaps[idx].commits(workflow).get(key)

        note = ""
        if good_idx is None:
            note = ("no passing report in the available history — extend the "
                    "history or supply a known-good commit to bound the range")
        return Range(
            repo=repo,
            good_sha=sha_at(good_idx),
            bad_sha=sha_at(bad_idx),
            good_ts=snaps[good_idx].ts if good_idx is not None else None,
            bad_ts=snaps[bad_idx].ts,
            note=note,
        )

    def bound_test(self, repo: RepoCfg | None, workflow: str,
                   suite: str, test: str) -> Range:
        return self._bound(
            repo, workflow,
            is_good=lambda s, w: s.test_result(w, suite, test) == "PASS",
            is_bad=lambda s, w: s.test_result(w, suite, test) in ("FAIL", "XPASS"),
        )

    def bound_build(self, repo: RepoCfg | None, workflow: str) -> Range:
        return self._bound(
            repo, workflow,
            is_good=lambda s, w: s.is_run_success(w),
            is_bad=lambda s, w: s.is_build_failure(w),
        )


def load_history(report_dir: pathlib.Path) -> History:
    reports_root = report_dir.parent
    dirs = sorted(d for d in reports_root.iterdir()
                  if d.is_dir() and _REPORT_TS_RE.match(d.name))
    snaps: list[Snapshot] = []
    target_idx = 0
    for d in dirs:
        if not (d / "summary.json").exists():
            continue
        snap = Snapshot(d)
        if d.resolve() == report_dir.resolve():
            target_idx = len(snaps)
        snaps.append(snap)
    return History(snaps, target_idx)


# ---------------------------------------------------------------------------
# git helpers (bisect range resolution)
# ---------------------------------------------------------------------------

def _git(repo_root: pathlib.Path, *args: str) -> tuple[int, str, str]:
    p = subprocess.run(["git", "-C", str(repo_root), *args],
                       capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def sha_present(repo_root: pathlib.Path, sha: str) -> bool:
    code, _, _ = _git(repo_root, "cat-file", "-e", f"{sha}^{{commit}}")
    return code == 0


def ensure_history(repo_root: pathlib.Path, shas: list[str], allow_fetch: bool) -> str:
    """
    Make sure every sha resolves locally. Shallow checkouts need unshallowing;
    prefer `mask fetch-history <repo>` (documented workflow), fall back to a
    direct git fetch. Returns a human note about what happened.
    """
    missing = [s for s in shas if s and not sha_present(repo_root, s)]
    if not missing:
        return "all commits present locally"
    if not allow_fetch:
        return f"{len(missing)} commit(s) not present locally; fetch disabled (--no-fetch-history)"

    repo_dir = repo_root.name
    mask = shutil.which("mask")
    if mask:
        subprocess.run([mask, "fetch-history", repo_dir], cwd=str(WORKSPACE),
                       capture_output=True, text=True)
    if any(not sha_present(repo_root, s) for s in missing):
        # Fall back to a direct unshallow / full fetch.
        if _git(repo_root, "rev-parse", "--is-shallow-repository")[1].strip() == "true":
            _git(repo_root, "fetch", "--unshallow")
        _git(repo_root, "fetch", "--all", "--tags")

    still = [s for s in missing if not sha_present(repo_root, s)]
    if still:
        return f"could not resolve {len(still)} commit(s) locally even after fetch: {', '.join(still)}"
    return "fetched full history to resolve the range"


def git_range_commits(repo_root: pathlib.Path, good: str, bad: str,
                      limit: int = 400) -> tuple[list[tuple[str, str]], str | None]:
    code, out, err = _git(repo_root, "log", "--no-merges", "--oneline",
                          f"-{limit}", f"{good}..{bad}")
    if code != 0:
        return [], err.strip() or "git log failed"
    commits: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split(" ", 1)
        if parts:
            commits.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return commits, None


# ---------------------------------------------------------------------------
# Full failure block + shader / DXIL helpers
# ---------------------------------------------------------------------------

def full_failure_block(snap: Snapshot, workflow: str, suite: str, test: str) -> str | None:
    text = snap._log(workflow)
    if not text:
        return None
    for b in mf.extract_failure_blocks(text):
        if b["suite"] == suite and b["test"] == test:
            return b["block"]
    return None


def build_failure_excerpt(snap: Snapshot, workflow: str, before: int = 25,
                          after: int = 8) -> str | None:
    """Log lines around the first build-error marker (falls back to the log tail)."""
    text = snap._log(workflow)
    if not text:
        return None
    lines = text.splitlines()
    for idx, raw in enumerate(lines):
        line = mf.strip_ts(raw)
        for _kind, rx in mf.BUILD_FAIL_MARKERS:
            if rx.search(line):
                lo, hi = max(0, idx - before), min(len(lines), idx + after)
                return "\n".join(mf.strip_ts(l) for l in lines[lo:hi]) or None
    return "\n".join(mf.strip_ts(l) for l in lines[-40:]) or None


_SPLIT_MARK_RE = re.compile(r"^[#/;]{1,3}---\s+(\S+)\s*$")


def parse_split_file(text: str) -> dict[str, str]:
    """Parse the lit split-file layout used by .test / .test.yaml shaders."""
    sections: dict[str, list[str]] = {}
    cur: str | None = None
    for line in text.splitlines():
        m = _SPLIT_MARK_RE.match(line)
        if m:
            cur = m.group(1)
            sections.setdefault(cur, [])
            continue
        if cur is not None:
            sections[cur].append(line)
    return {k: "\n".join(v).strip("\n") for k, v in sections.items()}


_PROFILE_RE = re.compile(r"-T\s+([a-z]{2}_\d+_\d+)")
_ENTRY_RE = re.compile(r"-E\s+(\w+)")
_RUN_SRC_RE = re.compile(r"%t[\\/][^\s'\"]*\.hlsl")


def parse_run_compiles(test_text: str) -> list[dict[str, Any]]:
    """
    One entry per shader-compile RUN line: {src, profile, entry, defines}.
    Graphics tests split into several shaders (vertex.hlsl / pixel.hlsl / ...),
    each compiled by its own RUN line with its own profile and entry.
    """
    compiles: list[dict[str, Any]] = []
    for line in test_text.splitlines():
        if "RUN:" not in line:
            continue
        prof = _PROFILE_RE.search(line)
        if not prof:
            continue
        srcm = _RUN_SRC_RE.search(line)
        src = re.split(r"[\\/]", srcm.group(0))[-1] if srcm else None
        entry = _ENTRY_RE.search(line)
        compiles.append({
            "src": src,
            "profile": prof.group(1),
            "entry": entry.group(1) if entry else None,
            "defines": re.findall(r"-D\s*\S+", line),
        })
    return compiles


def compile_dxil(dxc_bin: str, workdir: pathlib.Path, src_name: str, source: str,
                 profile: str, entry: str | None,
                 defines: list[str]) -> tuple[str | None, str]:
    """Compile one HLSL -> DXIL disassembly (-Fc). Never executes the shader."""
    workdir.mkdir(parents=True, exist_ok=True)
    src = workdir / src_name
    out = workdir / (src_name + ".dxil.txt")
    src.write_text(source)
    cmd = [dxc_bin, "-T", profile, "-Fc", str(out)]
    if entry:
        cmd += ["-E", entry]
    for d in defines:
        cmd += d.replace("-D", "-D ", 1).split()
    cmd.append(str(src))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"dxc invocation failed: {e}"
    if p.returncode != 0 or not out.exists():
        return None, (p.stderr or p.stdout or "dxc returned non-zero").strip()
    return out.read_text(errors="replace"), p.stderr.strip()


# ---------------------------------------------------------------------------
# Agent backend
# ---------------------------------------------------------------------------

class Agent:
    def __init__(self, enabled: bool, model: str | None, timeout: int):
        self.model = model
        self.timeout = timeout
        self.bin = shutil.which("pi") if enabled else None
        self.enabled = bool(self.bin)

    def ask(self, prompt: str, cwd: pathlib.Path | None = None) -> str | None:
        if not self.enabled:
            return None
        cmd = [self.bin, "-p"]
        if self.model:
            cmd += ["--model", self.model]
        cmd.append(prompt)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=self.timeout,
                               cwd=str(cwd) if cwd else None)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"_(agent invocation failed: {e})_"
        if p.returncode != 0:
            return f"_(agent exited {p.returncode}: {p.stderr.strip()[:400]})_"
        return p.stdout.strip()


def _clip(text: str | None, n: int) -> str:
    if not text:
        return "(none)"
    return text if len(text) <= n else text[:n] + f"\n... [clipped, {len(text)} chars total]"


# ---------------------------------------------------------------------------
# Triagers — each returns a (markdown, meta) pair
# ---------------------------------------------------------------------------

def _slug(*parts: str) -> str:
    s = "__".join(p for p in parts if p)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")[:120]


def triage_bisect(ctx: "Ctx", workflow: str, category: str | None, detail: str | None,
                  suite: str, test: str, classification: str | None,
                  rng: "Range | None" = None) -> tuple[str, dict]:
    repo = repo_for_failure(category, detail, classification)
    kind = classification or category or "failure"
    if category == "build_failure":
        if rng is None:
            rng = ctx.history.bound_build(repo, workflow)
        signature = f"{workflow} build failed (detail={detail})"
        block = build_failure_excerpt(ctx.target, workflow)
    else:
        if rng is None:
            rng = ctx.history.bound_test(repo, workflow, suite, test)
        signature = f"{suite} :: {test} — {classification}"
        block = full_failure_block(ctx.target, workflow, suite, test)

    meta: dict[str, Any] = {"strategy": "bisect", "repo": repo.slug if repo else None,
                            "good_sha": rng.good_sha, "bad_sha": rng.bad_sha,
                            "good_report": rng.good_ts, "bad_report": rng.bad_ts,
                            "compare_url": rng.compare_url}

    md = [f"# Triage (bisect): {signature}", "",
          f"- **Workflow:** {workflow}",
          f"- **Kind:** `{kind}`",
          f"- **Owning repo:** {repo.slug if repo else 'unknown'}", ""]

    md += ["## Commit range", ""]
    if rng.good_sha:
        md.append(f"- last **passing** report `{rng.good_ts}` built `{rng.good_sha}`")
    else:
        md.append("- last passing report: **none in available history**")
    md.append(f"- first **failing** report `{rng.bad_ts}` built `{rng.bad_sha or '?'}`")
    if rng.compare_url:
        md.append(f"- compare: {rng.compare_url}")
    if rng.note:
        md.append(f"- note: {rng.note}")
    md.append("")

    commits: list[tuple[str, str]] = []
    fetch_note = ""
    if repo and rng.bounded:
        repo_root = ctx.repo_root(repo)
        fetch_note = ensure_history(repo_root, [rng.good_sha, rng.bad_sha], ctx.allow_fetch)
        commits, err = git_range_commits(repo_root, rng.good_sha, rng.bad_sha)
        md += ["## Candidate commits", "", f"_{fetch_note}_", ""]
        if err:
            md.append(f"Could not list commits locally: {err}. Use the compare URL above.")
        elif not commits:
            md.append("Range is empty — good and bad built the same commit; the "
                      "regression is likely in the test suite or environment, not the compiler.")
        else:
            md.append(f"{len(commits)} non-merge commits in range (newest first):")
            md.append("")
            for sha, subj in commits[:60]:
                md.append(f"- `{sha}` {subj}")
            if len(commits) > 60:
                md.append(f"- … and {len(commits) - 60} more")
        md.append("")
    meta["candidate_count"] = len(commits)

    # Agent step 1 — commit bisection. Only meaningful with a bounded good..bad
    # range; without a last-passing report there is no known-good commit to
    # bisect against, so skip it entirely rather than request a range-less bisect.
    if rng.good_sha:
        bisect_prompt = _bisect_prompt(kind, workflow, signature, repo, rng, commits, block)
        md += _agent_section(ctx, bisect_prompt,
                             cwd=ctx.repo_root(repo) if repo and rng.bounded else None,
                             heading="Suspected first-faulting commit")
    else:
        md += ["## Bisect skipped", "",
               "_No last-passing report in the available history, so there is no "
               "known-good commit to bound a bisect. Proceeding straight to "
               "reproduction and root-cause analysis._", ""]
    meta["bisect_requested"] = bool(rng.good_sha)

    # Agent step 2 — always reproduce the failure and determine a root cause.
    repro_prompt = _repro_prompt(kind, workflow, signature, category, detail, repo, rng, block)
    md += _agent_section(ctx, repro_prompt, cwd=WORKSPACE,
                         heading="Reproduction and root cause")
    return "\n".join(md), meta


def _bisect_prompt(kind, workflow, signature, repo, rng: Range,
                   commits, block) -> str:
    listing = "\n".join(f"{s} {t}" for s, t in commits[:80]) or "(range not resolvable locally)"
    return (
        "You are triaging a compiler regression by reading commit diffs. Do NOT "
        "build, run tests, or run the offload test suite.\n\n"
        f"Failure: {signature}\nKind: {kind}\nWorkflow: {workflow}\n"
        f"Repo: {repo.slug if repo else 'unknown'}\n"
        f"Last-good commit: {rng.good_sha}\nFirst-bad commit: {rng.bad_sha}\n\n"
        "Candidate commits in range (newest first, `git show <sha>` to inspect):\n"
        f"{listing}\n\n"
        "Failure log block:\n"
        f"{_clip(block, 3000)}\n\n"
        "Identify the SINGLE most likely first-faulting commit. Prefer commits that "
        "touch code paths named in the failure. Output markdown:\n"
        "## Suspected first-faulting commit\n`<sha>` — <subject>\n"
        "## Reasoning\n- ...\n## Confidence\nlow | medium | high\n"
    )


def _repro_prompt(kind, workflow, signature, category, detail, repo,
                  rng: Range, block) -> str:
    """Ask the agent to reproduce the failure locally and pin the root cause.

    Runs regardless of whether a bisect was possible: even a bounded range only
    narrows *where* the regression entered; this step nails down *why* it fails.
    """
    is_build = category == "build_failure"
    what = "build" if is_build else "shader compilation"
    if is_build:
        repo_dir = repo.dirname if repo else "the affected checkout"
        how = (
            f"The failure is a compiler **build** break in `{repo_dir}` (detail={detail}). "
            "Reproduce it by configuring and building from the workspace root:\n"
            "  - llvm/llvm-project: `mask configure-llvm` then `mask build-llvm` "
            "(narrow with e.g. `mask build-llvm clang`)\n"
            "  - microsoft/DirectXShaderCompiler: `mask configure-dxc` then `mask build-dxc`\n"
            "Then read the first real compiler/linker error and trace it to source."
        )
    else:
        how = (
            "The failure is a **shader compilation** error. Reproduce it by locating the "
            "offload-test-suite test and invoking the compiler on its shader with the same "
            "`-T` profile, `-E` entry, and `-D` defines from the test's RUN line. A prebuilt "
            "`dxc` / `clang-dxc` may already exist under the relevant `build/bin` directory."
        )
    if is_build:
        mintc = ("Reduce the failure to a **minimal reproducible test case**: the smallest "
                 "self-contained input (e.g. a trimmed source snippet or preprocessed "
                 "translation unit) that, fed to the built compiler, still triggers the same "
                 "error. Give the exact command and the minimized input.")
    else:
        mintc = ("Reduce the failure to a **minimal reproducible test case**: the smallest "
                 "self-contained HLSL shader that still reproduces the compile error, plus the "
                 "exact compile command (profile / entry / defines). Strip unrelated "
                 "functions, resources, and inputs while confirming the error persists.")
    return (
        f"You are triaging a {what} failure. Reproduce it locally and determine the ROOT "
        "CAUSE. You MAY build the compiler and re-run the failing compilation. Do NOT run "
        "the offload test suite or any GPU workload.\n\n"
        f"Failure: {signature}\nKind: {kind}\nWorkflow: {workflow}\n"
        f"Repo: {repo.slug if repo else 'unknown'}\n"
        f"Failing (bad) commit: {rng.bad_sha or 'unknown'}\n"
        f"Last-good commit: {rng.good_sha or '(none in available history)'}\n\n"
        f"{how}\n\n"
        "Failure log excerpt:\n"
        f"{_clip(block, 3000)}\n\n"
        f"{mintc}\n\n"
        "Steps: (1) reproduce the failure and quote the exact error you observe; (2) read the "
        "relevant source to explain WHY it fails; (3) state the root cause; (4) reduce it to a "
        "minimal reproducible test case as described above. Output markdown:\n"
        "## Reproduction\n- commands run and the observed error\n"
        "## Root cause\n- ...\n"
        "## Minimal reproducible test case\n- exact command\n```\n<minimized source>\n```\n"
        "## Fix suggestion\n- ...\n## Confidence\nlow | medium | high\n"
    )


def triage_evidence(ctx: "Ctx", workflow: str, suite: str, test: str,
                    classification: str, div: dict | None) -> tuple[str, dict]:
    is_api = classification.startswith("api_backend")
    layer = "API/backend" if is_api else "per-vendor driver"
    fails_on = (div or {}).get("fails_on") or [workflow]
    passes_on = (div or {}).get("passes_on") or []
    axes = (div or {}).get("axes") or {}
    block = full_failure_block(ctx.target, workflow, suite, test)

    meta = {"strategy": "evidence", "layer": layer,
            "fails_on": fails_on, "passes_on": passes_on, "axes": axes}

    md = [f"# Triage (evidence): {suite} :: {test}", "",
          f"- **Classification:** `{classification}`",
          f"- **Suspected layer:** {layer}",
          f"- **Fails on:** {', '.join(fails_on) or '(this workflow only)'}",
          f"- **Passes on:** {', '.join(passes_on) or '(no passing peer in report)'}",
          f"- **Divergence axes:** {axes or '(none — no clean axis)'}", "",
          "## Why this points where it does", "",
          "Every workflow compiles the **same shader** from identical source, so the "
          "binary under test is byte-identical across the split above. A result that "
          f"differs only on a subset therefore isolates the fault to the {layer} layer "
          "rather than to shader codegen. This is evidence, **not proof**: an "
          "under-specified pipeline (uninitialised inputs, too-tight tolerance, wrong "
          "bindings/buffer sizes) can also be tolerated by lenient runtimes and rejected "
          "by stricter ones.", "",
          "## Failure excerpt", "", "```", _clip(block, 3500), "```", ""]

    prompt = (
        f"Write an evidence report for a suspected {layer}-specific test failure. Do NOT "
        "run the offload test suite or any GPU workload.\n\n"
        f"Test: {suite} :: {test}\nClassification: {classification}\n"
        f"Fails on: {fails_on}\nPasses on: {passes_on}\nDivergence axes: {axes}\n\n"
        "The SAME shader binary is produced for every workflow. Failure log block:\n"
        f"{_clip(block, 3500)}\n\n"
        "Explain what the pass/fail split proves about where the bug lives; give a "
        "plausible theory ONLY if the evidence supports one; and explicitly flag the "
        "test-spec-bug alternative when it cannot be ruled out. Output markdown with "
        "sections: ## Evidence, ## Interpretation, ## Theory, ## Caveats.\n"
    )
    md += _agent_section(ctx, prompt, cwd=ctx.otss_root, heading="Analysis")
    return "\n".join(md), meta


def triage_miscompile(ctx: "Ctx", workflow: str, suite: str, test: str,
                      classification: str, div: dict | None) -> tuple[str, dict]:
    fails_on = (div or {}).get("fails_on") or [workflow]
    passes_on = (div or {}).get("passes_on") or []
    axes = (div or {}).get("axes") or {}
    block = full_failure_block(ctx.target, workflow, suite, test)

    meta: dict[str, Any] = {"strategy": "dxil", "fails_on": fails_on,
                            "passes_on": passes_on, "axes": axes}

    md = [f"# Triage (DXIL): {suite} :: {test}", "",
          f"- **Classification:** `{classification}`",
          f"- **Fails on:** {', '.join(fails_on)}",
          f"- **Passes on:** {', '.join(passes_on) or '(none)'}",
          f"- **Divergence axes:** {axes or '(none)'}", "",
          "> The offload test suite is **not** run here (no GPU/renderer assumed). The "
          "miscompile is reasoned about by compiling the shader and interpreting the "
          "resulting DXIL statically.", ""]

    testfile = mf.find_test_file(ctx.otss_root, suite, test)
    pipeline = None
    shaders: list[dict[str, Any]] = []  # {name, source, profile, entry, dxil, err}
    if testfile is None:
        md += ["## Shader", "", "Test file not found on disk — cannot extract the shader.", ""]
    else:
        meta["test_file"] = str(testfile.relative_to(ctx.otss_root))
        text = testfile.read_text(errors="replace")
        sections = parse_split_file(text)
        pipeline = sections.get("pipeline.yaml")
        hlsl = {n: b for n, b in sections.items() if n.endswith(".hlsl")}
        compiles = parse_run_compiles(text)
        # Match each shader section to its compile RUN line (by filename); fall
        # back to the sole compile when the section is unambiguous.
        for name, body in hlsl.items():
            info = next((c for c in compiles if c["src"] == name), None)
            if info is None and len(compiles) == 1:
                info = compiles[0]
            profile = info["profile"] if info else None
            entry = info["entry"] if info else None
            dxil = err = None
            if profile and ctx.dxc_bin:
                workdir = ctx.triage_dir / "dxil" / _slug(suite, test)
                dxil, err = compile_dxil(ctx.dxc_bin, workdir, name, body, profile,
                                         entry, info["defines"] if info else [])
            shaders.append({"name": name, "source": body, "profile": profile,
                            "entry": entry, "dxil": dxil, "err": err})

        meta["shaders"] = [s["name"] for s in shaders]
        meta["dxil_compiled"] = [s["name"] for s in shaders if s["dxil"]]
        meta["dxil_compiler"] = ctx.dxc_bin
        shader_descs = [f"{s['name']} ({s['profile']}/{s['entry'] or 'main'})" for s in shaders]
        md += [f"- **Test file:** `{meta['test_file']}`",
               f"- **Shaders:** {', '.join(shader_descs) or '(none found)'}", ""]
        md += ["## Pipeline (inputs / expected)", "", "```yaml", _clip(pipeline, 2500), "```", ""]
        for s in shaders:
            md += [f"## Shader `{s['name']}`", "", "```hlsl", _clip(s["source"], 2500), "```", "",
                   "### DXIL disassembly", ""]
            if s["dxil"]:
                md += [f"_compiled with `{ctx.dxc_bin}` (proxy — the failing path may be "
                       "SPIR-V/Metal; DXIL still shows codegen intent)_", "",
                       "```llvm", _clip(s["dxil"], 5000), "```", ""]
            else:
                md += [f"Could not produce DXIL: {s['err'] or 'no dxc / missing profile'}", ""]

    def _shader_dump(src_clip: int, dxil_clip: int) -> str:
        parts = []
        for s in shaders:
            parts.append(f"### {s['name']} ({s['profile']}/{s['entry'] or 'main'})\n"
                         f"HLSL:\n{_clip(s['source'], src_clip)}\n"
                         f"DXIL:\n{_clip(s['dxil'], dxil_clip)}")
        return "\n\n".join(parts) or "(no shader/DXIL available)"

    prompt = (
        "A shader compiles cleanly but produces wrong results on some GPUs/APIs. "
        "Speculate on the cause by interpreting the DXIL STATICALLY. Do NOT run the "
        "shader or the offload test suite.\n\n"
        f"Test: {suite} :: {test}\nFails on: {fails_on}\nPasses on: {passes_on}\nAxes: {axes}\n\n"
        f"Pipeline (inputs/expected):\n{_clip(pipeline, 1800)}\n\n"
        f"Shaders:\n{_shader_dump(2200, 4500)}\n\n"
        f"Failure block:\n{_clip(block, 2000)}\n\n"
        "Look for anything in the DXIL that could plausibly explain a per-driver / "
        "per-backend value divergence: precision/rounding (fast-math, fp16/relaxed), "
        "operation ordering, uninitialised or under-specified values, resource/UAV "
        "state, undefined behaviour, or ops with implementation-defined results. Note "
        "the DXIL is a proxy if the failing path is SPIR-V. Output markdown: "
        "## DXIL observations, ## Hypotheses, ## What to check next.\n"
    )
    md += _agent_section(ctx, prompt, cwd=ctx.otss_root, heading="DXIL interpretation")
    return "\n".join(md), meta


def _agent_section(ctx: "Ctx", prompt: str, cwd, heading: str) -> list[str]:
    out = ctx.agent.ask(prompt, cwd=cwd)
    if out:
        return [f"## {heading} (agent)", "", out, ""]
    # No agent: emit the ready-to-run prompt so a human/agent can finish later.
    return [f"## {heading}", "",
            "_No agent available (`--no-agent` or `pi` not found). Prompt for manual "
            "follow-up:_", "", "```", prompt.strip(), "```", ""]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

@dataclass
class Ctx:
    history: History
    target: Snapshot
    triage_dir: pathlib.Path
    otss_root: pathlib.Path
    llvm_root: pathlib.Path
    dxc_root: pathlib.Path
    dxc_bin: str | None
    agent: Agent
    allow_fetch: bool
    divergences: dict[tuple[str, str], dict] = field(default_factory=dict)

    def repo_root(self, repo: RepoCfg) -> pathlib.Path:
        return self.llvm_root if repo is LLVM else self.dxc_root


BISECT_CLS = {"shader_compile_dxc", "shader_compile_clang_dxc"}


def triage_report(report_dir: pathlib.Path, args) -> dict:
    history = load_history(report_dir)
    target = history.snaps[history.target_idx]
    triage_dir = report_dir / "triage"
    triage_dir.mkdir(exist_ok=True)

    divergences: dict[tuple[str, str], dict] = {}
    dpath = report_dir / "divergences.json"
    if dpath.exists():
        for d in json.loads(dpath.read_text()):
            divergences[(d["suite"], d["test"])] = d

    ctx = Ctx(
        history=history, target=target, triage_dir=triage_dir,
        otss_root=pathlib.Path(args.otss_root).resolve(),
        llvm_root=pathlib.Path(args.llvm_root).resolve(),
        dxc_root=pathlib.Path(args.dxc_root).resolve(),
        dxc_bin=args.dxc_bin or shutil.which("dxc"),
        agent=Agent(not args.no_agent, args.agent_model, args.agent_timeout),
        allow_fetch=not args.no_fetch_history,
        divergences=divergences,
    )

    items: list[dict] = []
    seen: dict[Any, dict] = {}   # dedup key -> already-emitted item meta
    n = 0
    deduped = 0

    def _repo_slug(repo: RepoCfg | None) -> str | None:
        return repo.slug if repo else None

    for row in target.rows:
        wf = row["workflow"]
        category = row.get("category")
        detail = row.get("detail")

        if category == "build_failure":
            repo = repo_for_failure(category, detail, None)
            rng = ctx.history.bound_build(repo, wf)
            # Same repo + same good..bad commit range == the same build break,
            # regardless of which workflow surfaced it. Only merge when we could
            # actually pin commits; otherwise keep them separate (can't prove same).
            if rng.good_sha or rng.bad_sha:
                key = ("build", _repo_slug(repo), rng.good_sha, rng.bad_sha)
            else:
                key = ("build", _repo_slug(repo), detail, wf)
            dup = seen.get(key)
            if dup is not None:
                _mark_shared(dup, wf)
                deduped += 1
                print(f"  [dedup   ] {wf} shares build triage -> triage/{dup['file']}",
                      file=sys.stderr)
                continue
            _emit(ctx, items, "bisect", wf,
                  triage_bisect(ctx, wf, category, detail, "", "", None, rng),
                  _slug("build", wf))
            seen[key] = items[-1]
            n += 1
            if args.max and n >= args.max:
                break
            continue

        for t in row.get("tests") or []:
            cls = t.get("classification") or ""
            suite, test = t["suite"], t["test"]
            div = divergences.get((suite, test))
            if cls in BISECT_CLS:
                repo = repo_for_failure(category, detail, cls)
                rng = ctx.history.bound_test(repo, wf, suite, test)
                if rng.good_sha or rng.bad_sha:
                    key = ("shader", _repo_slug(repo), suite, test,
                           rng.good_sha, rng.bad_sha)
                else:
                    key = ("shader", suite, test, cls, wf)
                make = lambda: triage_bisect(ctx, wf, category, detail,
                                             suite, test, cls, rng)
            elif cls.endswith("_miscompile"):
                # DXIL analysis is per-(test, classification); workflow-independent.
                key = ("miscompile", suite, test, cls)
                make = lambda: triage_miscompile(ctx, wf, suite, test, cls, div)
            elif cls.startswith(("runtime_driver_suspected", "api_backend_suspected")):
                # Evidence report reasons over the whole pass/fail split (div),
                # so it only needs producing once per (test, classification).
                key = ("evidence", suite, test, cls)
                make = lambda: triage_evidence(ctx, wf, suite, test, cls, div)
            else:
                continue  # xpass etc. — already carries linked issues

            dup = seen.get(key)
            if dup is not None:
                _mark_shared(dup, wf)
                deduped += 1
                print(f"  [dedup   ] {wf} shares {suite}::{test} triage "
                      f"-> triage/{dup['file']}", file=sys.stderr)
                continue

            res = make()
            _emit(ctx, items, res[1]["strategy"], wf, res, _slug(wf, suite, test))
            seen[key] = items[-1]
            n += 1
            if args.max and n >= args.max:
                break
        if args.max and n >= args.max:
            break

    _write_index(ctx, items, target)
    (triage_dir / "triage.json").write_text(json.dumps(items, indent=2))
    return {"triaged": len(items), "deduped": deduped, "dir": str(triage_dir)}


def _emit(ctx: Ctx, items: list[dict], strategy: str, workflow: str,
          res: tuple[str, dict], slug: str) -> None:
    md, meta = res
    fname = f"{strategy}__{slug}.md"
    (ctx.triage_dir / fname).write_text(md)
    meta.update({"workflow": workflow, "file": fname})
    items.append(meta)
    print(f"  [{strategy:8s}] {workflow} -> triage/{fname}", file=sys.stderr)


def _mark_shared(item: dict, workflow: str) -> None:
    """Record that another workflow hit the same failure this item already covers."""
    shared = item.setdefault("shared_workflows", [])
    if workflow != item.get("workflow") and workflow not in shared:
        shared.append(workflow)


def _write_index(ctx: Ctx, items: list[dict], target: Snapshot) -> None:
    total_shared = sum(len(it.get("shared_workflows") or []) for it in items)
    md = [f"# Triage — {target.ts}", "",
          f"{len(items)} item(s)"
          f"{f' (collapsed {total_shared} duplicate workflow(s))' if total_shared else ''}"
          f". Agent: "
          f"{'on' if ctx.agent.enabled else 'off'}"
          f"{' (' + ctx.agent.model + ')' if ctx.agent.model else ''}.", "",
          "| strategy | workflow | detail | file |", "|---|---|---|---|"]
    for it in items:
        detail = it.get("repo") or it.get("layer") or it.get("test_file") or ""
        shared = it.get("shared_workflows") or []
        wf = it["workflow"] + (f" (+{len(shared)})" if shared else "")
        md.append(f"| {it['strategy']} | {wf} | {detail} | "
                  f"[{it['file']}]({it['file']}) |")
    (ctx.triage_dir / "README.md").write_text("\n".join(md) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report", help="path to a report directory (contains summary.json)")
    ap.add_argument("--otss-root", default=str(WORKSPACE / "offload-test-suite"))
    ap.add_argument("--llvm-root", default=str(WORKSPACE / "llvm-project"))
    ap.add_argument("--dxc-root", default=str(WORKSPACE / "DirectXShaderCompiler"))
    ap.add_argument("--dxc-bin", default=None, help="dxc for DXIL disassembly (default: PATH)")
    ap.add_argument("--no-agent", action="store_true", help="don't call pi; emit prompts instead")
    ap.add_argument("--agent-model", default=None, help="model override for the agent")
    ap.add_argument("--agent-timeout", type=int, default=900)
    ap.add_argument("--no-fetch-history", action="store_true",
                    help="don't unshallow repos to resolve commit ranges")
    ap.add_argument("--max", type=int, default=0, help="cap number of items triaged (0 = all)")
    args = ap.parse_args()

    report_dir = pathlib.Path(args.report).resolve()
    if not (report_dir / "summary.json").exists():
        raise SystemExit(f"not a report directory (no summary.json): {report_dir}")

    result = triage_report(report_dir, args)
    dd = result.get("deduped") or 0
    print(f"\nTriage: {result['triaged']} item(s)"
          f"{f' ({dd} duplicate workflow(s) collapsed)' if dd else ''}"
          f" -> {result['dir']}", file=sys.stderr)


if __name__ == "__main__":
    main()
