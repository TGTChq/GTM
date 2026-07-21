from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
import run_daily
from hiring_manager import Step3Result
from job_filter import FilterResult
from jsearch_scraper import ScrapeResult


class _Registry:
    def __init__(self):
        self.marked = []
        self.total_tracked = 0
    def mark_jobs(self, jobs):
        self.marked.extend(jobs)
        self.total_tracked = len(self.marked)


class RunDailyFinalPassV02Tests(unittest.TestCase):
    def test_strict_pipeline_counts_final_pass_and_writes_observability(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw.json"
            raw.write_text(json.dumps({"jobs": [{"job_id": "j1"}]}))
            filtered_path = root / "filtered.json"
            filtered_path.write_text(json.dumps({"jobs": [{"job_id": "j1", "job_title": "Staff Accountant", "employer_name": "Acme", "job_description": "x"}]}))
            rejected_path = root / "rejected.json"
            rejected_path.write_text(json.dumps({"jobs": []}))
            qualified_path = root / "qualified.json"
            qualified_path.write_text(json.dumps({"jobs": [{"job_id": "j1", "_job_gate_state": "PASS", "_role_gate_state": "PASS"}]}))
            enriched_path = root / "enriched.json"
            lead = {
                "job_id": "j1",
                "lead_key": "acme.com|jane@acme.com|finance",
                "employer_name": "Acme",
                "job_title": "Staff Accountant",
                "hiring_manager_email": "jane@acme.com",
                "_final_state": "FINAL_PASS",
                "_airtable_relevance": "accept",
                "_validation_version": config.VALIDATION_VERSION,
                "_account_gate_state": "PASS",
                "_gate_decisions": {gate: {"state": "PASS"} for gate in ("job", "account", "role", "contact", "email")},
            }
            enriched_path.write_text(json.dumps({
                "jobs": [lead],
                "processed_job_refs": [{"job_id": "j1"}],
                "processed_company_keys": ["acme.com"],
                "final_pass_target": 1,
                "stop_reason": "final_pass_target_reached",
                "strict_final_pass_mode": True,
                "validation_version": config.VALIDATION_VERSION,
            }))
            scrape = ScrapeResult(
                output_path=str(raw), total_jobs=1, roles_with_results=1,
                stats={"base_estimated_request_units": 1, "estimated_request_units": 1},
            )
            filtered = FilterResult(
                output_path=str(filtered_path), rejected_path=str(rejected_path),
                kept_count=1, rejected_count=0, stats={"input_total": 1},
            )
            qualification = SimpleNamespace(
                success=True, input_jobs=1, contact_eligible_jobs=1, rejected_jobs=0,
                unverified_jobs=0, needs_check_jobs=0, stats={}, output_path=str(qualified_path),
                nonpass_path=str(root / "nonpass.json"), errors=[],
            )
            enriched = Step3Result(
                output_path=str(enriched_path), total_input_jobs=1, total_output_leads=1,
                company_criteria_excluded=0, hiring_manager_found=1, hiring_manager_not_found=0,
                match_rate=1.0, contactable_hiring_managers=1, uncontactable_hiring_managers=0,
                contactable_rate=1.0, companies_considered=1, eligible_companies=1,
                final_pass_target=1, final_pass_leads=1, final_pass_target_reached=True,
                reviewable_leads=1, needs_check_leads=0, reroute_leads=0,
                unverified_leads=0, rejected_leads=0, stop_reason="final_pass_target_reached",
                processed_company_keys=["acme.com"], stats={},
            )
            registry = _Registry()
            audit = SimpleNamespace(passed=True, summary={}, report_path="audit", warnings=[], failures=[])
            airtable = {
                "reviewable": 1, "final_pass": 1, "needs_check": 0,
                "created": 1, "skipped_existing": 0, "skipped_existing_company": 0,
                "failed": 0,
            }
            with (
                patch.object(config, "PRODUCTION", True),
                patch.object(config, "FINAL_PASS_PIPELINE_ENABLED", True),
                patch.object(config, "JSEARCH_REVIEWABLE_TOPUP_ENABLED", False),
                patch.object(config, "MAX_ELIGIBLE_COMPANIES_PER_RUN", 90),
                patch.object(config, "get_final_pass_target", return_value=1),
                patch.object(run_daily, "SeenJobsRegistry", return_value=registry),
                patch.object(run_daily, "run_daily_scrape", return_value=scrape),
                patch.object(run_daily, "run_filter", return_value=filtered),
                patch.object(run_daily, "run_audit", return_value=audit),
                patch.object(run_daily, "run_precontact_qualification", return_value=qualification),
                patch.object(run_daily, "run_hiring_manager_identification", return_value=enriched) as hm_mock,
                patch.object(run_daily.airtable_client, "push_leads", return_value=airtable),
                patch.object(run_daily, "save_observability_report", return_value=str(root / "evidence.json")),
            ):
                summary = run_daily.run_pipeline()

        self.assertTrue(summary["success"])
        self.assertEqual(summary["steps"]["hiring_manager"]["final_pass_leads"], 1)
        self.assertEqual(summary["steps"]["observability"]["deficit_remaining"], 0)
        self.assertEqual(registry.marked, [{"job_id": "j1"}])
        self.assertEqual(hm_mock.call_args.kwargs["target_final_pass_leads"], 1)
        self.assertIsNone(hm_mock.call_args.kwargs["target_reviewable_leads"])


if __name__ == "__main__":
    unittest.main()
