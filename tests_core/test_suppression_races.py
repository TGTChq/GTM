"""An unsubscribe or reply that lands while a lead waits for delivery blocks the send;
duplicate events apply once; existing history suppresses before any spend."""

from __future__ import annotations

from tgtc_core.services.suppression import apply_outcome_event, check, company_function_keys
from tgtc_core.testing.fakes import FakeAirtable, FakeInstantly
from tgtc_core.testing.scenario import CONTROL_ID_BY_CAMPAIGN_KEY
from tests_core.helpers import delivery_service, opportunity_service, sql1, sqlall
from tests_core.seed import apollo_for, seed_opportunity

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


def test_reply_event_suppresses_future_approval_of_the_same_person(conn, clock):
    apply_outcome_event(conn, provider="instantly", event_type="reply", dedupe_key="r-1", email="good.buyer@acme.com")
    pid, eid, oid = seed_opportunity(conn, clock)
    out = opportunity_service(conn, apollo_for("acme.com", "Acme"), clock).process(oid)
    assert out.outcome == "closed"
    reasons = {a["reason"] for a in sqlall(conn, "SELECT reason FROM candidate_attempts")}
    assert "suppressed:person_email" in reasons
    assert sql1(conn, "SELECT count(*) FROM approvals") == 0


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


def test_error_and_rejected_rows_do_not_suppress(conn):
    from tgtc_core.services.suppression import import_airtable_rows

    counts = import_airtable_rows(conn, [
        {"id": "r1", "fields": {"Company": "Acme", "Website": "https://acme.com", "Role Bucket": "finance", "Status": "Error", "Email": "e@acme.com"}},
        {"id": "r2", "fields": {"Company": "Acme", "Website": "https://acme.com", "Role Bucket": "finance", "Status": "Rejected"}},
    ])
    assert counts.by_kind.get("company_function", 0) == 0
    assert counts.by_kind.get("person_email") == 1                # a contacted email is still a contacted email
