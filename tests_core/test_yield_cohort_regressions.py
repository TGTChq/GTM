"""Portable regressions from cohort 20260916; no providers, keys or database."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tgtc_core.domain.acquisition_query import (
    frozen_page_query, policy_filters, feed_covers_window, covering_time_frame, recent_size_exclusions,
)
from tgtc_core.domain.employer_attribution import employer_attribution_conflict
from tgtc_core.domain.facts import extract_job_facts
from tgtc_core.domain.approval import build_approved_lead, ApprovalRefusal
from tgtc_core.policy.requirements import excluded_industry
from tgtc_core.services.delivery import DeliveryService
from tgtc_core.services.opportunity import OpportunityService
from tests_core.test_approval_gate import _inputs

VEC_TEXT = (
    "Invoice Processing: Receive, review, and process invoices in accordance with VEC’s policies.\n"
    "(Volunteer Energy Cooperative is an Equal Opportunity Employer/Drug Free Workplace)\n"
    "If interested in applying, please email your resume, in pdf format, to hrdepartment@vec.org "
    "or by fax, no later than Friday."
)
PUBLISHER = "Tennessee Valley Public Power Association, Inc."


def test_named_employer_and_application_domain_detect_publisher_mismatch():
    out = employer_attribution_conflict(VEC_TEXT, employer_name=PUBLISHER, employer_domain="tvppa.com")
    assert out and out["named_employer"] == "Volunteer Energy Cooperative"
    assert out["application_domain"] == "vec.org"


@pytest.mark.parametrize("text,name,domain", [
    (VEC_TEXT, "Volunteer Energy Cooperative", "vec.org"),
    ("Email your resume to hrdepartment@vec.org", PUBLISHER, "tvppa.com"),
    ("Volunteer Energy Cooperative is an Equal Opportunity Employer", PUBLISHER, "tvppa.com"),
    (VEC_TEXT.replace("vec.org", "gmail.com"), PUBLISHER, "tvppa.com"),
    (VEC_TEXT.replace("vec.org", "unrelated.org"), PUBLISHER, "tvppa.com"),
    (VEC_TEXT.replace("email your resume", "email your privacy request"), PUBLISHER, "tvppa.com"),
    (VEC_TEXT, "Volunteer Energy Cooperative", "volunteerenergy.org"),
])
def test_no_identity_change_from_a_lone_email_or_unrelated_text(text, name, domain):
    assert employer_attribution_conflict(text, employer_name=name, employer_domain=domain) is None


def test_wrong_employer_is_refused_even_with_an_otherwise_valid_buyer():
    inputs = _inputs()
    inputs["posting"]["description_text"] = VEC_TEXT
    inputs["employer"].update(canonical_name=PUBLISHER, domain="tvppa.com")
    result = build_approved_lead(**inputs)
    assert isinstance(result, ApprovalRefusal)
    assert result.reason == "employer_attribution_conflict"


def test_delivery_rechecks_legacy_approval_without_an_external_call(monkeypatch):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.side_effect = [
        {"state": "approved", "lead_json": {"posting_id": 28},
         "canonical_name": PUBLISHER, "domain": "tvppa.com", "linkedin_slug": "tvppa"},
        {"id": 28, "state": "classified", "description_text": VEC_TEXT},
    ]
    svc = DeliveryService(conn, airtable=None, instantly=None)
    revoked = []
    monkeypatch.setattr(svc, "_revoke", lambda aid, reason: revoked.append((aid, reason)))
    assert svc._precheck(SimpleNamespace(approval_id=9)) == ("blocked", "employer_attribution_conflict")
    assert revoked == [(9, "employer_attribution_conflict")]


def test_reddit_privacy_notice_does_not_turn_full_time_into_contract():
    facts = extract_job_facts(title="Staff Software Engineer", employment_type="FULL_TIME",
        description="We evaluate your application for employment or an independent contractor role, as applicable.")
    assert facts.get("employment_type").value == "full_time"
    assert "employment:contract" not in [e.reason for e in facts.exclusions]
    # Phase 2 audit task 1 (2026-09-19, Luis): "Staff Software Engineer" still records
    # leadership_or_principal as a role_level FACT (TITLE_LEADERSHIP still matches it),
    # but that fact is never, on its own, grounds for exclusion any more.
    assert facts.get("role_level").value == "leadership_or_principal"
    assert "seniority:leadership_or_principal" not in [e.reason for e in facts.exclusions]


@pytest.mark.parametrize("description", ["You will work as an independent contractor.",
                                               "This position is a contract role.", "Independent contractor"])
def test_actual_contracts_stay_excluded(description):
    assert "employment:contract" in [e.reason for e in extract_job_facts(title="Analyst", description=description).exclusions]


@pytest.mark.parametrize("labels,reason", [
    (["FULL_TIME", "CONTRACTOR"], "employment:contract"),
    (["FULL_TIME", "PART_TIME"], "employment:part_time"),
    (["FULL_TIME", "INTERNSHIP"], "employment:internship"),
])
def test_multivalued_provider_employment_labels_cannot_hide_an_incompatible_type(labels, reason):
    facts = extract_job_facts(
        title="Analyst", description="Perform remote reporting and analysis for the team.",
        ai_employment_type=labels,
    )
    assert reason in [e.reason for e in facts.exclusions]


def test_full_time_provider_label_still_passes_when_it_is_the_only_type():
    facts = extract_job_facts(
        title="Analyst", description="Perform remote reporting and analysis for the team.",
        ai_employment_type=["FULL_TIME"],
    )
    assert facts.get("employment_type").value == "full_time"
    assert not [e for e in facts.exclusions if e.reason.startswith("employment:")]


def test_volunteer_advisor_is_not_a_full_time_hr_opportunity():
    facts = extract_job_facts(title="Legal and Governance Advisor (Volunteer)",
        description="This is a volunteer advisory position. Commitment: 4–8 hours per month. HR Department.",
        ai_employment_type="FULL_TIME")
    assert "program:volunteer" in [e.reason for e in facts.exclusions]


def test_volunteer_employer_name_and_staff_volunteer_benefit_are_not_volunteer_jobs():
    facts = extract_job_facts(title="Junior Accountant", employer_name="Volunteer Energy Cooperative",
                             description=VEC_TEXT + " Employees receive two volunteer days off.")
    assert "program:volunteer" not in [e.reason for e in facts.exclusions]


def test_intern_is_a_program_not_a_senior_executive():
    reasons = [e.reason for e in extract_job_facts(title="Fire Engineering Intern", description="Engineering internship.").exclusions]
    assert "program:internship" in reasons
    assert "seniority:leadership_or_principal" not in reasons


def test_virtual_nurse_is_rejected_for_licensure_not_previous_bedside_experience():
    facts = extract_job_facts(title="Telephone Triage Nurse",
        description="3+ years of experience in bedside nursing required. Licensure required for these states (NV, CA, and AK).")
    reasons = [e.reason for e in facts.exclusions]
    assert "deliverability:professional_license" in reasons
    assert "deliverability:physical_facility" not in reasons


def test_mandatory_gaming_license_is_an_explicit_restriction():
    facts = extract_job_facts(title="Compliance Support", description="Must be able to acquire and maintain a gaming license.")
    assert "deliverability:professional_license" in [e.reason for e in facts.exclusions]


def test_audit_excerpt_contains_the_restriction_not_unrelated_paragraph_start():
    facts = extract_job_facts(title="Analyst", description="Company background " * 100 + "Active security clearance required.")
    assert "Active security clearance required" in facts.get("security_clearance").excerpt


def test_hr_front_desk_is_distinguished_from_incidental_office_lifting():
    office = "Frequent computer use, data entry, phone communication, walking throughout the facility, and light lifting up to 20 pounds."
    facts = extract_job_facts(title="HR Coordinator", description=office)
    assert "deliverability:physical_facility" not in [e.reason for e in facts.exclusions]
    facts = extract_job_facts(title="HR Coordinator", description=(
        "Provides front desk support by greeting visitors, managing calls, handling mail, and maintaining office supplies. " + office))
    assert "greeting visitors" in facts.get("physical_facility").excerpt


def test_both_apollo_selectors_contribute_candidates_without_paid_calls(monkeypatch):
    from tests_core.helpers import apollo_client
    from tests_core.seed import apollo_for, good_buyer
    domain = "acme.com"
    first = good_buyer(domain, "Acme", id="first", status="extrapolated")
    second = good_buyer(domain, "Acme", id="second")
    fake = apollo_for(domain, "Acme", people=[first])
    fake.people_by_org_id["org-acme"] = [first, second]
    svc = OpportunityService.__new__(OpportunityService)
    svc.apollo = apollo_client(fake)
    svc.search_pages = 2
    monkeypatch.setattr(svc, "_guard_provider", lambda **k: None)
    intents = []
    monkeypatch.setattr(svc, "_intent", lambda *a, **k: intents.append(k) or len(intents))
    monkeypatch.setattr(svc, "_finish", lambda *a, **k: None)
    monkeypatch.setattr(svc, "_handle_global", lambda *a, **k: None)
    emp = {"domain": domain, "apollo_org_id": "org-acme", "canonical_name": "Acme", "employee_count": 120}
    people, dropped, stats = svc._search_candidates({"id": 1}, emp, [first["title"]], set())
    assert [p["id"] for p in people] == ["first", "second"]
    assert stats["selectors_tried"] == 2 and dropped == []
    assert all(x["estimated_credits"] == 0 for x in intents)
    assert fake.served_paid == 0


def test_region_in_legal_notice_is_not_a_foreign_only_role():
    text = "Full-time accounting position. Reconcile accounts and prepare invoices. EMEA-only privacy notice."
    facts = extract_job_facts(description=text, title="Accountant", countries=["US"])
    assert facts.get("intent_market").value == "us_market"
    assert "market:foreign_only" not in [x.reason for x in facts.exclusions]
    restricted = extract_job_facts(description="This role is EMEA-only. Reconcile accounts.", title="Accountant")
    assert "market:foreign_only" in [x.reason for x in restricted.exclusions]


def test_search_summary_records_coverage_without_candidate_data():
    from tgtc_core.providers.apollo import ApolloResult, Outcome
    result = ApolloResult(Outcome.SERVED, data={"people": [{"id": "private", "email": "private@example.com"}],
                         "total_entries": 201, "pagination": {"page": 1, "total_pages": 9}})
    summary = result.summary()
    assert summary["returned_people"] == 1 and summary["total_entries"] == 201
    assert summary["total_pages"] == 9
    assert "private" not in str(summary)


def test_size_exclusion_expires_and_keeps_unknown_or_allowed_companies():
    now = datetime(2026, 9, 16, tzinfo=timezone.utc)
    def row(slug, count, days=0):
        return {"linkedin_slug": slug, "employee_count": count, "created_at": now - timedelta(days=days)}
    rows = [row("huge", 2000), row("small", 10), row("unknown", None), row("allowed", 100),
            row("old", 2000, 7), row("unsafe,slug", 10000), row("future", 2000, -1)]
    assert recent_size_exclusions(rows, now=now, minimum=25, maximum=1000) == ["huge", "small"]


def test_old_unstarted_window_selects_backfill_but_resume_stays_frozen():
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    assert covering_time_frame("7d", now - timedelta(days=20), now - timedelta(days=19), now) == "6m"
    old = {"endpoint": "/v1/active-jb", "time_frame": "7d"}
    new = {"endpoint": "/v1/active-jb", "time_frame": "6m"}
    assert frozen_page_query(new, old, offset=50, limit=50)["time_frame"] == "7d"


def test_broader_discovery_recovers_strict_search_miss_without_relaxing_gates(monkeypatch):
    from tgtc_core.providers.apollo import ApolloClient
    from tgtc_core.providers.http import Response
    import json
    calls = []
    class Transport:
        def request(self, method, url, **kw):
            params = dict(kw["params"])
            calls.append(params)
            people = [] if params["include_similar_titles"] == "false" else [
                {"id": "valid", "title": "Marketing Manager", "organization": {"name": "Acme"}},
                {"id": "irrelevant", "title": "Marketing Intern", "organization": {"name": "Acme"}}]
            return Response(200, text=json.dumps({"people": people, "total_entries": len(people)}))
    svc = OpportunityService.__new__(OpportunityService)
    svc.apollo = ApolloClient(Transport(), base_url="https://api.apollo.io/api/v1", api_key="sim")
    svc.search_pages = 2
    monkeypatch.setattr(svc, "_guard_provider", lambda **kw: None)
    monkeypatch.setattr(svc, "_intent", lambda *a, **kw: 1)
    monkeypatch.setattr(svc, "_finish", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_handle_global", lambda *a, **kw: None)
    emp = {"domain": "acme.com", "canonical_name": "Acme", "employee_count": 120}
    people, dropped, stats = svc._search_candidates({"id": 1}, emp, ["Marketing Manager"], set())
    assert [p["id"] for p in people] == ["valid"]
    assert [p["id"] for p in dropped] == ["irrelevant"]
    assert stats["broadened_searches"] == 1 and len(calls) == 2


def test_filters_only_exclude_existing_policy_industries_and_agencies():
    filters = policy_filters()
    assert set(filters) == {"organization_agency", "exclude_organization_industry"}
    assert all(excluded_industry(x) for x in filters["exclude_organization_industry"].split(","))
    assert filters["organization_agency"] == "exclude"


def test_resume_preserves_old_unfiltered_query_without_changing_offset_meaning():
    old = {"endpoint": "/v1/active-jb", "time_frame": "7d", "location": '"United States"',
           "date_created_gte": "2026-09-16T03:00:00Z", "offset": 0, "limit": 50}
    proposed = {**old, **policy_filters(), "location": "Canada"}
    frozen = frozen_page_query(proposed, old, offset=50, limit=25)
    assert frozen == {**old, "offset": 50, "limit": 25}
    assert old["offset"] == 0
    assert frozen_page_query(proposed, None, offset=0, limit=50) == proposed


def test_cursor_without_query_history_stops_instead_of_skipping_jobs():
    with pytest.raises(ValueError, match="partition_query_history_missing"):
        frozen_page_query({"endpoint": "/v1/active-jb", "time_frame": "7d"}, None, offset=50, limit=50)


def test_historical_window_cannot_be_marked_complete_after_feed_expiration():
    now = datetime(2026, 9, 16, 9, 15, tzinfo=timezone.utc)
    assert feed_covers_window({"time_frame": "7d"}, now - timedelta(hours=4), now - timedelta(hours=3), now)
    assert not feed_covers_window({"time_frame": "7d"}, now - timedelta(days=8), now - timedelta(days=7), now)
    assert feed_covers_window({"time_frame": "1h"}, now.replace(hour=7, minute=0), now.replace(hour=8, minute=0), now)
    assert not feed_covers_window({"time_frame": "1h"}, now.replace(hour=6, minute=0), now.replace(hour=7, minute=0), now)


@pytest.mark.parametrize("facts,reason", [
    ({"industry": "Hospital & Health Care"}, "employer_excluded_industry:hospital & health care"),
    ({"employee_count": 5000}, "employer_too_large"),
    ({"employee_count": 12}, "employer_too_small"),
    ({"agency_flag": True}, "employer_is_agency"),
])
def test_known_company_rejection_never_enriches(monkeypatch, facts, reason):
    svc = OpportunityService.__new__(OpportunityService)
    monkeypatch.setattr(svc, "_load", lambda _: ({"state": "open"}, {"canonical_name": "Acme", **facts}, {"id": 1}, {}))
    monkeypatch.setattr(svc, "_close", lambda oid, why: why)
    monkeypatch.setattr(svc, "_ensure_employer_facts", lambda *a: pytest.fail("paid enrichment for known rejection"))
    assert svc.process(1) == reason
