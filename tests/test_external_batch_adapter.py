"""External (Apify) Fantastic batch ingestion.

The contract this file protects: a flattened Apify CSV row must produce the
EXACT canonical job the provider-native Fantastic record produces. If the two
ever diverge, the external batch is silently running a different pipeline than
the paid lane, and every yield number measured from it is unusable.
"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import config
import external_batch_adapter as EBA
from fantastic_jobs_adapter import map_record


#: One realistic provider-native Fantastic record (arrays as real arrays).
NATIVE = {
    "id": "2326691141",
    "linkedin_id": "4457894916",
    "title": "Revenue Operations Manager",
    "organization": "LaunchDarkly",
    "organization_url": "https://www.linkedin.com/company/launchdarkly",
    "org_linkedin_slug": "launchdarkly",
    "org_linkedin_website": "https://launchdarkly.com",
    "org_linkedin_headcount": "669",
    "org_linkedin_industry": "Software Development",
    "org_linkedin_size": "501-1,000 employees",
    "org_linkedin_recruitment_agency_derived": False,
    "date_posted": "2026-08-23T17:58:06.476",
    "date_created": "2026-08-23T18:02:06.548568",
    "date_valid_through": "2026-09-23T00:00:00",
    "description_text": "Own the revenue operations stack.\nReport to the CRO.",
    "url": "https://www.linkedin.com/jobs/view/revops-4457894916",
    "source": "linkedin",
    "source_type": "jobboard",
    "location_type": "",
    "ats_duplicate": True,
    "employment_type": ["FULL_TIME"],
    "locations_derived": ["Oakland, California, United States"],
    "countries_derived": ["United States"],
    "cities_derived": ["Oakland"],
    "regions_derived": ["California"],
    "ai_taxonomies_a": ["Software", "Sales", "Technology"],
    "ai_key_skills": ["Salesforce", "Forecasting", "SQL"],
    "ai_salary_min_value": "120000",
    "ai_salary_max_value": "160000",
    "ai_salary_currency": "USD",
    "ai_salary_unit_text": "YEAR",
    "seniority": "Mid-Senior level",
}


def _flatten(record):
    """Flatten a provider-native record exactly the way the Apify actor does."""
    flat = {}
    for key, value in record.items():
        if isinstance(value, list):
            # The actor emits BOTH a (usually empty) scalar column and the
            # indexed columns; reproduce that so the shadowing rule is exercised.
            flat[key] = ""
            for index, item in enumerate(value):
                flat[f"{key}/{index}"] = "" if item is None else str(item)
        elif isinstance(value, bool):
            flat[key] = "true" if value else "false"
        else:
            flat[key] = "" if value is None else str(value)
    return flat


def _write_csv(rows, path):
    columns = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})


class UnflattenTests(unittest.TestCase):
    def test_indexed_columns_rebuild_arrays_in_numeric_order(self):
        row = {f"ai_keywords/{i}": f"k{i}" for i in range(12)}
        got = EBA.unflatten_row(row)
        self.assertEqual(got["ai_keywords"], [f"k{i}" for i in range(12)])

    def test_index_ten_follows_index_nine_not_index_one(self):
        """Lexicographic ordering would put /10 between /1 and /2."""
        row = {"employment_type/1": "b", "employment_type/10": "c",
               "employment_type/2": "a"}
        self.assertEqual(EBA.unflatten_row(row)["employment_type"], ["b", "a", "c"])

    def test_indexed_group_wins_over_empty_scalar_of_same_name(self):
        row = {"ai_benefits": "", "ai_benefits/0": "Dental"}
        self.assertEqual(EBA.unflatten_row(row)["ai_benefits"], ["Dental"])

    def test_blank_array_cells_are_dropped_not_kept_as_empty_strings(self):
        row = {"ai_key_skills/0": "SQL", "ai_key_skills/1": "", "ai_key_skills/2": "dbt"}
        self.assertEqual(EBA.unflatten_row(row)["ai_key_skills"], ["SQL", "dbt"])

    def test_scalar_with_no_indexed_siblings_is_preserved(self):
        row = {"cities_derived": "Oakland"}
        self.assertEqual(EBA.unflatten_row(row)["cities_derived"], "Oakland")

    def test_booleans_are_recovered_as_real_booleans(self):
        got = EBA.unflatten_row({"ats_duplicate": "true",
                                 "org_linkedin_recruitment_agency_derived": "false"})
        self.assertIs(got["ats_duplicate"], True)
        self.assertIs(got["org_linkedin_recruitment_agency_derived"], False)

    def test_numeric_looking_ids_stay_strings(self):
        """Coercing these to int would corrupt zero-padded provider identifiers."""
        got = EBA.unflatten_row({"id": "0023456", "linkedin_id": "4457894916"})
        self.assertEqual(got["id"], "0023456")
        self.assertIsInstance(got["linkedin_id"], str)

    def test_nested_object_columns_rebuild_a_list_of_dicts(self):
        row = {"locations/0/city": "Oakland", "locations/0/region": "CA",
               "locations/1/city": "Austin"}
        self.assertEqual(EBA.unflatten_row(row)["locations"],
                         [{"city": "Oakland", "region": "CA"}, {"city": "Austin"}])

    def test_bom_prefixed_header_is_not_treated_as_a_distinct_column(self):
        got = EBA.unflatten_row({"﻿ai_benefits": "Dental"})
        self.assertEqual(got.get("ai_benefits"), "Dental")


class NormalizationEquivalenceTests(unittest.TestCase):
    """The whole point: CSV in must equal provider-native in."""

    def _csv_job(self, native):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "batch.csv"
            _write_csv([_flatten(native)], path)
            jobs, stats = EBA.normalize_batch(str(path), classify=False)
            return (jobs[0] if jobs else None), stats

    def test_csv_row_maps_to_the_same_canonical_job_as_the_native_record(self):
        native_job, reason = map_record(dict(NATIVE), EBA.EXTERNAL_SOURCE)
        self.assertIsNone(reason or None, reason)
        csv_job, _stats = self._csv_job(NATIVE)
        self.assertIsNotNone(csv_job)
        # The batch-provenance marker is added by the external path only.
        csv_job = {k: v for k, v in csv_job.items() if k != "_external_batch"}
        self.assertEqual(csv_job, native_job)

    def test_employer_domain_survives_the_linkedin_organization_url(self):
        """organization_url is a linkedin.com host; the employer domain must come
        from org_linkedin_website, not from the intermediary."""
        csv_job, _ = self._csv_job(NATIVE)
        self.assertEqual(csv_job["employer_website"], "launchdarkly.com")

    def test_employment_type_array_becomes_a_scalar_token(self):
        csv_job, _ = self._csv_job(NATIVE)
        self.assertEqual(csv_job["job_employment_type"], "FULL_TIME")
        self.assertNotIn("[", csv_job["job_employment_type"])

    def test_us_location_and_taxonomy_arrays_survive_the_round_trip(self):
        csv_job, _ = self._csv_job(NATIVE)
        self.assertTrue(csv_job["_fantastic_us_location"])
        self.assertEqual(csv_job["_ai_taxonomies"], ["Software", "Sales", "Technology"])
        self.assertEqual(csv_job["_ai_taxonomy_primary"], "Software")
        self.assertEqual(csv_job["job_required_skills"], ["Salesforce", "Forecasting", "SQL"])

    def test_provider_clocks_are_not_conflated(self):
        csv_job, _ = self._csv_job(NATIVE)
        self.assertEqual(csv_job["_fantastic_date_posted"], "2026-08-23T17:58:06.476000")
        self.assertEqual(csv_job["_fantastic_date_created"], "2026-08-23T18:02:06.548568")

    def test_source_label_is_external_but_provider_fields_are_preserved(self):
        csv_job, _ = self._csv_job(NATIVE)
        self.assertEqual(csv_job["_acquisition_source"], "external_apify_fantastic")
        self.assertEqual(csv_job["_fantastic_source"], "linkedin")
        self.assertEqual(csv_job["_fantastic_source_type"], "jobboard")
        self.assertEqual(csv_job["_provider_dataset"], "jb")


class PiiAndFailClosedTests(unittest.TestCase):
    def test_contact_pii_columns_never_reach_the_canonical_job(self):
        native = dict(NATIVE)
        native.update({
            "recruiter_name": "A Person",
            "recruiter_url": "https://linkedin.com/in/a-person",
            "ai_hiring_manager_name": "Another Person",
            "ai_hiring_manager_email_address": "person@example.com",
        })
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.csv"
            _write_csv([_flatten(native)], path)
            jobs, _stats = EBA.normalize_batch(str(path), classify=False)
        blob = json.dumps(jobs[0]).lower()
        for leaked in ("a person", "another person", "person@example.com"):
            self.assertNotIn(leaked, blob)

    def test_row_missing_identity_fails_closed_and_is_counted(self):
        bad = dict(NATIVE)
        bad["organization"] = ""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.csv"
            _write_csv([_flatten(bad), _flatten(NATIVE)], path)
            jobs, stats = EBA.normalize_batch(str(path), classify=False)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(stats["raw_rows"], 2)
        self.assertEqual(stats["rejected_by_reason"].get("missing_identity"), 1)

    def test_a_malformed_row_never_aborts_the_batch(self):
        good_a, good_b = dict(NATIVE), dict(NATIVE)
        good_b["id"] = "999"
        broken = dict(NATIVE)
        broken["id"] = ""          # no stable id -> fails closed
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.csv"
            _write_csv([_flatten(good_a), _flatten(broken), _flatten(good_b)], path)
            jobs, stats = EBA.normalize_batch(str(path), classify=False)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(stats["rejected_by_reason"].get("missing_stable_id"), 1)

    def test_huge_description_cell_does_not_raise_field_size_limit(self):
        native = dict(NATIVE)
        native["description_text"] = "x" * 400_000
        csv_jobs, _stats = None, None
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.csv"
            _write_csv([_flatten(native)], path)
            csv_jobs, _stats = EBA.normalize_batch(str(path), classify=False)
        self.assertEqual(len(csv_jobs[0]["job_description"]), 400_000)


class LaneRunnerTests(unittest.TestCase):
    def test_lane_reports_zero_provider_requests_and_zero_credits(self):
        from orchestrator.adapters_real import real_external_batch_runner
        from orchestrator.lanes import LaneManager

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.csv"
            _write_csv([_flatten(NATIVE)], path)
            result = real_external_batch_runner(str(path))(LaneManager(budget=None))
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.physical_requests, 0)
        self.assertEqual(result.attribution["jobs_quota_consumed"], 0)
        self.assertEqual(result.attribution["requests"], 0)
        self.assertEqual(len(result.jobs), 1)

    def test_lane_classifies_so_the_role_gate_has_a_target(self):
        from orchestrator.adapters_real import real_external_batch_runner
        from orchestrator.lanes import LaneManager

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.csv"
            _write_csv([_flatten(NATIVE)], path)
            result = real_external_batch_runner(str(path))(LaneManager(budget=None))
        self.assertTrue(result.jobs[0].get("_matched_role"))
        self.assertEqual(result.attribution["role_classified"], 1)

    def test_unreadable_batch_fails_the_lane_rather_than_reporting_zero_jobs(self):
        from orchestrator.adapters_real import real_external_batch_runner
        from orchestrator.lanes import LaneManager

        result = real_external_batch_runner("does/not/exist.csv")(LaneManager(budget=None))
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.errors)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
