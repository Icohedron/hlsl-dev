"""Regression tests for offloader-scripts/monitor_failures.py.

Pins parser + classifier behavior against small checked-in log excerpts under
fixtures/. Runs offline (no GH_TOKEN needed).

Run:  python3 -m unittest discover -s offloader-scripts/tests -v
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
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
        ("Windows Lavapipe AMD DXC",       {"api": "Vulkan",   "gpu": "Lavapipe", "compiler": "dxc",   "host": "x64",   "variant": "none"}),
        ("Windows ARM64 Lavapipe Clang",   {"api": "Vulkan",   "gpu": "Lavapipe", "compiler": "clang", "host": "ARM64", "variant": "none"}),
        ("Windows D3D12 Warp DXC",         {"api": "D3D12",    "gpu": "Warp",     "compiler": "dxc",   "host": "x64",   "variant": "none"}),
        ("Windows D3D12 QC Clang",         {"api": "D3D12",    "gpu": "QC",       "compiler": "clang", "host": "ARM64", "variant": "none"}),
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
        # set mixes x64 (AMD/NVIDIA) with ARM64 (QC), so NO host_pattern should
        # be attributed — QC is an ARM64 board, not x64. (Regression guard: this
        # used to wrongly report 'host_pattern: x64-only'.)
        a = mf.attribute_divergence(
            fails_on=["Windows Vulkan AMD Clang", "Windows Vulkan NVIDIA Clang", "Windows Vulkan QC Clang"],
            passes_on=["Windows D3D12 AMD Clang", "Windows D3D12 NVIDIA Clang", "macOS Metal Clang"],
        )
        self.assertEqual(a.get("api_pattern"), "Vulkan-only")
        self.assertNotIn("host_pattern", a)

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

    def test_compiler_only_requires_clean_partition(self):
        # Some Clang workflows PASS this test while others FAIL. Because a
        # compiler build's DXIL is config-independent, a passing Clang run means
        # the compiler can't be the differentiator (the split is really per
        # GPU/API/driver, or a toolchain version skew). We must NOT report a
        # contradictory 'clang-only' axis while Clang is demonstrably passing.
        a = mf.attribute_divergence(
            fails_on=["Windows Vulkan NVIDIA Clang"],
            passes_on=["Windows D3D12 NVIDIA Clang", "Windows D3D12 NVIDIA DXC"],
        )
        self.assertNotIn("compiler_pattern", a)

    def test_gpu_axis_allows_partial_passes_of_same_value(self):
        # Contrast with the compiler axis: a driver bug can be NVIDIA-only under
        # Vulkan yet fine on the same NVIDIA card under D3D12 (same binary,
        # runtime-layer behaviour), so a passing NVIDIA workflow does NOT veto a
        # gpu_pattern the way a passing Clang vetoes compiler_pattern.
        a = mf.attribute_divergence(
            fails_on=["Windows Vulkan NVIDIA DXC"],
            passes_on=["Windows D3D12 NVIDIA DXC", "Windows Vulkan AMD DXC"],
        )
        self.assertEqual(a.get("gpu_pattern"), "NVIDIA-only")

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

    def test_xpass_test_not_double_counted_as_fail(self):
        # Regression: lit counts an unexpected pass as a suite failure, so the
        # SAME test emits BOTH an `XPASS:` summary line and a `TEST '...' FAILED`
        # banner. It must yield exactly one block (XPASS), never a duplicate
        # FAIL + XPASS pair. Mirrors Feature/WaveOps/WaveActiveCountBits.test on
        # Windows Vulkan QC Clang.
        log = (
            "2026-07-14T02:38:56.8Z XPASS: OffloadTest-clang-vk :: Feature/WaveOps/WaveActiveCountBits.test (439 of 570)\n"
            "2026-07-14T02:38:56.8Z ******************** TEST 'OffloadTest-clang-vk :: Feature/WaveOps/WaveActiveCountBits.test' FAILED ********************\n"
            "2026-07-14T02:38:56.8Z offloader.exe -debug-layer pipeline.yaml out.o\n"
            "2026-07-14T02:38:56.8Z # command output: some noise\n"
            "2026-07-14T02:38:56.8Z ********************\n"
        )
        blocks = mf.extract_failure_blocks(log)
        wac = [b for b in blocks
               if b["test"] == "Feature/WaveOps/WaveActiveCountBits.test"]
        self.assertEqual(len(wac), 1, f"expected one block, got {[(b['result'], b['test']) for b in wac]}")
        self.assertEqual(wac[0]["result"], "XPASS")

    def test_no_test_appears_as_both_fail_and_xpass(self):
        # General invariant: a given (suite, test) is never emitted as both a
        # FAIL and an XPASS block.
        log = (
            "XPASS: S :: a.test (1 of 3)\n"
            "******************** TEST 'S :: a.test' FAILED ********************\n"
            "body\n"
            "********************\n"
            "******************** TEST 'S :: b.test' FAILED ********************\n"
            "body\n"
            "********************\n"
        )
        blocks = mf.extract_failure_blocks(log)
        from collections import defaultdict
        seen = defaultdict(set)
        for b in blocks:
            seen[(b["suite"], b["test"])].add(b["result"])
        conflicting = {k: v for k, v in seen.items() if len(v) > 1}
        self.assertFalse(conflicting, f"tests recorded as both FAIL and XPASS: {conflicting}")
        # a.test is the XPASS; b.test is a genuine FAIL.
        self.assertEqual(seen[("S", "a.test")], {"XPASS"})
        self.assertEqual(seen[("S", "b.test")], {"FAIL"})

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

    def test_backward_compat_issue_urls_singleton(self):
        # Each clause in the canonical convention has exactly one linked issue.
        cs = mf.parse_xfail_clauses(_MULTI_XFAIL)
        self.assertEqual(
            cs[0]["issue_urls"],
            ["https://github.com/llvm/llvm-project/issues/156775"],
        )
        self.assertEqual(cs[1]["issue_urls"],
                         ["https://github.com/llvm/offload-test-suite/issues/525"])

    def test_multiple_issues_per_clause(self):
        # Real offload-test-suite pattern: one clause referencing two bugs.
        text = (
            "# Metal: two separate defects.\n"
            "# Bug https://github.com/llvm/offload-test-suite/issues/304\n"
            "# Bug https://github.com/llvm/offload-test-suite/issues/305\n"
            "# XFAIL: Metal\n"
        )
        cs = mf.parse_xfail_clauses(text)
        self.assertEqual(len(cs), 1)
        # issue_urls preserves source order (top-to-bottom).
        self.assertEqual(cs[0]["issue_urls"], [
            "https://github.com/llvm/offload-test-suite/issues/304",
            "https://github.com/llvm/offload-test-suite/issues/305",
        ])
        # issue_url stays the nearest (closest to the XFAIL line) for back-compat.
        self.assertEqual(cs[0]["issue_url"],
                         "https://github.com/llvm/offload-test-suite/issues/305")

    def test_two_urls_on_one_comment_line(self):
        text = (
            "# Bug A https://github.com/x/y/issues/1 and B https://github.com/x/y/issues/2\n"
            "# XFAIL: Metal\n"
        )
        cs = mf.parse_xfail_clauses(text)
        self.assertEqual(cs[0]["issue_urls"],
                         ["https://github.com/x/y/issues/1", "https://github.com/x/y/issues/2"])

    def test_url_in_multiline_description(self):
        # URL embedded mid-sentence, several description lines above the XFAIL.
        text = (
            "# SV_CullDistance whole-primitive culling. Tracked under\n"
            "# https://github.com/llvm/wg-hlsl/issues/25 for the frontend.\n"
            "# Clang does not yet lower it on either backend path.\n"
            "# XFAIL: Clang\n"
        )
        cs = mf.parse_xfail_clauses(text)
        self.assertEqual(cs[0]["issue_url"], "https://github.com/llvm/wg-hlsl/issues/25")

    def test_blank_lines_inside_comment_block(self):
        # A blank line splitting the rationale must not sever the link search.
        text = (
            "# Bug https://github.com/x/y/issues/7\n"
            "#\n"
            "\n"
            "# rationale continues here\n"
            "# XFAIL: Vulkan\n"
        )
        cs = mf.parse_xfail_clauses(text)
        self.assertEqual(cs[0]["issue_url"], "https://github.com/x/y/issues/7")

    def test_does_not_bleed_across_previous_xfail(self):
        # The issue belongs to the DirectX clause; the Clang clause below has
        # no link of its own and must NOT steal it across the XFAIL boundary.
        text = (
            "# Bug https://github.com/x/y/issues/664\n"
            "# XFAIL: DirectX || Metal\n"
            "# min16 not supported.\n"
            "# XFAIL: Clang\n"
        )
        cs = mf.parse_xfail_clauses(text)
        self.assertEqual(cs[0]["issue_url"], "https://github.com/x/y/issues/664")
        self.assertIsNone(cs[1]["issue_url"])
        self.assertEqual(cs[1]["issue_urls"], [])

    def test_does_not_bleed_across_other_directive(self):
        # An UNSUPPORTED line above also bounds the block.
        text = (
            "# Bug https://github.com/x/y/issues/1294\n"
            "# UNSUPPORTED: Metal || DirectX\n"
            "# min16 not supported.\n"
            "# XFAIL: Clang\n"
        )
        cs = mf.parse_xfail_clauses(text)
        self.assertEqual(len(cs), 1)
        self.assertIsNone(cs[0]["issue_url"])

    def test_prose_unsupported_word_is_not_a_boundary(self):
        # Prose 'Unsupported:' (mixed case) is description, not a lit directive,
        # so the URL on it is still collected. Real: StaticSamplers.test.
        text = (
            "# Unsupported: https://github.com/llvm/llvm-project/issues/101558\n"
            "# XFAIL: Clang\n"
        )
        cs = mf.parse_xfail_clauses(text)
        self.assertEqual(cs[0]["issue_url"],
                         "https://github.com/llvm/llvm-project/issues/101558")


# ---------------------------------------------------------------------------
# Corpus-shape coverage: every distinct XFAIL comment-block shape observed in
# llvm/offload-test-suite/test as of this writing. Each fixture faithfully
# mirrors a real file (cited in `src`) but is inlined so the suite stays
# offline and independent of the sibling checkout. `expected` is the list of
# issue URLs the clause under test should resolve to (issue_urls), and the
# nearest one (issue_url) is asserted to be the last of that list.
# ---------------------------------------------------------------------------

_U = "https://github.com/llvm/offload-test-suite/issues/"
_L = "https://github.com/llvm/llvm-project/issues/"
_D = "https://github.com/microsoft/DirectXShaderCompiler/issues/"


class RealCorpusXfailShapes(unittest.TestCase):
    # (label, source citation, file body, index of clause under test, expected urls)
    CASES = [
        # 1. Bug + XFAIL, preceded by a blank then a previous XFAIL clause
        #    (the most common shape).
        ("bug_then_xfail_after_prev_clause",
         "Basic/Matrix/matrix_bool_and_operator.test:131",
         f"# Bug {_D}8129\n"
         f"# XFAIL: DXC && Vulkan\n"
         f"\n"
         f"# Bug {_U}703\n"
         f"# XFAIL: Clang && NV && Vulkan\n",
         1, [f"{_U}703"]),

        # 2. First clause in a file: Bug + XFAIL with a #--- end marker above.
        ("first_clause_after_source_marker",
         "Basic/Matrix/matrix_bool_and_operator.test:128",
         f"#--- end\n"
         f"# Bug {_D}8129\n"
         f"# XFAIL: DXC && Vulkan\n",
         0, [f"{_D}8129"]),

        # 3. Two stacked XFAILs, second has no link of its own (boundary stop).
        ("stacked_xfail_no_link",
         "Bugs/Texture-Row-Pitch-Readback.test:281",
         f"# XFAIL: Clang && DirectX\n"
         f"# XFAIL: Clang && Metal\n",
         1, []),

        # 4. Multiline rationale + Bug + XFAIL, bounded above by REQUIRES.
        ("multiline_desc_bug_bounded_by_requires",
         "Bugs/Adjacent-Partial-Writes.yaml:65",
         f"# REQUIRES: Int64\n"
         f"\n"
         f"# When compiled with DXC this hits a memory coherence issue on Intel\n"
         f"# UHD drivers. Does not reproduce with Clang.\n"
         f"# Bug {_U}226\n"
         f"# XFAIL: Intel-Memory-Coherence-Issue-226 && !Clang\n",
         0, [f"{_U}226"]),

        # 5. Multiline rationale with NO link, blank line above it.
        ("multiline_desc_no_link",
         "Bugs/Texture-Row-Pitch-Readback.test:280",
         f"# unpadded last row and silently sidestep the bug.\n"
         f"\n"
         f"# Clang's HLSL -> DXIL lowering does not implement the graphics-stage\n"
         f"# signature intrinsics needed for SV_POSITION / SV_TARGET plumbing.\n"
         f"# XFAIL: Clang && DirectX\n",
         0, []),

        # 6. Bug + XFAIL directly under a previous clause (no blank), boundary.
        ("bug_xfail_adjacent_prev_clause",
         "Feature/CBuffer/arrays.test:98",
         f"# Bug: {_D}7819\n"
         f"# XFAIL: DXC && Vulkan\n"
         f"# Bug: {_L}180600\n"
         f"# XFAIL: Clang && Vulkan\n",
         1, [f"{_L}180600"]),

        # 7. XFAIL after a blank then an UNSUPPORTED directive: no link.
        ("xfail_after_unsupported_block",
         "Feature/Semantics/DomainSystemValues.test:243",
         f"# Mapping HLSL HS/DS onto Metal isn't wired up, so skip Metal.\n"
         f"# UNSUPPORTED: Metal\n"
         f"\n"
         f"# XFAIL: Clang\n",
         0, []),

        # 8. One clause, TWO linked issues, with a leading description line.
        ("two_issues_one_clause_with_desc",
         "Feature/StructuredBuffer/inc_counter_array.test:45",
         f"# Offload tests are missing counter/resource-array support on Metal\n"
         f"# Unimplemented {_U}304\n"
         f"# Unimplemented {_U}305\n"
         f"# XFAIL: Metal\n",
         0, [f"{_U}304", f"{_U}305"]),

        # 9. BUG + XFAIL, blank line then source above.
        ("bug_xfail_blank_then_source",
         "Feature/CBuffer/Matrix/SingleSubscript/mat_cbuffer.f32.test:172",
         f"        Binding: 7\n"
         f"\n"
         f"# BUG {_L}191070\n"
         f"# XFAIL: Clang && Vulkan\n",
         0, [f"{_L}191070"]),

        # 10. Bug + XFAIL immediately under a #--- end marker (no blank).
        ("bug_xfail_under_end_marker",
         "Feature/HLSLLib/asuint_mat.32.test:345",
         f"#--- end\n"
         f"# Bug: {_L}186864\n"
         f"# XFAIL: Clang && Vulkan\n",
         0, [f"{_L}186864"]),

        # 11. Bug + XFAIL bounded above by an UNSUPPORTED directive (link kept).
        ("bug_xfail_bounded_by_unsupported",
         "Feature/PushConstant/types_16bit.test:53",
         f"# UNSUPPORTED: Metal || DirectX\n"
         f"#\n"
         f"# Bug {_U}1294\n"
         f"# XFAIL: Vulkan && DXC && Intel-Gen-Current\n",
         0, [f"{_U}1294"]),

        # 12. Two linked issues, blank + REQUIRES boundary above.
        ("two_issues_bounded_by_requires",
         "WaveOps/QuadReadAcrossX.int64.test:237",
         f"# REQUIRES: Int64\n"
         f"\n"
         f"# Bug: {_U}988\n"
         f"# Bug: {_U}989\n"
         f"# XFAIL: Metal\n",
         0, [f"{_U}988", f"{_U}989"]),
    ]

    def test_all_shapes(self):
        for label, src, body, idx, expected in self.CASES:
            with self.subTest(shape=label, src=src):
                cs = mf.parse_xfail_clauses(body)
                self.assertGreater(len(cs), idx, f"{label}: clause {idx} missing")
                c = cs[idx]
                self.assertEqual(c["issue_urls"], expected,
                                 f"{label} ({src}): issue_urls mismatch")
                # issue_url is the nearest (last-in-source-order) linked issue,
                # or None when the clause has no link.
                self.assertEqual(c["issue_url"], expected[-1] if expected else None,
                                 f"{label} ({src}): issue_url mismatch")

    def test_every_clause_parses_without_error(self):
        # Sanity: each fixture yields at least the clause under test and every
        # returned clause carries the expected keys.
        for label, src, body, idx, expected in self.CASES:
            with self.subTest(shape=label, src=src):
                for c in mf.parse_xfail_clauses(body):
                    self.assertIn("expr", c)
                    self.assertIn("issue_url", c)
                    self.assertIn("issue_urls", c)
                    self.assertIn("line_no", c)
                    # issue_url, when present, is always the last of issue_urls.
                    if c["issue_urls"]:
                        self.assertEqual(c["issue_url"], c["issue_urls"][-1])
                    else:
                        self.assertIsNone(c["issue_url"])


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
        # Lavapipe is a software Vulkan renderer: lit reports api=Vulkan and a
        # `Lavapipe` feature, and does NOT add the AMD vendor token (the device
        # is llvmpipe, not the Radeon GPU). Lavapipe-x64 runs on the AMD builder
        # (per docs/CI.md), so it still inherits that host's AVX512.
        ("Windows Lavapipe AMD DXC",       {"Vulkan", "DXC", "Lavapipe", "Windows", "x64", "AVX512"}),
        # ARM64 hosts (QC or its ARM64 Warp/Lavapipe siblings) — no AVX512
        ("Windows ARM64 D3D12 Warp Clang", {"DirectX", "Clang", "WARP", "Windows", "ARM64"}),
        ("Windows ARM64 Lavapipe DXC",     {"Vulkan", "DXC", "Lavapipe", "Windows", "ARM64"}),
        # QC (Snapdragon X Plus) is ARM64 even though the name has no ARM64
        # token — never x64, never AVX512.
        ("Windows Vulkan QC Clang",        {"Vulkan", "Clang", "Clang-Vulkan", "QC", "Windows", "ARM64"}),
        ("Windows D3D12 QC DXC",           {"DirectX", "DXC", "QC", "Windows", "ARM64"}),
    ]

    def test_all(self):
        for name, expected in self.CASES:
            with self.subTest(name=name):
                self.assertEqual(mf.workflow_features(name), expected)

    def test_qc_is_arm64_never_x64(self):
        # Regression: QC boards are ARM64; the workflow name carries no ARM64
        # token, so a naive Windows->x64 default mislabelled them. That in turn
        # produced a bogus 'host_pattern: x64-only' divergence axis.
        for name in ("Windows Vulkan QC Clang", "Windows D3D12 QC DXC",
                     "Windows QC Clang Lavapipe"):
            with self.subTest(name=name):
                feats = mf.workflow_features(name)
                self.assertIn("ARM64", feats, name)
                self.assertNotIn("x64", feats, name)
                self.assertNotIn("AVX512", feats, name)
                self.assertEqual(mf.parse_workflow_axes(name)["host"], "ARM64", name)

    def test_lavapipe_is_gpu_not_api(self):
        # Regression: Lavapipe is a software Vulkan renderer, not an API. It must
        # never appear on the `api` axis; its api is Vulkan and it *is* the gpu.
        # A vendor token in the name (AMD) is the builder host, not the device,
        # so the AMD gpu feature must NOT be emitted for a Lavapipe run.
        for name in ("Windows Lavapipe AMD DXC", "Windows ARM64 Lavapipe Clang",
                     "Windows QC Clang Lavapipe"):
            with self.subTest(name=name):
                ax = mf.parse_workflow_axes(name)
                self.assertEqual(ax["api"], "Vulkan", name)
                self.assertEqual(ax["gpu"], "Lavapipe", name)
                feats = mf.workflow_features(name)
                self.assertIn("Vulkan", feats, name)
                self.assertIn("Lavapipe", feats, name)
                self.assertNotIn("AMD", feats, name)
                self.assertNotIn("QC", feats, name)

    def test_lavapipe_never_in_api_tokens(self):
        self.assertNotIn("Lavapipe", mf._API_TOKENS)
        self.assertIn("Lavapipe", mf._GPU_TOKENS)


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

    def test_surfaces_issue_urls_for_matched_clause(self):
        # A matched clause with two linked bugs exposes both via issue_urls,
        # while issue_url stays the nearest one.
        text = (
            "# Bug https://github.com/llvm/offload-test-suite/issues/304\n"
            "# Bug https://github.com/llvm/offload-test-suite/issues/305\n"
            "# XFAIL: Metal\n"
        )
        r = mf.match_xpass_to_issue(text, "macOS Metal DXC")
        self.assertEqual(r["issue_url"], "https://github.com/llvm/offload-test-suite/issues/305")
        self.assertEqual(r["issue_urls"], [
            "https://github.com/llvm/offload-test-suite/issues/304",
            "https://github.com/llvm/offload-test-suite/issues/305",
        ])

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
    When a runner name is provided, hardware features come from the runners.json
    table keyed on the hostname — not from the (gpu, host) guess. This matters for
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
        # If we somehow don't recognise the runner, fall back to the (gpu, host)
        # -> builder-host mapping (_BUILDER_HOSTS).
        f = mf.workflow_features("Windows D3D12 AMD DXC", runner_name="HLSLPC-NEW42")
        self.assertIn("AVX512", f)  # static (gpu=AMD, host=x64) still yields AVX512

    def test_runner_overrides_name_guess(self):
        # Hypothetical: an AMD-name workflow tested on the NVIDIA runner. The
        # runner-derived features (empty set) must win over the (gpu=AMD)
        # static guess (which would say AVX512).
        f = mf.workflow_features("Windows D3D12 AMD DXC", runner_name="HLSLPC-NVIDIA01")
        self.assertNotIn("AVX512", f)


class RunnerTable(unittest.TestCase):
    """
    The hardware feature table is loaded from the human-editable runners.json,
    keyed on physical machines (HLSLPC-*), and software renderers (WARP/Lavapipe)
    inherit a host's CPU features but not its GPU features.
    """
    def test_runners_json_loads(self):
        self.assertTrue(mf._RUNNER_CPU_FEATURES, "no runners loaded")
        self.assertIn("HLSLPC-AMD01", mf._RUNNER_CPU_FEATURES)
        # gpu features are keyed per (machine, device); each device's machine is
        # a known runner.
        self.assertTrue(mf._RUNNER_GPU_FEATURES)
        for (host, _gpu) in mf._RUNNER_GPU_FEATURES:
            self.assertIn(host, mf._RUNNER_CPU_FEATURES)

    def test_builder_hosts_map_axes_to_machines(self):
        self.assertEqual(mf._BUILDER_HOSTS[("AMD", "x64")], "HLSLPC-AMD01")
        self.assertEqual(mf._BUILDER_HOSTS[("Warp", "x64")], "HLSLPC-INTEL01")
        self.assertEqual(mf._BUILDER_HOSTS[("Warp", "ARM64")], "HLSLPC-QC01")
        self.assertEqual(mf._BUILDER_HOSTS[("Metal", "macOS")], "HLSLPC-APPLE01")

    def test_per_gpu_features(self):
        # Each device on a machine carries its own features: the Intel Arc adds
        # Intel-Gen-Current, WARP on the same box adds nothing.
        self.assertEqual(mf._host_hw_features("HLSLPC-INTEL01", "Intel"),
                         {"Intel-Gen-Current"})
        self.assertEqual(mf._host_hw_features("HLSLPC-INTEL01", "Warp"), set())
        # CPU features apply to every device, including software renderers.
        self.assertEqual(mf._host_hw_features("HLSLPC-AMD01", "Lavapipe"), {"AVX512"})
        self.assertEqual(mf._host_hw_features("HLSLPC-AMD01", "AMD"), {"AVX512"})
        # Apple GPU device feature.
        self.assertEqual(mf._host_hw_features("HLSLPC-APPLE01", "Metal"), {"AppleM4"})

    def test_warp_does_not_inherit_host_gpu_feature(self):
        # WARP x64 runs on the Intel builder but must NOT get Intel-Gen-Current
        # (the device under test is WARP, not the Arc GPU). Regression guard for
        # the old (gpu, host) -> features table that couldn't express this.
        for rn in (None, "HLSLPC-INTEL01"):
            with self.subTest(runner_name=rn):
                feats = mf.workflow_features("Windows D3D12 Warp DXC", runner_name=rn)
                self.assertNotIn("Intel-Gen-Current", feats)
                self.assertNotIn("AVX512", feats)

    def test_load_runner_table_tolerates_missing_file(self):
        cpu, gpu, hosts = mf._load_runner_table(pathlib.Path("/no/such/runners.json"))
        self.assertEqual((cpu, gpu, hosts), ({}, {}, {}))

    def test_host_arch_inferred_from_cpu(self):
        self.assertEqual(mf._infer_host_arch("AMD Ryzen 7 9700X", None), "x64")
        self.assertEqual(mf._infer_host_arch("Intel Core i5-14400F", None), "x64")
        self.assertEqual(mf._infer_host_arch("Qualcomm Snapdragon X Plus (ARM64)", None), "ARM64")
        self.assertEqual(mf._infer_host_arch("Apple M4 Pro", None), "macOS")
        # explicit host wins over inference
        self.assertEqual(mf._infer_host_arch("Some Unrecognised CPU", "ARM64"), "ARM64")
        # unclassifiable CPU defaults to x64
        self.assertEqual(mf._infer_host_arch("Some Unrecognised CPU", None), "x64")

    def test_gpus_paired_with_inferred_arch(self):
        # A machine's `gpus` are paired with its CPU-derived arch (host not
        # repeated per device): QC is ARM64, so all its devices map at ARM64.
        self.assertEqual(mf._BUILDER_HOSTS[("QC", "ARM64")], "HLSLPC-QC01")
        self.assertEqual(mf._BUILDER_HOSTS[("Lavapipe", "ARM64")], "HLSLPC-QC01")
        self.assertEqual(mf._BUILDER_HOSTS[("Lavapipe", "x64")], "HLSLPC-AMD01")
        self.assertNotIn(("QC", "x64"), mf._BUILDER_HOSTS)  # no x64 QC config


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
        "runtime_driver_error", "runtime_pipeline_error",
        "runtime_miscompile", "runtime_unknown",
        "xpass",
        # Upgraded labels from the cross-workflow pivot
        "runtime_driver_suspected_miscompile",
        "runtime_driver_suspected_crash",
        "runtime_driver_suspected_unknown",
        "api_backend_suspected_miscompile",
        "api_backend_suspected_crash",
        "api_backend_suspected_unknown",
        # NOTE: no compiler_suspected_* labels — a compiler-axis divergence is
        # reported as an axis but never upgraded to a fault-implying label.
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

    def test_no_confirmed_labels(self):
        # A GPU/API-specific divergence proves the failure is
        # runtime-environment-specific, not that the driver (rather than the
        # test's own pipeline I/O spec) is at fault. So no label claims to
        # "confirm" a driver/backend bug — the pivot only ever upgrades to a
        # "suspected" label.
        for label in mf.CLASSIFICATION_LEGEND:
            self.assertNotIn("confirmed", label,
                             f"{label!r} overclaims; divergence is suspicion, not proof")
        for label, expl in mf.CLASSIFICATION_LEGEND.items():
            if label.endswith("_suspected_crash"):
                # The crash-mode upgrade must spell out the test-spec caveat.
                self.assertRegex(
                    expl.lower(),
                    r"unconfirmed|test-spec|test-authoring|test itself|pipeline",
                    f"{label!r} should note the failure may be a test-spec bug",
                )


class BuiltCommits(unittest.TestCase):
    LOG = "\n".join([
        "2026-07-14T06:58:33Z   repository: Microsoft/DirectXShaderCompiler",
        "2026-07-14T06:58:33Z Syncing repository: Microsoft/DirectXShaderCompiler",
        "2026-07-14T06:58:33Z HEAD is now at dc3e6c48 [SM6.10] LinAlg (#8608)",
        "2026-07-14T06:58:37Z HEAD is now at dc3e6c48 [SM6.10] LinAlg (#8608)",
        "2026-07-14T06:58:46Z   repository: llvm/llvm-project",
        "2026-07-14T06:58:46Z Syncing repository: llvm/llvm-project",
        "2026-07-14T06:58:47Z HEAD is now at f60650c77 [BitcodeReader] (#208175)",
    ])

    def test_attributes_and_dedups(self):
        c = mf.extract_built_commits(self.LOG)
        self.assertEqual(c["directxshadercompiler"], "dc3e6c48")
        self.assertEqual(c["llvm-project"], "f60650c77")

    def test_empty(self):
        self.assertEqual(mf.extract_built_commits(""), {})

# ---------------------------------------------------------------------------
# Session additions: pipeline-error classification, suite normalisation,
# compiler axis, compact slugs, issue rendering, commit-pinned test reads,
# ambiguous XFAIL, and HTML report.
# ---------------------------------------------------------------------------


def _runtime_block(cmd_kind: str, exit_status: str, stdout: str = "", stderr: str = "") -> str:
    """
    Build a minimal lit failure block: a dxc compile that succeeds followed by a
    later command that fails, in the exact shape _parse_lit_commands expects.
    """
    tool = {"offloader": "offloader.exe", "filecheck": "FileCheck"}.get(cmd_kind, cmd_kind)
    lines = [
        "# executed command: dxc.exe -T cs_6_0 -Fo shader.o shader.hlsl",
        f"# executed command: {tool} pipeline.yaml shader.o",
    ]
    if stdout:
        lines += ["# .---command stdout------------"]
        lines += [f"# | {ln}" for ln in stdout.splitlines()]
        lines += ["# `-----------------------------"]
    if stderr:
        lines += ["# .---command stderr------------"]
        lines += [f"# | {ln}" for ln in stderr.splitlines()]
        lines += ["# `-----------------------------"]
    lines += [f"# error: command failed with exit status: {exit_status}"]
    return "\n".join(lines)


class RuntimePipelineError(unittest.TestCase):
    def test_d3d12_pso_creation(self):
        b = _runtime_block("offloader", "1", stdout="Failed to create PSO.")
        self.assertEqual(mf.classify_runtime(b), "runtime_pipeline_error")

    def test_vulkan_pipeline_creation(self):
        b = _runtime_block("offloader", "1", stderr="Failed to create compute pipeline.")
        self.assertEqual(mf.classify_runtime(b), "runtime_pipeline_error")

    def test_driver_crash_takes_precedence_over_pipeline(self):
        # A device-removed during PSO creation is still a driver crash.
        b = _runtime_block("offloader", "1", stdout="Failed to create PSO. device removed")
        self.assertEqual(mf.classify_runtime(b), "runtime_driver_error")

    def test_nt_status_crash_still_driver_error(self):
        b = _runtime_block("offloader", "0xc0000005", stdout="Failed to create PSO.")
        self.assertEqual(mf.classify_runtime(b), "runtime_driver_error")

    def test_miscompile_not_misclassified_as_pipeline(self):
        b = _runtime_block("offloader", "1", stdout="Test failed: image mismatch")
        self.assertEqual(mf.classify_runtime(b), "runtime_miscompile")

    def test_plain_nonzero_still_unknown(self):
        b = _runtime_block("offloader", "1", stdout="something unhelpful")
        self.assertEqual(mf.classify_runtime(b), "runtime_unknown")

    def test_graphics_and_mesh_pso_variants(self):
        # D3D12 graphics / mesh-shader PSO wording ("Failed to create graphics
        # PSO.") must classify as pipeline error, not fall through to unknown.
        for msg in ("gpu-exec: error: Failed to create graphics PSO.",
                    "gpu-exec: error: Failed to create mesh shader PSO.",
                    "gpu-exec: error: Failed to create graphics pipeline."):
            with self.subTest(msg=msg):
                b = _runtime_block("offloader", "1", stdout=msg)
                self.assertEqual(mf.classify_runtime(b), "runtime_pipeline_error")

    def test_root_signature_is_not_a_pipeline_marker(self):
        b = _runtime_block("offloader", "1", stdout="gpu-exec: error: Failed to create root signature.")
        self.assertEqual(mf.classify_runtime(b), "runtime_unknown")

    def test_gpu_exec_prefix_treated_as_offloader(self):
        # Even if the command kind weren't 'offloader', the gpu-exec error prefix
        # marks the payload as runtime (offloader) output, so PSO/mismatch
        # markers in it are still honoured.
        cmds = mf._parse_lit_commands(_runtime_block("gpu-exec", "1",
                                                     stdout="gpu-exec: error: Failed to create PSO."))
        self.assertTrue(any(c["kind"] == "offloader" for c in cmds))


class NormalizeSuite(unittest.TestCase):
    def test_platform_suffixes_collapse(self):
        for s in ["OffloadTest-vk", "OffloadTest-clang-vk", "OffloadTest-d3d12",
                  "OffloadTest-clang-d3d12", "OffloadTest-warp-d3d12",
                  "OffloadTest-clang-warp-d3d12", "OffloadTest-mtl",
                  "OffloadTest-clang-mtl"]:
            with self.subTest(suite=s):
                self.assertEqual(mf.normalize_suite(s), "OffloadTest")

    def test_non_platform_suite_unchanged(self):
        self.assertEqual(mf.normalize_suite("OffloadTest-Unit"), "OffloadTest-Unit")

    def test_test_key_groups_across_platforms(self):
        k1 = mf.normalize_test_key("OffloadTest-warp-d3d12", "Basic/simple.test")
        k2 = mf.normalize_test_key("OffloadTest-clang-vk", "Basic/simple.test")
        self.assertEqual(k1, k2)
        self.assertEqual(k1, ("OffloadTest", "Basic/simple.test"))


class DivergenceSuspectPrefix(unittest.TestCase):
    def test_gpu_wins(self):
        self.assertEqual(
            mf.divergence_suspect_prefix({"gpu_pattern": "AMD-only", "api_pattern": "Vulkan-only"}),
            "runtime_driver_suspected")

    def test_api_when_no_gpu(self):
        self.assertEqual(
            mf.divergence_suspect_prefix({"api_pattern": "Vulkan-only"}),
            "api_backend_suspected")

    def test_compiler_axis_is_not_upgraded(self):
        # A compiler-only split is reported as an axis but must NOT be upgraded
        # to a fault-implying label — clang-dxc/DXC emit different DXIL, so a
        # compiler split is different-output, not a proven compiler bug.
        self.assertIsNone(
            mf.divergence_suspect_prefix({"compiler_pattern": "clang-only"}))

    def test_fallback(self):
        self.assertEqual(
            mf.divergence_suspect_prefix({"host_pattern": "ARM64-only"}),
            "runtime_driver_suspected")


class CompactWorkflow(unittest.TestCase):
    CASES = [
        ("Windows Vulkan AMD DXC",        "AMD/Vulkan/DXC"),
        ("Windows Vulkan AMD Clang",      "AMD/Vulkan/Clang"),
        ("Windows D3D12 Warp DXC",        "Warp/D3D12/DXC"),
        ("macOS Metal DXC",              "Metal/DXC/macOS"),
        ("Windows ARM64 D3D12 Warp DXC",  "Warp/D3D12/DXC/ARM64"),
        ("Windows D3D12 AMD Clang GBV",   "AMD/D3D12/Clang/GBV"),
    ]

    def test_slugs(self):
        for name, slug in self.CASES:
            with self.subTest(name=name):
                self.assertEqual(mf.compact_workflow(name), slug)

    def test_md_wf_list_full_names_with_count(self):
        out = mf._md_wf_list(["Windows Vulkan AMD DXC", "macOS Metal DXC"])
        self.assertEqual(out, "2: Windows Vulkan AMD DXC, macOS Metal DXC")

    def test_md_wf_list_empty(self):
        self.assertEqual(mf._md_wf_list([]), "-")

    def test_truncate(self):
        self.assertEqual(mf._truncate("abcdef", 10), "abcdef")
        self.assertTrue(mf._truncate("x" * 100, 20).endswith("\u2026"))
        self.assertEqual(len(mf._truncate("x" * 100, 20)), 20)

    def test_fail_mode(self):
        self.assertEqual(mf._fail_mode("runtime_driver_suspected_crash"), "crash")
        self.assertEqual(mf._fail_mode("api_backend_suspected_miscompile"), "miscompile")
        self.assertEqual(mf._fail_mode("runtime_driver_suspected_unknown"), "unknown")
        # not an upgraded label -> returned as-is
        self.assertEqual(mf._fail_mode("runtime_pipeline_error"), "runtime_pipeline_error")


class IssueRendering(unittest.TestCase):
    T = {
        "linked_issue": {"url": "https://github.com/llvm/offload-test-suite/issues/337",
                         "state": "closed", "state_reason": "completed", "title": "AMD bug"},
        "linked_issues": [
            {"url": "https://github.com/llvm/llvm-project/issues/162841",
             "state": "open", "state_reason": None, "title": "VK bug"},
            {"url": "https://github.com/llvm/offload-test-suite/issues/337",
             "state": "closed", "state_reason": "completed", "title": "AMD bug"},
        ],
    }

    def test_records_primary_first_deduped(self):
        recs = mf._issue_records(self.T)
        urls = [r["url"] for r in recs]
        self.assertEqual(urls, [
            "https://github.com/llvm/offload-test-suite/issues/337",
            "https://github.com/llvm/llvm-project/issues/162841",
        ])

    def test_issue_number(self):
        self.assertEqual(mf._issue_number("https://github.com/o/r/issues/42"), "#42")
        self.assertEqual(mf._issue_number("not-a-url"), "issue")

    def test_emoji(self):
        self.assertEqual(mf._issue_emoji("open"), "\U0001F7E2")
        self.assertEqual(mf._issue_emoji("closed", "completed"), "\U0001F7E3")
        self.assertEqual(mf._issue_emoji("closed", "not_planned"), "\u26AB")
        self.assertEqual(mf._issue_emoji(None), "\u26AA")


class AmbiguousXfailMatch(unittest.TestCase):
    TEXT = (
        "# Bug https://github.com/llvm/llvm-project/issues/162841\n"
        "# XFAIL: Clang && Vulkan\n"
        "\n"
        "# Bug https://github.com/llvm/offload-test-suite/issues/337\n"
        "# XFAIL: AMD\n"
    )

    def test_reports_all_matched_issues(self):
        r = mf.match_xpass_to_issue(self.TEXT, "Windows Vulkan AMD Clang", "HLSLPC-AMD01")
        self.assertTrue(r.get("ambiguous"))
        self.assertEqual(sorted(r["issue_urls"]), [
            "https://github.com/llvm/llvm-project/issues/162841",
            "https://github.com/llvm/offload-test-suite/issues/337",
        ])
        self.assertIn("ambiguous", r["note"])

    def test_single_match_not_ambiguous(self):
        # A D3D12 AMD DXC run matches only the AMD clause.
        r = mf.match_xpass_to_issue(self.TEXT, "Windows D3D12 AMD DXC", "HLSLPC-AMD01")
        self.assertNotIn("ambiguous", r)
        self.assertEqual(r["issue_url"], "https://github.com/llvm/offload-test-suite/issues/337")


class ReadTestFileForRun(unittest.TestCase):
    def test_worktree_fallback_when_no_commit(self):
        # commit=None -> always the working tree, source 'worktree'.
        with unittest.mock.patch.object(mf, "find_test_file") as ff:
            fake = pathlib.Path(FIX) / "lit_status_lines.txt"  # any real file under tests/
            ff.return_value = fake
            # relative_to needs a root that contains the file:
            text, rel, src = mf.read_test_file_for_run(FIX, "OffloadTest-vk", "lit_status_lines.txt", None)
        self.assertEqual(src, "worktree")
        self.assertTrue(text)

    def test_prefers_commit_blob_when_available(self):
        with unittest.mock.patch.object(mf, "find_test_file") as ff, \
             unittest.mock.patch.object(mf, "git_commit_available", return_value=True), \
             unittest.mock.patch.object(mf, "git_show_file", return_value="BLOB CONTENT") as gs:
            ff.return_value = pathlib.Path(FIX) / "lit_status_lines.txt"
            text, rel, src = mf.read_test_file_for_run(FIX, "OffloadTest-vk", "lit_status_lines.txt", "abc1234567")
        self.assertEqual(text, "BLOB CONTENT")
        self.assertEqual(src, "abc123456")  # short sha (9)
        gs.assert_called_once()

    def test_falls_back_when_blob_missing(self):
        with unittest.mock.patch.object(mf, "find_test_file") as ff, \
             unittest.mock.patch.object(mf, "git_commit_available", return_value=True), \
             unittest.mock.patch.object(mf, "git_show_file", return_value=None):
            ff.return_value = pathlib.Path(FIX) / "lit_status_lines.txt"
            text, rel, src = mf.read_test_file_for_run(FIX, "OffloadTest-vk", "lit_status_lines.txt", "abc1234567")
        self.assertEqual(src, "worktree")
        self.assertTrue(text)

    def test_missing_file_returns_none(self):
        with unittest.mock.patch.object(mf, "find_test_file", return_value=None):
            text, rel, src = mf.read_test_file_for_run(FIX, "OffloadTest-vk", "nope.test", "abc")
        self.assertIsNone(text)
        self.assertIsNone(rel)


class GitFetchMode(unittest.TestCase):
    def setUp(self):
        mf._GIT_COMMIT_AVAILABLE.clear()
        mf._GIT_DEEPENED.clear()
        self._mode = mf._GIT_FETCH_MODE

    def tearDown(self):
        mf.set_git_fetch_mode(self._mode)
        mf._GIT_COMMIT_AVAILABLE.clear()
        mf._GIT_DEEPENED.clear()

    def test_off_mode_never_fetches(self):
        mf.set_git_fetch_mode("off")
        with unittest.mock.patch.object(mf, "_git_resolves", return_value=False), \
             unittest.mock.patch.object(mf, "_git") as g, \
             unittest.mock.patch.object(mf, "_git_deepen") as deep:
            self.assertFalse(mf.git_commit_available(pathlib.Path("/x"), "sha1"))
        g.assert_not_called()
        deep.assert_not_called()

    def test_targeted_fetch_only_for_full_sha(self):
        # A full 40-char sha the server allows can be fetched directly.
        full = "a" * 40
        mf.set_git_fetch_mode("targeted")
        with unittest.mock.patch.object(mf, "_git_resolves", side_effect=[False, True]), \
             unittest.mock.patch.object(mf, "_git") as g, \
             unittest.mock.patch.object(mf, "_git_deepen") as deep:
            self.assertTrue(mf.git_commit_available(pathlib.Path("/x"), full))
        g.assert_called_once()          # one targeted fetch
        deep.assert_not_called()

    def test_abbreviated_sha_skips_targeted_fetch(self):
        # Log SHAs are abbreviated and can't be fetched by ref, so no targeted
        # fetch is attempted (targeted mode just gives up).
        mf.set_git_fetch_mode("targeted")
        with unittest.mock.patch.object(mf, "_git_resolves", return_value=False), \
             unittest.mock.patch.object(mf, "_git") as g, \
             unittest.mock.patch.object(mf, "_git_deepen") as deep:
            self.assertFalse(mf.git_commit_available(pathlib.Path("/x"), "b69c9706"))
        g.assert_not_called()
        deep.assert_not_called()

    def test_unshallow_resolves_abbreviated_sha(self):
        # The real case: an abbreviated sha, missing locally, resolved by
        # deepening (unshallow) rather than a by-sha fetch.
        mf.set_git_fetch_mode("unshallow")
        with unittest.mock.patch.object(mf, "_git_resolves", side_effect=[False, True]), \
             unittest.mock.patch.object(mf, "_git") as g, \
             unittest.mock.patch.object(mf, "_git_deepen") as deep:
            self.assertTrue(mf.git_commit_available(pathlib.Path("/x"), "b69c9706"))
        g.assert_not_called()            # no doomed by-sha fetch
        deep.assert_called_once()        # deepened to get the history

    def test_result_is_cached(self):
        mf.set_git_fetch_mode("targeted")
        with unittest.mock.patch.object(mf, "_git_resolves", return_value=True) as res:
            self.assertTrue(mf.git_commit_available(pathlib.Path("/x"), "sha5"))
            self.assertTrue(mf.git_commit_available(pathlib.Path("/x"), "sha5"))
        self.assertEqual(res.call_count, 1)  # second call served from cache

    def test_deepen_unshallows_only_shallow_repos(self):
        # _git_deepen picks --unshallow for a shallow repo, plain fetch otherwise,
        # and runs at most once per repo.
        with unittest.mock.patch.object(mf, "_git_is_shallow", return_value=True), \
             unittest.mock.patch.object(mf, "_git") as g:
            mf._git_deepen(pathlib.Path("/x"))
            mf._git_deepen(pathlib.Path("/x"))  # cached: no second fetch
        self.assertEqual(g.call_count, 1)
        self.assertIn("--unshallow", g.call_args[0])


class HtmlReport(unittest.TestCase):
    def _report(self):
        from collections import Counter
        summary = [{
            "workflow": "Windows Vulkan AMD Clang", "conclusion": "failure",
            "category": "test_failure", "detail": None, "run_url": "https://x/1",
            "commits": {}, "tests": [{
                "result": "XPASS", "suite": "OffloadTest-clang-vk",
                "test": "Feature/StructuredBuffer/inc_counter_array.test",
                "classification": "xpass", "note": "ambiguous \u2014 2 clauses",
                "linked_issue": {"url": "https://github.com/llvm/offload-test-suite/issues/337",
                                 "state": "closed", "state_reason": "completed", "title": "AMD bug"},
                "linked_issues": [
                    {"url": "https://github.com/llvm/offload-test-suite/issues/337",
                     "state": "closed", "state_reason": "completed", "title": "AMD bug"}],
                "passes_on": ["Windows D3D12 Warp DXC"]}]}]
        divs = [{"suite": "OffloadTest", "test": "Feature/StructuredBuffer/inc_counter_array.test",
                 "classifications": ["runtime_driver_suspected_crash",
                                     "runtime_driver_suspected_miscompile"],
                 "fail_classifications": {
                     "Windows Lavapipe AMD DXC": "runtime_driver_suspected_crash",
                     "Windows Vulkan AMD Clang": "runtime_driver_suspected_miscompile"},
                 "axes": {"api_pattern": "Vulkan-only"},
                 "fails_on": ["Windows Lavapipe AMD DXC", "Windows Vulkan AMD Clang"],
                 "passes_on": ["Windows D3D12 Warp DXC"]}]
        return mf.render_html_report("2026-07-14T00-00-00Z", summary, divs, Counter({"xpass": 1}))

    def test_well_formed_and_contains_key_content(self):
        h = self._report()
        self.assertTrue(h.lstrip().lower().startswith("<!doctype"))
        self.assertTrue(h.rstrip().endswith("</html>"))
        # balanced non-void tags
        from html.parser import HTMLParser

        class V(HTMLParser):
            void = {"meta", "input", "br", "img", "link", "hr"}

            def __init__(self):
                super().__init__()
                self.stack = []

            def handle_starttag(self, t, a):
                if t not in self.void:
                    self.stack.append(t)

            def handle_endtag(self, t):
                if self.stack and self.stack[-1] == t:
                    self.stack.pop()
                elif t in self.stack:
                    while self.stack and self.stack.pop() != t:
                        pass

        v = V()
        v.feed(h)
        self.assertEqual(v.stack, [])

    def test_issue_badge_has_hover_title(self):
        self.assertIn('title="AMD bug"', self._report())

    def test_classification_chip_present(self):
        self.assertIn("chip", self._report())

    def test_axis_rendered_as_chips_not_plain_text(self):
        h = self._report()
        self.assertIn('class="axis ax-api"', h)
        self.assertIn("Vulkan-only", h)
        # the old plain "api: Vulkan-only" rendering is gone
        self.assertNotIn("<td>api: Vulkan-only", h)

    def test_axes_legend_paragraph_present(self):
        h = self._report()
        self.assertIn("<b>Axes.</b>", h)
        self.assertIn("non-Vulkan workflow passes", h)
        # the value key lists every dimension
        for dim in ("api", "gpu", "compiler", "host", "variant"):
            self.assertIn(f"<span class=k>{dim}</span>", h)

    def test_fails_passes_as_named_pills(self):
        h = self._report()
        # divergence fails-on / passes-on render as count badge + full-name pills,
        # not a slash-compacted comma list.
        self.assertIn("<span class=wfcount>", h)
        # passes-on pill is a plain full name; no compact slash-slug in HTML.
        self.assertIn("<span class=wf>Windows D3D12 Warp DXC</span>", h)
        self.assertNotIn("AMD/Vulkan/Clang", h)

    def test_divergence_shows_per_workflow_failure_modes(self):
        # A test that crashes on one workflow but miscompiles on others must show
        # BOTH classifications and annotate each failing workflow with its mode.
        h = self._report()
        self.assertIn("runtime_driver_suspected_crash", h)
        self.assertIn("runtime_driver_suspected_miscompile", h)
        self.assertIn("Windows Lavapipe AMD DXC <span class=wfmode>crash</span>", h)
        self.assertIn("Windows Vulkan AMD Clang <span class=wfmode>miscompile</span>", h)

    def test_toolbar_has_title_timestamp_filter_and_toggle(self):
        h = self._report()
        self.assertIn("<div id=toolbar>", h)
        self.assertIn("<span class=title>offload-test-suite report</span>", h)
        self.assertIn("2026-07-14T00-00-00Z", h)      # timestamp in the sub span
        # filter + toggle live in the toolbar; exactly one of each in the doc
        self.assertEqual(h.count("id=filter"), 1)
        self.assertEqual(h.count("id=themeBtn"), 1)
        # the old in-flow header is gone (title/timestamp now live in the bar)
        self.assertNotIn("<h1>", h)

    def test_dark_mode_toggle_present(self):
        h = self._report()
        self.assertIn("id=themeBtn", h)               # toggle button
        self.assertIn("toggleTheme()", h)             # onclick + fn
        self.assertIn('[data-theme="dark"]', h)       # dark palette
        self.assertIn("prefers-color-scheme", h)      # OS default
        self.assertIn("otss-theme", h)                # localStorage persistence


class ColumnLegends(unittest.TestCase):
    """
    Guards the workflow table's category/detail legends against drift, the same
    way ClassificationLegend guards the per-test labels.
    """
    EXPECTED_CATEGORIES = {"build_failure", "test_failure"}
    # build subtree values from classify_build_failure + the test_failure detail
    EXPECTED_DETAILS = {"clang_llvm", "dxc", "other", "unknown", "unknown_no_blocks"}

    def test_category_legend_complete_and_not_stale(self):
        self.assertEqual(set(mf.CATEGORY_LEGEND), self.EXPECTED_CATEGORIES)

    def test_detail_legend_complete_and_not_stale(self):
        self.assertEqual(set(mf.DETAIL_LEGEND), self.EXPECTED_DETAILS)

    def test_explanations_non_trivial(self):
        for legend in (mf.CATEGORY_LEGEND, mf.DETAIL_LEGEND):
            for key, expl in legend.items():
                with self.subTest(key=key):
                    self.assertGreater(len(expl), 40, f"legend for {key!r} is too short")

    def test_classify_build_failure_details_are_documented(self):
        # Every value classify_build_failure can return must have a legend entry.
        for detail in ("clang_llvm", "dxc", "other", "unknown"):
            self.assertIn(detail, mf.DETAIL_LEGEND)


class FmtCommits(unittest.TestCase):
    def test_includes_all_three_repos(self):
        out = mf._fmt_commits({
            "llvm-project": "f60650c77abc",
            "directxshadercompiler": "dc3e6c48ef",
            "offload-test-suite": "abc1234def",
        })
        self.assertEqual(out, "llvm `f60650c77` \u00b7 dxc `dc3e6c48e` \u00b7 offload `abc1234de`")

    def test_offload_alone(self):
        self.assertEqual(mf._fmt_commits({"offload-test-suite": "abc1234def"}),
                         "offload `abc1234de`")

    def test_empty(self):
        self.assertEqual(mf._fmt_commits({}), "")


# ---------------------------------------------------------------------------
# End-to-end smoke test: drive main() with the network boundary mocked so the
# full report-generation path runs — workflow loop, classification, the
# cross-workflow pivot, and the markdown / html / json / csv assembly.
# ---------------------------------------------------------------------------

_LOG_A = """\
Runner name: 'HLSLPC-AMD01'
Syncing repository: llvm/offload-test-suite
HEAD is now at abc1234ef offload commit
Syncing repository: llvm/llvm-project
HEAD is now at f60650c77 llvm commit
Testing: 3 tests
PASS: OffloadTest-clang-vk :: Feature/Foo/keep.test (1 of 3)
FAIL: OffloadTest-clang-vk :: Feature/Foo/bar.test (2 of 3)
XPASS: OffloadTest-clang-vk :: Feature/StructuredBuffer/inc_counter_array.test (3 of 3)
******************** TEST 'OffloadTest-clang-vk :: Feature/Foo/bar.test' FAILED ********************
# executed command: dxc.exe -T cs_6_0 -Fo bar.o bar.hlsl
# executed command: offloader.exe pipeline.yaml bar.o
# .---command stdout------------
# | Test failed: image mismatch
# `-----------------------------
# error: command failed with exit status: 1
********************
"""

_LOG_B = """\
Runner name: 'HLSLPC-AMD01'
Syncing repository: llvm/offload-test-suite
HEAD is now at abc1234ef offload commit
Testing: 2 tests
PASS: OffloadTest-d3d12 :: Feature/Foo/bar.test (1 of 2)
PASS: OffloadTest-d3d12 :: Feature/Foo/keep.test (2 of 2)
"""

_LOG_C = """\
Runner name: 'HLSLPC-NVIDIA01'
Syncing repository: llvm/llvm-project
HEAD is now at f60650c77 llvm commit
CMake Error at clang/CMakeLists.txt:10 (message):
  something broke in llvm-project
ninja: build stopped: subcommand failed.
"""


class MainSmoke(unittest.TestCase):
    def _run_main(self, out_root: pathlib.Path, otss_root: pathlib.Path):
        workflows = [
            {"id": 1, "name": "Windows Vulkan AMD Clang",
             "path": ".github/workflows/a.yaml", "state": "active"},
            {"id": 2, "name": "Windows D3D12 AMD DXC",
             "path": ".github/workflows/b.yaml", "state": "active"},
            {"id": 3, "name": "Windows D3D12 NVIDIA Clang",
             "path": ".github/workflows/c.yaml", "state": "active"},
        ]
        runs = {
            1: {"id": 1, "html_url": "https://x/1", "conclusion": "failure",
                "status": "completed", "created_at": "t", "head_sha": "aaa"},
            2: {"id": 2, "html_url": "https://x/2", "conclusion": "success",
                "status": "completed", "created_at": "t", "head_sha": "bbb"},
            3: {"id": 3, "html_url": "https://x/3", "conclusion": "failure",
                "status": "completed", "created_at": "t", "head_sha": "ccc"},
        }
        logs = {1: _LOG_A, 2: _LOG_B, 3: _LOG_C}
        argv = ["monitor_failures.py",
                "--otss-root", str(otss_root), "--out-root", str(out_root)]
        with unittest.mock.patch.object(mf, "load_token", return_value="tok"), \
             unittest.mock.patch.object(mf, "fetch_workflows", return_value=workflows), \
             unittest.mock.patch.object(mf, "latest_scheduled_run",
                                        side_effect=lambda gh, wid: runs[wid]), \
             unittest.mock.patch.object(mf, "download_run_logs",
                                        side_effect=lambda gh, rid: rid), \
             unittest.mock.patch.object(mf, "combined_log_text",
                                        side_effect=lambda z: logs[z]), \
             unittest.mock.patch.object(mf, "git_commit_available", return_value=False), \
             unittest.mock.patch.object(mf, "fetch_issue_state",
                 return_value={"state": "closed", "state_reason": "completed", "title": "AMD bug"}), \
             unittest.mock.patch.object(sys, "argv", argv):
            mf.main()
        out_dirs = [p for p in out_root.iterdir() if p.is_dir()]
        self.assertEqual(len(out_dirs), 1)
        return out_dirs[0]

    def test_full_report_generation(self):
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            otss = td / "otss"
            tf = otss / "test" / "Feature" / "StructuredBuffer" / "inc_counter_array.test"
            tf.parent.mkdir(parents=True)
            tf.write_text("# Bug https://github.com/llvm/offload-test-suite/issues/337\n"
                          "# XFAIL: AMD\n")
            out_root = td / "reports"
            out_dir = self._run_main(out_root, otss)

            # All five artifacts exist.
            for name in ("summary.md", "summary.html", "summary.json",
                         "summary.csv", "divergences.json"):
                self.assertTrue((out_dir / name).exists(), f"missing {name}")

            summary = json.loads((out_dir / "summary.json").read_text())
            self.assertEqual(len(summary), 3)
            by_wf = {r["workflow"]: r for r in summary}

            # Build failure classified.
            self.assertEqual(by_wf["Windows D3D12 NVIDIA Clang"]["category"], "build_failure")
            self.assertEqual(by_wf["Windows D3D12 NVIDIA Clang"]["detail"], "clang_llvm")

            # Runtime miscompile + XPASS classified on workflow A.
            a_tests = {t["test"]: t for t in by_wf["Windows Vulkan AMD Clang"]["tests"]}
            self.assertIn("Feature/Foo/bar.test", a_tests)
            self.assertEqual(a_tests["Feature/StructuredBuffer/inc_counter_array.test"]["result"], "XPASS")
            self.assertEqual(
                a_tests["Feature/StructuredBuffer/inc_counter_array.test"]["linked_issue"]["url"],
                "https://github.com/llvm/offload-test-suite/issues/337")

            # Cross-workflow pivot upgraded bar.test (fails on Vulkan, passes on D3D12).
            self.assertTrue(a_tests["Feature/Foo/bar.test"]["classification"].endswith("_suspected_miscompile"))
            divs = json.loads((out_dir / "divergences.json").read_text())
            self.assertTrue(any(d["test"] == "Feature/Foo/bar.test" for d in divs))

            # Markdown structure: collapsible sections, short issue link, no suite prefix.
            md = (out_dir / "summary.md").read_text()
            # Renamed commits column + offload-test-suite sha present (log A
            # syncs offload-test-suite at abc1234ef).
            self.assertIn("tested commits (llvm / dxc / offload)", md)
            self.assertIn("offload `abc1234ef`", md)
            self.assertIn("<details", md)
            self.assertIn("## Failures by workflow", md)
            # Each per-workflow section links its run (workflow A's run is /1).
            self.assertIn("[run](https://x/1)", md)
            # Category / detail column legend rendered with the used values.
            self.assertIn("Category / detail column legend", md)
            self.assertIn("reached the lit test stage", md)  # test_failure
            self.assertIn("llvm-project / clang / clang-dxc subtree", md)  # detail clang_llvm
            self.assertIn("[#337](", md)
            self.assertIn("| result | test | classification | issues | notes |", md)
            self.assertNotIn("OffloadTest-clang-vk ::", md)  # suite prefix stripped

            # HTML: badge with hover title + chip, well-formed close.
            h = (out_dir / "summary.html").read_text()
            self.assertIn('title="AMD bug"', h)
            self.assertIn("chip", h)
            self.assertTrue(h.rstrip().endswith("</html>"))

            # Log archives written per workflow.
            self.assertTrue((out_dir / "logs").is_dir())
            self.assertTrue(list((out_dir / "logs").glob("*.log.gz")))

if __name__ == "__main__":
    unittest.main()
