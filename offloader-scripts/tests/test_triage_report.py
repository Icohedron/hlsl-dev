"""Regression tests for offloader-scripts/triage_report.py.

Covers the deterministic (offline, stdlib-only) triage logic: built-commit
extraction from logs, repo dispatch, split-file / RUN-line parsing, and the
report-history commit-range bounding. The reasoning-heavy agent, git-range,
and DXIL-compile steps shell out to external tools and are not exercised here.

Run:  python3 -m unittest discover -s offloader-scripts/tests -v
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import triage_report as tr  # noqa: E402


# ---------------------------------------------------------------------------
# extract_built_commits
# ---------------------------------------------------------------------------

class BuiltCommits(unittest.TestCase):
    LOG = "\n".join([
        "2026-07-14T06:58:33.3561160Z   repository: Microsoft/DirectXShaderCompiler",
        "2026-07-14T06:58:33.6135422Z Syncing repository: Microsoft/DirectXShaderCompiler",
        "2026-07-14T06:58:33.8811631Z HEAD is now at dc3e6c48 [SM6.10] LinAlg (#8608)",
        "2026-07-14T06:58:37.3075250Z HEAD is now at dc3e6c48 [SM6.10] LinAlg (#8608)",
        "2026-07-14T06:58:46.3415510Z   repository: llvm/llvm-project",
        "2026-07-14T06:58:46.4303091Z Syncing repository: llvm/llvm-project",
        "2026-07-14T06:58:47.0567699Z HEAD is now at f60650c77 [BitcodeReader] (#208175)",
        "2026-07-14T06:59:03.4109423Z   repository: llvm/offload-test-suite",
        "2026-07-14T06:59:03.5020581Z Syncing repository: llvm/offload-test-suite",
        "2026-07-14T06:59:03.6922551Z HEAD is now at bda4d3e [VK] XFAIL (#1369)",
    ])

    def test_attributes_sha_to_repo(self):
        c = tr.extract_built_commits(self.LOG)
        self.assertEqual(c["directxshadercompiler"], "dc3e6c48")
        self.assertEqual(c["llvm-project"], "f60650c77")
        self.assertEqual(c["offload-test-suite"], "bda4d3e")

    def test_first_sha_wins_on_duplicate_head_lines(self):
        # dc3e6c48 is logged twice (fetch + checkout); must not be overwritten.
        self.assertEqual(tr.extract_built_commits(self.LOG)["directxshadercompiler"], "dc3e6c48")

    def test_empty_log(self):
        self.assertEqual(tr.extract_built_commits(""), {})

    def test_head_without_repo_decl_is_ignored(self):
        self.assertEqual(tr.extract_built_commits("HEAD is now at deadbeef x"), {})


# ---------------------------------------------------------------------------
# repo_for_failure
# ---------------------------------------------------------------------------

class RepoDispatch(unittest.TestCase):
    def test_build_failure_dxc(self):
        self.assertIs(tr.repo_for_failure("build_failure", "dxc", None), tr.DXC)

    def test_build_failure_clang_llvm_and_unknown_go_to_llvm(self):
        self.assertIs(tr.repo_for_failure("build_failure", "clang_llvm", None), tr.LLVM)
        self.assertIs(tr.repo_for_failure("build_failure", "other", None), tr.LLVM)
        self.assertIs(tr.repo_for_failure("build_failure", None, None), tr.LLVM)

    def test_shader_compile_dxc(self):
        self.assertIs(tr.repo_for_failure("test_failure", None, "shader_compile_dxc"), tr.DXC)

    def test_shader_compile_clang(self):
        self.assertIs(tr.repo_for_failure("test_failure", None, "shader_compile_clang"), tr.LLVM)

    def test_runtime_classification_has_no_repo(self):
        self.assertIsNone(tr.repo_for_failure("test_failure", None, "runtime_driver_suspected_crash"))


# ---------------------------------------------------------------------------
# split-file + RUN-line parsing
# ---------------------------------------------------------------------------

SINGLE_TEST = """\
#--- source.hlsl
[numthreads(1,1,1)]
void main() {}
//--- pipeline.yaml
Shaders: []
#--- end

# RUN: split-file %s %t
# RUN: %dxc_target -T cs_6_0 -Fo %t.o %t/source.hlsl
# RUN: not %offloader %t/pipeline.yaml %t.o
"""

GRAPHICS_TEST = """\
#--- vertex.hlsl
float4 mainVS() : SV_Position { return 0; }
#--- pixel.hlsl
float4 mainPS() : SV_Target { return 0; }
//--- pipeline.yaml
Shaders: []
#--- end

# RUN: split-file %s %t
# RUN: %dxc_target -T vs_6_0 -E mainVS -Fo %t-vs.o %t/vertex.hlsl
# RUN: %dxc_target -T ps_6_0 -E mainPS -Fo %t-ps.o %t/pixel.hlsl
"""


class SplitFile(unittest.TestCase):
    def test_single_source(self):
        s = tr.parse_split_file(SINGLE_TEST)
        self.assertIn("source.hlsl", s)
        self.assertIn("void main()", s["source.hlsl"])
        self.assertIn("pipeline.yaml", s)

    def test_multi_shader_sections(self):
        s = tr.parse_split_file(GRAPHICS_TEST)
        self.assertEqual({k for k in s if k.endswith(".hlsl")}, {"vertex.hlsl", "pixel.hlsl"})
        self.assertIn("mainVS", s["vertex.hlsl"])


class RunCompiles(unittest.TestCase):
    def test_single(self):
        c = tr.parse_run_compiles(SINGLE_TEST)
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]["profile"], "cs_6_0")
        self.assertEqual(c[0]["src"], "source.hlsl")
        self.assertIsNone(c[0]["entry"])

    def test_multi_maps_profile_and_entry_per_shader(self):
        c = tr.parse_run_compiles(GRAPHICS_TEST)
        by_src = {x["src"]: x for x in c}
        self.assertEqual(by_src["vertex.hlsl"]["profile"], "vs_6_0")
        self.assertEqual(by_src["vertex.hlsl"]["entry"], "mainVS")
        self.assertEqual(by_src["pixel.hlsl"]["profile"], "ps_6_0")
        self.assertEqual(by_src["pixel.hlsl"]["entry"], "mainPS")

    def test_ignores_non_compile_run_lines(self):
        # split-file / offloader lines carry no -T and must be skipped.
        self.assertEqual(len(tr.parse_run_compiles(SINGLE_TEST)), 1)


# ---------------------------------------------------------------------------
# History commit-range bounding (fakes avoid touching the filesystem)
# ---------------------------------------------------------------------------

class FakeSnap:
    """Stand-in for tr.Snapshot with canned results/commits."""

    def __init__(self, ts, result=None, commit=None, build_fail=False, success=False):
        self.ts = ts
        self._result = result
        self._commit = commit
        self._build_fail = build_fail
        self._success = success

    def test_result(self, wf, suite, test):
        return self._result

    def commits(self, wf):
        return {"llvm-project": self._commit} if self._commit else {}

    def is_build_failure(self, wf):
        return self._build_fail

    def is_run_success(self, wf):
        return self._success


class BoundRange(unittest.TestCase):
    def test_bounds_pass_to_fail_transition(self):
        snaps = [
            FakeSnap("t0", result="PASS", commit="good0"),
            FakeSnap("t1", result="PASS", commit="goodA"),   # last good
            FakeSnap("t2", result="FAIL", commit="badB"),    # first bad
            FakeSnap("t3", result="FAIL", commit="badC"),    # target
        ]
        h = tr.History(snaps, target_idx=3)
        rng = h.bound_test(tr.LLVM, "wf", "S", "T")
        self.assertTrue(rng.bounded)
        self.assertEqual(rng.good_sha, "goodA")
        self.assertEqual(rng.bad_sha, "badB")   # earliest bad in the fail streak
        self.assertEqual(rng.good_ts, "t1")
        self.assertEqual(rng.bad_ts, "t2")
        self.assertIn("/compare/goodA...badB", rng.compare_url)

    def test_unbounded_when_no_prior_pass(self):
        snaps = [
            FakeSnap("t0", result="FAIL", commit="badA"),
            FakeSnap("t1", result="FAIL", commit="badB"),   # target
        ]
        h = tr.History(snaps, target_idx=1)
        rng = h.bound_test(tr.LLVM, "wf", "S", "T")
        self.assertFalse(rng.bounded)
        self.assertIsNone(rng.good_sha)
        self.assertEqual(rng.bad_sha, "badA")
        self.assertIn("no passing report", rng.note)

    def test_absent_result_stops_the_walk(self):
        # A gap (workflow absent / not run) must not be treated as still-failing.
        snaps = [
            FakeSnap("t0", result="PASS", commit="old"),
            FakeSnap("t1", result=None, commit=None),       # absent -> boundary
            FakeSnap("t2", result="FAIL", commit="badB"),   # target
        ]
        h = tr.History(snaps, target_idx=2)
        rng = h.bound_test(tr.LLVM, "wf", "S", "T")
        self.assertFalse(rng.bounded)   # cannot see the "old" PASS across the gap

    def test_build_failure_transition(self):
        snaps = [
            FakeSnap("t0", success=True, commit="goodA"),
            FakeSnap("t1", build_fail=True, commit="badB"),  # target
        ]
        h = tr.History(snaps, target_idx=1)
        rng = h.bound_build(tr.LLVM, "wf")
        self.assertTrue(rng.bounded)
        self.assertEqual(rng.good_sha, "goodA")
        self.assertEqual(rng.bad_sha, "badB")


class CompareUrl(unittest.TestCase):
    def test_url_uses_repo_slug(self):
        rng = tr.Range(repo=tr.DXC, good_sha="aaa", bad_sha="bbb", good_ts="t0", bad_ts="t1")
        self.assertEqual(rng.compare_url,
                         "https://github.com/microsoft/DirectXShaderCompiler/compare/aaa...bbb")

    def test_no_url_without_bounds(self):
        rng = tr.Range(repo=tr.LLVM, good_sha=None, bad_sha="bbb", good_ts=None, bad_ts="t1")
        self.assertIsNone(rng.compare_url)


# ---------------------------------------------------------------------------
# plan_triage_units — consolidation into one unit per unique failure
# ---------------------------------------------------------------------------

class PlanTriageUnits(unittest.TestCase):
    def _units_by_key(self, rows):
        return {u["key"]: u for u in tr.plan_triage_units(rows)}

    def test_build_failures_merge_by_repo_and_detail(self):
        # Every workflow in a scheduled run builds the same commits, so the same
        # build break on N workflows is ONE regression -> one unit, rest shared.
        rows = [
            {"workflow": "A", "category": "build_failure", "detail": "clang_llvm"},
            {"workflow": "B", "category": "build_failure", "detail": "clang_llvm"},
        ]
        units = tr.plan_triage_units(rows)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["workflow"], "A")       # first-seen represents
        self.assertEqual(units[0]["shared"], ["B"])
        self.assertEqual(units[0]["strategy"], "bisect")

    def test_build_failures_split_by_repo(self):
        rows = [
            {"workflow": "A", "category": "build_failure", "detail": "clang_llvm"},
            {"workflow": "B", "category": "build_failure", "detail": "dxc"},
        ]
        self.assertEqual(len(tr.plan_triage_units(rows)), 2)

    def test_same_test_classification_merges_across_workflows(self):
        # Same shader binary everywhere -> one (test, classification) failure,
        # triaged once however many workflows report it.
        t = {"suite": "S", "test": "t1",
             "classification": "runtime_driver_suspected_miscompile"}
        rows = [
            {"workflow": "A", "tests": [t]},
            {"workflow": "B", "tests": [t]},
        ]
        units = tr.plan_triage_units(rows)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["strategy"], "dxil")   # *_miscompile -> DXIL
        self.assertEqual(units[0]["shared"], ["B"])

    def test_distinct_classifications_of_same_test_are_separate_units(self):
        rows = [{"workflow": "A", "tests": [
            {"suite": "S", "test": "t", "classification": "runtime_driver_suspected_crash"},
            {"suite": "S", "test": "t", "classification": "runtime_driver_suspected_miscompile"},
        ]}]
        units = tr.plan_triage_units(rows)
        self.assertEqual(len(units), 2)
        self.assertEqual({u["strategy"] for u in units}, {"evidence", "dxil"})

    def test_strategy_dispatch_covers_each_family(self):
        rows = [{"workflow": "A", "tests": [
            {"suite": "S", "test": "a", "classification": "shader_compile_clang"},
            {"suite": "S", "test": "b", "classification": "api_backend_suspected_miscompile"},
            {"suite": "S", "test": "c", "classification": "api_backend_suspected_crash"},
        ]}]
        by_test = {u["test"]: u["strategy"] for u in tr.plan_triage_units(rows)}
        self.assertEqual(by_test["a"], "bisect")   # shader compile
        self.assertEqual(by_test["b"], "dxil")     # miscompile beats the suspected-prefix
        self.assertEqual(by_test["c"], "evidence")

    def test_xpass_and_unhandled_are_skipped(self):
        rows = [{"workflow": "A", "tests": [
            {"suite": "S", "test": "x", "classification": "xpass"},
            {"suite": "S", "test": "y", "classification": "runtime_pipeline_error"},
        ]}]
        self.assertEqual(tr.plan_triage_units(rows), [])

    def test_first_seen_order_is_preserved(self):
        rows = [
            {"workflow": "A", "category": "build_failure", "detail": "dxc"},
            {"workflow": "B", "tests": [
                {"suite": "S", "test": "t", "classification": "shader_compile_clang"}]},
        ]
        units = tr.plan_triage_units(rows)
        self.assertEqual([u["kind"] for u in units], ["build", "shader"])


# ---------------------------------------------------------------------------
# Every affected workflow is visible to the agent (not just the representative)
# ---------------------------------------------------------------------------

class _FakeSnapNoLog:
    """Snapshot stand-in whose logs are unavailable (block extraction -> None)."""
    def _log(self, workflow):
        return None


class _FakeHistory:
    """Returns an unbounded range so triage_bisect skips git entirely."""
    def bound_build(self, repo, wf):
        return tr.Range(repo, None, "badsha", None, "t1")

    def bound_test(self, repo, wf, suite, test):
        return tr.Range(repo, None, "badsha", None, "t1")


def _offline_ctx(divergences=None):
    tmp = pathlib.Path(tr.WORKSPACE)  # any real dir; find_test_file just misses
    return tr.Ctx(
        history=_FakeHistory(), target=_FakeSnapNoLog(), triage_dir=tmp,
        otss_root=tmp, llvm_root=tmp, dxc_root=tmp, dxc_bin=None,
        agent=tr.Agent(False, None, 1),   # disabled -> the prompt is emitted verbatim
        allow_fetch=False, divergences=divergences or {},
    )


class WorkflowVisibility(unittest.TestCase):
    def test_bisect_lists_all_workflows(self):
        md, meta = tr.triage_bisect(
            _offline_ctx(), "WF-A", "test_failure", None, "S", "t",
            "shader_compile_clang", workflows=["WF-A", "WF-B", "WF-C"])
        self.assertEqual(meta["workflows"], ["WF-A", "WF-B", "WF-C"])
        # Header + the (agent-disabled) prompt both name every workflow.
        for wf in ("WF-A", "WF-B", "WF-C"):
            self.assertIn(wf, md)
        self.assertIn("Failing workflows (3)", md)

    def test_build_bisect_lists_all_workflows(self):
        md, meta = tr.triage_bisect(
            _offline_ctx(), "WF-A", "build_failure", "clang_llvm", "", "", None,
            workflows=["WF-A", "WF-B"])
        self.assertEqual(meta["workflows"], ["WF-A", "WF-B"])
        self.assertIn("WF-B", md)

    def test_evidence_uses_per_classification_failing_set(self):
        div = {"passes_on": ["P1"], "fails_on": ["WF-A"], "axes": {}}
        md, meta = tr.triage_evidence(
            _offline_ctx(), "WF-A", "S", "t", "runtime_driver_suspected_crash",
            div, workflows=["WF-A", "WF-B", "WF-C"])
        # fails_on reflects ALL workflows for this classification, not just the div union.
        self.assertEqual(meta["fails_on"], ["WF-A", "WF-B", "WF-C"])
        for wf in ("WF-A", "WF-B", "WF-C"):
            self.assertIn(wf, md)

    def test_miscompile_lists_all_even_without_divergence(self):
        # A miscompile that fails on every workflow has NO divergence entry; the
        # agent must still see all affected workflows (regression: previously
        # only the single representative was passed).
        md, meta = tr.triage_miscompile(
            _offline_ctx(), "WF-A", "S", "t", "runtime_miscompile", None,
            workflows=["WF-A", "WF-B"])
        self.assertEqual(meta["fails_on"], ["WF-A", "WF-B"])
        self.assertIn("WF-B", md)


if __name__ == "__main__":
    unittest.main()
