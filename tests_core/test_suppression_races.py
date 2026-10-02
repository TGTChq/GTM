"""An unsubscribe or reply that lands while a lead waits for delivery blocks the send;
duplicate events apply once; existing history suppresses before any spend."""

from __future__ import annotations

from tgtc_core.services.suppression import apply_outcome_event, check, company_function_keys
from tgtc_core.testing.fakes import FakeAirtable, FakeInstantly
from tgtc_core.testing.scenario import CONTROL_ID_BY_CAMPAIGN_KEY
from tests_core.helpers import delivery_service, opportunity_service, sql1, sqlall
from tests_core.seed import apollo_for, seed_opportunity
from tests_core.seed import good_buyer

CS_CAMPAIGN = CONTROL_ID_BY_CAMPAIGN_KEY["customer_experience"]


def test_unsubscribe_between_approval_and_delivery_blocks_both_channels(conn, clock):
    pid, eid, oid = seed_opportunity(conn, clock)
    assert opportunity_service(conn, apollo_for("acme.com", "Acme"), clock).process(oid).outcome == "approved"
    r1 = apply_outcome_event(conn, provider="instantly", event_type="unsubscribe", dedupe_key="evt-1", email="good.buyer@acme.com", campaign_id=CS_CAMPAIGN)
    r2 = apply_outcome_event(conn, provider="instantly", event_type="unsubscribe", dedupe_key="evt-1", email="good.buyer@acme.com", campaign_id=CS_CAMPAIGN)
    assert (r1, r2) == ("applied", "duplicate")
    assert sql1(conn, "SELECT count(*) FROM suppressions WHERE kind = 'person_email'") == 1
    assert sql1(conn, "SELECT count(*) FROM outcome_events") == 1
    at, ins = FakeAirtable(), FakeInstantly(campaign_status={CS_CAMPAIGN: 1})
    svc = delivery_service(conn, at, ins, clock)
    a, i = svc.drain("airtable"), svc.drain("instantly")
    assert [x.outcome for x in a] == ["blocked"] and a[0].reason.startswith("suppressed_before_delivery:person_email")
    assert [x.outcome for x in i] == ["blocked"]
    assert len(at.records) == 0 and len(ins.leads) == 0
    assert sql1(conn, "SELECT count(*) FROM delivery_receipts WHERE receipt_kind IN ('created','existing','reconciled')") == 0


def test_an_opt_out_suppresses_future_approval_of_the_same_person(conn, clock):
    apply_outcome_event(conn, provider="instantly", event_type="opt_out", dedupe_key="r-1",
                        email="good.buyer@acme.com")
    pid, eid, oid = seed_opportunity(conn, clock)
    out = opportunity_service(conn, apollo_for("acme.com", "Acme"), clock).process(oid)
    assert out.outcome == "wait" and out.reason == "buyer_search_pending:no_verified_buyer_in_candidates"
    reasons = {a["reason"] for a in sqlall(conn, "SELECT reason FROM candidate_attempts")}
    assert "suppressed:person_email" in reasons
    assert sql1(conn, "SELECT count(*) FROM approvals") == 0


def test_an_out_of_office_leaves_the_person_approvable(conn, clock):
    """The reason the suppressing set changed: an auto-answer from somebody on holiday
    is not a relationship ending, and it used to end one."""
    apply_outcome_event(conn, provider="instantly", event_type="out_of_office", dedupe_key="ooo-1",
                        email="good.buyer@acme.com")
    assert sql1(conn, "SELECT count(*) FROM suppressions") == 0
    pid, eid, oid = seed_opportunity(conn, clock)
    out = opportunity_service(conn, apollo_for("acme.com", "Acme"), clock).process(oid)
    assert out.outcome == "approved"
    assert sql1(conn, "SELECT count(*) FROM approvals") == 1


def test_active_company_function_history_closes_before_any_spend(conn, clock):
    from tgtc_core.services.suppression import import_airtable_rows

    import_airtable_rows(conn, [{"id": "rec1", "fields": {"Lead Key": "acme.com|old@acme.com|customer_success", "Company": "Acme",
                                                          "Website": "https://acme.com", "Role Bucket": "customer_success",
                                                          "Status": "Enrolled", "Email": "old@acme.com"}}])
    pid, eid, oid = seed_opportunity(conn, clock)
    fake = apollo_for("acme.com", "Acme")
    out = opportunity_service(conn, fake, clock).process(oid)
    assert out.outcome == "closed" and out.reason.startswith("suppressed:company_function:") and out.reason.endswith("|bucket:customer_success")
    assert fake.requests == []                                     # nothing was asked of Apollo
    # a DIFFERENT function at the same company is not suppressed (account-level policy is off)
    pid2, _, oid2 = seed_opportunity(conn, clock, function_key="finance", job_id="fin-1")
    hits = check(conn, company_function=company_function_keys(domain="acme.com", name="Acme", slug="", function_key="finance"))
    assert hits == []


def test_three_contact_quota_counts_existing_airtable_history(conn, clock):
    from tgtc_core.services.suppression import import_airtable_rows

    row = {"id": "rec1", "fields": {
        "Lead Key": "acme.com|old@acme.com|customer_success", "Company": "Acme",
        "Website": "https://acme.com", "Role Bucket": "customer_success",
        "Status": "Approved", "Email": "old@acme.com",
    }}
    first = import_airtable_rows(conn, [row])
    second = import_airtable_rows(conn, [row])
    assert first.inserted == 1 and second.already_present == 1
    _, _, oid = seed_opportunity(conn, clock)
    # Phase 2 audit task 9 (2026-09-19): "three people in the same role are NOT
    # diversification" -- role-diverse titles (functional owner + executive
    # leader; customer_success has no third, TA/People-leader persona), so
    # both remaining quota slots are actually reachable rather than one being
    # skipped as a same-persona repeat.
    people = [good_buyer("acme.com", "Acme", id=f"new-{i}", email=f"new{i}@acme.com") for i in range(3)]
    people[0]["title"] = "Customer Success Director"
    out = opportunity_service(
        conn, apollo_for("acme.com", "Acme", people=people), clock,
        max_contacts_per_opportunity=3,
    ).process(oid)
    assert out.outcome == "approved"
    assert out.details["approvals_created"] == 2
    assert sql1(conn, "SELECT count(*) FROM approvals WHERE opportunity_id = %s", (oid,)) == 2


def test_error_and_rejected_rows_do_not_suppress(conn):
    from tgtc_core.services.suppression import import_airtable_rows

    counts = import_airtable_rows(conn, [
        {"id": "r1", "fields": {"Company": "Acme", "Website": "https://acme.com", "Role Bucket": "finance", "Status": "Error", "Email": "e@acme.com"}},
        {"id": "r2", "fields": {"Company": "Acme", "Website": "https://acme.com", "Role Bucket": "finance", "Status": "Rejected"}},
    ])
    assert counts.by_kind.get("company_function", 0) == 0
    assert counts.by_kind.get("person_email") == 1                # a contacted email is still a contacted email
