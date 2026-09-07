"""Executed regressions for avoidable loss, with every provider boundary offline."""
import copy
import json
from contextlib import contextmanager
from unittest import mock

import pytest

import config
import fantastic_jobs_adapter as fja
import hiring_manager as hm
from apollo_client import OrgEnrichment, PersonMatch
from decision_types import GateDecision, GateState
from reroute_state import RerouteRegistry


@contextmanager
def strict_company(tmp_path, *, cache=False):
    job = {"job_id": "fresh-job", "job_title": "Staff Accountant",
           "employer_name": "Acme", "canonical_employer_name": "Acme",
           "employer_website": "https://acme.com", "_matched_role": "Staff Accountant",
           "_job_gate_state": "PASS", "_role_gate_state": "PASS",
           "_job_gate_decision": GateDecision("job", GateState.PASS, "JOB_PASS").to_dict(),
           "_role_gate_decision": GateDecision("role", GateState.PASS, "ROLE_PASS").to_dict()}
    candidate = {"id": "person-one", "title": "Controller",
                 "organization": {"name": "Acme", "domain": "acme.com"}}
    person = PersonMatch(True, person_id="person-one", first_name="Jane", last_name="Doe",
        title="Controller", organization_name="Acme", organization_domain="acme.com",
        email="jane@acme.com", email_status="verified", country="United States",
        linkedin_url="https://linkedin.com/in/example",
        raw={"current_organization": {"name": "Acme", "domain": "acme.com"}})
    account = GateDecision("account", GateState.PASS, "ACCOUNT_PASS", metadata={
        "canonical_domain": "acme.com", "canonical_company_name": "Acme",
        "business_model": "commercial_product_or_service"})
    with mock.patch.multiple(config,
            APOLLO_CACHE_ENABLED=cache, APOLLO_CACHE_PATH=str(tmp_path / "cache.json"),
            REROUTE_STATE_FILE=str(tmp_path / "reroute.json"), APOLLO_RATE_LIMIT_DELAY=0,
            VERIFY_WITH_HUNTER=False, HM_SECOND_PASS_TITLE_BROADENING=False,
            APOLLO_ORG_ID_ZERO_PEOPLE_FALLBACK_ENABLED=False,
            APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN=1), mock.patch.object(
            hm.apollo, "enrich_organization", return_value=OrgEnrichment(
                True, organization_id="org-acme", name="Acme", domain="acme.com",
                employee_count=100)), mock.patch.object(
            hm.AccountGate, "evaluate", return_value=account), mock.patch.object(
            hm.apollo, "search_people_at_company", return_value=[candidate]) as search, mock.patch.object(
            hm.apollo, "match_person", return_value=person) as paid:
        hm.reset_apollo_cache()
        hm.reset_paid_match_budget()
        try:
            yield job, candidate, search, paid
        finally:
            hm.reset_apollo_cache()
            hm.reset_paid_match_budget()


def test_unfunded_candidate_remains_eligible_in_a_later_funded_run(tmp_path):
    with strict_company(tmp_path) as (job, candidate, search, paid):
        hm._record_paid_match(False)  # this run's authorized match is already spent
        leads, stats = hm.process_company([copy.deepcopy(job)])
        paid.assert_not_called()
        assert stats["paid_match_budget_deferred"] == 1
        assert leads[0]["_row2_diagnostic"]["person_match_attempts"] == 0
        assert RerouteRegistry().attempted_ids("acme.com|finance") == set()
        hm.reset_paid_match_budget()  # a separate authorized run, same candidate
        recovered, _ = hm.process_company([copy.deepcopy(job)])
        assert paid.call_count == 1
        assert recovered[0]["hiring_manager_email"] == "jane@acme.com"


def test_domain_only_negative_cannot_block_newly_available_org_id_search(tmp_path):
    with strict_company(tmp_path, cache=True) as (job, candidate, search, paid):
        search.return_value = []
        hm.process_company([copy.deepcopy(job)])
        paid.assert_not_called()
        with mock.patch.object(config, "APOLLO_ORG_ID_ZERO_PEOPLE_FALLBACK_ENABLED", True), mock.patch.object(
                hm.apollo, "search_people_by_org_id", return_value=[candidate]) as fallback:
            _, stats = hm.process_company([copy.deepcopy(job)])
            fallback.assert_called_once_with("org-acme", mock.ANY, expected_domain="acme.com")
            assert stats["apollo_people_org_id_people_found"] == 1


def test_failed_org_id_search_is_not_a_confirmed_empty_result():
    import apollo_client as ac
    with mock.patch.object(ac, "request_with_retry", side_effect=TimeoutError("offline failure")):
        with pytest.raises(TimeoutError):
            ac.search_people_by_org_id("org-acme", ["Controller"], expected_domain="acme.com")


def _fetch(pages, *, stop_before_date=None):
    quota = fja._QuotaState()
    metrics = {"segments": {}}
    with mock.patch.object(fja, "_request", side_effect=[(rows, {}) for rows in pages]), mock.patch.multiple(
            config, FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=0,
            FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING=0):
        jobs = fja._fetch_segment("/offline", {}, "linkedin", 300, quota, mock.Mock(), set(), metrics,
                                  durable_cursor=True, stop_before_date=stop_before_date)
    return jobs, quota, metrics["segments"]["linkedin"]


def test_repeated_paid_page_still_consumes_the_acquisition_budget():
    from tests.test_fantastic_topup_slice_budget import _recs
    page = _recs(100)
    jobs, quota, segment = _fetch([page, page])
    assert len(jobs) == 100
    assert segment["stop_reason"] == "repeated_page"
    assert segment["returned"] == 200
    assert quota.jobs_consumed == 200


def test_head_boundary_counts_the_entire_already_paid_response():
    from tests.test_fantastic_topup_slice_budget import _recs
    page = _recs(100)
    jobs, quota, segment = _fetch([page], stop_before_date=page[20]["date_posted"])
    assert len(jobs) < 100
    assert segment["stop_reason"] == "head_boundary"
    assert segment["returned"] == 100
    assert quota.jobs_consumed == 100


def test_duplicate_candidate_cannot_consume_multiple_reroute_slots(tmp_path):
    with strict_company(tmp_path) as (job, candidate, search, paid), mock.patch.multiple(
            config, CONTACT_MAX_REROUTE_ATTEMPTS_PER_BUCKET=2,
            APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN=2):
        wrong = dict(candidate, id="a-wrong")
        search.return_value = [wrong, dict(wrong), candidate]
        valid = paid.return_value
        invalid = copy.deepcopy(valid)
        invalid.person_id = "a-wrong"
        invalid.title = "Controller EMEA"
        invalid.country = "United Kingdom"
        paid.side_effect = lambda p: invalid if p["id"] == "a-wrong" else valid
        leads, stats = hm.process_company([copy.deepcopy(job)])
        assert [c.args[0]["id"] for c in paid.call_args_list] == ["a-wrong", "person-one"]
        assert leads[0]["hiring_manager_email"] == "jane@acme.com"


def test_old_fallback_marker_does_not_block_a_current_primary_candidate(tmp_path):
    with strict_company(tmp_path) as (job, candidate, search, paid):
        job["_apollo_org_id_recovered"] = True  # retained metadata from a previous run
        leads, _ = hm.process_company([copy.deepcopy(job)])
        paid.assert_called_once()
        assert leads[0]["hiring_manager_email"] == "jane@acme.com"


def test_fallback_error_cannot_negative_cache_the_company(tmp_path):
    with strict_company(tmp_path, cache=True) as (job, candidate, search, paid), mock.patch.object(
            config, "APOLLO_ORG_ID_ZERO_PEOPLE_FALLBACK_ENABLED", True), mock.patch.object(
            hm.apollo, "search_people_by_org_id", side_effect=[TimeoutError("offline"), [candidate]]) as fallback:
        search.return_value = []
        hm.process_company([copy.deepcopy(job)])
        _, stats = hm.process_company([copy.deepcopy(job)])
        assert fallback.call_count == 2
        assert stats["apollo_people_org_id_people_found"] == 1


def test_precontact_rejects_finish_custody_without_becoming_contact_leads(tmp_path):
    from orchestrator import pending_work
    from orchestrator.adapters_real import RealDelivery, RealEnrichmentStage
    from orchestrator.lanes import LaneResult
    from orchestrator.modes import ExecutionMode as EM, policy_for
    from orchestrator.pipeline import Orchestrator, OrchestratorPlan
    from orchestrator.runcontrol import RunContext
    from orchestrator.state import StateManager
    from qualification_pipeline import run_precontact_qualification
    from tests.test_pipeline_run_ledger import TOPUP_CONFIG, _Budget

    jobs = [{"job_id": f"rejected-{i}", "employer_name": "Acme", "job_title": "Staff Accountant"}
            for i in range(3)]
    pending_work.record(tmp_path / pending_work.STORE, "earlier", jobs)
    ctx = RunContext.create(EM.LIVE_ACQUISITION_AND_ENRICHMENT, {}, run_id="audit-rejects")
    state = StateManager(tmp_path, policy_for(ctx.mode), run_id=ctx.run_id)

    class RejectGate:
        def annotate(self, job, **kw):
            decision = GateDecision("job", GateState.REJECT, "REJECT_NON_US")
            return dict(job, _job_gate_state="REJECT", _job_gate_reason="REJECT_NON_US",
                        _job_gate_decision=decision.to_dict())

    def qualify(path, **kw):
        return run_precontact_qualification(path, job_gate=RejectGate(), **kw)

    with mock.patch.multiple(config, **dict(TOPUP_CONFIG,
            PENDING_WORK_ENABLED=True, PENDING_WORK_RESUME_MAX_PER_RUN=2,
            COMPANY_OPPORTUNITY_COLLAPSE_ENABLED=False, RUN_APPROVED_TARGET_ENABLED=True)), mock.patch(
            "qualification_pipeline.run_precontact_qualification", side_effect=qualify), mock.patch.object(
            hm, "validate_preflight"), mock.patch.object(hm, "process_company") as paid_company:
        result = Orchestrator(ctx, state, _Budget()).run(OrchestratorPlan(
            lanes=["fantastic"], lane_runners={"fantastic": lambda _: LaneResult(
                lane="fantastic", status="complete", jobs=[])},
            enrichment_engine=RealEnrichmentStage(workdir=str(tmp_path / "enrichment")),
            delivery_manager=RealDelivery(enable_airtable_write=False, auto_approve=False,
                                           enable_instantly=False)), resume=False)
    paid_company.assert_not_called()
    assert result["enrichment"]["leads"] == 0
    assert result["enrichment"]["funnel"]["qual_rejected"] == 3
    assert pending_work.load(tmp_path / pending_work.STORE)[0] == []


@pytest.mark.parametrize("store_name", ["postings.json", "delivered_leads.json"])
def test_corrupt_suppression_cannot_be_overwritten_as_an_empty_history(tmp_path, store_name):
    from orchestrator.modes import ExecutionMode as EM, policy_for
    from orchestrator.state import StateManager
    from orchestrator.suppression import SuppressionStore
    state = StateManager(tmp_path, policy_for(EM.LIVE_ACQUISITION_AND_ENRICHMENT), run_id="audit")
    path = state.store_path("seen_suppression") / store_name
    path.write_text("{truncated", encoding="utf-8")
    store = SuppressionStore(state)
    with pytest.raises(RuntimeError):
        if store_name == "postings.json":
            store.commit_postings(["fresh-job"])
        else:
            store.commit_delivered(["new-contact"])
    assert path.read_text() == "{truncated"


def test_corrupt_custody_is_never_replaced_by_only_the_new_batch(tmp_path):
    from orchestrator import pending_work
    path = tmp_path / "run.json"
    path.write_text("{truncated")
    result = pending_work.record(tmp_path, "run", [{"job_id": "new"}])
    assert result["ok"] is False
    assert path.read_text() == "{truncated"
    with pytest.raises(RuntimeError):
        pending_work.load(tmp_path)


def test_corrupt_approval_history_cannot_count_an_old_contact_as_new(tmp_path):
    from orchestrator import daily_target
    path = tmp_path / "daily_approved.json"
    path.write_text("{truncated")
    with pytest.raises(RuntimeError):
        daily_target.record_approved(tmp_path, ["old-contact"], run_id="new-run")
    assert path.read_text() == "{truncated"


def test_configured_free_lane_runs_once_with_zero_fantastic_grant(tmp_path):
    from orchestrator.lanes import LaneResult
    from orchestrator.modes import ExecutionMode as EM, policy_for
    from orchestrator.pipeline import Orchestrator, OrchestratorPlan, _lane_billing_totals
    from orchestrator.runcontrol import RunContext
    from orchestrator.state import StateManager
    from tests.test_pipeline_run_ledger import TOPUP_CONFIG, _Budget, _Engine, _Delivery
    ctx = RunContext.create(EM.LIVE_ACQUISITION_AND_ENRICHMENT, {}, run_id="free-lane")
    state = StateManager(tmp_path, policy_for(ctx.mode), run_id=ctx.run_id)
    free = LaneResult(lane="ats", status="complete", jobs=[{"job_id": "free-new", "employer_name": "Acme",
             "employer_website": "https://acme.com", "job_title": "Staff Accountant"}])
    ats, fantastic = mock.Mock(return_value=free), mock.Mock()
    engine = _Engine()
    with mock.patch.multiple(config, **dict(TOPUP_CONFIG,
            FANTASTIC_JOBS_MAX_JOBS_PER_RUN=0, PENDING_WORK_ENABLED=True,
            RUN_APPROVED_TARGET_ENABLED=True)):
        result = Orchestrator(ctx, state, _Budget()).run(OrchestratorPlan(
            lanes=["fantastic", "ats"], lane_runners={"fantastic": fantastic, "ats": ats},
            enrichment_engine=engine, delivery_manager=_Delivery()))
    fantastic.assert_not_called()
    ats.assert_called_once()
    assert engine.calls == 1
    assert result["acquisition"]["cumulative"]["jobs_returned_billed"] == 0
    assert _lane_billing_totals({"ats": free}, 1) == (1, 0)
    assert result["capacity"]["opportunities"] == 1


@pytest.mark.parametrize("sliced", [False, True])
def test_paid_page_survives_interruption_before_the_next_request(tmp_path, sliced):
    from orchestrator import pending_work
    from tests.test_fantastic_watermark_ats import _recs, NOW
    from types import SimpleNamespace
    wm = tmp_path / "watermark.json"
    from datetime import timedelta
    page = _recs(100)
    # Match the first bootstrap slice, rather than returning out-of-window jobs.
    for row in page:
        row["date_created"] = (NOW - timedelta(days=7) + timedelta(minutes=5)).isoformat()
    cfg = dict(FANTASTIC_WATERMARK_STATE_PATH=str(wm),
               FANTASTIC_WINDOW_SLICING_ENABLED=sliced,
               FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=0,
               FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING=0)
    def engine():
        return fja.DateCreatedWatermarkEngine(result=SimpleNamespace(jobs=[]),
            quota=fja._QuotaState(), http_get=mock.Mock(), seen_ids=set(),
            metrics={"segments": {}}, run_cap=300, now=NOW)
    def hold(rows):
        return pending_work.record(tmp_path / "held", "interrupted", rows)["ok"]
    with mock.patch.multiple(config, **cfg), mock.patch.object(fja, "_CUSTODY_HOOK", hold), mock.patch.object(
            fja, "_request", side_effect=[(page, {}), KeyboardInterrupt("before next response")]):
        first = engine()
        first.open()
        with pytest.raises(KeyboardInterrupt):
            first.run_stream("/offline", {}, "linkedin", 300, None)
        held, _ = pending_work.load(tmp_path / "held")
        assert len(held) == 100
        # Restart against the exact same selected window. The purchased page
        # belongs to custody; the provider must resume beyond that page.
        with mock.patch.object(fja, "_request", return_value=([], {})) as request:
            resumed = engine()
            resumed.open()
            resumed.run_stream("/offline", {}, "linkedin", 100, None)
        assert request.call_args_list[0].args[1]["offset"] == 100


def test_governor_history_compaction_and_partial_commit_never_refund_spend(tmp_path):
    from orchestrator.fantastic_governor import GovernorLedger
    from datetime import datetime, timezone
    ledger = GovernorLedger(str(tmp_path / "spend.json"), keep_runs=2)
    now = datetime(2026, 9, 7, tzinfo=timezone.utc)
    for run in ["old", "current", "latest"]:
        ledger.record_run(run, 100, 200, now)
    ledger.record_run("latest", 150, 200, now)
    assert ledger.used == 350  # the old compacted run still cost 100
    ledger.record_run("latest", 0, 200, now)  # incomplete outer-controller total
    assert ledger.used == 350
    assert ledger.spent_on_day("2026-09-07") == 350


def test_corrupt_governor_cannot_create_a_fresh_spending_allowance(tmp_path):
    from orchestrator.fantastic_governor import GovernorLedger
    path = tmp_path / "spend.json"
    path.write_text("{truncated")
    with pytest.raises(RuntimeError):
        GovernorLedger(str(path))
    assert path.read_text() == "{truncated"


@pytest.mark.parametrize("failed_step", ["adopt_from_artifacts", "pending_run_ids"])
def test_retention_preserves_originals_if_custody_protection_fails(tmp_path, failed_step):
    from orchestrator import pending_work
    from orchestrator.pipeline import Orchestrator, OrchestratorPlan
    from orchestrator.modes import ExecutionMode as EM, policy_for
    from orchestrator.runcontrol import RunContext
    from orchestrator.state import StateManager
    from tests.test_pipeline_run_ledger import _Budget
    ctx = RunContext.create(EM.LIVE_ACQUISITION_AND_ENRICHMENT, {}, run_id="retain-evidence")
    state = StateManager(tmp_path, policy_for(ctx.mode), run_id=ctx.run_id)
    runner = Orchestrator(ctx, state, _Budget())
    with mock.patch.object(config, "PENDING_WORK_ENABLED", True), mock.patch.object(
            runner, "_run_body", return_value={}), mock.patch.object(
            pending_work, failed_step, side_effect=RuntimeError("offline unreadable store")), mock.patch.object(
            state, "prune") as prune:
        runner.run(OrchestratorPlan([], {}, None, None), retention_keep=0)
        prune.assert_not_called()


def test_historical_page_is_held_before_a_cursor_can_advance(tmp_path):
    from tests.test_historical_recovery import _rows
    path = tmp_path / "historical.json"
    with mock.patch.multiple(config, FANTASTIC_HISTORICAL_RECOVERY_ENABLED=True,
            FANTASTIC_HISTORICAL_RECOVERY_STATE_PATH=str(path)), mock.patch.object(
            fja, "_request", return_value=(_rows(0, 100), {})) as request, mock.patch.object(
            fja, "_CUSTODY_HOOK", return_value=False):
        with pytest.raises(RuntimeError):
            fja.run_historical_recovery(mock.Mock(), fja._QuotaState(), set(),
                                       {"segments": {}}, max_rows=200)
        request.assert_called_once()
        assert not path.exists()


def test_historical_initial_request_uses_the_same_id_order_as_following_pages(tmp_path):
    from tests.test_historical_recovery import _rows
    records = _rows(0, 200)
    for n, row in enumerate(records, 1):
        row["id"] = n
    def request(_endpoint, params, *_args):
        # Official contract: absent cursor => date_posted DESC, cursor => id ASC.
        # Recent posts here have the highest ids; switching after page one skips old jobs.
        if "cursor" not in params:
            return (list(reversed(records))[:params["limit"]], {})
        after = int(params["cursor"])
        return ([r for r in records if r["id"] > after][:params["limit"]], {})
    with mock.patch.multiple(config, FANTASTIC_HISTORICAL_RECOVERY_ENABLED=True,
            FANTASTIC_HISTORICAL_RECOVERY_STATE_PATH=str(tmp_path / "history.json")), mock.patch.object(
            fja, "_request", side_effect=request):
        jobs = fja.run_historical_recovery(mock.Mock(), fja._QuotaState(), set(),
                                          {"segments": {}}, max_rows=200)
    assert len({j["job_id"] for j in jobs}) == 200


def test_paid_page_spend_survives_a_later_request_interruption(tmp_path):
    from tests.test_fantastic_topup_slice_budget import _recs
    from orchestrator.fantastic_governor import GovernorLedger
    from datetime import datetime, timezone
    path = tmp_path / "spend.json"
    ledger = GovernorLedger(str(path))
    def persist(count):
        ledger.record_run("interrupted", count, 300, datetime.now(timezone.utc))
        ledger.save()
    with mock.patch.object(fja, "_BILLING_HOOK", persist), mock.patch.object(
            fja, "_request", side_effect=[(_recs(100), {}), KeyboardInterrupt()]), mock.patch.multiple(
            config, FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=0,
            FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING=0):
        with pytest.raises(KeyboardInterrupt):
            fja._fetch_segment("/offline", {}, "linkedin", 300, fja._QuotaState(),
                               mock.Mock(), set(), {"segments": {}}, durable_cursor=True)
    assert GovernorLedger(str(path)).used == 100


def test_corrupt_watermark_cannot_restart_paid_acquisition_from_zero(tmp_path):
    from types import SimpleNamespace
    path = tmp_path / "watermark.json"
    path.write_text("{truncated")
    with mock.patch.object(config, "FANTASTIC_WATERMARK_STATE_PATH", str(path)):
        with pytest.raises(RuntimeError):
            fja.DateCreatedWatermarkEngine(result=SimpleNamespace(jobs=[]),
                quota=fja._QuotaState(), http_get=mock.Mock(), seen_ids=set(),
                metrics={"segments": {}}, run_cap=100)
    assert path.read_text() == "{truncated"


def test_completed_function_survives_a_budget_stop_inside_the_same_company(tmp_path):
    from apollo_client import ApolloBudgetExhaustedError
    with strict_company(tmp_path) as (job, candidate, search, paid), mock.patch.multiple(
            config, STEP3_OUTPUT_DIR=str(tmp_path / "stage"),
            APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN=20,
            ORCHESTRATOR_ENRICHMENT_MAX_RUNTIME_SECONDS=0), mock.patch.object(
            hm, "validate_preflight"), mock.patch.object(
            hm, "get_bucket_name_for_job", side_effect=lambda j: j.get("test_bucket", "finance")):
        jobs = [dict(job, job_id="first", test_bucket="finance"),
                dict(job, job_id="second", test_bucket="operations")]
        source = tmp_path / "input.json"
        source.write_text(json.dumps({"jobs": jobs}))
        person = paid.return_value
        other = dict(candidate, id="person-two")
        search.side_effect = [[candidate], [other], [other]]
        paid.side_effect = [person, ApolloBudgetExhaustedError("grant spent")]
        first = hm.run_hiring_manager_identification(str(source))
        rows = json.loads(__import__("pathlib").Path(first.output_path).read_text())["jobs"]
        assert [r["job_id"] for r in rows] == ["first"]
        assert first.stop_reason == "apollo_circuit_open"
        # A new grant in the SAME resumed workload must only match the unfinished
        # function. No reuse of an incomplete company as a completed one.
        paid.reset_mock(side_effect=True)
        paid.return_value = person
        resumed = hm.run_hiring_manager_identification(str(source))
        rows = json.loads(__import__("pathlib").Path(resumed.output_path).read_text())["jobs"]
        assert {r["job_id"] for r in rows} == {"first", "second"}
        assert paid.call_count == 1
        assert hm.apollo.enrich_organization.call_count == 1


def test_paid_replies_survive_run_cleanup_and_changed_recovery_metadata(tmp_path):
    import shutil
    from pathlib import Path
    with strict_company(tmp_path) as (job, candidate, search, paid), mock.patch.multiple(
            config, STEP3_OUTPUT_DIR=str(tmp_path / "old-run"),
            ORCHESTRATOR_ENRICHMENT_MAX_RUNTIME_SECONDS=0), mock.patch.object(hm, "validate_preflight"):
        source = tmp_path / "input.json"
        source.write_text(json.dumps({"jobs": [job]}))
        evidence = str(tmp_path / "durable" / "paid_replies")
        hm.run_hiring_manager_identification(str(source), paid_evidence_dir=evidence)
        assert paid.call_count == 1
        shutil.rmtree(tmp_path / "old-run")
        recovered = dict(job, _resumed_from_pending=True)
        source.write_text(json.dumps({"jobs": [recovered]}))
        search.return_value = [dict(candidate, extra_provider_metadata="changed")]
        with mock.patch.object(config, "STEP3_OUTPUT_DIR", str(tmp_path / "new-run")):
            result = hm.run_hiring_manager_identification(str(source), paid_evidence_dir=evidence)
        rows = json.loads(Path(result.output_path).read_text())["jobs"]
        assert rows[0]["hiring_manager_email"] == "jane@acme.com"
        assert paid.call_count == 1
        assert hm.apollo.enrich_organization.call_count == 1


def test_incomplete_empty_search_never_becomes_a_negative_cache(tmp_path):
    from apollo_client import PeopleSearchResults
    with strict_company(tmp_path, cache=True) as (job, candidate, search, paid):
        search.return_value = PeopleSearchResults([], complete=False)
        hm.process_company([copy.deepcopy(job)])
        search.return_value = [candidate]
        result, stats = hm.process_company([copy.deepcopy(job)])
        assert search.call_count == 2
        assert result[0]["hiring_manager_email"] == "jane@acme.com"


def test_negative_cache_replay_is_not_reported_as_a_new_people_search(tmp_path):
    with strict_company(tmp_path, cache=True) as (job, candidate, search, paid):
        search.return_value = []
        hm.process_company([copy.deepcopy(job)])
        leads, stats = hm.process_company([copy.deepcopy(job)])
        assert search.call_count == 1
        assert stats.get("row2_companies_with_people_search_call", 0) == 0
        assert leads[0]["_row2_diagnostic"]["people_search_call"] is False


def test_partial_enrichment_stops_topup_and_remains_incomplete(tmp_path):
    from orchestrator.lanes import LaneResult
    from orchestrator.modes import ExecutionMode as EM, policy_for
    from orchestrator.pipeline import Orchestrator, OrchestratorPlan
    from orchestrator.runcontrol import RunContext
    from orchestrator.state import StateManager
    from tests.test_pipeline_run_ledger import TOPUP_CONFIG, _Budget, _Engine, _Delivery
    class Partial(_Engine):
        def run(self, jobs, **kwargs):
            result = super().run(jobs, **kwargs)
            result.enrichment_incomplete = True
            result.stop_reason = "enrichment_runtime_budget_reached"
            return result
    ctx = RunContext.create(EM.LIVE_ACQUISITION_AND_ENRICHMENT, {}, run_id="partial")
    state = StateManager(tmp_path, policy_for(ctx.mode), run_id=ctx.run_id)
    runner = mock.Mock(return_value=LaneResult(lane="fantastic", status="complete",
                                              jobs=[{"job_id": "first"}, {"job_id": "second"}]))
    engine = Partial()
    with mock.patch.multiple(config, **dict(TOPUP_CONFIG, RUN_APPROVED_TARGET_ENABLED=True)):
        result = Orchestrator(ctx, state, _Budget()).run(OrchestratorPlan(
            lanes=["fantastic"], lane_runners={"fantastic": runner},
            enrichment_engine=engine, delivery_manager=_Delivery()))
    assert engine.calls == 1
    assert ctx.status.value == "incomplete"
    assert result["enrichment"]["enrichment_incomplete"] is True
    assert result["enrichment"]["stop_reason"] == "enrichment_runtime_budget_reached"


def test_one_run_is_not_a_sustainable_capacity_forecast():
    from orchestrator.capacity import build_capacity_report
    from orchestrator.enrichment import EnrichmentReport
    report = build_capacity_report(raw_postings=1, opportunities=1,
        enrichment=EnrichmentReport(), delivered_final_pass=0,
        acquisition_requests=1, enrichment_calls=None, runtime_seconds=1,
        quota_consumed=1, inventory_remaining=9999).to_dict()
    assert report["projected_sustainable_final_pass_per_day"] is None
    assert report["enrichment_calls"] is None


def test_new_head_larger_than_one_grant_does_not_skip_the_unseen_middle(tmp_path):
    from tests.test_fantastic_continuation import _rec, _feed, _run, _FIXED_NOW
    path = str(tmp_path / "continuation.json")
    old = [_rec(100 + i, f"2026-08-18T01:00:{i:02d}") for i in range(20)]
    fresh = [_rec(200 + i, f"2026-08-18T02:00:{i:02d}") for i in range(20)]
    all_seen = set()
    initial, _ = _run(_feed(old), path, cap=8, now=_FIXED_NOW)
    all_seen.update(j["_fantastic_internal_id"] for j in initial.jobs)
    for _ in range(8):
        result, _ = _run(_feed(old + fresh), path, cap=8, now=_FIXED_NOW)
        all_seen.update(j["_fantastic_internal_id"] for j in result.jobs)
        assert result.metadata["jobs_quota_consumed"] <= 8
    assert {r["id"] for r in fresh} <= all_seen


def test_company_checkpoint_does_not_freeze_a_budget_deferred_outcome(tmp_path):
    from pathlib import Path
    with strict_company(tmp_path) as (job, candidate, search, paid), mock.patch.multiple(
            config, STEP3_OUTPUT_DIR=str(tmp_path / "stage"),
            ORCHESTRATOR_ENRICHMENT_MAX_RUNTIME_SECONDS=0), mock.patch.object(hm, "validate_preflight"):
        source = tmp_path / "input.json"
        source.write_text(json.dumps({"jobs": [job]}))
        hm._record_paid_match(False)
        hm.run_hiring_manager_identification(str(source), reset_run_budgets=False)
        paid.assert_not_called()
        hm.reset_paid_match_budget()
        resumed = hm.run_hiring_manager_identification(str(source))
        rows = json.loads(Path(resumed.output_path).read_text())["jobs"]
        paid.assert_called_once()
        assert rows[0]["hiring_manager_email"] == "jane@acme.com"


@pytest.mark.parametrize("org_id", [False, True])
def test_people_search_auth_denial_opens_the_global_circuit(org_id):
    import requests
    import apollo_client as ac
    response = requests.Response()
    response.status_code = 401
    response._content = b'{"error":"Invalid API key"}'
    error = requests.HTTPError("offline unauthorized", response=response)
    with mock.patch.object(ac, "request_with_retry", side_effect=error):
        with pytest.raises(ac.ApolloAuthorizationError):
            if org_id:
                ac.search_people_by_org_id("org-acme", ["Controller"])
            else:
                ac.search_people_at_company("acme.com", ["Controller"])


@pytest.mark.parametrize("org_id", [False, True])
def test_malformed_success_is_not_an_empty_people_search(org_id):
    import apollo_client as ac
    with mock.patch.object(ac, "request_with_retry"), mock.patch.object(
            ac, "safe_json", return_value={"unexpected_error": "bad payload"}):
        with pytest.raises(ValueError):
            if org_id:
                ac.search_people_by_org_id("org-acme", ["Controller"])
            else:
                ac.search_people_at_company("acme.com", ["Controller"])


def test_cohort_outcomes_use_company_function_not_primary_posting_ids():
    from types import SimpleNamespace
    from orchestrator.pipeline import _account_recovery_cohort
    from tests.test_recovery_cohort_attribution import _cohort, _lead, _delivery
    cohort = _cohort(["one", "two"])
    rows = []
    for pid in ("one", "two"):
        lead = _lead(pid, contact_key="acme.com|jane@acme.com|finance")
        lead.company = {"name": "Acme"}
        lead.contact["_airtable_row"] = {"job_id": pid, "employer_name": "Acme",
            "employer_website": "https://acme.com", "job_title": "Staff Accountant",
            "_matched_role": "Staff Accountant"}
        rows.append(lead)
    _account_recovery_cohort(cohort, rows, _delivery())
    assert len(cohort["attempted_opportunity_keys"]) == 1
    assert len(cohort["contact_opportunity_keys"]) == 1


def test_failed_lane_cannot_erase_another_lanes_acquired_work(tmp_path):
    from orchestrator.lanes import LaneResult
    from orchestrator.modes import ExecutionMode as EM, policy_for
    from orchestrator.pipeline import Orchestrator, OrchestratorPlan
    from orchestrator.runcontrol import RunContext
    from orchestrator.state import StateManager
    from tests.test_pipeline_run_ledger import TOPUP_CONFIG, _Budget, _Engine, _Delivery
    ctx = RunContext.create(EM.LIVE_ACQUISITION_AND_ENRICHMENT, {}, run_id="mixed-lanes")
    state = StateManager(tmp_path, policy_for(ctx.mode), run_id=ctx.run_id)
    engine = _Engine()
    good = LaneResult(lane="ats", status="complete", jobs=[{"job_id": "good"}])
    bad = LaneResult(lane="fantastic", status="failed", errors=["offline provider failure"])
    with mock.patch.multiple(config, **dict(TOPUP_CONFIG, RUN_APPROVED_TARGET_ENABLED=True)):
        result = Orchestrator(ctx, state, _Budget()).run(OrchestratorPlan(
            lanes=["fantastic", "ats"], lane_runners={"fantastic": lambda _: bad, "ats": lambda _: good},
            enrichment_engine=engine, delivery_manager=_Delivery()))
    assert engine.calls == 1
    assert ctx.status.value == "failed"
    assert result["acquisition"]["cumulative"]["net_new_jobs_captured"] == 1
