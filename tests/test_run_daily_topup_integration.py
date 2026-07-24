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


class RunDailyTopupIntegrationTests(unittest.TestCase):
    def _step3(self, path, reviewable, company_keys):
        return Step3Result(
            output_path=str(path),
            total_input_jobs=reviewable,
            total_output_leads=reviewable,
            company_criteria_excluded=0,
            hiring_manager_found=reviewable,
            hiring_manager_not_found=0,
            match_rate=1.0,
            contactable_hiring_managers=reviewable,
            uncontactable_hiring_managers=0,
            contactable_rate=1.0,
            companies_considered=reviewable,
            eligible_companies=reviewable,
            target_reviewable_leads=2,
            reviewable_leads=reviewable,
            reviewable_target_reached=reviewable >= 2,
            max_eligible_companies=90,
            stop_reason=(
                "reviewable_lead_target_reached"
                if reviewable >= 2
                else "candidate_pool_exhausted"
            ),
            processed_company_keys=company_keys,
            stats={},
        )

    def test_pipeline_reserves_initial_budget_and_pushes_combined_topup_payload_once(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            raw = directory / "raw.json"
            raw.write_text(json.dumps({"jobs": [{"job_id": "j1"}]}))
            filtered_file = directory / "filtered.json"
            filtered_file.write_text(json.dumps({"jobs": [{"job_id": "j1"}]}))
            rejected_file = directory / "rejected.json"
            rejected_file.write_text(json.dumps({"jobs": []}))
            initial_output = directory / "initial.json"
            initial_output.write_text(json.dumps({
                "jobs": [{
                    "lead_key": "a",
                    "_step3_status": "found",
                    "hiring_manager_email": "a@example.com",
                }],
                "processed_job_refs": [{"job_id": "j1"}],
                "processed_company_keys": ["a.com"],
            }))
            combined_output = directory / "combined.json"
            combined_output.write_text(json.dumps({
                "jobs": [
                    {"lead_key": "a", "_step3_status": "found", "hiring_manager_email": "a@example.com"},
                    {"lead_key": "b", "_step3_status": "found", "hiring_manager_email": "b@example.com"},
                ],
                "processed_job_refs": [{"job_id": "j1"}, {"job_id": "j2"}],
                "processed_company_keys": ["a.com", "b.com"],
            }))
            scrape = ScrapeResult(
                output_path=str(raw),
                total_jobs=1,
                stats={
                    "base_estimated_request_units": 1,
                    "estimated_request_units": 1,
                    "adaptive_extra_queries": 0,
                    "adaptive_prefilter_viable_added": 0,
                    "adaptive_lookback_queries": 0,
                    "adaptive_lookback_prefilter_viable_added": 0,
                    "adaptive_bucket_counts": {},
                    "adaptive_lookback_variant_counts": {},
                    "query_variant_metrics": {},
                    "excluded_by_seniority": 0,
                },
                roles_with_results=1,
            )
            filtered = FilterResult(
                output_path=str(filtered_file),
                rejected_path=str(rejected_file),
                kept_count=1,
                rejected_count=0,
                stats={"input_total": 1, "kept": 1},
            )
            initial = self._step3(initial_output, 1, ["a.com"])
            combined = self._step3(combined_output, 2, ["a.com", "b.com"])
            registry = _Registry()
            audit = SimpleNamespace(
                passed=True,
                summary={},
                report_path="audit.txt",
                warnings=[],
                failures=[],
            )
            airtable_result = {
                "reviewable": 2,
                "created": 2,
                "skipped_existing": 0,
                "skipped_existing_company": 0,
                "failed": 0,
            }
            checkpoint = SimpleNamespace(
                pending_jobs=lambda: [], query_metrics=lambda: {}, append_jobs=lambda *a, **k: None,
                remove_jobs=lambda *a, **k: None, clear=lambda: None,
            )
            recovery = SimpleNamespace(
                due_jobs=lambda: [], upsert=lambda *a, **k: None, remove=lambda *a, **k: None,
            )
            inventory = SimpleNamespace(
                stage=lambda *a, **k: None, available=lambda limit=None: [], reserve=lambda *a, **k: None,
                mark_persisted=lambda *a, **k: None, release_failed=lambda *a, **k: None,
            )

            with (
                patch.object(config, "PRODUCTION", True),
                patch.object(config, "ACQUISITION_MODE", "jsearch"),
                patch.object(config, "RAPIDAPI_KEY", "test-key"),
                patch.object(config, "FINAL_PASS_PIPELINE_ENABLED", False),
                patch.object(config, "JSEARCH_REVIEWABLE_TOPUP_ENABLED", True),
                patch.object(config, "JSEARCH_TOPUP_MAX_ROUNDS", 3),
                patch.object(config, "JSEARCH_TOPUP_INITIAL_PAGES", 1),
                patch.object(config, "TARGET_REVIEWABLE_LEADS_PER_RUN", 2),
                patch.object(config, "MAX_ELIGIBLE_COMPANIES_PER_RUN", 90),
                patch.object(run_daily, "SeenJobsRegistry", return_value=registry),
                patch.object(run_daily, "PipelineCheckpoint", return_value=checkpoint),
                patch.object(run_daily, "RecoverableJobQueue", return_value=recovery),
                patch.object(run_daily, "FinalPassInventory", return_value=inventory),
                patch.object(run_daily, "run_daily_scrape", return_value=scrape) as scrape_mock,
                patch.object(run_daily, "run_filter", return_value=filtered),
                patch.object(run_daily, "run_audit", return_value=audit),
                patch.object(run_daily, "run_hiring_manager_identification", return_value=initial),
                patch.object(run_daily, "run_reviewable_topup", return_value=(combined, {
                    "enabled": True,
                    "rounds": [{"round": 1, "reviewable_added": 1}],
                    "topup_query_units": 3,
                    "total_query_units": 4,
                    "stop_reason": "reviewable_lead_target_reached",
                })) as topup_mock,
                patch.object(run_daily.airtable_client, "get_active_existing_company_keys_for_pipeline", return_value=set()),
                patch.object(run_daily.airtable_client, "push_leads", return_value=airtable_result) as push_mock,
            ):
                summary = run_daily.run_pipeline()

        self.assertTrue(summary["success"])
        scrape_mock.assert_called_once_with(
            registry=registry,
            base_num_pages=1,
            allow_adaptive=None,
        )
        self.assertEqual(topup_mock.call_count, 1)
        self.assertEqual(len(push_mock.call_args.args[0]), 2)
        self.assertEqual([row["job_id"] for row in registry.marked], ["j1", "j2"])
        self.assertEqual(summary["steps"]["hiring_manager"]["reviewable_leads"], 2)


if __name__ == "__main__":
    unittest.main()
