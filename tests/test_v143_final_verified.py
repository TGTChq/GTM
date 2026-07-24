from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
import run_free_source_shadow
from ats_board_registry import AtsBoardRegistry, detect_board_ref
from company_identity import is_placeholder_company_name
from job_quality import assess_posting_integrity


class PlaceholderEmployerV142Tests(unittest.TestCase):
    def test_schema_and_confidential_placeholders_are_rejected(self):
        for value in (
            "name",
            "Company Name",
            "employer",
            "Confidential Employer",
            "unknown",
            "name withheld",
        ):
            with self.subTest(value=value):
                self.assertTrue(is_placeholder_company_name(value))

    def test_legitimate_brand_names_are_not_overblocked(self):
        for value in ("Namecheap", "The Company Store", "Unknown Worlds", "Employer.com"):
            with self.subTest(value=value):
                self.assertFalse(is_placeholder_company_name(value))

    def test_structured_provider_cannot_make_placeholder_employer_trusted(self):
        assessment = assess_posting_integrity(
            {
                "job_title": "Customer Success Manager",
                "employer_name": "name",
                "job_description": "Own onboarding and customer adoption.",
                "job_apply_link": "https://himalayas.app/companies/example/jobs/123",
                "job_publisher": "Himalayas",
                "_acquisition_source": "himalayas",
                "_provider_record_structured": True,
            }
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.reason, "untrustworthy_employer_identity")

    def test_legacy_placeholder_registry_entry_is_pruned(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "boards.json"
            key = "ashby:acme:https://api.ashbyhq.com"
            path.write_text(
                json.dumps(
                    {
                        "boards": {
                            key: {
                                "provider": "ashby",
                                "identifier": "acme",
                                "api_base": "https://api.ashbyhq.com",
                                "company_name": "name",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry = AtsBoardRegistry(path=str(path))

        self.assertEqual(registry.invalid_entries_pruned, 1)
        self.assertEqual(registry.entries, {})

    def test_placeholder_company_never_seeds_ats_registry(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "boards.json"
            registry = AtsBoardRegistry(path=str(path))
            changed = registry.upsert_from_job(
                {
                    "employer_name": "name",
                    "job_apply_link": "https://jobs.ashbyhq.com/acme/123",
                    "_acquisition_source": "himalayas",
                }
            )
        self.assertEqual(changed, 0)
        self.assertEqual(registry.entries, {})


class WorkdayRegistryV142Tests(unittest.TestCase):
    def test_job_path_is_not_mistaken_for_workday_site_id(self):
        self.assertIsNone(
            detect_board_ref(
                "https://boeing.wd1.myworkdayjobs.com/job/USA-TX/Software-Engineer_R123"
            )
        )

    def test_real_workday_site_after_locale_is_detected(self):
        ref = detect_board_ref(
            "https://boeing.wd1.myworkdayjobs.com/en-US/EXTERNAL_CAREERS/job/USA-TX/Software-Engineer_R123"
        )
        self.assertIsNotNone(ref)
        self.assertEqual(ref.provider, "workday")
        self.assertEqual(ref.identifier, "boeing|EXTERNAL_CAREERS")

    def test_common_real_workday_site_ids_remain_discoverable(self):
        for site in ("External", "Careers", "Jobs", "Recruiting", "Default", "EXTERNAL_CAREERS"):
            with self.subTest(site=site):
                ref = detect_board_ref(
                    f"https://acme.wd5.myworkdayjobs.com/en-US/{site}/job/United-States/Test_R1"
                )
                self.assertIsNotNone(ref)
                self.assertEqual(ref.identifier, f"acme|{site}")

    def test_legacy_job_site_entry_is_pruned_but_real_site_remains(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "boards.json"
            invalid_key = "workday:boeing|job:https://boeing.wd1.myworkdayjobs.com"
            valid_key = (
                "workday:boeing|external_careers:"
                "https://boeing.wd1.myworkdayjobs.com"
            )
            path.write_text(
                json.dumps(
                    {
                        "boards": {
                            invalid_key: {
                                "provider": "workday",
                                "identifier": "boeing|job",
                                "api_base": "https://boeing.wd1.myworkdayjobs.com",
                                "company_name": "Boeing",
                            },
                            valid_key: {
                                "provider": "workday",
                                "identifier": "boeing|external_careers",
                                "api_base": "https://boeing.wd1.myworkdayjobs.com",
                                "company_name": "Boeing",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry = AtsBoardRegistry(path=str(path))
            persisted = json.loads(path.read_text(encoding="utf-8"))["boards"]

        self.assertEqual(registry.invalid_entries_pruned, 1)
        self.assertNotIn(invalid_key, registry.entries)
        self.assertIn(valid_key, registry.entries)
        self.assertNotIn(invalid_key, persisted)


class ShadowAgeWindowV142Tests(unittest.TestCase):
    def test_shadow_explicitly_uses_primary_age_window_not_legacy_env_value(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw.json"
            filtered_path = root / "filtered.json"
            rejected_path = root / "rejected.json"
            qualified_path = root / "qualified.json"
            nonpass_path = root / "nonpass.json"
            for path in (raw, filtered_path, rejected_path, qualified_path, nonpass_path):
                path.write_text(json.dumps({"jobs": []}), encoding="utf-8")

            acquisition = SimpleNamespace(
                output_path=str(raw),
                total_jobs=0,
                stats={},
                success=True,
                errors=[],
            )
            filtered = SimpleNamespace(
                output_path=str(filtered_path),
                rejected_path=str(rejected_path),
                kept_count=0,
                rejected_count=0,
                stats={"kept": 0},
                success=True,
                errors=[],
            )
            qualified = SimpleNamespace(
                output_path=str(qualified_path),
                nonpass_path=str(nonpass_path),
                input_jobs=0,
                contact_eligible_jobs=0,
                rejected_jobs=0,
                unverified_jobs=0,
                needs_check_jobs=0,
                stats={},
                success=True,
                errors=[],
            )

            with (
                patch.object(config, "STATE_DIR", str(root / "state")),
                patch.object(config, "MAX_JOB_AGE_DAYS", 3),
                patch.object(config, "PRIMARY_MAX_JOB_AGE_DAYS", 14),
                patch.object(
                    run_free_source_shadow,
                    "run_multi_source_acquisition",
                    return_value=acquisition,
                ),
                patch.object(
                    run_free_source_shadow,
                    "run_filter",
                    return_value=filtered,
                ) as filter_mock,
                patch.object(
                    run_free_source_shadow,
                    "run_precontact_qualification",
                    return_value=qualified,
                ),
                patch("sys.argv", ["run_free_source_shadow.py"]),
                patch("builtins.print"),
            ):
                result = run_free_source_shadow.main()

        self.assertEqual(result, 0)
        self.assertEqual(filter_mock.call_args.kwargs["max_age_days"], 14)


class ShortCatalogAliasV143Tests(unittest.TestCase):
    def test_configured_short_aliases_are_not_rejected_as_malformed(self):
        for title, matched_role in (
            ("DBA", "Database Administrator"),
            ("BDR", "Business Development Representative"),
            ("SDR", "Sales Development Representative"),
        ):
            with self.subTest(title=title):
                assessment = assess_posting_integrity({
                    "job_title": title,
                    "employer_name": "Acme",
                    "employer_website": "https://acme.com",
                    "job_description": f"Acme is hiring a full-time {title} for its commercial team.",
                    "job_apply_link": "https://acme.com/careers/role",
                    "_matched_role": matched_role,
                })
                self.assertTrue(assessment.eligible)

    def test_arbitrary_short_titles_remain_rejected(self):
        for title in ("A", "IT", "--"):
            with self.subTest(title=title):
                assessment = assess_posting_integrity({
                    "job_title": title,
                    "employer_name": "Acme",
                    "employer_website": "https://acme.com",
                    "job_description": "Acme is hiring for a full-time business role.",
                    "job_apply_link": "https://acme.com/careers/role",
                    "_matched_role": "Account Executive",
                })
                self.assertFalse(assessment.eligible)
                self.assertEqual(assessment.reason, "malformed_job_title")


class SpecializedRolePunctuationV143Tests(unittest.TestCase):
    def test_specialized_roles_accept_catalog_equivalent_punctuation(self):
        from role_relevance import assess_role

        for canonical, variant in (
            ("AI Engineer", "AI-Engineer"),
            ("GTM Engineer", "GTM / Engineer"),
            ("Automation Specialist", "Automation  Specialist"),
            ("Graphic Designer", "Graphic-Designer"),
            ("Video Editor", "Video / Editor"),
            ("Performance Marketing Manager", "Performance-Marketing-Manager"),
            ("Customer Support", "Customer-Support"),
            ("Customer Success Manager", "Customer / Success / Manager"),
        ):
            with self.subTest(canonical=canonical, variant=variant):
                result = assess_role(
                    {
                        "job_title": variant,
                        "job_description": f"Full-time {canonical} responsibilities.",
                    },
                    canonical,
                )
                self.assertEqual(result.status, "accept")

    def test_negative_description_still_overrides_equivalent_title(self):
        from role_relevance import assess_role

        result = assess_role(
            {
                "job_title": "AI-Engineer",
                "job_description": "This is an AI trainer and data annotation position.",
            },
            "AI Engineer",
        )
        self.assertNotEqual(result.status, "accept")


class EmploymentAndPhysicalAlignmentV143Tests(unittest.TestCase):
    def test_permanent_label_is_eligible_full_time(self):
        from job_filter import assess_employment_quality

        result = assess_employment_quality({
            "job_title": "Data Analyst",
            "job_description": "Permanent employee role with benefits.",
            "job_employment_type": "Permanent",
        })
        self.assertTrue(result.eligible)
        self.assertEqual(result.classification, "full_time")

    def test_ambiguous_employee_labels_are_deferred_not_rejected(self):
        from job_filter import assess_employment_quality

        for label in ("Regular", "Employee", "Salaried"):
            with self.subTest(label=label):
                result = assess_employment_quality({
                    "job_title": "Data Analyst",
                    "job_description": "Employee role with no contract or part-time signal.",
                    "job_employment_type": label,
                })
                self.assertTrue(result.eligible)
                self.assertEqual(result.classification, "unknown")

    def test_warehouse_operations_analyst_is_physical_at_prefilter(self):
        from job_filter import assess_pre_enrichment_viability

        result = assess_pre_enrichment_viability({
            "job_title": "Warehouse Operations Analyst",
            "job_description": "Work daily in the warehouse and operate machinery.",
            "job_location": "Dallas, TX, United States",
            "job_country": "US",
            "job_employment_type": "Full-time",
            "employer_name": "Acme",
            "employer_website": "https://acme.com",
            "job_apply_link": "https://acme.com/jobs/warehouse-analyst",
            "_matched_role": "Operations Analyst",
        }, max_age_days=14)
        self.assertFalse(result.eligible)
        self.assertEqual(result.stat_name, "excluded_in_person")


if __name__ == "__main__":
    unittest.main()
