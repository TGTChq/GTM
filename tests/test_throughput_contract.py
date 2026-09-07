"""Production-loop regressions; provider/delivery boundaries are offline fakes.

These establish machinery, not live contact yield or production approvals.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
from orchestrator import daily_target, pending_work
from orchestrator.adapters_real import RealDeliveryReport
from orchestrator.enrichment import Disposition, EnrichmentReport, Lead
from orchestrator.lanes import LaneResult
from orchestrator.modes import ExecutionMode as EM, policy_for
from orchestrator.pipeline import Orchestrator, OrchestratorPlan
from orchestrator.reasons import ReasonCode
from orchestrator.runcontrol import RunContext
from orchestrator.state import StateManager
from tests.test_pipeline_run_ledger import TOPUP_CONFIG, _Budget


class RecoveryProductionLoop(unittest.TestCase):
    def exercise(self, total, batch, target, *, watermark=True, prior=0, approved=True,
                 continue_after=False, deferred=False, own_pending=False, same_employer=False,
                 delivery_failed=False):
        root = Path(tempfile.mkdtemp())
        jobs = [{"job_id": f"owed-{i}", "posting_id": f"owed-{i}",
                 "employer_name": f"Company {i}", "company_name": f"Company {i}",
                 "job_title": "Head of Sales"} for i in range(total)]
        pending_work.record(root / pending_work.STORE, "earlier", jobs)
        ctx = RunContext.create(EM.LIVE_ACQUISITION_AND_ENRICHMENT, {},
                                run_id="20260906T220000Z-regression")
        state = StateManager(root, policy_for(ctx.mode), run_id=ctx.run_id)
        if own_pending:
            pending_work.record(root / pending_work.STORE, ctx.run_id,
                [{"job_id": "own-work", "posting_id": "own-work", "employer_name": "Own"}])
        if prior:
            daily_target.record_approved(state.store_path("daily_target"),
                [f"prior-{i}" for i in range(prior)], run_id="other-run")
        reached, acquisitions = [], []

        class Engine:
            def run(self, opportunities, **kw):
                if same_employer and deferred:
                    assert not kw.get("exclude_company_function_keys"), "withheld work is not coverage"
                reached.extend(j["posting_id"] for j in opportunities)
                leads = [Lead(
                    posting_id=j["posting_id"], company={"name": j["company_name"]},
                    contact={"email": f"{j['posting_id']}@example.com"},
                    contact_key=j["posting_id"], disposition=Disposition.FINAL_PASS,
                    primary_reason=ReasonCode.OK) for j in opportunities]
                if same_employer:
                    from tests.test_fantastic_send_safe_only_writes import _fantastic_job
                    for lead in leads:
                        lead.contact["_airtable_row"] = _fantastic_job(
                            f"acme.com|{lead.contact_key}@acme.com|engineering")
                if deferred and leads:
                    leads[0].disposition = Disposition.NEEDS_CHECK
                    leads[0].related_posting_ids = [l.posting_id for l in leads[1:]]
                    leads = leads[:1]
                return EnrichmentReport(stages=[], leads=leads,
                    funnel={"qualification_input": len(opportunities)})

        class Delivery:
            def deliver(self, leads, **kw):
                keys = [l.contact_key for l in leads] if not deferred else []
                if delivery_failed:
                    return RealDeliveryReport(entered=len(keys), reviewable_submitted=len(keys),
                        failed=len(keys), failed_rows=[{"lead_key": key} for key in keys],
                        detail={"airtable": {"failed_lead_keys": keys}, "withheld_before_submit": 0})
                return RealDeliveryReport(entered=len(keys), reviewable_submitted=len(keys),
                    created=len(keys), delivered_lead_keys=keys,
                    detail={"airtable": {"created_lead_keys": keys,
                            "created_approved_lead_keys": keys if approved else [],
                            "not_written_send_safe_reasons": {"test_missing_fact": 1}},
                            "withheld_before_submit": 0})

        def acquire(_):
            acquisitions.append(1)
            return LaneResult(lane="fantastic", status="complete", jobs=[])

        cfg = dict(TOPUP_CONFIG, PENDING_WORK_ENABLED=True,
                   PENDING_WORK_RESUME_MAX_PER_RUN=batch,
                   FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=watermark,
                   RUN_APPROVED_TARGET_ENABLED=True, RUN_APPROVED_TARGET=target,
                   RUN_APPROVED_CONTINUE_AFTER_TARGET=continue_after,
                   DAILY_APPROVED_TARGET_ENABLED=False, NET_NEW_SEND_SAFE_TARGET=0,
                   AIRTABLE_SUPPRESS_EXISTING_COMPANY_FUNCTION=True,
                   FANTASTIC_AUTO_APPROVE_SEND_SAFE=True)
        snapshot = {"company_function_keys": set(), "company_bare_keys": set(), "existing": {}}
        with mock.patch.multiple(config, **cfg), mock.patch.object(
                Orchestrator, "_existing_identity_snapshot", return_value=snapshot if same_employer else None):
            result = Orchestrator(ctx, state, _Budget()).run(OrchestratorPlan(
                lanes=["fantastic"], lane_runners={"fantastic": acquire},
                enrichment_engine=Engine(), delivery_manager=Delivery()), resume=False)
        return result, reached, acquisitions, root

    def test_watermark_keeps_one_acquisition_but_drains_terminal_batches(self):
        result, reached, acquisitions, root = self.exercise(7, 3, 1000)
        self.assertEqual(len(reached), 7)
        self.assertEqual(len(set(reached)), 7)
        self.assertLessEqual(len(acquisitions), 1)
        self.assertEqual(pending_work.load(root / pending_work.STORE)[0], [])

    def test_terminal_release_does_not_double_subtract_unattempted_tail(self):
        _, reached, _, _ = self.exercise(7, 3, 1000, watermark=False)
        self.assertEqual(len(reached), 7)

    def test_previous_run_cannot_satisfy_this_runs_thousand(self):
        result, reached, _, _ = self.exercise(1200, 200, 1000, prior=1000)
        self.assertEqual(len(reached), 1000)
        self.assertEqual(result["topup"]["final_stop_reason"], "run_approved_target_met")
        goal = result["acquisition"]["cumulative"]["run_approved_goal"]
        self.assertEqual(goal["approved_this_run"], 1000)

    def test_created_pending_rows_cannot_satisfy_the_approved_target(self):
        result, reached, _, _ = self.exercise(7, 3, 3, approved=False)
        self.assertEqual(len(reached), 7)
        self.assertNotEqual(result["topup"]["final_stop_reason"], "run_approved_target_met")

    def test_thousand_is_a_minimum_not_a_stop_when_more_work_is_available(self):
        result, reached, _, _ = self.exercise(1200, 200, 1000, continue_after=True)
        self.assertEqual(len(reached), 1200)
        goal = result["acquisition"]["cumulative"]["run_approved_goal"]
        self.assertEqual(goal["approved_this_run"], 1200)
        self.assertTrue(goal["met"])

    def test_deferred_related_postings_remain_resumable_after_the_run(self):
        import json
        _, reached, _, root = self.exercise(2, 2, 1000, deferred=True)
        self.assertEqual(len(reached), 2)
        suppressed = json.loads((root / "seen_suppression" / "postings.json").read_text())["keys"]
        rows, _ = pending_work.load(root / pending_work.STORE, exclude_keys=set(suppressed))
        self.assertEqual({r["posting_id"] for r in rows}, {"owed-0", "owed-1"})

    def test_delivery_reasons_and_approved_keys_survive_slice_aggregation(self):
        result, _, _, _ = self.exercise(7, 3, 1000)
        detail = result["delivery"]["detail"]["airtable"]
        self.assertEqual(detail["not_written_send_safe_reasons"]["test_missing_fact"], 3)
        self.assertEqual(len(detail["created_approved_lead_keys"]), 7)

    def test_withheld_contact_does_not_suppress_the_company_in_later_batches(self):
        _, reached, _, _ = self.exercise(3, 1, 1000, deferred=True, same_employer=True)
        self.assertEqual(len(reached), 3)

    def test_failed_airtable_delivery_keeps_final_pass_postings_recoverable(self):
        import json
        result, reached, _, root = self.exercise(3, 1, 1000, delivery_failed=True)
        self.assertEqual(len(reached), 3)
        self.assertEqual(result["delivery"]["failed"], 3)
        suppressed = json.loads((root / "seen_suppression" / "postings.json").read_text())["keys"]
        self.assertEqual(suppressed, [])
        rows, _ = pending_work.load(root / pending_work.STORE)
        self.assertEqual({r["posting_id"] for r in rows}, {"owed-0", "owed-1", "owed-2"})


class ActualAirtableStatus(unittest.TestCase):
    def test_failed_existing_row_repair_is_not_a_delivery_receipt(self):
        import airtable_client as ac
        from orchestrator.adapters_real import RealDelivery
        from tests.test_orchestrator_airtable_review import _lead
        rows = [_lead("repair-failed", Disposition.FINAL_PASS),
                _lead("created", Disposition.FINAL_PASS)]
        with mock.patch.object(ac, "push_leads", return_value={
                "created": 1, "failed": 1,
                "persisted_lead_keys": ["repair-failed", "created"],
                "failed_lead_keys": ["repair-failed"]}):
            result = RealDelivery(enable_airtable_write=True, auto_approve=True,
                                  enable_instantly=False).deliver(rows)
        self.assertEqual(result.delivered_lead_keys, ["created"])

    def test_only_returned_approved_records_count_and_withholding_is_named(self):
        import airtable_client as ac
        from tests.test_fantastic_send_safe_only_writes import _fantastic_job
        jobs = [_fantastic_job(f"c{i}.com|jane@c{i}.com|engineering") for i in range(3)]
        jobs[2]["outbound_company_confidence"] = "low"

        def request(method, url, **kw):
            rows = kw["json_body"]["records"]
            self.assertEqual(len(rows), 2)
            rows[0]["fields"]["Status"] = config.AIRTABLE_STATUS_APPROVED
            rows[1]["fields"]["Status"] = config.AIRTABLE_STATUS_PENDING
            return mock.Mock(json=lambda: {"records": rows})

        with mock.patch.object(ac, "validate_preflight"), mock.patch.object(
                ac, "_get_existing_leads", return_value={}), mock.patch.object(
                ac, "request_with_retry", side_effect=request), mock.patch.multiple(
                config, AIRTABLE_RATE_LIMIT_DELAY=0, AIRTABLE_WRITE_SEND_SAFE_ONLY=True,
                FANTASTIC_AUTO_APPROVE_SEND_SAFE=True):
            result = ac.push_leads(jobs)
        self.assertEqual(result["created_approved_lead_keys"], [jobs[0]["lead_key"]])
        self.assertEqual(result["created_approval_status_unknown"], 0)
        self.assertEqual(sum(result["not_written_send_safe_reasons"].values()), 1)
        self.assertEqual(result["not_written_not_send_safe"], 1)

    def test_missing_returned_status_is_unknown_not_a_measured_pending_row(self):
        import airtable_client as ac
        from tests.test_fantastic_send_safe_only_writes import _fantastic_job
        jobs = [_fantastic_job("acme.com|jane@acme.com|engineering")]
        with mock.patch.object(ac, "validate_preflight"), mock.patch.object(
                ac, "_get_existing_leads", return_value={}), mock.patch.object(
                ac, "request_with_retry", return_value=mock.Mock(json=lambda: {
                    "records": [{"id": "rec-one"}]})), mock.patch.object(
                config, "AIRTABLE_RATE_LIMIT_DELAY", 0):
            result = ac.push_leads(jobs)
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["created_approved_lead_keys"], [])
        self.assertEqual(result["created_approval_status_unknown"], 1)


class PaidRetriesAreBounded(unittest.TestCase):
    def test_real_enrichment_batches_share_the_run_match_cap(self):
        import hiring_manager as hm
        from types import SimpleNamespace
        from orchestrator.adapters_real import RealEnrichmentStage
        from tests.test_orchestrator_runtime_budget import _job, _fake_process_company
        permitted = []

        def process(rows):
            allowed = hm._paid_match_allowed(False)
            permitted.append(allowed)
            if allowed:
                hm._record_paid_match(False)
            return _fake_process_company(rows)

        def qualify(path, **kw):
            return SimpleNamespace(output_path=path)

        with tempfile.TemporaryDirectory() as root:
            stage = RealEnrichmentStage(target_final_pass=1000, workdir=root)
            with mock.patch.multiple(config, APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN=1,
                    COMPANY_OPPORTUNITY_COLLAPSE_ENABLED=False,
                    ORCHESTRATOR_ENRICHMENT_MAX_RUNTIME_SECONDS=0), mock.patch(
                    "qualification_pipeline.run_precontact_qualification", side_effect=qualify), mock.patch.object(
                    hm, "validate_preflight"), mock.patch.object(hm, "process_company", side_effect=process), mock.patch.object(
                    stage, "_to_report", return_value=EnrichmentReport()):
                try:
                    stage.run([_job("one", "Acme")])
                    stage.run([_job("two", "Beta")])
                    self.assertEqual(permitted, [True, False])
                    self.assertEqual(hm.paid_match_budget_state()["used"], 1)
                finally:
                    hm.reset_paid_match_budget()

    def test_org_validation_fallback_reserves_a_second_request(self):
        import requests
        import apollo_client as ac
        from tests.test_apollo_recovery_budget import _cfg
        from orchestrator import apollo_budget
        invalid = requests.Response()
        invalid.status_code = 422
        invalid._content = b'{"error":"invalid organization name"}'
        with _cfg(APOLLO_RECOVERY_BUDGET_CALLS=1), mock.patch.object(
                requests, "request", return_value=invalid) as request:
            with self.assertRaises(ac.ApolloBudgetExhaustedError):
                ac.enrich_organization(domain="example.com", name="Example")
            self.assertEqual(request.call_count, 1)
            self.assertEqual(apollo_budget.summary()["consumed"], 1)

    def test_retry_cannot_escape_the_call_grant(self):
        import requests
        import apollo_client as ac
        import http_utils
        from tests.test_apollo_recovery_budget import _cfg
        from orchestrator import apollo_budget
        retry = requests.Response()
        retry.status_code = 503
        retry._content = b'{}'
        with _cfg(APOLLO_RECOVERY_BUDGET_CALLS=1), mock.patch.object(
                requests, "request", return_value=retry) as request, mock.patch.object(
                http_utils.time, "sleep"):
            with self.assertRaises(ac.ApolloBudgetExhaustedError):
                ac.match_person({"id": "person-one"})
            self.assertEqual(request.call_count, 1)
            self.assertEqual(apollo_budget.summary()["consumed"], 1)

    def test_missing_person_id_spends_nothing(self):
        import apollo_client as ac
        from tests.test_apollo_recovery_budget import _cfg
        from orchestrator import apollo_budget
        with _cfg(), mock.patch.object(ac, "request_with_retry") as request:
            ac.match_person({"name": "No ID"})
            request.assert_not_called()
            self.assertEqual(apollo_budget.summary()["consumed"], 0)
