"""The optional enrichment runtime budget is a fail-safe, not a truncation.

When ORCHESTRATOR_ENRICHMENT_MAX_RUNTIME_SECONDS is set and exceeded, the company
loop stops taking NEW companies, every already-enriched company stays checkpointed
(so a later run resumes without re-calling Apollo), and the run ends with
stop_reason "enrichment_runtime_budget_reached". Default 0 processes every company.
"""
from __future__ import annotations

import itertools
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import hiring_manager


def _job(job_id, employer):
    return {
        "job_id": job_id, "job_title": "Staff Accountant", "employer_name": employer,
        "canonical_employer_name": employer, "employer_website": f"https://{employer.lower()}.com",
        "_matched_role": "Staff Accountant", "_job_gate_state": "PASS", "_role_gate_state": "PASS",
    }


def _fake_process_company(company_jobs):
    employer = company_jobs[0]["employer_name"]
    lead = dict(company_jobs[0])
    lead.update({
        "_final_state": "FINAL_PASS", "_step3_status": "found", "_account_gate_state": "PASS",
        "lead_key": f"{employer}|x@x.com|finance", "hiring_manager_email": "x@x.com",
        "hiring_manager_name": "Test Person",
    })
    return [lead], {"person_match_attempts": 1}


class EnrichmentRuntimeBudgetTests(unittest.TestCase):
    def _run(self, *, budget, clock):
        jobs = [_job("j1", "Acme"), _job("j2", "Beta"), _job("j3", "Gamma")]
        with tempfile.TemporaryDirectory() as temp:
            input_path = Path(temp) / "input.json"
            input_path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
            with (
                patch.object(config, "STEP3_OUTPUT_DIR", temp),
                patch.object(config, "FINAL_PASS_PIPELINE_ENABLED", True),
                patch.object(config, "CONTINUE_AFTER_FINAL_PASS_TARGET", True),
                patch.object(config, "ORCHESTRATOR_ENRICHMENT_MAX_RUNTIME_SECONDS", budget),
                patch.object(hiring_manager, "validate_preflight", return_value=None),
                patch.object(hiring_manager, "process_company", side_effect=_fake_process_company),
                patch.object(hiring_manager.time, "monotonic", side_effect=clock),
            ):
                result = hiring_manager.run_hiring_manager_identification(
                    str(input_path), target_final_pass_leads=100)
                progress = json.loads(
                    (Path(temp) / "enrichment_progress.json").read_text(encoding="utf-8"))
        return result, progress

    def test_budget_stops_after_first_company_and_preserves_it(self):
        # clock: start=0, iter1 check=0 (process Acme), iter2 check=999 (trip).
        clock = itertools.chain([0.0, 0.0, 999.0], itertools.repeat(999.0))
        result, progress = self._run(budget=1, clock=clock)
        self.assertEqual(result.stop_reason, "enrichment_runtime_budget_reached")
        self.assertEqual(result.companies_considered, 1)
        # The completed company is checkpointed -> a later run resumes it (no re-Apollo).
        self.assertEqual(len(progress.get("companies", {})), 1)

    def test_budget_zero_processes_every_company(self):
        result, progress = self._run(budget=0, clock=itertools.repeat(0.0))
        self.assertEqual(result.companies_considered, 3)
        self.assertNotEqual(result.stop_reason, "enrichment_runtime_budget_reached")
        self.assertEqual(len(progress.get("companies", {})), 3)


if __name__ == "__main__":
    unittest.main()
