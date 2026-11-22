"""Synthetic-only acceptance tests; no live ASIN, account, or source connection."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import importlib.util
from itertools import product
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


PACKAGE = Path(__file__).resolve().parents[1]
SCRIPT = PACKAGE / "scripts" / "analyze.py"
SPEC = importlib.util.spec_from_file_location("rank_analysis", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
O, A, U = module.OBSERVED, module.ABSENT, module.UNKNOWN


def position(kind, **updates):
    item = {"kind": kind, "valid": True, "source_type": f"synthetic-{kind}",
            "captured_at": None, "total_rank": None, "page_rank": None, "page": None}
    if kind == "organic":
        item["total_rank"] = 3
    if kind == "ad":
        item["page"] = 2
    return {**item, **updates}


def sync_counts(payload):
    for day in payload["daily"]:
        size = len(day["entries"])
        day["pagination"].update(received_rows=size, reported_total=size)
    return payload


def fixture(patterns=None, n=3):
    patterns = patterns or {"synthetic term": "O" * n}
    end = date(2000, 1, 3)
    dates = [(end - timedelta(days=i)).isoformat() for i in reversed(range(n))]
    payload = {
        "schema_version": 2, "asin": "T" + "0" * 9, "country": "ZZ", "days": n,
        "source": {"name": "xiyou", "timezone": "UTC (synthetic test only)",
                   "connection": {"status": "connected", "checked_at": datetime.now(timezone.utc).isoformat(),
                                  "evidence": "synthetic preflight only; no real MCP connection",
                                  "daily_keywords_available": True},
                   "date_basis_evidence": "synthetic date contract",
                   "mapping_evidence": "synthetic position contract",
                   "coverage": {"organic": "synthetic natural sample", "ad": "synthetic ad sample"}},
        "window": {"initial_end": dates[-1], "end": dates[-1]},
        "product": {"title": "Synthetic lamp; not a real product", "evidence": "synthetic fixture"},
        "relevance": {k: {"status": "related", "reason": "synthetic test relevance",
                          "evidence": "synthetic title"} for k in patterns},
        "daily": [],
    }
    for index, selected in enumerate(dates):
        entries = []
        for keyword, pattern in patterns.items():
            token = pattern[index]
            kinds = {"N": [], "O": ["organic"], "A": ["ad"], "B": ["organic", "ad"],
                     "R": ["other"], "U": ["unknown"]}[token]
            entries.append({"keyword": keyword, "positions": [position(k) for k in kinds]})
        payload["daily"].append({
            "date": selected, "completion": "complete", "completion_evidence": "synthetic complete day",
            "date_verified": True, "date_evidence": "synthetic response date",
            "pagination": {"complete": True, "evidence": "synthetic final page",
                           "requested": [1], "received": [1]}, "entries": entries,
        })
    return sync_counts(payload)


class ConnectionPreflightTests(unittest.TestCase):
    def test_missing_preflight_blocks_analysis(self):
        payload = fixture()
        del payload["source"]["connection"]
        with self.assertRaisesRegex(ValueError, "缺少本次西柚 MCP 接入检查"):
            module.analyze(payload)

    def test_not_configured_prompts_configuration(self):
        payload = fixture()
        payload["source"]["connection"]["status"] = "not_configured"
        with self.assertRaisesRegex(ValueError, "尚未配置西柚 MCP.*完成配置、授权并启用"):
            module.analyze(payload)

    def test_unknown_status_never_implies_unconfigured(self):
        for status in ("unknown", None, "invented"):
            payload = fixture()
            payload["source"]["connection"]["status"] = status
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "未确认可用的西柚 MCP"):
                module.analyze(payload)

    def test_auth_and_connection_failure_have_distinct_prompts(self):
        for status, message in (("unauthorized", "尚未授权或权限不足"), ("unavailable", "已配置但当前连接不可用")):
            payload = fixture()
            payload["source"]["connection"]["status"] = status
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, message):
                module.analyze(payload)

    def test_missing_daily_capability_blocks(self):
        for available in (False, None, "true", 1):
            payload = fixture()
            payload["source"]["connection"]["daily_keywords_available"] = available
            with self.subTest(available=available), self.assertRaisesRegex(ValueError, "未确认日级反查能力"):
                module.analyze(payload)

    def test_connected_flag_alone_is_insufficient(self):
        for update in ({"evidence": ""}, {"evidence": None}, {"checked_at": ""},
                       {"checked_at": "not a time"}, {"checked_at": "2000-01-03T00:00:00"},
                       {"checked_at": True}):
            payload = fixture()
            payload["source"]["connection"].update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                module.analyze(payload)

    def test_preflight_runs_before_business_validation_and_processing(self):
        payload = fixture()
        payload["asin"] = "invalid"
        payload["source"]["connection"]["status"] = "not_configured"
        with patch.object(module, "normalize_day") as normalize:
            with self.assertRaisesRegex(ValueError, "尚未配置西柚 MCP"):
                module.analyze(payload)
            normalize.assert_not_called()

    def test_legacy_contract_is_rejected(self):
        payload = fixture()
        payload["schema_version"] = 1
        with self.assertRaisesRegex(ValueError, "current version is 2"):
            module.analyze(payload)

    def test_cli_no_report_when_mcp_is_unconfigured(self):
        with tempfile.TemporaryDirectory(prefix="rank-preflight-test-") as tmp:
            input_path, output_path = Path(tmp) / "input.json", Path(tmp) / "result.json"
            payload = fixture()
            payload["source"]["connection"]["status"] = "not_configured"
            input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(SCRIPT), "--input", str(input_path),
                                     "--output", str(output_path)], capture_output=True, encoding="utf-8")
            self.assertEqual(result.returncode, 2)
            self.assertIn("尚未配置西柚 MCP", result.stderr)
            self.assertIn("完成配置、授权并启用", result.stderr)
            self.assertFalse(output_path.exists())


class ClassificationTests(unittest.TestCase):
    def test_list_and_tuple_state_pairs_are_equivalent(self):
        self.assertEqual(module.classify([[O, O]]), "同日双端覆盖词")

    def test_exhaustive_known_three_day_states(self):
        counts = {category: 0 for category in module.CATEGORIES}
        for states in product(((A, A), (O, A), (A, O), (O, O)), repeat=3):
            counts[module.classify(states)] += 1
        self.assertEqual(list(counts.values()), [1, 6, 1, 6, 37, 12, 1, 0])

    def test_exhaustive_three_state_priority(self):
        for states in product(tuple(product((O, A, U), repeat=2)), repeat=3):
            result = module.classify(states)
            self.assertEqual(result == "数据不足", any(U in pair for pair in states))

    def test_eight_categories_and_two_core_lists(self):
        patterns = dict(zip(module.CATEGORIES, ["OOO", "ONO", "AAA", "ANA", "BNN", "OAN", "RRR", "UON"]))
        result = module.analyze(fixture(patterns))
        self.assertEqual(result["category_counts"], dict.fromkeys(module.CATEGORIES, 1))
        self.assertEqual(result["core_natural"], ["自然持续出现词"])
        self.assertEqual(result["core_ad_only"], ["广告持续出现词"])
        self.assertEqual(result["status"], "partial")

    def test_one_day_is_labelled_and_has_no_occasional_categories(self):
        result = module.analyze(fixture({"a": "O", "b": "A", "c": "B", "d": "R"}, 1))
        self.assertEqual(result["window"]["observation_label"], "单日观测")
        self.assertEqual(result["category_counts"]["自然观察词"], 0)
        self.assertEqual(result["category_counts"]["广告观察词"], 0)

    def test_default_days(self):
        payload = fixture()
        del payload["days"]
        self.assertEqual(module.analyze(payload)["request"]["days"], 3)

    def test_invalid_days(self):
        for invalid in (0, -1, 1.5, "3", True, None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                module.analyze({**fixture(), "days": invalid})

    def test_invalid_identity_or_source(self):
        for field, value in (("asin", "short"), ("country", ""), ("schema_version", True)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                module.analyze({**fixture(), field: value})
        payload = fixture()
        payload["source"]["name"] = "another-source"
        with self.assertRaises(ValueError):
            module.analyze(payload)


class IntegrityTests(unittest.TestCase):
    def test_missing_middle_day_never_backfills(self):
        payload = fixture()
        missing = payload["daily"].pop(1)["date"]
        result = module.analyze(payload)
        self.assertFalse(result["window_complete"])
        self.assertEqual(result["core_natural"], [])
        self.assertIn(missing, result["window"]["dates"])
        self.assertEqual(result["keywords"][0]["daily"][1]["organic"], U)

    def test_out_of_window_and_duplicate_dates_rejected(self):
        for kind in ("outside", "duplicate"):
            payload = fixture()
            extra = deepcopy(payload["daily"][0])
            if kind == "outside":
                extra["date"] = "1999-12-31"
            payload["daily"].append(extra)
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                module.analyze(payload)

    def test_shift_requires_evidence_and_limit(self):
        for shift, evidence, allowed in ((7, True, True), (8, True, False), (1, False, False), (-1, True, False)):
            payload = fixture()
            payload["window"]["initial_end"] = (date(2000, 1, 3) + timedelta(days=shift)).isoformat()
            if evidence:
                payload["window"].update(shift_reason="confirmed_update_lag", shift_evidence="synthetic lag")
            with self.subTest(shift=shift, evidence=evidence):
                if allowed:
                    self.assertEqual(module.analyze(payload)["window"]["shift_days"], shift)
                else:
                    with self.assertRaises(ValueError):
                        module.analyze(payload)

    def test_confirmed_empty_is_valid(self):
        payload = fixture()
        for day in payload["daily"]:
            day["entries"] = []
        result = module.analyze(sync_counts(payload))
        self.assertTrue(result["window_complete"])
        self.assertEqual(result["keyword_count"], 0)
        self.assertEqual(result["core_status"]["natural"], "本期无符合条件的关键词")

    def test_empty_without_completion_not_interpreted(self):
        payload = fixture()
        for day in payload["daily"]:
            day.update(entries=[], completion="unknown", completion_evidence="")
        result = module.analyze(sync_counts(payload))
        self.assertFalse(result["window_complete"])
        self.assertEqual(result["core_status"]["ad_only"], "数据不足，无法判定")

    def test_paging_failure_blocks_core_but_keeps_known_observation(self):
        payload = fixture()
        payload["daily"][1]["pagination"].update(requested=[1, 2], received=[1], complete=False)
        result = module.analyze(payload)
        self.assertEqual(result["keywords"][0]["category"], "数据不足")
        self.assertEqual(result["keywords"][0]["daily"][1]["organic"], O)
        self.assertEqual(result["core_natural"], [])

    def test_paging_totals_and_duplicate_page_ids_fail_closed(self):
        for update in ({"reported_total": 2}, {"received_rows": 2}, {"received": [1, 1]}, {"evidence": ""}):
            payload = fixture()
            payload["daily"][0]["pagination"].update(update)
            with self.subTest(update=update):
                self.assertFalse(module.analyze(payload)["window_complete"])

    def test_date_completion_and_mapping_evidence_required(self):
        for field in ("completion_evidence", "date_evidence"):
            payload = fixture()
            payload["daily"][0][field] = ""
            with self.subTest(field=field):
                self.assertFalse(module.analyze(payload)["window_complete"])
        for field in ("timezone", "date_basis_evidence", "mapping_evidence"):
            payload = fixture()
            payload["source"][field] = ""
            with self.subTest(field=field):
                self.assertEqual(module.analyze(payload)["core_natural"], [])

    def test_unknown_ad_scope_blocks_negative_claim(self):
        payload = fixture()
        payload["source"]["coverage"]["ad"] = ""
        result = module.analyze(payload)
        self.assertEqual(result["keywords"][0]["daily"][0]["ad"], U)
        self.assertEqual(result["core_natural"], [])

    def test_partial_ad_scope_is_kept_on_each_result(self):
        payload = fixture()
        payload["source"]["coverage"]["ad"] = "synthetic single ad type only"
        result = module.analyze(payload)
        self.assertEqual(result["keywords"][0]["scope"]["ad"], "synthetic single ad type only")


class ObservationTests(unittest.TestCase):
    def test_page_only_ad_counts_without_inventing_rank(self):
        result = module.analyze(fixture({"page only": "AAA"}))
        item = result["keywords"][0]
        self.assertEqual(item["ad_days"], 3)
        self.assertEqual(item["category"], "广告持续出现词")
        self.assertIsNone(item["daily"][0]["positions"][0]["total_rank"])

    def test_numeric_rank_not_required_for_confirmed_organic_presence(self):
        payload = fixture()
        for day in payload["daily"]:
            day["entries"][0]["positions"][0]["total_rank"] = None
        self.assertEqual(module.analyze(payload)["keywords"][0]["organic_days"], 3)

    def test_raw_zero_is_not_accepted_as_normalized_rank(self):
        for invalid in (0, -1, True, 1.2, "3"):
            payload = fixture()
            payload["daily"][0]["entries"][0]["positions"][0]["total_rank"] = invalid
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                module.analyze(payload)

    def test_blank_position_type_is_not_valid_evidence(self):
        payload = fixture()
        for day in payload["daily"]:
            day["entries"][0]["positions"][0]["source_type"] = "   "
        result = module.analyze(payload)
        self.assertEqual(result["core_natural"], [])
        self.assertEqual(result["keywords"][0]["category"], "数据不足")

    def test_side_coverage_missing_preserves_other_side_absence(self):
        payload = fixture({"ad": "AAA"})
        payload["source"]["coverage"]["ad"] = ""
        result = module.analyze(payload)
        self.assertEqual(result["keywords"][0]["daily"][0]["organic"], A)
        self.assertEqual(result["keywords"][0]["category"], "数据不足")
        self.assertFalse(result["core_ad_only"])

    def test_whitespace_source_evidence_never_confirms_observation(self):
        for key in ("mapping_evidence", "date_basis_evidence", "timezone"):
            payload = fixture()
            payload["source"][key] = "   "
            with self.subTest(key=key):
                result = module.analyze(payload)
                self.assertEqual(result["keywords"][0]["daily"][0]["organic"], U)
                self.assertFalse(result["core_natural"])

    def test_unknown_raw_zero_can_be_retained_without_advice(self):
        payload = fixture()
        pos = payload["daily"][0]["entries"][0]["positions"][0]
        pos.update(total_rank=None, valid=False, raw={"rank": 0})
        self.assertEqual(module.analyze(payload)["keywords"][0]["category"], "数据不足")

    def test_ambiguity_does_not_disappear_when_positive_record_exists(self):
        payload = fixture({"a": "BBB"})
        payload["daily"][0]["entries"][0]["positions"].append(position("unknown"))
        result = module.analyze(payload)
        self.assertEqual(result["keywords"][0]["daily"][0]["ad"], O)
        self.assertEqual(result["keywords"][0]["category"], "数据不足")

    def test_explicit_unknown_flag_blocks_core(self):
        payload = fixture()
        payload["daily"][1]["entries"][0]["unknown"] = {"ad": True}
        self.assertEqual(module.analyze(payload)["core_natural"], [])

    def test_keyword_union_and_missing_row_in_complete_day(self):
        payload = fixture({"first day only": "ONN", "last day only": "NNA"})
        payload["daily"][0]["entries"].pop(1)
        payload["daily"][1]["entries"] = []
        payload["daily"][2]["entries"].pop(0)
        result = module.analyze(sync_counts(payload))
        self.assertEqual(result["keyword_count"], 2)
        item = next(k for k in result["keywords"] if k["keyword"] == "first day only")
        self.assertEqual(item["daily"][1]["organic"], A)

    def test_exact_duplicates_removed_not_multiple_captures(self):
        payload = fixture()
        for day in payload["daily"]:
            row = day["entries"][0]
            row["positions"].append(deepcopy(row["positions"][0]))
            row["positions"].append(position("organic", total_rank=8, captured_at="synthetic later capture"))
            day["entries"].append(deepcopy(row))
        result = module.analyze(sync_counts(payload))
        self.assertEqual(result["keywords"][0]["organic_days"], 3)
        self.assertEqual(len(result["keywords"][0]["daily"][0]["positions"]), 2)
        self.assertEqual(result["daily_coverage"][0]["raw_rows"], 2)
        self.assertEqual(result["daily_coverage"][0]["unique_rows"], 1)

    def test_word_forms_are_not_merged(self):
        result = module.analyze(fixture(dict.fromkeys(["lamp", "lamps", "Lamp", "lamp mini", "mini lamp"], "OOO")))
        self.assertEqual(result["keyword_count"], 5)

    def test_multiple_ad_types_still_count_once_per_day(self):
        payload = fixture({"ads": "AAA"})
        for day in payload["daily"]:
            day["entries"][0]["positions"].append(position("ad", source_type="synthetic-second-ad", page=4))
        result = module.analyze(payload)
        self.assertEqual(result["keywords"][0]["ad_days"], 3)
        self.assertEqual(len(result["keywords"][0]["daily"][0]["positions"]), 2)


class RecommendationTests(unittest.TestCase):
    def test_product_failure_keeps_ad_core_only(self):
        payload = fixture({"natural": "OOO", "ads": "AAA"})
        payload["product"] = {}
        result = module.analyze(payload)
        self.assertEqual(result["core_natural"], [])
        self.assertEqual(result["core_ad_only"], ["ads"])
        self.assertEqual(result["category_counts"]["自然持续出现词"], 1)
        self.assertIn("相关性待核查", result["core_status"]["natural"])
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["product_available"])

    def test_ambiguous_and_unrelated_do_not_enter_natural_core(self):
        payload = fixture({"review": "OOO", "unrelated": "OOO", "no evidence": "OOO"})
        payload["relevance"]["review"]["status"] = "review"
        payload["relevance"]["unrelated"]["status"] = "unrelated"
        payload["relevance"]["no evidence"]["evidence"] = ""
        self.assertEqual(module.analyze(payload)["core_natural"], [])

    def test_conditional_branches_and_observation_actions(self):
        payload = fixture({"natural": "OOO", "occasional": "ANA", "both": "BBB", "alternate": "OAN"})
        result = module.analyze(payload)
        rows = {r["keyword"]: r for r in result["keywords"]}
        self.assertEqual(rows["natural"]["directions"], [module.CONDITIONAL_DIRECTION])
        self.assertTrue(set(module.AD_CHECKS).issubset(rows["occasional"]["directions"]))
        self.assertNotIn("occasional", result["core_ad_only"])
        for keyword in ("both", "alternate"):
            self.assertNotIn("提高竞价", "".join(rows[keyword]["directions"]))

    def test_uncertain_keyword_does_not_block_other_complete_keywords(self):
        result = module.analyze(fixture({"good": "OOO", "uncertain": "UOO"}))
        self.assertEqual(result["core_natural"], ["good"])
        self.assertEqual(result["category_counts"]["数据不足"], 1)


class PackagingAndCliTests(unittest.TestCase):
    def test_flat_skill_metadata_against_creator_requirements(self):
        # This package intentionally uses only flat scalar frontmatter.
        content = (PACKAGE / "SKILL.md").read_text(encoding="utf-8")
        matched = re.match(r"^---\n(.*?)\n---\n", content, re.S)
        self.assertIsNotNone(matched)
        metadata = {}
        for line in matched.group(1).splitlines():
            key, separator, value = line.partition(": ")
            self.assertTrue(separator)
            self.assertNotIn(key, metadata)
            metadata[key] = value
        self.assertEqual(set(metadata), {"name", "description"})
        self.assertEqual(metadata["name"], PACKAGE.name)
        self.assertRegex(metadata["name"], r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
        self.assertLessEqual(len(metadata["name"]), 64)
        self.assertTrue(metadata["description"])
        self.assertLessEqual(len(metadata["description"]), 1024)
        self.assertNotRegex(metadata["description"], r"[<>]|\[TODO:")
        self.assertNotRegex(content, r"(?m)^\s*\[TODO:.*\]\s*$")
        self.assertIn("跨境吴老师", metadata["description"])

    def test_platform_metadata_uses_quoted_strings_and_optional_policy(self):
        # JSON string literals are valid YAML scalars; this checks our small
        # supported mapping without adding a runtime YAML dependency.
        sections, current = {}, None
        for line in (PACKAGE / "agents" / "openai.yaml").read_text(encoding="utf-8").splitlines():
            if not line.startswith(" "):
                self.assertIn(line, ("interface:", "policy:"))
                current = line[:-1]
                self.assertNotIn(current, sections)
                sections[current] = {}
            else:
                self.assertTrue(line.startswith("  "))
                key, separator, value = line.strip().partition(": ")
                self.assertTrue(separator)
                self.assertNotIn(key, sections[current])
                sections[current][key] = json.loads(value)
        interface = sections["interface"]
        self.assertEqual(set(interface), {"display_name", "short_description", "default_prompt"})
        for value in interface.values():
            self.assertIsInstance(value, str)
            self.assertIn("跨境吴老师" if value != interface["default_prompt"] else "$" + PACKAGE.name, value)
        self.assertTrue(25 <= len(interface["short_description"]) <= 64)
        self.assertEqual(sections["policy"], {"allow_implicit_invocation": True})

    def test_cli_writes_output_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="rank-synthetic-test-") as tmp:
            input_path, output_path = Path(tmp) / "input.json", Path(tmp) / "result.json"
            input_path.write_text(json.dumps(fixture(), ensure_ascii=False), encoding="utf-8")
            command = [sys.executable, "-B", str(SCRIPT), "--input", str(input_path), "--output", str(output_path)]
            first = subprocess.run(command, capture_output=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            original = output_path.read_bytes()
            second = subprocess.run(command, capture_output=True)
            self.assertEqual(second.returncode, 2)
            self.assertEqual(output_path.read_bytes(), original)
            self.assertEqual(json.loads(original)["status"], "completed")

    def test_invalid_input_produces_no_report(self):
        with tempfile.TemporaryDirectory(prefix="rank-synthetic-invalid-") as tmp:
            input_path, output_path = Path(tmp) / "input.json", Path(tmp) / "result.json"
            input_path.write_text('{"schema_version": 2}', encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--input", str(input_path), "--output", str(output_path)], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output_path.exists())

    def test_relative_reference_targets_exist(self):
        for file in PACKAGE.rglob("*.md"):
            for relative in re.findall(r"\]\(([^)]+)\)", file.read_text(encoding="utf-8")):
                if relative.startswith(("https://", "http://", "#")):
                    continue
                self.assertTrue((file.parent / relative).is_file(), (file.name, relative))

    def test_core_instructions_have_no_absolute_machine_paths(self):
        paths = [PACKAGE / "SKILL.md", *PACKAGE.glob("references/*.md")]
        for file in paths:
            content = file.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"[A-Za-z]:[\\/]", content), file.name)
            self.assertNotIn("/Users/", content)
            self.assertNotIn("/home/", content)


if __name__ == "__main__":
    unittest.main()
