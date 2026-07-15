"""Regression tests for offloader-scripts/build_site.py.

Covers the deterministic site-assembly logic: timestamp parsing, copying new
reports (minus logs), 30-day pruning, health summarisation, and landing-page
rendering. Offline, stdlib-only, no GH_TOKEN needed.

Run:  python3 -m unittest discover -s offloader-scripts/tests -v
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import build_site as bs  # noqa: E402


def _write_report(root: pathlib.Path, ts: str, rows, *, with_logs: bool = True) -> pathlib.Path:
    d = root / ts
    (d / "logs").mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps(rows))
    (d / "summary.html").write_text(f"<html>{ts}</html>")
    (d / "summary.csv").write_text("h\n")
    if with_logs:
        (d / "logs" / "a.log.gz").write_bytes(b"\x1f\x8b")  # bulky, should be skipped
    return d


class ParseTsTest(unittest.TestCase):
    def test_valid(self):
        got = bs.parse_ts("2026-07-15T01-26-47Z")
        self.assertEqual(got, dt.datetime(2026, 7, 15, 1, 26, 47, tzinfo=dt.UTC))

    def test_rejects_junk(self):
        for name in ("index.html", "2026-07-15", "reports", "2026-13-99T99-99-99Z"):
            self.assertIsNone(bs.parse_ts(name), name)


class CopyNewReportsTest(unittest.TestCase):
    def test_copies_publish_files_and_skips_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            reports = tmp / "reports"
            _write_report(reports, "2026-07-15T01-26-47Z", [{"conclusion": "success"}])
            site_reports = tmp / "site" / "reports"
            site_reports.mkdir(parents=True)

            n = bs.copy_new_reports(reports, site_reports)
            self.assertEqual(n, 1)
            dst = site_reports / "2026-07-15T01-26-47Z"
            self.assertTrue((dst / "summary.html").is_file())
            self.assertTrue((dst / "summary.json").is_file())
            self.assertFalse((dst / "logs").exists(), "logs dir must not be published")

    def test_ignores_non_report_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            reports = tmp / "reports"
            (reports / "not-a-report").mkdir(parents=True)
            site_reports = tmp / "site"
            site_reports.mkdir()
            self.assertEqual(bs.copy_new_reports(reports, site_reports), 0)


class PruneOldTest(unittest.TestCase):
    def test_removes_only_older_than_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            site_reports = pathlib.Path(tmp)
            now = dt.datetime.now(dt.UTC)
            fresh = now.strftime("%Y-%m-%dT%H-%M-%SZ")
            old = (now - dt.timedelta(days=45)).strftime("%Y-%m-%dT%H-%M-%SZ")
            _write_report(site_reports, fresh, [], with_logs=False)
            _write_report(site_reports, old, [], with_logs=False)

            removed = bs.prune_old(site_reports, max_age_days=30)
            self.assertEqual(removed, [old])
            self.assertTrue((site_reports / fresh).exists())
            self.assertFalse((site_reports / old).exists())


class ReportHealthTest(unittest.TestCase):
    def test_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            d = _write_report(root, "2026-07-15T01-26-47Z", [
                {"conclusion": "success"},
                {"conclusion": "failure", "tests": [{}, {}]},
                {"conclusion": None, "status": "in_progress"},
            ], with_logs=False)
            info = bs.report_health(d)
            self.assertTrue(info["ok"])
            self.assertEqual(info["workflows"], 3)
            self.assertEqual(info["failing"], 1)
            self.assertEqual(info["incomplete"], 1)
            self.assertEqual(info["tests"], 2)

    def test_missing_summary_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = bs.report_health(pathlib.Path(tmp))
            self.assertFalse(info["ok"])


class RenderIndexTest(unittest.TestCase):
    def test_lists_reports_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            site_reports = pathlib.Path(tmp)
            _write_report(site_reports, "2026-07-15T01-00-00Z",
                          [{"conclusion": "failure", "tests": [{}]}], with_logs=False)
            _write_report(site_reports, "2026-07-15T02-00-00Z",
                          [{"conclusion": "success"}], with_logs=False)
            out = bs.render_index(site_reports, "llvm/offload-test-suite")
            self.assertIn("reports/2026-07-15T02-00-00Z/summary.html", out)
            self.assertIn("reports/2026-07-15T01-00-00Z/summary.html", out)
            # newest (02:00) must appear before older (01:00)
            self.assertLess(out.index("2026-07-15T02-00-00Z"),
                            out.index("2026-07-15T01-00-00Z"))
            self.assertIn("all green", out)
            self.assertIn("1 failing", out)

    def test_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = bs.render_index(pathlib.Path(tmp), "llvm/offload-test-suite")
            self.assertIn("No reports yet", out)


if __name__ == "__main__":
    unittest.main()
