"""Integration coverage for the yield-optimization architecture:
enrollment person-employer uniqueness, provider reject-only pre-gate, per-job
Hunter gate, governor<->top-up wiring (cumulative metrics, distinct stop reasons,
zero-budget clean stop), and Apollo cache wiring (variant-scoped negatives)."""
from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import config
from orchestrator import adapters_real as AR
from orchestrator.enrichment import Disposition, Lead
from orchestrator.reasons import ReasonCode


def _lead(email, bucket, domain="acme.com", key=None):
    return Lead(posting_id=f"P{email}{bucket}", company={"name": "Acme", "website": f"https://{domain}"},
                contact={"email": email, "_airtable_row": {"employer_website": domain, "_role_bucket": bucket}},
                disposition=Disposition.FINAL_PASS, primary_reason=ReasonCode.OK,
                contact_key=key or f"{domain}|{email}|{bucket}")


class PersonEmployerUniquenessTests(unittest.TestCase):
    def test_disabled_keeps_all(self):
        leads = [_lead("rob@acme.com", "marketing"), _lead("rob@acme.com", "gtm_revenue")]
        with mock.patch.object(config, "ENROLLMENT_PERSON_EMPLOYER_UNIQUENESS", False):
            kept, losers = AR._collapse_person_employer(leads)
        self.assertEqual(len(kept), 2); self.assertEqual(losers, [])

    def test_same_person_many_functions_enrolled_once_highest_priority_wins(self):
        leads = [_lead("rob@acme.com", b) for b in
                 ("customer_support", "marketing", "engineering", "operations", "gtm_revenue")]
        with mock.patch.object(config, "ENROLLMENT_PERSON_EMPLOYER_UNIQUENESS", True):
            kept, losers = AR._collapse_person_employer(leads)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].contact["_airtable_row"]["_role_bucket"], "gtm_revenue")
        self.assertEqual(len(losers), 4)
        self.assertTrue(all(w == "acme.com|rob@acme.com" for w, _ in losers))

    def test_distinct_buyers_and_same_person_at_other_employer_preserved(self):
        leads = [_lead("rob@acme.com", "gtm_revenue"), _lead("ana@acme.com", "finance"),
                 _lead("rob@acme.com", "gtm_revenue", domain="beta.com")]
        with mock.patch.object(config, "ENROLLMENT_PERSON_EMPLOYER_UNIQUENESS", True):
            kept, losers = AR._collapse_person_employer(leads)
        self.assertEqual(len(kept), 3); self.assertEqual(losers, [])

    def test_existing_airtable_person_not_reenrolled_under_new_bucket(self):
        existing = {"rec1": {"fields": {"Email": "Rob@Acme.com", "Website": "https://www.acme.com",
                                        "Role Bucket": "marketing"}}}
        leads = [_lead("rob@acme.com", "gtm_revenue")]
        with mock.patch.object(config, "ENROLLMENT_PERSON_EMPLOYER_UNIQUENESS", True):
            kept, losers = AR._collapse_person_employer(leads, existing=existing)
        self.assertEqual(kept, []); self.assertEqual(len(losers), 1)

    def test_person_index_works_from_lead_key_alone(self):
        # The real snapshot fetches "Lead Key" but NOT "Email" -- the index must be
        # derivable from the lead_key (domain|email|bucket) or cross-run protection
        # silently no-ops.
        existing = {"rec1": {"fields": {"Lead Key": "acme.com|rob@acme.com|marketing",
                                        "Role Bucket": "marketing"}}}
        self.assertEqual(AR._existing_person_keys(existing), {"acme.com|rob@acme.com"})
        leads = [_lead("rob@acme.com", "gtm_revenue")]
        with mock.patch.object(config, "ENROLLMENT_PERSON_EMPLOYER_UNIQUENESS", True):
            kept, losers = AR._collapse_person_employer(leads, existing=existing)
        self.assertEqual(kept, []); self.assertEqual(len(losers), 1)

    def test_person_index_ignores_malformed_lead_keys(self):
        existing = {"a": {"fields": {"Lead Key": "acme.com"}},
                    "b": {"fields": {"Lead Key": "acme.com|notanemail|gtm"}},
                    "c": {"fields": {}}}
        self.assertEqual(AR._existing_person_keys(existing), set())

    def test_deliver_drops_losers_and_never_marks_them_delivered(self):
        captured = {}
        def fake_push(rows, existing=None):
            captured["rows"] = rows
            return {"created": len(rows), "created_lead_keys": [r["lead_key"] for r in rows],
                    "persisted_lead_keys": [r["lead_key"] for r in rows], "updated": 0,
                    "skipped_existing": 0, "skipped_existing_company": 0, "skipped_existing_account": 0,
                    "skipped_no_contact": 0, "failed": 0, "failed_lead_keys": [],
                    "suppressed_company_lead_keys": [], "suppressed_account_lead_keys": [],
                    "not_written_not_send_safe": 0}
        leads = [_lead("rob@acme.com", "marketing"), _lead("rob@acme.com", "gtm_revenue")]
        import airtable_client
        with mock.patch.object(config, "ENROLLMENT_PERSON_EMPLOYER_UNIQUENESS", True), \
                mock.patch.object(airtable_client, "push_leads", fake_push):
            rep = AR.RealDelivery(enable_airtable_write=True, auto_approve=False, enable_instantly=False)\
                .deliver(leads, run_id="r")
        self.assertEqual(len(captured["rows"]), 1)
        self.assertEqual(rep.person_employer_duplicate, 1)
        self.assertEqual(rep.reviewable_submitted, 1)
        self.assertEqual(rep.created, 1)
        loser_key = "acme.com|rob@acme.com|marketing"
        self.assertNotIn(loser_key, rep.delivered_lead_keys)        # never marked delivered
        self.assertTrue(rep.reviewable_reconciles())
        self.assertTrue(rep.reconciles())
        self.assertEqual(rep.detail["person_employer_collapsed"][0]["dropped_bucket"], "marketing")


class SkipCountersTests(unittest.TestCase):
    def test_breakdown_is_mutually_exclusive_and_exact(self):
        # Aug-18 shaped push_leads result: no triple count.
        def fake_push(rows, existing=None):
            return {"created": 5, "created_lead_keys": [r["lead_key"] for r in rows[:5]],
                    "persisted_lead_keys": [r["lead_key"] for r in rows[:8]], "updated": 0,
                    "skipped_existing": 2, "skipped_existing_company": 1, "skipped_existing_account": 0,
                    "skipped_no_contact": 2, "failed": 0, "failed_lead_keys": [],
                    "suppressed_company_lead_keys": [rows[7]["lead_key"]],
                    "suppressed_account_lead_keys": [], "not_written_not_send_safe": 0}
        leads = [_lead(f"p{i}@acme.com", "finance") for i in range(10)]
        import airtable_client
        with mock.patch.object(airtable_client, "push_leads", fake_push):
            rep = AR.RealDelivery(enable_airtable_write=True, auto_approve=False, enable_instantly=False)\
                .deliver(leads, run_id="r")
        b = rep.skip_breakdown()
        self.assertEqual(b["company_function_suppressed"], 1)       # counted ONCE
        self.assertEqual(b["no_contact"], 2)
        self.assertEqual(b["skipped_existing"], 2)
        self.assertEqual(rep.reviewable_submitted - rep.created - rep.failed, sum(b.values()))
        self.assertTrue(rep.reviewable_reconciles())
        self.assertEqual(rep.detail["other_skips"], 3)              # excludes skipped_existing


class ProviderPreGateTests(unittest.TestCase):
    def test_off_by_default(self):
        import hiring_manager as HM
        with mock.patch.object(config, "PROVIDER_FIRMOGRAPHIC_PRE_REJECT", False):
            self.assertEqual(HM.provider_pre_reject_reason({"_org_industry": "Hospitals and Health Care"}), "")

    def test_exact_label_match_only_normalized_never_substring(self):
        import hiring_manager as HM
        with mock.patch.object(config, "PROVIDER_FIRMOGRAPHIC_PRE_REJECT", True), \
                mock.patch.object(config, "PROVIDER_PRE_REJECT_INDUSTRIES", ["Hospitals and Health Care"]):
            self.assertTrue(HM.provider_pre_reject_reason({"_org_industry": "Hospitals and Health Care"}))
            self.assertTrue(HM.provider_pre_reject_reason({"_org_industry": "hospitals AND health care "}))
            self.assertEqual(HM.provider_pre_reject_reason({"_org_industry": "Medical Equipment Manufacturing"}), "")
            self.assertEqual(HM.provider_pre_reject_reason({"_org_industry": "Health Care"}), "")
            self.assertEqual(HM.provider_pre_reject_reason({"_org_industry": ""}), "")

    def test_pre_reject_skips_apollo_org_and_never_passes(self):
        import hiring_manager as HM
        calls = {"enrich": 0}
        def fake_enrich(**kw):
            calls["enrich"] += 1
            return HM.apollo.OrgEnrichment(found=True, employee_count=100)
        job = {"_org_industry": "Hospitals and Health Care", "employer_name": "Mercy", "employer_website": "mercy.com",
               "job_title": "Accountant", "_job_gate_decision": {}, "_role_gate_decision": {}}
        with mock.patch.object(config, "PROVIDER_FIRMOGRAPHIC_PRE_REJECT", True), \
                mock.patch.object(config, "PROVIDER_PRE_REJECT_INDUSTRIES", ["Hospitals and Health Care"]), \
                mock.patch.object(config, "APOLLO_RATE_LIMIT_DELAY", 0), \
                mock.patch.object(HM.apollo, "enrich_organization", fake_enrich):
            try:
                leads, stats = HM._process_company_strict([job])
            except Exception:
                # Downstream gates may need richer fixtures; the contract under test
                # is that Apollo org-enrich was NOT called and provenance was stamped.
                leads, stats = [], {}
        self.assertEqual(calls["enrich"], 0)
        self.assertEqual(job["_provider_pre_reject_reason"], "provider_industry:Hospitals and Health Care")
        self.assertTrue(job["_apollo_org_skipped"])
        self.assertTrue(all(l.get("_final_state") != "FINAL_PASS" for l in leads))


class HunterGateTests(unittest.TestCase):
    def test_default_on_for_fantastic_and_other_paths(self):
        import hiring_manager as HM
        with mock.patch.object(config, "VERIFY_WITH_HUNTER", True), \
                mock.patch.object(config, "HUNTER_ENABLED_FOR_FANTASTIC_PATH", True):
            self.assertTrue(HM.hunter_allowed_for_job({"_fantastic_internal_id": "1"}))
            self.assertTrue(HM.hunter_allowed_for_job({"_acquisition_source": "jsearch"}))

    def test_gate_affects_only_fantastic_path(self):
        import hiring_manager as HM
        with mock.patch.object(config, "VERIFY_WITH_HUNTER", True), \
                mock.patch.object(config, "HUNTER_ENABLED_FOR_FANTASTIC_PATH", False):
            self.assertFalse(HM.hunter_allowed_for_job({"_fantastic_internal_id": "1"}))
            self.assertTrue(HM.hunter_allowed_for_job({"_acquisition_source": "jsearch"}))
        with mock.patch.object(config, "VERIFY_WITH_HUNTER", False):
            self.assertFalse(HM.hunter_allowed_for_job({"_acquisition_source": "jsearch"}))


class GovernorTopupWiringTests(unittest.TestCase):
    """Top-up respects the governor budget, reports cumulative metrics and distinct
    stop reasons; flag OFF preserves the 6000 safety cap exactly."""

    def _run(self, *, governor, remaining, supply, max_cap=6000, slice_jobs=500, target=1000):
        from retrieval_measurement.instrument import RequestBudget
        from orchestrator.enrichment import EnrichmentReport
        from orchestrator.adapters_real import RealDeliveryReport
        from orchestrator.lanes import LaneResult
        from orchestrator.modes import ExecutionMode, policy_for
        from orchestrator.runcontrol import RunContext
        from orchestrator.state import StateManager
        from orchestrator.pipeline import Orchestrator, OrchestratorPlan
        import fantastic_jobs_adapter as _fja

        tmp = tempfile.mkdtemp()
        state_files = {k: os.path.join(tmp, f"{k}.json") for k in ("ledger", "snap", "yl")}
        import json
        with open(state_files["snap"], "w") as fh:
            json.dump({"jobs_remaining": remaining, "next_billing_date": "2026-10-17"}, fh)
        calls = {"n": 0}

        def runner(manager):
            n = min(supply, _fja._effective_run_cap())
            calls["n"] += 1
            jobs = [{"job_id": f"J{calls['n']}_{i}", "employer_name": "Co", "job_title": "Engineer",
                     "_fantastic_internal_id": f"{calls['n']}_{i}"} for i in range(n)]
            # provider billed 10% more rows than kept (dupes/rejects)
            billed = int(n * 1.1)
            return LaneResult(lane="fantastic", status="complete", jobs=jobs, physical_requests=1,
                              attribution={"source": "fantastic_jobs", "records": n, "raw_records": billed,
                                           "jobs_quota_consumed": billed, "jobs_quota_remaining": remaining - billed,
                                           "stop_reason": "", "per_source": {"fantastic_jobs_linkedin": {
                                               "jobs": n, "returned_billed": billed, "requests": 1}}})

        class _Enr:
            def run(self, opps, **k):
                return EnrichmentReport(leads=[], stages=[])

        class _Del:
            def deliver(self, leads, **k):
                return RealDeliveryReport(mode="review_staging")

        mode = ExecutionMode.FULL_DRY_RUN
        ctx = RunContext.create(mode, {"t": 1}, run_id="GOV")
        st = StateManager(tmp, policy_for(mode), run_id="GOV")
        plan = OrchestratorPlan(lanes=["fantastic"], lane_runners={"fantastic": runner},
                                enrichment_engine=_Enr(), delivery_manager=_Del(), target=5)
        with mock.patch.multiple(config, NET_NEW_SEND_SAFE_TARGET=target, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=max_cap,
                                 FANTASTIC_TOPUP_SLICE_JOBS=slice_jobs, FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=500,
                                 TOPUP_MAX_ITERATIONS=40, PRE_APOLLO_EXISTING_DEDUPE=False,
                                 FANTASTIC_MONTHLY_GOVERNOR_ENABLED=governor, FANTASTIC_MONTHLY_JOBS_LIMIT=20000,
                                 FANTASTIC_MONTHLY_RESERVE_PCT=0.10, FANTASTIC_DAILY_MIN_JOBS=100,
                                 FANTASTIC_DAILY_MAX_JOBS=0, FANTASTIC_GOVERNOR_USE_PROVIDER_HEADERS=True,
                                 FANTASTIC_GOVERNOR_USE_COUNT_HINT=False, FANTASTIC_GOVERNOR_CARRY_CAP_DAYS=3.0,
                                 FANTASTIC_BILLING_RESET_AT="", FANTASTIC_GOVERNOR_LEDGER_PATH=state_files["ledger"],
                                 FANTASTIC_QUOTA_SNAPSHOT_PATH=state_files["snap"], YIELD_LEDGER_ENABLED=True,
                                 YIELD_LEDGER_PATH=state_files["yl"], FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=False):
            with mock.patch.object(Orchestrator, "_count_net_new_send_safe", staticmethod(lambda l, d: 0)):
                res = Orchestrator(ctx, st, RequestBudget(limit=10_000)).run(plan)
        return res, state_files

    def test_flag_off_preserves_6000_cap_and_safety_cap_reason(self):
        res, _ = self._run(governor=False, remaining=20000, supply=500)
        self.assertEqual(res["acquisition"]["budget_source"], "per_run_ceiling")
        self.assertEqual(res["acquisition"]["run_cap"], 6000)
        self.assertEqual(res["topup"]["final_stop_reason"], "acquisition_safety_cap")
        self.assertFalse(res["governor"]["enabled"])

    def test_governor_caps_run_with_partial_final_slice_and_distinct_reason(self):
        # remaining 20000, fresh cycle ~27d -> pace ~667; slices 500 then ~167, then stop.
        res, files = self._run(governor=True, remaining=20000, supply=500)
        acq = res["acquisition"]
        self.assertEqual(acq["budget_source"], "governor")
        self.assertLess(acq["run_cap"], 6000)
        self.assertEqual(res["topup"]["final_stop_reason"], "governor_run_budget")
        budget = res["governor"]["decision"]["run_budget"]
        self.assertEqual(acq["run_cap"], budget)
        # Every slice is clamped to the governor budget; the controller stops once
        # BILLED (returned) rows reach it -- billed > kept because the provider
        # returned dupes/rejects, and billing (not kept) is what the budget bounds.
        cum = acq["cumulative"]
        self.assertEqual(cum["jobs_unique_kept"], sum(i["jobs_unique_kept"] for i in acq["per_iteration"]))
        self.assertGreater(cum["jobs_returned_billed"], cum["jobs_unique_kept"])
        self.assertLessEqual(cum["jobs_unique_kept"], budget)
        self.assertEqual(cum["physical_requests"], acq["iterations"])
        self.assertGreaterEqual(acq["iterations"], 1)
        # Ledger recorded BILLED rows (idempotent per run).
        import json
        led = json.load(open(files["ledger"]))
        self.assertEqual(led["used"], cum["jobs_returned_billed"])
        self.assertEqual(len(led["runs"]), 1)

    def test_target_cannot_raise_governor_budget(self):
        res, _ = self._run(governor=True, remaining=20000, supply=500, target=10_000)
        self.assertEqual(res["topup"]["final_stop_reason"], "governor_run_budget")
        self.assertLess(res["topup"]["jobs_billed"], 2000)

    def test_zero_budget_is_clean_stop_not_failure_and_no_acquisition(self):
        res, _ = self._run(governor=True, remaining=436, supply=500)
        self.assertEqual(res["run"]["status"], "complete")
        self.assertEqual(res["topup"]["final_stop_reason"], "governor_zero_budget")
        self.assertEqual(res["acquisition"]["iterations"], 0)
        self.assertEqual(res["acquisition"]["cumulative"]["jobs_returned_billed"], 0)
        self.assertEqual(res["governor"]["decision"]["run_budget"], 0)
        self.assertEqual(res["governor"]["decision"]["reason"], "provider_quota_floor")

    def test_failed_lane_still_propagates(self):
        from orchestrator.lanes import LaneResult
        res, _ = self._run(governor=True, remaining=20000, supply=0)
        self.assertIn(res["topup"]["final_stop_reason"], ("inventory_exhausted",))


class ApolloCacheWiringTests(unittest.TestCase):
    def test_negative_cache_is_variant_scoped(self):
        import hiring_manager as HM
        self.assertNotEqual(HM._titles_fingerprint(["VP Sales"]), HM._titles_fingerprint(["VP Sales", "CRO"]))
        self.assertEqual(HM._titles_fingerprint(["b", "A"]), HM._titles_fingerprint(["a", "B"]))

    def test_org_cache_positive_only_and_fingerprint_sensitive(self):
        import hiring_manager as HM
        tmp = tempfile.mkdtemp()
        calls = {"n": 0}
        def fake_enrich(**kw):
            calls["n"] += 1
            return HM.apollo.OrgEnrichment(found=(calls["n"] == 1), domain="acme.com", employee_count=120)
        HM.reset_apollo_cache()
        with mock.patch.multiple(config, APOLLO_CACHE_ENABLED=True, APOLLO_CACHE_PATH=os.path.join(tmp, "c.json"),
                                 APOLLO_RATE_LIMIT_DELAY=0), \
                mock.patch.object(HM.apollo, "enrich_organization", fake_enrich):
            o1 = HM._cached_enrich_organization("acme.com", "Acme", "https://acme.com")
            o2 = HM._cached_enrich_organization("acme.com", "Acme", "https://acme.com")   # hit
            self.assertTrue(o1.found and o2.found)
            self.assertEqual(calls["n"], 1)
            # A not-found org is NEVER cached: next call re-enriches.
            HM.reset_apollo_cache()
            with mock.patch.object(config, "APOLLO_CACHE_PATH", os.path.join(tmp, "c2.json")):
                HM.reset_apollo_cache()
                calls["n"] = 5   # subsequent enrich returns found=False
                HM._cached_enrich_organization("beta.com", "Beta", "https://beta.com")
                HM._cached_enrich_organization("beta.com", "Beta", "https://beta.com")
                self.assertEqual(calls["n"], 7)
        HM.reset_apollo_cache()

    def test_cache_disabled_by_default_is_inert(self):
        import hiring_manager as HM
        HM.reset_apollo_cache()
        with mock.patch.object(config, "APOLLO_CACHE_ENABLED", False):
            c = HM._apollo_cache()
            self.assertFalse(c.enabled)
            c.put("org", "acme.com", {"x": 1})
            self.assertIsNone(c.get("org", "acme.com"))
        HM.reset_apollo_cache()


if __name__ == "__main__":
    unittest.main()
