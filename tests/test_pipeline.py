"""
test_pipeline.py — End-to-end tests against the real sample_inputs/ files,
plus dedicated robustness tests against deliberately broken/garbage inputs.

WHY end-to-end tests in addition to per-module unit tests?
    Unit tests prove each stage works in isolation. They don't prove the
    stages compose correctly (e.g. that extractor field names actually
    match what the merger expects, or that provenance survives the full
    trip). A small number of high-value end-to-end tests close that gap.

GOLD-PROFILE COMPARISON (optional deliverable, "ideally one that covers
an edge case"):
    test_gold_profile_raunit_full_merge is exactly this — it encodes the
    EXPECTED final state for one specific, hand-verified candidate (Raunit,
    merged across all 4 source types, including the cross-email entity
    resolution edge case) and asserts the pipeline reproduces it exactly.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import unittest
import tempfile
import json
from pathlib import Path

from src.pipeline import run_pipeline

SAMPLE_DIR = Path(__file__).parent.parent / "sample_inputs"


class TestEndToEndSampleInputs(unittest.TestCase):

    def setUp(self):
        self.input_paths = [
            str(SAMPLE_DIR / "recruiter_export.csv"),
            str(SAMPLE_DIR / "ats_blob.json"),
            str(SAMPLE_DIR / "github_profiles.json"),
            str(SAMPLE_DIR / "recruiter_notes.txt"),
        ]

    def test_produces_three_candidates(self):
        result = run_pipeline(self.input_paths)
        self.assertEqual(len(result.canonical_profiles), 3)

    def test_no_sources_skipped_on_clean_input(self):
        result = run_pipeline(self.input_paths)
        self.assertEqual(result.skipped_sources, [])

    def test_all_validations_pass_with_default_config(self):
        result = run_pipeline(self.input_paths)
        self.assertTrue(all(v.is_valid for v in result.validation_results))

    def test_gold_profile_raunit_full_merge(self):
        """
        Gold-profile comparison for the cross-source entity resolution edge
        case: Raunit appears in all 4 sources under TWO DIFFERENT emails
        (gmail in CSV/notes, IIT email in ATS) and must still merge into
        ONE candidate via phone-number matching.
        """
        result = run_pipeline(self.input_paths)
        raunit = next(
            (p for p in result.canonical_profiles if p.full_name == "Raunit Patel"), None
        )
        self.assertIsNotNone(raunit, "Raunit Patel should be found as a merged candidate")

        # Identity resolution edge case: both emails present despite
        # originating from different source files with no shared email.
        self.assertIn("raunit.patel@gmail.com", raunit.emails)
        self.assertIn("raunit@iitg.ac.in", raunit.emails)

        # Phone present and E.164-normalized (from 3 different raw formats
        # across CSV/ATS/notes, all converging on one normalized value).
        self.assertEqual(raunit.phones, ["+919876543210"])

        # Location merged field-by-field from ATS (city/region/country)
        self.assertEqual(raunit.location.city, "Guwahati")
        self.assertEqual(raunit.location.region, "Assam")
        self.assertEqual(raunit.location.country, "IN")

        # Skills corroborated across 3 sources should rank at the top
        # with confidence above any single source's base weight.
        top_skill_names = {s.name for s in raunit.skills[:5]}
        self.assertIn("Python", top_skill_names)
        python_skill = next(s for s in raunit.skills if s.name == "Python")
        self.assertGreater(python_skill.confidence, 0.7)

        # Education only present in ATS source — must survive the merge.
        self.assertEqual(len(raunit.education), 1)
        self.assertEqual(raunit.education[0].institution, "IIT Guwahati")

        # Two distinct work-history entries from ATS work_history array.
        companies = {e.company for e in raunit.experience}
        self.assertIn("Oracle Cloud Infrastructure", companies)
        self.assertIn("HPY Healthtech", companies)

        # overall_confidence should be a sensible value, not 0 or 1 exactly
        self.assertGreater(raunit.overall_confidence, 0.5)
        self.assertLessEqual(raunit.overall_confidence, 1.0)


class TestRobustnessAgainstGarbageInputs(unittest.TestCase):
    """
    Direct tests of the explicit requirement: "a missing or garbage source
    must not crash the run." Each test feeds in one bad file alongside one
    good file and asserts the pipeline still returns a sensible result
    instead of raising.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.good_csv = str(SAMPLE_DIR / "recruiter_export.csv")

    def _write(self, name, content):
        path = os.path.join(self.tmpdir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_malformed_json_does_not_crash_pipeline(self):
        bad_json = self._write("bad.json", "{ this is [[[ not valid json")
        result = run_pipeline([self.good_csv, bad_json])  # should not raise
        self.assertIn(bad_json, result.skipped_sources)
        self.assertGreater(len(result.canonical_profiles), 0)

    def test_empty_file_does_not_crash_pipeline(self):
        empty_csv = self._write("empty.csv", "")
        result = run_pipeline([self.good_csv, empty_csv])
        self.assertGreater(len(result.canonical_profiles), 0)

    def test_nonexistent_file_does_not_crash_pipeline(self):
        result = run_pipeline([self.good_csv, "/path/does/not/exist.csv"])
        self.assertIn("/path/does/not/exist.csv", result.skipped_sources)
        self.assertGreater(len(result.canonical_profiles), 0)

    def test_unrecognized_extension_does_not_crash_pipeline(self):
        weird_file = self._write("data.xyz", "some random content")
        result = run_pipeline([self.good_csv, weird_file])
        self.assertIn(weird_file, result.skipped_sources)
        self.assertGreater(len(result.canonical_profiles), 0)

    def test_csv_with_missing_columns_degrades_gracefully(self):
        partial_csv = self._write("partial.csv", "name\nJohn Nobody\n")
        result = run_pipeline([partial_csv])  # only has a name, nothing else
        self.assertEqual(len(result.canonical_profiles), 1)
        self.assertEqual(result.canonical_profiles[0].full_name, "John Nobody")
        self.assertEqual(result.canonical_profiles[0].emails, [])

    def test_all_sources_garbage_returns_empty_not_crash(self):
        bad1 = self._write("bad1.json", "not json")
        bad2 = self._write("bad2.txt", "")
        result = run_pipeline([bad1, bad2])
        self.assertEqual(len(result.canonical_profiles), 0)


if __name__ == "__main__":
    unittest.main()
