from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
import final_pass_topup
from hiring_manager import Step3Result
from jsearch_scraper import ScrapeResult
from pipeline_state import SeenJobsRegistry


class FinalPassTopupV02Tests(unittest.TestCase):
    def _result(self, path: Path, *, final_pass: int, review_rows: int, company: str) -> Step3Result:
        return Step3Result(
            output_path=str(path),
            total_input_jobs=1,
            total_output_leads=review_rows,
            company_criteria_excluded=0,
            hiring_manager_found=review_rows,
            hiring_manager_not_found=0,
            match_rate=1.0,
            contactable_hiring_managers=review_rows,
            uncontactable_hiring_managers=0,
            contactable_rate=1.0,
            companies_considered=1,
            eligible_companies=1,
            company_criteria_excluded_companies=0,
            final_pass_target=2,
            final_pass_leads=final_pass,
            needs_check_leads=review_rows-final_pass,
            final_pass_target_reached=final_pass >= 2,
            reviewable_leads=review_rows,
            reviewable_target_reached=review_rows >= 2,
            max_eligible_companies=90,
            stop_reason="candidate_pool_exhausted",
            processed_company_keys=[company],
            stats={},
        )

    def test_needs_check_does_not_reduce_deficit_and_microbatch_stops_on_pass_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw.json"
            raw.write_text(json.dumps({"jobs": [{"job_id": "j1"}]}))
            initial_path = root / "initial.json"
            initial_path.write_text(json.dumps({
                "jobs": [
                    {"job_id": "j1", "lead_key": "pass-1", "_final_state": "FINAL_PASS", "_search_role": "Accountant", "_account_gate_state": "PASS"},
                    {"job_id": "j-review", "lead_key": "review-1", "_final_state": "NEEDS_CHECK", "_search_role": "Accountant", "_account_gate_state": "PASS"},
                ],
                "processed_job_refs": [{"job_id": "j1"}],
                "processed_company_keys": ["one.com"],
            }))
            initial_result = self._result(initial_path, final_pass=1, review_rows=2, company="one.com")
            initial_scrape = ScrapeResult(
                output_path=str(raw), total_jobs=1, roles_with_results=1,
                stats={"estimated_request_units": 10, "query_metrics": {}},
            )

            topup_raw = root / "topup_raw.json"
            topup_raw.write_text(json.dumps({"jobs": [{"job_id": "j2"}]}))
            topup_scrape = ScrapeResult(
                output_path=str(topup_raw), total_jobs=1, roles_with_results=1,
                stats={
                    "estimated_request_units": 2,
                    "queries_attempted": 1,
                    "topup_new_prefilter_viable": 1,
                    "topup_stop_reason": "target_reached",
                    "query_metrics": {},
                },
            )
            filtered = root / "filtered.json"
            filtered.write_text(json.dumps({"jobs": [{"job_id": "j2"}]}))
            qualified = root / "qualified.json"
            qualified.write_text(json.dumps({"jobs": [{"job_id": "j2", "_job_gate_state": "PASS"}]}))
            enriched_path = root / "enriched.json"
            enriched_path.write_text(json.dumps({
                "jobs": [{"job_id": "j2", "lead_key": "pass-2", "_final_state": "FINAL_PASS", "_account_gate_state": "PASS"}],
                "processed_job_refs": [{"job_id": "j2"}],
                "processed_company_keys": ["two.com"],
            }))
            enriched = self._result(enriched_path, final_pass=1, review_rows=1, company="two.com")

            with (
                patch.object(config, "STEP3_OUTPUT_DIR", str(root)),
                patch.object(config, "FILTERED_OUTPUT_DIR", str(root)),
                patch.object(config, "FINAL_PASS_MAX_TOPUP_ITERATIONS", 5),
                patch.object(config, "FINAL_PASS_MAX_RUNTIME_SECONDS", 300),
                patch.object(config, "FINAL_PASS_MICROBATCH_QUERY_UNITS", 6),
                patch.object(config, "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN", 100),
                patch.object(final_pass_topup, "run_targeted_topup_scrape", return_value=topup_scrape) as scrape_mock,
                patch.object(final_pass_topup, "run_filter", return_value=SimpleNamespace(
                    output_path=str(filtered), kept_count=1, rejected_count=0, success=True, errors=[]
                )),
                patch.object(final_pass_topup, "run_precontact_qualification", return_value=SimpleNamespace(
                    output_path=str(qualified), contact_eligible_jobs=1, rejected_jobs=0, unverified_jobs=0
                )),
                patch.object(final_pass_topup, "run_hiring_manager_identification", return_value=enriched),
            ):
                combined, details = final_pass_topup.run_final_pass_topup(
                    initial_scrape=initial_scrape,
                    initial_enriched=initial_result,
                    registry=SeenJobsRegistry(path=str(root / "seen.json")),
                    target_final_pass_leads=2,
                    max_eligible_companies=90,
                )

        self.assertEqual(scrape_mock.call_count, 1)
        self.assertEqual(combined.final_pass_leads, 2)
        self.assertTrue(combined.final_pass_target_reached)
        self.assertEqual(details["deficit_remaining"], 0)
        self.assertEqual(details["stop_reason"], "final_pass_target_reached")
        self.assertEqual(details["rounds"][0]["final_pass_added"], 1)
        self.assertEqual(scrape_mock.call_args.kwargs["unit_budget"], 6)


if __name__ == "__main__":
    unittest.main()
