"""Regression tests for offloader-scripts/monitor_failures.py.

Pins parser + classifier behavior against small checked-in log excerpts under
fixtures/. Runs offline (no GH_TOKEN needed).

Run:  python3 -m unittest discover -s offloader-scripts/tests -v
"""
from __future__ import annotations

import os
import pathlib
import sys
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import monitor_failures as mf  # noqa: E402

FIX = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIX / name).read_text()


def first_block(fixture: str, needle: str):
    for b in mf.extract_failure_blocks(load(fixture)):
        if needle in b["test"] and b["result"] == "FAIL":
            return b
    raise AssertionError(f"no block for {needle!r} in {fixture}")


class ParseWorkflowAxes(unittest.TestCase):
    CASES = [
        ("Windows Vulkan AMD Clang",       {"api": "Vulkan",   "gpu": "AMD",      "compiler": "clang", "host": "x64",   "variant": "none"}),
        ("Windows D3D12 NVIDIA DXC",       {"api": "D3D12",    "gpu": "NVIDIA",   "compiler": "dxc",   "host": "x64",   "variant": "none"}),
        ("macOS Metal DXC",                {"api": "Metal",    "gpu": "Metal",    "compiler": "dxc",   "host": "macOS", "variant": "none"}),
        ("Windows Lavapipe AMD DXC",       {"api": "Lavapipe", "gpu": "AMD",      "compiler": "dxc",   "host": "x64",   "variant": "none"}),
        ("Windows ARM64 Lavapipe Clang",   {"api": "Lavapipe", "gpu": "Lavapipe", "compiler": "clang", "host": "ARM64", "variant": "none"}),
        ("Windows D3D12 Warp DXC",         {"api": "D3D12",    "gpu": "Warp",     "compiler": "dxc",   "host": "x64",   "variant": "none"}),
        ("Windows D3D12 QC Clang",         {"api": "D3D12",    "gpu": "QC",       "compiler": "clang", "host": "x64",   "variant": "none"}),
        ("Windows D3D12 AMD Clang GBV",    {"api": "D3D12",    "gpu": "AMD",      "compiler": "clang", "host": "x64",   "variant": "GBV"}),
        ("Windows D3D12 Warp Preview DXC", {"api": "D3D12",    "gpu": "Warp",     "compiler": "dxc",   "host": "x64",   "variant": "Preview"}),
        ("Windows ARM64 D3D12 Warp DXC",   {"api": "D3D12",    "gpu": "Warp",     "compiler": "dxc",   "host": "ARM64", "variant": "none"}),
    ]

    def test_all(self):
        for name, expected in self.CASES:
            with self.subTest(name=name):
                self.assertEqual(mf.parse_workflow_axes(name), expected)


class AttributeDivergence(unittest.TestCase):
    def test_api_only(self):
        # Vulkan fails; D3D12 passes. The passing set spans multiple hosts
        # (Windows + macOS), so 'api_pattern' should be reported. The failing
        # set is also all-x64, so 'host_pattern: x64-only' is a legitimate
        # secondary attribution — both are true, we assert on the primary.
        a = mf.attribute_divergence(
            fails_on=["Windows Vulkan AMD Clang", "Windows Vulkan NVIDIA Clang", "Windows Vulkan QC Clang"],
            passes_on=["Windows D3D12 AMD Clang", "Windows D3D12 NVIDIA Clang", "macOS Metal Clang"],
        )
        self.assertEqual(a.get("api_pattern"), "Vulkan-only")

    def test_gpu_only(self):
        a = mf.attribute_divergence(
            fails_on=["Windows Vulkan NVIDIA DXC", "Windows D3D12 NVIDIA DXC"],
            passes_on=["Windows Vulkan AMD DXC", "Windows D3D12 AMD DXC", "Windows Vulkan Intel DXC"],
        )
        self.assertEqual(a, {"gpu_pattern": "NVIDIA-only"})

    def test_compiler_only(self):
        a = mf.attribute_divergence(
            fails_on=["Windows Vulkan AMD Clang", "Windows D3D12 AMD Clang"],
            passes_on=["Windows Vulkan AMD DXC", "Windows D3D12 AMD DXC"],
        )
        self.assertEqual(a, {"compiler_pattern": "clang-only"})

    def test_mixed_no_axis(self):
        a = mf.attribute_divergence(
            fails_on=["Windows Vulkan NVIDIA DXC", "Windows D3D12 AMD Clang"],
            passes_on=["Windows Vulkan AMD DXC"],
        )
        self.assertEqual(a, {})

    def test_host_only(self):
        # ARM64 build breaks on both compilers, x64 counterparts pass.
        a = mf.attribute_divergence(
            fails_on=["Windows ARM64 D3D12 Warp Clang", "Windows ARM64 D3D12 Warp DXC"],
            passes_on=["Windows D3D12 Warp Clang", "Windows D3D12 Warp DXC"],
        )
        self.assertEqual(a, {"host_pattern": "ARM64-only"})

    def test_variant_only(self):
        # A test fails on both GBV variants and passes on both base variants.
        # (Hypothetical — the real workflow set only has a Clang GBV, no DXC
        # GBV; kept as a synthetic case to exercise the variant axis.)
        a = mf.attribute_divergence(
            fails_on=["Windows D3D12 AMD Clang GBV", "Windows D3D12 AMD DXC GBV"],
            passes_on=["Windows D3D12 AMD Clang", "Windows D3D12 AMD DXC"],
        )
        self.assertEqual(a.get("variant_pattern"), "GBV-only")

    def test_variant_none_never_a_pattern(self):
        # Base workflows (variant='none') failing while variant workflows pass
        # must NOT surface a 'none-only' variant pattern.
        a = mf.attribute_divergence(
            fails_on=["Windows D3D12 AMD Clang"],
            passes_on=["Windows D3D12 AMD Clang GBV"],
        )
        self.assertNotIn("variant_pattern", a)


class LitBlockParsing(unittest.TestCase):
    def test_extract_failure_blocks_finds_test(self):
        blocks = mf.extract_failure_blocks(load("shader_compile_clang_dxc.txt"))
        fails = [b for b in blocks if b["result"] == "FAIL"]
        self.assertTrue(any("matrix_swizzle_one_based" in b["test"] for b in fails))

    def test_parse_lit_commands_extracts_compiler_step(self):
        b = first_block("shader_compile_clang_dxc.txt", "matrix_swizzle_one_based")
        cmds = mf._parse_lit_commands(b["block"])
        compilers = [c for c in cmds if c["kind"] == "clang_dxc"]
        self.assertTrue(compilers, msg=f"no clang_dxc command; kinds={[c['kind'] for c in cmds]}")
        self.assertEqual(compilers[0]["exit_status"], "1")
        self.assertTrue(compilers[0]["stderr"])


class Classifiers(unittest.TestCase):
    def test_shader_compile_positive(self):
        b = first_block("shader_compile_clang_dxc.txt", "matrix_swizzle_one_based")
        self.assertEqual(mf.classify_shader_compile(b["block"]), "shader_compile_clang_dxc")

    def test_shader_compile_negative_when_dxc_succeeded(self):
        # dxc succeeded; failure was offloader.exe NT-status crash. A prior
        # buggy heuristic wrongly flagged this as a shader-compile failure.
        b = first_block("runtime_driver_primitive_index.txt", "primitive-index")
        self.assertIsNone(mf.classify_shader_compile(b["block"]))

    def test_runtime_driver_error_nt_status(self):
        b = first_block("runtime_driver_primitive_index.txt", "primitive-index")
        self.assertEqual(mf.classify_runtime(b["block"]), "runtime_driver_error")

    def test_runtime_driver_error_hull(self):
        b = first_block("runtime_driver_hull.txt", "HullSystemValues")
        self.assertEqual(mf.classify_runtime(b["block"]), "runtime_driver_error")

    def test_runtime_miscompile_smoothstep(self):
        b = first_block("runtime_miscompile_smoothstep.txt", "smoothstep")
        self.assertEqual(mf.classify_runtime(b["block"]), "runtime_miscompile")


class ExtractAllResults(unittest.TestCase):
    def test_status_kinds(self):
        res = mf.extract_all_results(load("lit_status_lines.txt"))
        valid = {"PASS", "FAIL", "XFAIL", "UNSUPPORTED", "XPASS", "SKIPPED", "UNRESOLVED", "TIMEOUT"}
        self.assertTrue(res, "expected at least one result parsed")
        self.assertTrue(set(res.values()) <= valid, f"unexpected result kinds: {set(res.values()) - valid}")


# ---------------------------------------------------------------------------
# XPASS → per-workflow XFAIL / issue matching
# ---------------------------------------------------------------------------


# A minimal file body mirroring the real offload-test-suite convention:
# each XFAIL is preceded by a `# Bug <url>` comment.
_MULTI_XFAIL = """\
#--- source.hlsl
void main() {}

# Bug https://github.com/llvm/llvm-project/issues/156775
# XFAIL: Vulkan && Clang
# Bug https://github.com/llvm/offload-test-suite/issues/525
# XFAIL: NV && Clang && DirectX
# Bug https://github.com/llvm/offload-test-suite/issues/1293
# XFAIL: Intel-Gen-Current && Clang && DirectX
# Bug https://github.com/llvm/offload-test-suite/issues/393
# XFAIL: Metal
"""


class ParseXfailClauses(unittest.TestCase):
    def test_extracts_all_clauses_with_urls(self):
        cs = mf.parse_xfail_clauses(_MULTI_XFAIL)
        self.assertEqual(len(cs), 4)
        self.assertEqual(cs[0]["expr"], "Vulkan && Clang")
        self.assertEqual(cs[0]["issue_url"], "https://github.com/llvm/llvm-project/issues/156775")
        self.assertEqual(cs[3]["expr"], "Metal")
        self.assertEqual(cs[3]["issue_url"], "https://github.com/llvm/offload-test-suite/issues/393")

    def test_missing_bug_comment_still_parses_expr(self):
        text = "# XFAIL: Vulkan\n"
        cs = mf.parse_xfail_clauses(text)
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0]["expr"], "Vulkan")
        self.assertIsNone(cs[0]["issue_url"])


class XfailExpressionEval(unittest.TestCase):
    def test_simple_true(self):
        self.assertTrue(mf._eval_xfail("Vulkan", {"Vulkan"}))
        self.assertFalse(mf._eval_xfail("Vulkan", {"DirectX"}))

    def test_and_or_not(self):
        self.assertTrue(mf._eval_xfail("Vulkan && Clang", {"Vulkan", "Clang"}))
        self.assertFalse(mf._eval_xfail("Vulkan && Clang", {"Vulkan"}))
        self.assertTrue(mf._eval_xfail("Vulkan || Metal", {"Metal"}))
        self.assertTrue(mf._eval_xfail("!Vulkan", {"DirectX"}))
        self.assertTrue(mf._eval_xfail("(Vulkan || Metal) && !Clang", {"Metal"}))


class WorkflowFeatures(unittest.TestCase):
    CASES = [
        # Name-derived only (base cases from the workflow-name axes)
        ("Windows D3D12 NVIDIA DXC",       {"DirectX", "DXC", "NV", "Windows", "x64"}),
        ("Windows Vulkan NVIDIA Clang",    {"Vulkan", "Clang", "Clang-Vulkan", "NV", "Windows", "x64"}),
        # Runner-hardware features from docs/CI.md
        ("Windows Vulkan AMD Clang",       {"Vulkan", "Clang", "Clang-Vulkan", "AMD", "Windows", "x64", "AVX512"}),
        ("Windows D3D12 Intel Clang",      {"DirectX", "Clang", "Intel", "Windows", "x64", "Intel-Gen-Current"}),
        ("macOS Metal DXC",                {"Metal", "DXC", "Darwin", "AppleM4"}),
        # Lavapipe-x64 runs on the AMD builder (per docs/CI.md), inherits AVX512
        ("Windows Lavapipe AMD DXC",       {"Vulkan", "DXC", "AMD", "Windows", "x64", "AVX512"}),
        # ARM64 hosts (QC or its ARM64 Warp/Lavapipe siblings) — no AVX512
        ("Windows ARM64 D3D12 Warp Clang", {"DirectX", "Clang", "WARP", "Windows", "ARM64"}),
        ("Windows ARM64 Lavapipe DXC",     {"Vulkan", "DXC", "Windows", "ARM64"}),
    ]

    def test_all(self):
        for name, expected in self.CASES:
            with self.subTest(name=name):
                self.assertEqual(mf.workflow_features(name), expected)


class MatchXpassToIssue(unittest.TestCase):
    def test_picks_matching_clause_by_workflow(self):
        r = mf.match_xpass_to_issue(_MULTI_XFAIL, "Windows Vulkan AMD Clang")
        self.assertEqual(r["issue_url"], "https://github.com/llvm/llvm-project/issues/156775")
        self.assertEqual(r["matched_expr"], "Vulkan && Clang")

    def test_nv_d3d12_clang(self):
        r = mf.match_xpass_to_issue(_MULTI_XFAIL, "Windows D3D12 NVIDIA Clang")
        self.assertEqual(r["issue_url"], "https://github.com/llvm/offload-test-suite/issues/525")

    def test_metal(self):
        r = mf.match_xpass_to_issue(_MULTI_XFAIL, "macOS Metal DXC")
        self.assertEqual(r["issue_url"], "https://github.com/llvm/offload-test-suite/issues/393")

    def test_runner_hardware_feature_now_decidable(self):
        # Intel-Gen-Current is a runner-hardware feature we CAN now infer from
        # the builder table (Intel builder = Arc Pro B50 = Intel Gen11-14/Xe).
        # So "Windows D3D12 Intel Clang" XPASSing on the "Intel-Gen-Current"
        # clause should pick issue #1293.
        r = mf.match_xpass_to_issue(_MULTI_XFAIL, "Windows D3D12 Intel Clang")
        self.assertEqual(r["issue_url"], "https://github.com/llvm/offload-test-suite/issues/1293")

    def test_avx512_decidable_from_builder(self):
        text = (
            "# Bug https://github.com/example/example/issues/1\n"
            "# XFAIL: DXC && DirectX && AVX512\n"
        )
        # AMD builder has AVX512, so this fires on AMD DXC.
        r = mf.match_xpass_to_issue(text, "Windows D3D12 AMD DXC")
        self.assertEqual(r["issue_url"], "https://github.com/example/example/issues/1")
        # NVIDIA builder does NOT (i5-14400F, consumer Intel disabled AVX512
        # from 12th gen), so the same XFAIL wouldn't fire there.
        r = mf.match_xpass_to_issue(text, "Windows D3D12 NVIDIA DXC")
        self.assertNotIn("issue_url", r)

    def test_no_matching_clause(self):
        r = mf.match_xpass_to_issue(_MULTI_XFAIL, "Windows D3D12 AMD DXC")
        self.assertNotIn("issue_url", r)
        self.assertIsNotNone(r["note"])


class ExtractRunnerNames(unittest.TestCase):
    def test_split_build_via_section_headers(self):
        # Real log layout: section headers `===== N_<jobname> _ <role>.txt =====`
        # decide which runner is build vs test. Test job's section is emitted
        # first in the archive (idx 0), followed by build (idx 1).
        log = (
            "\n===== 0_Windows-D3D12-AMD-DXC _ test.txt =====\n"
            "Runner name: 'HLSLPC-AMD01'\n"
            "\n===== 1_Windows-D3D12-AMD-DXC _ build.txt =====\n"
            "Runner name: 'HLSLPC-INTEL01'\n"
        )
        self.assertEqual(mf.extract_runner_names(log), {"build": "HLSLPC-INTEL01", "test": "HLSLPC-AMD01"})

    def test_split_build_reversed_order(self):
        # Order in the archive shouldn't matter — attribution is by header.
        log = (
            "\n===== 1_X _ build.txt =====\n"
            "Runner name: 'HLSLPC-INTEL01'\n"
            "\n===== 0_X _ test.txt =====\n"
            "Runner name: 'HLSLPC-AMD01'\n"
        )
        self.assertEqual(mf.extract_runner_names(log), {"build": "HLSLPC-INTEL01", "test": "HLSLPC-AMD01"})

    def test_single_runner(self):
        # Non-split macOS-style log — one Runner name, no section header,
        # falls into 'test'.
        log = "Runner name: 'HLSLPC-APPLE01'\n"
        self.assertEqual(mf.extract_runner_names(log), {"build": None, "test": "HLSLPC-APPLE01"})

    def test_non_split_workflow_promotes_build_to_test(self):
        # Non-SplitBuild macOS workflow: only a build job exists and it also
        # runs the tests. The build-labeled section's runner is the test
        # runner too — 'test' must be filled in.
        log = (
            "\n===== 0_macOS-Metal-DXC _ build.txt =====\n"
            "Runner name: 'HLSLPC-APPLE01'\n"
        )
        self.assertEqual(mf.extract_runner_names(log), {"build": "HLSLPC-APPLE01", "test": "HLSLPC-APPLE01"})

    def test_missing(self):
        self.assertEqual(mf.extract_runner_names("nothing here"), {"build": None, "test": None})


class WorkflowFeaturesFromRunner(unittest.TestCase):
    """
    When a runner name is provided, hardware features come from _RUNNER_FEATURES
    keyed on the hostname — not from the (gpu, host) guess. This matters for
    SplitBuild workflows where the test job runs on a different machine than
    the workflow name suggests.
    """

    def test_amd_workflow_built_on_intel_still_gets_amd_test_features(self):
        # Real case: Windows D3D12 AMD DXC builds on HLSLPC-INTEL01 but tests
        # on HLSLPC-AMD01. XFAIL features must reflect the *test* runner.
        f = mf.workflow_features("Windows D3D12 AMD DXC", runner_name="HLSLPC-AMD01")
        self.assertIn("AVX512", f)
        self.assertNotIn("Intel-Gen-Current", f)

    def test_unknown_runner_falls_back_to_static_guess(self):
        # If we somehow don't recognise the runner, fall back to _BUILDER_FEATURES.
        f = mf.workflow_features("Windows D3D12 AMD DXC", runner_name="HLSLPC-NEW42")
        self.assertIn("AVX512", f)  # static (gpu=AMD, host=x64) still yields AVX512

    def test_runner_overrides_name_guess(self):
        # Hypothetical: an AMD-name workflow tested on the NVIDIA runner. The
        # runner-derived features (empty set) must win over the (gpu=AMD)
        # static guess (which would say AVX512).
        f = mf.workflow_features("Windows D3D12 AMD DXC", runner_name="HLSLPC-NVIDIA01")
        self.assertNotIn("AVX512", f)


class MatchXpassWithRunner(unittest.TestCase):
    def test_avx512_clause_fires_on_amd_runner_but_not_nvidia(self):
        text = (
            "# Bug https://github.com/example/example/issues/1\n"
            "# XFAIL: DXC && DirectX && AVX512\n"
        )
        r = mf.match_xpass_to_issue(text, "Windows D3D12 AMD DXC", runner_name="HLSLPC-AMD01")
        self.assertEqual(r["issue_url"], "https://github.com/example/example/issues/1")
        r = mf.match_xpass_to_issue(text, "Windows D3D12 AMD DXC", runner_name="HLSLPC-NVIDIA01")
        self.assertNotIn("issue_url", r)


class LoadToken(unittest.TestCase):
    """Auth is env-only: $GH_TOKEN preferred, $GITHUB_TOKEN as fallback."""

    def test_prefers_gh_token(self):
        with unittest.mock.patch.dict(os.environ, {"GH_TOKEN": "abc  ", "GITHUB_TOKEN": "xyz"}, clear=True):
            self.assertEqual(mf.load_token(), "abc")

    def test_falls_back_to_github_token(self):
        with unittest.mock.patch.dict(os.environ, {"GITHUB_TOKEN": "  xyz\n"}, clear=True):
            self.assertEqual(mf.load_token(), "xyz")

    def test_empty_value_falls_through(self):
        # An exported-but-empty GH_TOKEN shouldn't block the GITHUB_TOKEN fallback.
        with unittest.mock.patch.dict(os.environ, {"GH_TOKEN": "", "GITHUB_TOKEN": "xyz"}, clear=True):
            self.assertEqual(mf.load_token(), "xyz")

    def test_no_token_raises_systemexit(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                mf.load_token()
            self.assertIn("GH_TOKEN", str(ctx.exception))


class ClassificationLegend(unittest.TestCase):
    """
    Guards against the classifier drifting from the report legend. If a new
    classification label is added to the code path but not to
    `CLASSIFICATION_LEGEND`, the report emits a placeholder and this test
    fails so we catch it before shipping.
    """
    EXPECTED_LABELS = {
        # Base labels from classify_run
        "build_failure",
        "shader_compile_dxc", "shader_compile_clang_dxc",
        "runtime_driver_error", "runtime_miscompile", "runtime_unknown",
        "xpass",
        # Upgraded labels from the cross-workflow pivot
        "runtime_driver_suspected_miscompile",
        "runtime_driver_confirmed",
        "runtime_driver_suspected_unknown",
        "api_backend_suspected_miscompile",
        "api_backend_confirmed",
        "api_backend_suspected_unknown",
    }

    def test_every_label_documented(self):
        missing = self.EXPECTED_LABELS - set(mf.CLASSIFICATION_LEGEND)
        self.assertFalse(missing, f"legend missing entries for: {sorted(missing)}")

    def test_no_stale_entries(self):
        stale = set(mf.CLASSIFICATION_LEGEND) - self.EXPECTED_LABELS
        self.assertFalse(stale, f"legend has entries for labels that don't exist: {sorted(stale)}")

    def test_explanations_are_non_trivial(self):
        for label, expl in mf.CLASSIFICATION_LEGEND.items():
            with self.subTest(label=label):
                self.assertGreater(len(expl), 40, f"legend for {label!r} is suspiciously short")


if __name__ == "__main__":
    unittest.main()
