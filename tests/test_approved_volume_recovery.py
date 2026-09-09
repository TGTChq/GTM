"""Volume regressions: real processing, simulated providers and selected gate outcomes."""
from contextlib import ExitStack
from datetime import timedelta
from unittest.mock import patch

import pytest

import config
import domain_utils
import fantastic_jobs_adapter as fja
import hiring_manager as hm
from apollo_client import OrgEnrichment, PersonMatch
from decision_types import GateDecision, GateState
from tests.test_continuation_multirun_replay import BASE, CFG, LABEL, MovingFloorFeed, _Result, _run


@pytest.mark.parametrize("suffix", ["gov.in", "edu.ph", "gov.za", "org.kg"])
def test_public_suffix_never_collapses_distinct_employers(suffix):
    assert domain_utils.normalize_company_domain(f"careers.alpha.{suffix}") == f"alpha.{suffix}"
    assert domain_utils.normalize_company_domain(f"careers.beta.{suffix}") == f"beta.{suffix}"
    assert domain_utils.normalize_company_domain(suffix) == ""


@pytest.mark.parametrize("host,expected", [
    ("jobs.acme.co.uk", "acme.co.uk"),
    ("www.acme.com", "acme.com"),
    ("acme.test", "acme.test"),
    ("tenant.myworkdayjobs.com", "myworkdayjobs.com"),
    ("www.city.kawasaki.jp", "city.kawasaki.jp"),
    ("", ""), ("https://127.0.0.1", ""), ("not a domain", ""),
])
def test_domain_normalization_preserves_platform_and_validation_boundaries(host, expected):
    assert domain_utils.normalize_company_domain(host) == expected


@pytest.fixture
def sliced_engine(tmp_path):
    settings = dict(CFG, FANTASTIC_WINDOW_SLICING_ENABLED=True,
                    FANTASTIC_WATERMARK_STATE_PATH=str(tmp_path / "watermark.json"))
    with patch.multiple(config, **settings):
        feed = MovingFloorFeed(per_day=3)
        engine = fja.DateCreatedWatermarkEngine(
            result=_Result(), quota=fja._QuotaState(), http_get=feed,
            seen_ids=set(), metrics={"segments": {}, "watermark": {}},
            run_cap=12, now=BASE)
        engine.open()
        yield engine


def test_source_grant_is_shared_across_slices_not_renewed_per_slice(sliced_engine):
    engine = sliced_engine
    engine.run_stream("/active-jb-7d", {"source": "linkedin"}, LABEL, 4, ("linkedin",))
    assert engine.quota.jobs_consumed == 4
    assert len(engine.result.jobs) == 4
    assert not engine.source_already_drained(LABEL)
    assert engine.metrics["watermark"]["slices"][LABEL]["budget_exhausted"]
    assert engine.metrics["segments"][LABEL]["stop_reason"] == "cap_reached"


def test_source_grant_leaves_room_for_other_sources_and_resumes_exact_tail(sliced_engine):
    engine = sliced_engine
    engine.run_stream("/active-jb-7d", {"source": "linkedin"}, LABEL, 4, ("linkedin",))
    first_ids = {job["job_id"] for job in engine.result.jobs}
    assert engine.quota.jobs_consumed == 4
    # A second source has its own cursor. Its overlap is suppressed, but billed.
    engine.run_stream("/active-jb-7d", {"source": "linkedin"}, "other_source", 4, None)
    assert engine.quota.jobs_consumed == 8
    assert engine.metrics["segments"]["other_source"]["returned"] == 4
    engine.run_stream("/active-jb-7d", {"source": "linkedin"}, LABEL, 4, ("linkedin",))
    assert engine.quota.jobs_consumed == 12
    assert len(engine.result.jobs) == 8
    assert first_ids < {job["job_id"] for job in engine.result.jobs}
    assert engine.metrics["segments"][LABEL]["duplicates"] == 0


def test_a_stale_slice_is_not_proof_that_the_current_window_drained(sliced_engine):
    engine = sliced_engine
    engine.state["window_drained_sources"] = {LABEL: True}
    engine.state["window_slices"] = {LABEL: ["2020-01-01|2020-01-02"]}
    assert not engine.source_already_drained(LABEL)
    assert not engine.window_drained((LABEL,))


def test_cron_jitter_does_not_rebuy_every_completed_date_slice(tmp_path):
    state = str(tmp_path / "watermark.json")
    feed = MovingFloorFeed()
    seen = set()
    _run(state, feed, BASE, cap=180, seen=seen, FANTASTIC_WINDOW_SLICING_ENABLED=True)
    before = set(seen)
    _engine, metrics, _ = _run(state, feed, BASE + timedelta(days=1, seconds=37),
                               cap=180, seen=seen, FANTASTIC_WINDOW_SLICING_ENABLED=True)
    assert metrics["segments"][LABEL]["duplicates"] == 0
    assert len(seen - before) == metrics["segments"][LABEL]["returned"] == 180


def test_held_outcome_cannot_be_replayed_as_final_after_policy_correction(tmp_path):
    progress = hm._EnrichmentProgress.load(str(tmp_path))
    row = {"job_id": "held", "_final_state": "FINAL_PASS", "_outbound_company_hold": True}
    progress.record("acme.com", "workload", [row], {})
    reopened = hm._EnrichmentProgress.load(str(tmp_path))
    assert reopened.get("acme.com", "workload") is None
    # Evidence is retained, not deleted to force a retry.
    assert reopened.companies["acme.com"]["workloads"]["workload"]["leads"] == [row]


def test_safe_completed_checkpoint_is_still_reused(tmp_path):
    progress = hm._EnrichmentProgress.load(str(tmp_path))
    row = {"job_id": "safe", "_final_state": "FINAL_PASS", "_outbound_company_hold": False}
    progress.record("acme.com", "workload", [row], {"person_match_attempts": 1})
    assert hm._EnrichmentProgress.load(str(tmp_path)).get("acme.com", "workload") == (
        [row], {"person_match_attempts": 1})


def _job():
    return {
        "job_id": "finance-one", "job_title": "Staff Accountant",
        "canonical_job_title": "Staff Accountant", "employer_name": "Acme",
        "canonical_employer_name": "Acme", "employer_website": "https://acme.com",
        "_employer_domain_input": "acme.com", "_matched_role": "Staff Accountant",
        "_search_role": "Staff Accountant", "_job_gate_state": "PASS",
        "_role_gate_state": "PASS",
        "_job_gate_decision": GateDecision("job", GateState.PASS, "JOB_PASS").to_dict(),
        "_role_gate_decision": GateDecision("role", GateState.PASS, "ROLE_PASS").to_dict(),
    }


def _person(identity):
    return PersonMatch(
        True, person_id=identity, first_name="A", last_name=identity,
        title="Controller", organization_name="Acme", organization_domain="acme.com",
        email=f"{identity}@acme.com", email_status="verified", country="United States",
        linkedin_url=f"https://linkedin.com/in/{identity}",
        raw={"current_organization": {"name": "Acme", "domain": "acme.com"}})


def _contact_run(tmp_path, decisions, *, cap=3, alternate=True, fallback=False,
                 continuous=False, fallback_explicit=False):
    org = OrgEnrichment(found=True, name="Acme", domain="acme.com", employee_count=100,
                        organization_id="org-acme", industry="Software",
                        raw={"description": "Accounting software"})
    account = GateDecision("account", GateState.PASS, "ACCOUNT_PASS", metadata={
        "canonical_domain": "acme.com", "canonical_company_name": "Acme",
        "business_model": "commercial_product_or_service"})
    candidates = [{"id": f"p{i}", "title": "Controller"} for i in range(len(decisions))]
    with ExitStack() as stack:
        stack.enter_context(patch.multiple(config, REROUTE_STATE_FILE=str(tmp_path / "reroute.json"),
            APOLLO_RATE_LIMIT_DELAY=0, HUNTER_RATE_LIMIT_DELAY=0, HUNTER_API_KEY="",
            APOLLO_CACHE_ENABLED=False, CONTACT_MAX_REROUTE_ATTEMPTS_PER_BUCKET=cap,
            ALTERNATE_CONTACT_CASCADE_ENABLED=alternate,
            ALTERNATE_CONTACT_MAX_ENRICHMENTS_PER_RUN=10,
            APOLLO_CONTINUOUS_MODE=continuous,
            APOLLO_ORG_ID_ZERO_PEOPLE_FALLBACK_ENABLED=fallback,
            APOLLO_ORG_ID_FALLBACK_MAX_PAID_MATCHES_PER_RUN=0))
        stack.enter_context(patch.object(config, "APOLLO_ORG_ID_FALLBACK_BUDGET_CONFIGURED",
                                         fallback_explicit, create=True))
        stack.enter_context(patch.object(hm, "_cached_enrich_organization", return_value=org))
        stack.enter_context(patch.object(hm, "_cached_verified_person", return_value=None))
        stack.enter_context(patch.object(hm, "_remember_verified_person"))
        stack.enter_context(patch.object(hm.AccountGate, "evaluate", return_value=account))
        stack.enter_context(patch.object(hm.ContactGate, "evaluate", side_effect=decisions))
        stack.enter_context(patch.object(hm.apollo, "search_people_at_company",
                                         return_value=[] if fallback else candidates))
        stack.enter_context(patch.object(hm.apollo, "search_people_by_org_id", return_value=candidates))
        match = stack.enter_context(patch.object(hm.apollo, "match_person",
                                                side_effect=lambda p: _person(p["id"])))
        hm.reset_alternate_contact_budget()
        hm.reset_paid_match_budget()
        leads, stats = hm.process_company([_job()])
    return leads, stats, match.call_count


def test_contact_review_does_not_hide_a_later_fully_qualified_candidate(tmp_path):
    leads, stats, calls = _contact_run(tmp_path, [
        GateDecision("contact", GateState.NEEDS_CHECK, "NEEDS_CHECK_CURRENT_EMPLOYMENT"),
        GateDecision("contact", GateState.PASS, "CONTACT_PASS"),
    ])
    assert calls == 2
    assert leads[0]["hiring_manager_person_id"] == "p1"
    assert leads[0]["_final_state"] == "FINAL_PASS"
    assert stats["alternate_cascade_advanced"] == 1


def test_no_safe_alternate_keeps_review_and_never_relaxes_gates(tmp_path):
    leads, _stats, calls = _contact_run(tmp_path, [
        GateDecision("contact", GateState.NEEDS_CHECK, "NEEDS_CHECK_CURRENT_EMPLOYMENT"),
        GateDecision("contact", GateState.REJECT, "WRONG_EMPLOYER"),
    ])
    assert calls == 2
    assert leads[0]["hiring_manager_person_id"] == "p0"
    assert leads[0]["_final_state"] == "NEEDS_CHECK"


@pytest.mark.parametrize("cap,alternate", [(1, True), (3, False)])
def test_contact_recovery_respects_existing_attempt_and_cascade_controls(tmp_path, cap, alternate):
    leads, _stats, calls = _contact_run(tmp_path, [
        GateDecision("contact", GateState.NEEDS_CHECK, "NEEDS_CHECK_CURRENT_EMPLOYMENT"),
        GateDecision("contact", GateState.PASS, "CONTACT_PASS"),
    ], cap=cap, alternate=alternate)
    assert calls == 1
    assert leads[0]["_final_state"] == "NEEDS_CHECK"


def test_continuous_mode_enriches_trusted_org_id_recovery_without_a_second_grant(tmp_path):
    leads, stats, calls = _contact_run(tmp_path, [
        GateDecision("contact", GateState.PASS, "CONTACT_PASS")], fallback=True, continuous=True)
    assert calls == 1
    assert leads[0]["_final_state"] == "FINAL_PASS"
    assert stats["apollo_people_org_id_fallback_attempted"] == 1
    assert hm.paid_match_budget_state()["fallback_used"] == 1


@pytest.mark.parametrize("continuous,explicit", [(False, False), (True, True)])
def test_legacy_no_grant_and_explicit_zero_still_block_paid_recovery(tmp_path, continuous, explicit):
    leads, _stats, calls = _contact_run(tmp_path, [
        GateDecision("contact", GateState.PASS, "CONTACT_PASS")], fallback=True,
        continuous=continuous, fallback_explicit=explicit)
    assert calls == 0
    assert leads[0]["_final_state"] != "FINAL_PASS"


@pytest.mark.parametrize("setting", ["HM_DOMAIN_CORROBORATION_RECOVERY",
    "HM_SECOND_PASS_TITLE_BROADENING", "APOLLO_ORG_ID_ZERO_PEOPLE_FALLBACK_ENABLED",
    "ALTERNATE_CONTACT_CASCADE_ENABLED"])
def test_recovery_policy_change_invalidates_decisions_not_paid_reply_custody(setting):
    with patch.object(config, setting, False):
        old = hm._enrichment_workload_key([_job()])
    with patch.object(config, setting, True):
        assert hm._enrichment_workload_key([_job()]) != old
