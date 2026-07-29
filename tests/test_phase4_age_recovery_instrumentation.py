"""Row 5 regression test: age_recovery must log/record its own pass-level
companies_considered/eligible_companies instead of only the post-merge totals.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import age_recovery
from hiring_manager import Step3Result
from job_filter import FilterResult
from jsearch_scraper import ScrapeResult
from qualification_pipeline import QualificationResult


def _step3_result(**overrides) -> Step3Result:
    base = dict(
        output_path="unused.json",
        total_input_jobs=0,
        total_output_leads=0,
        company_criteria_excluded=0,
        hiring_manager_found=0,
        hiring_manager_not_found=0,
        match_rate=0.0,
        contactable_hiring_managers=0,
        uncontactable_hiring_managers=0,
        contactable_rate=0.0,
        companies_considered=0,
        eligible_companies=0,
        company_criteria_excluded_companies=0,
        target_reviewable_leads=1,
        reviewable_leads=0,
        reviewable_target_reached=False,
        final_pass_target=1,
        final_pass_leads=0,
        needs_check_leads=0,
        reroute_leads=0,
        unverified_leads=0,
        rejected_leads=0,
        final_pass_target_reached=False,
        max_eligible_companies=10,
        eligible_company_limit_reached=False,
        target_reached=False,
        stop_reason="",
        processed_company_keys=[],
        stats={},
        success=True,
        errors=[],
    )
    base.update(overrides)
    return Step3Result(**base)


class AgeRecoveryCompanyCountInstrumentationTests(unittest.TestCase):
    def test_recovered_company_counts_are_recorded_and_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            initial_path = tmp_path / "initial.json"
            initial_path.write_text(json.dumps({"jobs": [], "processed_job_refs": [], "processed_company_keys": []}), encoding="utf-8")
            initial_enriched = _step3_result(
                output_path=str(initial_path),
                companies_considered=5,
                eligible_companies=3,
                final_pass_leads=0,
                reviewable_leads=0,
            )

            recovered_filter_path = tmp_path / "age_recovery_filtered.json"
            recovered_filter_path.write_text(
                json.dumps({"jobs": [{"employer_name": "Acme", "job_title": "Sales Manager"}]}),
                encoding="utf-8",
            )
            recovered_filter = FilterResult(
                output_path=str(recovered_filter_path),
                rejected_path=str(tmp_path / "age_recovery_rejected.json"),
                kept_count=1,
                rejected_count=0,
                stats={},
            )

            qualified_path = tmp_path / "qualified.json"
            qualified_path.write_text(json.dumps({"jobs": []}), encoding="utf-8")
            qualified = QualificationResult(
                output_path=str(qualified_path),
                nonpass_path=str(tmp_path / "qualified_nonpass.json"),
                input_jobs=1,
                contact_eligible_jobs=1,
                rejected_jobs=0,
                unverified_jobs=0,
                needs_check_jobs=0,
                stats={},
            )

            recovered_hm_path = tmp_path / "recovered_hm.json"
            recovered_hm_path.write_text(json.dumps({"jobs": [], "processed_job_refs": [], "processed_company_keys": []}), encoding="utf-8")
            recovered_result = _step3_result(
                output_path=str(recovered_hm_path),
                companies_considered=1,
                eligible_companies=1,
                final_pass_leads=1,
                reviewable_leads=1,
            )

            initial_scrape = ScrapeResult(output_path=str(tmp_path / "scrape.json"), total_jobs=0, stats={})

            with (
                patch.object(age_recovery, "run_filter", return_value=recovered_filter),
                patch.object(age_recovery, "run_precontact_qualification", return_value=qualified),
                patch.object(age_recovery, "run_hiring_manager_identification", return_value=recovered_result),
                self.assertLogs("age_recovery", level="INFO") as captured,
            ):
                combined, details = age_recovery.run_age_recovery(
                    initial_scrape=initial_scrape,
                    initial_enriched=initial_enriched,
                    registry=object(),
                    target_final_pass_leads=5,
                    max_eligible_companies=None,
                )

        self.assertEqual(details["recovered_companies_considered"], 1)
        self.assertEqual(details["recovered_eligible_companies"], 1)
        self.assertTrue(
            any("companies_considered=1" in message and "eligible_companies=1" in message for message in captured.output)
        )


if __name__ == "__main__":
    unittest.main()
