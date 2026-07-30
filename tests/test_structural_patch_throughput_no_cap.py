"""Phase 4: explicit proof that reaching the FINAL_PASS target does not stop
processing by default -- 30 (or any configured target) is a minimum SLA, not
a cap, per the spec's non-negotiable throughput policy.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import hiring_manager


def _job(job_id, employer):
    return {
        "job_id": job_id,
        "job_title": "Staff Accountant",
        "employer_name": employer,
        "canonical_employer_name": employer,
        "employer_website": f"https://{employer.lower()}.com",
        "_matched_role": "Staff Accountant",
        "_job_gate_state": "PASS",
        "_role_gate_state": "PASS",
    }


class ThroughputIsMinimumNotCapTests(unittest.TestCase):
    def test_default_config_continues_past_target(self):
        self.assertTrue(
            config.CONTINUE_AFTER_FINAL_PASS_TARGET,
            "CONTINUE_AFTER_FINAL_PASS_TARGET must default True: reaching the "
            "FINAL_PASS target must only set target_reached, never stop "
            "processing remaining valid opportunities.",
        )

    def _run(self, *, continue_after_target: bool):
        jobs = [_job("j1", "Acme"), _job("j2", "Beta")]
        with tempfile.TemporaryDirectory() as temp:
            input_path = Path(temp) / "input.json"
            input_path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")

            def fake_process_company(company_jobs):
                employer = company_jobs[0]["employer_name"]
                lead = dict(company_jobs[0])
                lead["_final_state"] = "FINAL_PASS"
                lead["_step3_status"] = "found"
                lead["_account_gate_state"] = "PASS"
                lead["lead_key"] = f"{employer}|x@x.com|finance"
                lead["hiring_manager_email"] = "x@x.com"
                lead["hiring_manager_name"] = "Test Person"
                return [lead], {"person_match_attempts": 1}

            with (
                patch.object(config, "STEP3_OUTPUT_DIR", temp),
                patch.object(config, "FINAL_PASS_PIPELINE_ENABLED", True),
                patch.object(config, "CONTINUE_AFTER_FINAL_PASS_TARGET", continue_after_target),
                patch.object(hiring_manager, "validate_preflight", return_value=None),
                patch.object(hiring_manager, "process_company", side_effect=fake_process_company),
            ):
                result = hiring_manager.run_hiring_manager_identification(
                    str(input_path), target_final_pass_leads=1,
                )
        return result

    def test_continues_processing_remaining_companies_past_target_by_default(self):
        result = self._run(continue_after_target=True)
        # Both companies were considered even though the first alone met the
        # target of 1 -- reaching the target only sets target_reached, it does
        # not truncate the run.
        self.assertEqual(result.companies_considered, 2)
        self.assertTrue(result.target_reached)
        self.assertNotEqual(result.stop_reason, "airtable_review_target_reached")

    def test_stops_early_only_when_explicitly_configured_to_cap(self):
        result = self._run(continue_after_target=False)
        self.assertEqual(result.companies_considered, 1)
        self.assertEqual(result.stop_reason, "airtable_review_target_reached")


if __name__ == "__main__":
    unittest.main()
