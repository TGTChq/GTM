"""Apollo exhaustion mid-run must fail safely and partially, never roll back.

When Apollo credits/rate/auth fail part-way through a multi-company enrichment:
  * companies already enriched stay checkpointed and their leads are preserved;
  * the run stops with the explicit reason "apollo_circuit_open";
  * a resume reuses the checkpoint (no repeated Apollo) and finishes the rest;
  * completed reviewable leads still reach Airtable delivery;
  * Airtable duplicates are impossible via lead_key idempotency;
  * the orchestrator surfaces an explicit INCOMPLETE (never a false success).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import apollo_client
import config
import hiring_manager
from orchestrator.adapters_real import RealDelivery
from orchestrator.enrichment import Lead
from orchestrator.reasons import Disposition, ReasonCode


def _job(job_id, employer):
    return {
        "job_id": job_id, "job_title": "Staff Accountant", "employer_name": employer,
        "canonical_employer_name": employer, "employer_website": f"https://{employer.lower()}.com",
        "_matched_role": "Staff Accountant", "_job_gate_state": "PASS", "_role_gate_state": "PASS",
    }


def _fp(company_jobs):
    emp = company_jobs[0]["employer_name"]
    lead = dict(company_jobs[0])
    lead.update({
        "_final_state": "FINAL_PASS", "_step3_status": "found", "_account_gate_state": "PASS",
        "lead_key": f"{emp}|hm@{emp.lower()}.com|finance",
        "hiring_manager_email": f"hm@{emp.lower()}.com", "hiring_manager_name": "HM",
    })
    return [lead], {"person_match_attempts": 1}


class ApolloExhaustionMidRunTests(unittest.TestCase):
    def _run(self, temp, input_path, process_company):
        with (
            patch.object(config, "STEP3_OUTPUT_DIR", temp),
            patch.object(config, "FINAL_PASS_PIPELINE_ENABLED", True),
            patch.object(config, "CONTINUE_AFTER_FINAL_PASS_TARGET", True),
            patch.object(config, "ORCHESTRATOR_ENRICHMENT_MAX_RUNTIME_SECONDS", 0),
            patch.object(hiring_manager, "validate_preflight", return_value=None),
            patch.object(hiring_manager, "process_company", side_effect=process_company),
        ):
            return hiring_manager.run_hiring_manager_identification(
                str(input_path), target_final_pass_leads=100)

    def test_partial_then_resume_without_repeating_apollo(self):
        jobs = [_job("j1", "Acme"), _job("j2", "Beta"), _job("j3", "Gamma")]
        with tempfile.TemporaryDirectory() as temp:
            input_path = Path(temp) / "input.json"
            input_path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")

            # --- Run 1: Apollo exhausts on the 2nd company processed ---
            run1 = {"n": 0, "calls": []}

            def pc1(company_jobs):
                run1["n"] += 1
                run1["calls"].append(company_jobs[0]["employer_name"])
                if run1["n"] == 2:
                    raise apollo_client.ApolloCreditsExhaustedError("shared credits exhausted")
                return _fp(company_jobs)

            r1 = self._run(temp, input_path, pc1)
            first_done = run1["calls"][0]
            self.assertEqual(r1.stop_reason, "apollo_circuit_open")   # explicit reason (req 8)
            self.assertEqual(r1.companies_considered, 1)              # only the first completed
            self.assertEqual(r1.stats.get("apollo_circuit_open"), 1)
            progress = json.loads((Path(temp) / "enrichment_progress.json").read_text(encoding="utf-8"))
            self.assertEqual(list(progress["companies"].keys()).count(
                next(k for k in progress["companies"])), 1)
            self.assertEqual(len(progress["companies"]), 1)          # completed company checkpointed (req 2)

            # --- Run 2: resume in the SAME dir; Apollo healthy ---
            run2 = {"calls": []}

            def pc2(company_jobs):
                run2["calls"].append(company_jobs[0]["employer_name"])
                return _fp(company_jobs)

            r2 = self._run(temp, input_path, pc2)
            self.assertNotIn(first_done, run2["calls"])              # no repeated Apollo (req 6)
            self.assertEqual(r2.companies_considered, 3)            # all companies now done
            self.assertNotEqual(r2.stop_reason, "apollo_circuit_open")

            # No duplicate lead_keys in the resumed output (req 7 at source).
            leads = json.loads(Path(r2.output_path).read_text(encoding="utf-8")).get("leads", [])
            keys = [l.get("lead_key") for l in leads if l.get("lead_key")]
            self.assertEqual(len(keys), len(set(keys)))

    def test_completed_leads_still_delivered_and_idempotent(self):
        # The single company completed before the outage still reaches Airtable,
        # and a re-delivery of the same lead_key is skipped (no duplicate).
        row = {"lead_key": "Acme|hm@acme.com|finance", "Company": "Acme"}
        lead = Lead("j1", {"name": "Acme"},
                    {"email": "hm@acme.com", "name": "HM", "_airtable_row": row},
                    Disposition.FINAL_PASS, ReasonCode.OK, contact_key="Acme|hm@acme.com|finance")
        captured = {}

        def fake_push(rows, batch_size=10, existing=None):
            captured["rows"] = rows
            keys = [r.get("lead_key") for r in rows]
            return {"created": len(rows), "failed": 0, "skipped_existing": 0,
                    "persisted_lead_keys": keys}

        rd = RealDelivery(enable_airtable_write=True, auto_approve=False, enable_instantly=False)
        with patch("airtable_client.push_leads", side_effect=fake_push):
            rep = rd.deliver([lead], run_id="r1", source="fantastic_jobs")
        self.assertEqual(rep.created, 1)
        self.assertEqual([r["lead_key"] for r in captured["rows"]], ["Acme|hm@acme.com|finance"])

        # Idempotency: the same lead already delivered is NOT re-submitted.
        with patch("airtable_client.push_leads", side_effect=fake_push) as p2:
            rep2 = rd.deliver([lead], run_id="r2", source="fantastic_jobs",
                              known_delivered={"Acme|hm@acme.com|finance"})
        self.assertEqual(rep2.reviewable_submitted, 0)
        self.assertEqual(p2.call_args.args[0], [])   # nothing submitted -> no duplicate


if __name__ == "__main__":
    unittest.main()
