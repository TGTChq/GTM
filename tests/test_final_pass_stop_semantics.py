"""Incident 1 regression tests: FINAL_PASS stop semantics.

Production (config-C, 2026-07-30) emitted the logically invalid combination

    FINAL_PASS=15/30   review_rows=30/30   stop_reason=final_pass_target_reached

because ``run_final_pass_topup`` measured progress with the Airtable-reviewable
*surface* count (FINAL_PASS + NEEDS_CHECK/UNVERIFIED review rows) instead of the
genuine reconciled FINAL_PASS count. Reaching the 30 review-row target therefore
satisfied the FINAL_PASS target and stopped acquisition at 15 genuine leads.

Every test below pins the corrected contract:

* progress / deficit / stop_reason are computed against genuine FINAL_PASS only;
* review rows never reduce the FINAL_PASS deficit;
* ``final_pass_target_reached`` is emitted only at genuine FINAL_PASS >= target;
* 30 is a floor, never a cap (FINAL_PASS > 30 is preserved, not truncated);
* below-target stops carry an exact non-target reason and never loop forever.

The fixture in ``test_incident_fixture_*`` reproduces the exact production
condition; it fails against the pre-fix surface-based behavior (which would stop
immediately with ``final_pass_target_reached_initial_pass`` and never call the
top-up scraper) and passes after the correction.
"""
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


def _fp_lead(key: str) -> dict:
    return {
        "job_id": f"job-{key}",
        "lead_key": key,
        "_final_state": "FINAL_PASS",
        "_account_gate_state": "PASS",
        "_search_role": "Staff Accountant",
        "hiring_manager_email": f"{key}@example.com",
        "hiring_manager_name": "Test Person",
    }


def _nc_lead(key: str) -> dict:
    return {
        "job_id": f"job-{key}",
        "lead_key": key,
        "_final_state": "NEEDS_CHECK",
        "_account_gate_state": "NEEDS_CHECK",
        "_search_role": "Staff Accountant",
        "hiring_manager_email": f"{key}@example.com",
        "hiring_manager_name": "Test Person",
    }


def _reject_lead(key: str) -> dict:
    return {"job_id": f"job-{key}", "lead_key": key, "_final_state": "REJECT"}


def _uv_lead(key: str) -> dict:
    return {
        "job_id": f"job-{key}",
        "lead_key": key,
        "_final_state": "UNVERIFIED",
        "_account_gate_state": "UNVERIFIED",
        "_search_role": "Staff Accountant",
        "hiring_manager_email": f"{key}@example.com",
        "hiring_manager_name": "Test Person",
    }


def _step3(path: Path, *, final_pass: int, review_rows: int, companies: list[str]) -> Step3Result:
    return Step3Result(
        output_path=str(path),
        total_input_jobs=max(1, review_rows),
        total_output_leads=review_rows,
        company_criteria_excluded=0,
        hiring_manager_found=review_rows,
        hiring_manager_not_found=0,
        match_rate=1.0,
        contactable_hiring_managers=review_rows,
        uncontactable_hiring_managers=0,
        contactable_rate=1.0,
        companies_considered=len(companies) or 1,
        eligible_companies=len(companies) or 1,
        company_criteria_excluded_companies=0,
        final_pass_target=30,
        final_pass_leads=final_pass,
        needs_check_leads=max(0, review_rows - final_pass),
        final_pass_target_reached=False,
        reviewable_leads=review_rows,
        reviewable_target_reached=False,
        max_eligible_companies=0,
        stop_reason="candidate_pool_exhausted",
        processed_company_keys=companies,
        stats={},
    )


class FinalPassStopSemanticsTests(unittest.TestCase):
    """Drive run_final_pass_topup with a per-round mock funnel.

    Each ``round`` spec dict controls one micro-batch:
      units, attempted, viable, kept, contact  -> gate values,
      add_fp / add_nc                          -> new leads the enrichment emits.
    A shared index lets every mocked stage read the current round, so the loop's
    real control flow (deficit, budget, zero-downstream, target) is exercised.
    """

    def _run(self, *, target, initial_fp, initial_nc, rounds, initial_reject=0,
             initial_uv=0, cfg=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = {"i": -1}

            initial_leads = (
                [_fp_lead(f"init-fp-{n}") for n in range(initial_fp)]
                + [_nc_lead(f"init-nc-{n}") for n in range(initial_nc)]
                + [_uv_lead(f"init-uv-{n}") for n in range(initial_uv)]
                + [_reject_lead(f"init-rej-{n}") for n in range(initial_reject)]
            )
            initial_path = root / "initial.json"
            initial_path.write_text(json.dumps({
                "jobs": initial_leads,
                "processed_job_refs": [{"job_id": "seed"}],
                "processed_company_keys": ["seed.com"],
            }), encoding="utf-8")
            initial_result = _step3(
                initial_path, final_pass=initial_fp,
                review_rows=initial_fp + initial_nc + initial_uv, companies=["seed.com"],
            )
            initial_scrape = ScrapeResult(
                output_path=str(initial_path), total_jobs=1, roles_with_results=1,
                stats={"estimated_request_units": 0, "query_metrics": {}},
            )

            def scrape_side(**kwargs):
                state["i"] += 1
                spec = rounds[state["i"]]
                raw = root / f"topup_raw_{state['i']}.json"
                raw.write_text(json.dumps({
                    "jobs": [{"job_id": f"tj-{state['i']}"}] if spec.get("viable", 1) else [],
                }), encoding="utf-8")
                return ScrapeResult(
                    output_path=str(raw),
                    total_jobs=1 if spec.get("viable", 1) else 0,
                    roles_with_results=1 if spec.get("viable", 1) else 0,
                    stats={
                        "estimated_request_units": spec.get("units", 6),
                        "queries_attempted": spec.get("attempted", 1),
                        "queried_search_roles": [f"role-{state['i']}"],
                        "topup_new_prefilter_viable": spec.get("viable", 1),
                        "topup_stop_reason": "",
                        "query_metrics": {},
                    },
                )

            def filter_side(**kwargs):
                spec = rounds[state["i"]]
                out = root / f"filtered_{state['i']}.json"
                out.write_text(json.dumps({"jobs": [{"job_id": f"tj-{state['i']}"}]}), encoding="utf-8")
                return SimpleNamespace(
                    output_path=str(out), kept_count=spec.get("kept", 1),
                    rejected_count=0, success=True, errors=[],
                )

            def qual_side(*args, **kwargs):
                spec = rounds[state["i"]]
                out = root / f"qualified_{state['i']}.json"
                out.write_text(json.dumps({"jobs": [{"job_id": f"tj-{state['i']}"}]}), encoding="utf-8")
                return SimpleNamespace(
                    output_path=str(out), contact_eligible_jobs=spec.get("contact", 1),
                    rejected_jobs=0, unverified_jobs=0, nonpass_path="",
                )

            def enrich_side(*args, **kwargs):
                i = state["i"]
                if i < 0:  # reroute recovery path (unused here) -> no-op payload
                    out = root / "reroute_enriched.json"
                    out.write_text(json.dumps({"jobs": [], "processed_company_keys": []}), encoding="utf-8")
                    return _step3(out, final_pass=0, review_rows=0, companies=[])
                spec = rounds[i]
                new = (
                    [_fp_lead(f"r{i}-fp-{n}") for n in range(spec.get("add_fp", 0))]
                    + [_nc_lead(f"r{i}-nc-{n}") for n in range(spec.get("add_nc", 0))]
                )
                out = root / f"enriched_{i}.json"
                out.write_text(json.dumps({
                    "jobs": new,
                    "processed_job_refs": [{"job_id": f"tj-{i}"}],
                    "processed_company_keys": [f"company-{i}.com"],
                }), encoding="utf-8")
                return _step3(
                    out, final_pass=spec.get("add_fp", 0),
                    review_rows=spec.get("add_fp", 0) + spec.get("add_nc", 0),
                    companies=[f"company-{i}.com"],
                )

            patches = {
                "STEP3_OUTPUT_DIR": str(root),
                "FILTERED_OUTPUT_DIR": str(root),
                "ACQUISITION_MODE": "multi_source",
                "FINAL_PASS_MAX_RUNTIME_SECONDS": 300,
                "FINAL_PASS_MICROBATCH_QUERY_UNITS": 6,
                "FINAL_PASS_MAX_EMPTY_QUERY_CYCLES": 2,
                "MULTI_SOURCE_TOPUP_MAX_ZERO_DOWNSTREAM_BATCHES": 4,
                "JSEARCH_TOPUP_UNIT_BUDGET": 250,
                "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN": 450,
                "MAX_ELIGIBLE_COMPANIES_PER_RUN": 0,
            }
            patches.update(cfg or {})
            cms = [patch.object(config, key, value) for key, value in patches.items()]
            cms += [
                patch.object(final_pass_topup, "run_targeted_topup_scrape", side_effect=scrape_side),
                patch.object(final_pass_topup, "run_filter", side_effect=filter_side),
                patch.object(final_pass_topup, "run_precontact_qualification", side_effect=qual_side),
                patch.object(final_pass_topup, "run_hiring_manager_identification", side_effect=enrich_side),
            ]
            for cm in cms:
                cm.start()
            try:
                combined, details = final_pass_topup.run_final_pass_topup(
                    initial_scrape=initial_scrape,
                    initial_enriched=initial_result,
                    registry=SeenJobsRegistry(path=str(root / "seen.json")),
                    target_final_pass_leads=target,
                    max_eligible_companies=0,
                )
            finally:
                for cm in reversed(cms):
                    cm.stop()
            self._scrape_rounds = state["i"] + 1
            return combined, details

    # 1 -----------------------------------------------------------------
    def test_fp15_review30_does_not_emit_target_reached(self):
        combined, details = self._run(
            target=30, initial_fp=15, initial_nc=15,
            rounds=[{"attempted": 0}, {"attempted": 0}],  # exhaust valid inventory
        )
        self.assertEqual(combined.final_pass_leads, 15)
        self.assertFalse(combined.final_pass_target_reached)
        self.assertNotIn(details["stop_reason"], {
            "final_pass_target_reached", "final_pass_target_reached_initial_pass",
        })
        self.assertEqual(details["stop_reason"], "valid_inventory_exhausted")

    # 2 -----------------------------------------------------------------
    def test_fp15_review30_continues_when_budget_and_query_remain(self):
        combined, details = self._run(
            target=30, initial_fp=15, initial_nc=15,
            rounds=[{"add_fp": 15}],  # a viable query exists -> must run it
        )
        self.assertGreaterEqual(self._scrape_rounds, 1)
        self.assertEqual(combined.final_pass_leads, 30)
        self.assertEqual(details["stop_reason"], "final_pass_target_reached")

    # 3 -----------------------------------------------------------------
    def test_fp29_review_over_30_continues(self):
        combined, details = self._run(
            target=30, initial_fp=29, initial_nc=10,  # review_rows = 39 > 30
            rounds=[{"add_fp": 1}],
        )
        self.assertGreaterEqual(self._scrape_rounds, 1)
        self.assertEqual(combined.final_pass_leads, 30)
        self.assertEqual(details["stop_reason"], "final_pass_target_reached")

    # 4 -----------------------------------------------------------------
    def test_fp_exactly_30_emits_target_reached(self):
        combined, details = self._run(
            target=30, initial_fp=15, initial_nc=0,
            rounds=[{"add_fp": 15}],
        )
        self.assertEqual(combined.final_pass_leads, 30)
        self.assertTrue(combined.final_pass_target_reached)
        self.assertEqual(details["stop_reason"], "final_pass_target_reached")
        self.assertEqual(details["deficit_remaining"], 0)

    # 5 -----------------------------------------------------------------
    def test_fp_over_30_is_preserved_without_truncation(self):
        combined, details = self._run(
            target=30, initial_fp=15, initial_nc=0,
            rounds=[{"add_fp": 20}],  # overshoots the floor
        )
        # 35 genuine FINAL_PASS are reconciled and preserved -- the floor of 30
        # did not cap or truncate the surplus.
        self.assertEqual(combined.final_pass_leads, 35)
        self.assertGreaterEqual(details["final_pass_leads"], 35)
        self.assertEqual(details["deficit_remaining"], 0)

    # 6 -----------------------------------------------------------------
    def test_review_states_never_reduce_final_pass_deficit(self):
        # 5 genuine FINAL_PASS plus a large pile of NEEDS_CHECK / UNVERIFIED /
        # REJECT rows. Deficit must be 30-5=25, never reduced by review rows.
        combined, details = self._run(
            target=30, initial_fp=5, initial_nc=40, initial_uv=10, initial_reject=20,
            rounds=[{"attempted": 0}, {"attempted": 0}],
        )
        self.assertEqual(combined.final_pass_leads, 5)
        self.assertEqual(details["deficit_remaining"], 25)
        self.assertFalse(combined.final_pass_target_reached)

    # 7 -----------------------------------------------------------------
    def test_topup_budget_exhaustion_below_target_exact_reason(self):
        combined, details = self._run(
            target=30, initial_fp=10, initial_nc=0,
            rounds=[{"add_fp": 1, "units": 6}],  # consumes the whole topup budget
            cfg={"JSEARCH_TOPUP_UNIT_BUDGET": 6, "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN": 450},
        )
        self.assertLess(combined.final_pass_leads, 30)
        self.assertEqual(details["stop_reason"], "jsearch_topup_budget_exhausted")
        self.assertFalse(combined.final_pass_target_reached)

    # 8 -----------------------------------------------------------------
    def test_global_quota_exhaustion_below_target_exact_reason(self):
        combined, details = self._run(
            target=30, initial_fp=10, initial_nc=0,
            rounds=[{"add_fp": 1, "units": 6}],
            cfg={"JSEARCH_TOPUP_UNIT_BUDGET": 0, "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN": 6},
        )
        self.assertLess(combined.final_pass_leads, 30)
        self.assertEqual(details["stop_reason"], "jsearch_run_global_budget_exhausted")
        self.assertFalse(combined.final_pass_target_reached)

    # 9 -----------------------------------------------------------------
    def test_repeated_zero_downstream_stops_safely_without_false_target(self):
        # Every round scrapes viable jobs but they all fail qualification
        # (contact_eligible=0): genuine zero downstream yield.
        zero = {"viable": 1, "kept": 1, "contact": 0}
        combined, details = self._run(
            target=30, initial_fp=12, initial_nc=18,  # review already 30
            rounds=[dict(zero) for _ in range(4)],
            cfg={"MULTI_SOURCE_TOPUP_MAX_ZERO_DOWNSTREAM_BATCHES": 4},
        )
        self.assertEqual(details["stop_reason"], "zero_downstream_yield")
        self.assertEqual(combined.final_pass_leads, 12)
        self.assertFalse(combined.final_pass_target_reached)
        # bounded: it stopped after exactly the configured number of empty batches
        self.assertLessEqual(self._scrape_rounds, 4)

    # 10 ----------------------------------------------------------------
    def test_incident_fixture_fp15_review30_continues_and_reconciles(self):
        # Exact production condition: FINAL_PASS=15, review_rows=30, top-up
        # budget largely unspent, a viable acquisition strategy remains.
        # Pre-fix (surface-based) code would emit
        # final_pass_target_reached_initial_pass and never call the scraper.
        combined, details = self._run(
            target=30, initial_fp=15, initial_nc=15,
            rounds=[{"add_fp": 5}, {"add_fp": 5}, {"add_fp": 5}],
            cfg={"JSEARCH_TOPUP_UNIT_BUDGET": 250,
                 "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN": 450},
        )
        # It did NOT short-circuit at the initial check.
        self.assertGreaterEqual(self._scrape_rounds, 1)
        self.assertNotEqual(
            details["stop_reason"], "final_pass_target_reached_initial_pass"
        )
        # It genuinely reached 30 FINAL_PASS through real top-up work.
        self.assertEqual(combined.final_pass_leads, 30)
        self.assertTrue(combined.final_pass_target_reached)
        self.assertEqual(details["stop_reason"], "final_pass_target_reached")


if __name__ == "__main__":
    unittest.main()
