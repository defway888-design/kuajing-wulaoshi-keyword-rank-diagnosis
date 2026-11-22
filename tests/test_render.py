"""Synthetic renderer tests. These never contact or verify a live MCP."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from test_analyze import fixture, sync_counts, position, PACKAGE

sys.path.insert(0, str(PACKAGE / "scripts"))
import render_dashboard as renderer


class RenderTests(unittest.TestCase):
    def test_template_manifest_and_single_slot(self):
        self.assertEqual(renderer.read_template().count("__REPORT_DATA__"), 1)

    def test_tampered_template_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            for name in ("dashboard_template.html", "template_manifest.json"):
                (root / "assets" / name).write_bytes((PACKAGE / "assets" / name).read_bytes())
            with (root / "assets" / "dashboard_template.html").open("a") as f:
                f.write("changed")
            with self.assertRaisesRegex(ValueError, "SHA256"):
                renderer.render(fixture(), package=root)

    def test_missing_template_stops(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(OSError):
            renderer.render(fixture(), package=Path(tmp))

    def test_no_connection_cannot_render_even_preview(self):
        payload = fixture()
        del payload["source"]["connection"]
        for preview in (False, True):
            with self.subTest(preview=preview), self.assertRaises(ValueError):
                renderer.render(payload, preview)

    def test_categories_and_gate_match_analyzer(self):
        payload = fixture(dict(zip("abcdefgh", ["OOO", "ONO", "AAA", "ANA", "BNN", "OAN", "RRR", "UON"])))
        html, data = renderer.render(payload)
        self.assertEqual(len(data["keywords"]), 8)
        self.assertEqual(sum(r["coreA"] for r in data["keywords"]), 1)
        self.assertEqual(sum(r["coreB"] for r in data["keywords"]), 1)
        self.assertEqual(data["status"], "partial")
        self.assertNotIn("function classify(", html)

    def test_global_incomplete_cannot_be_reclassified_by_browser(self):
        payload = fixture()
        payload["daily"][0]["pagination"]["complete"] = False
        data = renderer.view_data(payload)
        self.assertEqual(data["keywords"][0]["category"], "insufficient")
        self.assertFalse(data["keywords"][0]["coreA"])

    def test_ambiguity_alongside_positive_is_retained(self):
        payload = fixture()
        payload["daily"][0]["entries"][0]["positions"].append(position("unknown"))
        data = renderer.view_data(payload)
        self.assertEqual(data["keywords"][0]["daily"][0]["organic"], "observed")
        self.assertTrue(data["keywords"][0]["daily"][0]["ambiguous"]["organic"])
        self.assertEqual(data["keywords"][0]["category"], "insufficient")

    def test_all_rank_fields_and_capture_records_preserved(self):
        payload = fixture()
        positions = payload["daily"][0]["entries"][0]["positions"]
        positions[0].update(total_rank=23, page_rank=3, page=2, captured_at="2000-01-01T01:00:00Z")
        positions.append(position("organic", total_rank=27, captured_at="2000-01-01T02:00:00Z"))
        records = renderer.view_data(payload)["keywords"][0]["daily"][0]["records"]
        self.assertEqual(len(records), 2)
        self.assertEqual([records[0][k] for k in ("totalRank", "pageRank", "page")], [23, 3, 2])

    def test_source_valid_is_distinct_from_confirmed_semantics(self):
        payload = fixture()
        payload["daily"][0]["entries"][0]["positions"].append(position("organic", source_type="   ", total_rank=987))
        data = renderer.view_data(payload)
        records = data["keywords"][0]["daily"][0]["records"]
        self.assertTrue(records[0]["valid"])
        self.assertTrue(records[1]["sourceValid"])
        self.assertFalse(records[1]["valid"])
        self.assertEqual(records[1]["totalRank"], 987)
        self.assertEqual(data["keywords"][0]["category"], "insufficient")

    def test_data_script_cannot_be_closed_by_input(self):
        value = '</script><img src=x onerror="alert(1)">&'
        html, data = renderer.render(fixture({value: "OOO"}))
        self.assertNotIn(value, html)
        encoded = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', html, re.S)[1]
        self.assertEqual(json.loads(encoded)["keywords"][0]["keyword"], value)

    def test_raw_fields_and_connection_evidence_not_embedded(self):
        payload = fixture()
        payload["source"]["connection"]["evidence"] = "private_connection_marker"
        payload["daily"][0]["entries"][0]["raw"] = {"secret": "private_raw_marker"}
        payload["window"]["extra"] = "private_window_marker"
        html, _ = renderer.render(payload)
        for value in ("private_connection_marker", "private_raw_marker", "private_window_marker"):
            self.assertNotIn(value, html)

    def test_empty_and_single_day_and_missing_product(self):
        empty = fixture()
        for day in empty["daily"]:
            day["entries"] = []
        self.assertEqual(renderer.view_data(sync_counts(empty))["keywords"], [])
        one = renderer.view_data(fixture({"single": "O"}, 1))
        self.assertEqual(one["window"]["observation_label"], "单日观测")
        payload = fixture({"a": "OOO", "b": "AAA"})
        payload["product"] = {}
        data = renderer.view_data(payload)
        self.assertEqual(sum(r["coreA"] for r in data["keywords"]), 0)
        self.assertEqual(sum(r["coreB"] for r in data["keywords"]), 1)

    def test_preview_is_explicit_and_not_default(self):
        self.assertEqual(renderer.view_data(fixture())["mode"], "xiyou-diagnosis")
        self.assertEqual(renderer.view_data(fixture(), True)["mode"], "synthetic-preview")

    def test_no_runtime_mock_data_or_external_assets(self):
        html = renderer.read_template()
        self.assertNotIn('"keywords":[', html)
        self.assertNotRegex(html, r'(?:src|href)=["\']https?://')
        self.assertNotIn("SP 搜索广告", html)
        self.assertNotIn("3/3", html)
        self.assertIn("connect-src 'none'", html)

    def test_cli_no_overwrite_or_output_on_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = root / "input.json", root / "result.html"
            source.write_text(json.dumps(fixture()), encoding="utf-8")
            command = [sys.executable, "-B", str(PACKAGE / "scripts" / "render_dashboard.py"), "--input", str(source), "--output", str(target), "--preview"]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            before = target.read_bytes()
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
            self.assertEqual(target.read_bytes(), before)
            invalid = fixture()
            invalid["source"]["connection"]["status"] = "not_configured"
            source.write_text(json.dumps(invalid), encoding="utf-8")
            command[command.index(str(target))] = str(root / "blocked.html")
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
            self.assertFalse((root / "blocked.html").exists())


if __name__ == "__main__":
    unittest.main()
