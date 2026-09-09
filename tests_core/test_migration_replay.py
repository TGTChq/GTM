"""Importing twice keeps exclusions and receipts and duplicates no work; schema re-apply keeps
everything the new system delivered."""

from __future__ import annotations

from tgtc_core.db import apply_schema
from tgtc_core.services.suppression import import_airtable_rows, import_instantly_emails
from tgtc_core.testing.fakes import FakeAirtable, FakeInstantly
from tgtc_core.testing.scenario import CONTROL_ID_BY_CAMPAIGN_KEY
from tests_core.helpers import delivery_service, opportunity_service, sql1
from tests_core.seed import apollo_for, seed_opportunity

ROWS = [
    {"id": "rec1", "fields": {"Lead Key": "acme.com|a@acme.com|finance", "Company": "Acme Inc", "Website": "https://acme.com",
                              "Role Bucket": "finance", "Status": "Approved", "Email": "a@acme.com",
                              "Outbound Company Identity": "linkedin:acme", "Outbound Company Confidence": "high"}},
    {"id": "rec2", "fields": {"Lead Key": "beta.io|b@beta.io|marketing", "Company": "Beta", "Website": "https://beta.io",
                              "Role Bucket": "marketing", "Status": "Pending", "Email": "b@beta.io"}},
    {"id": "rec3", "fields": {"Lead Key": "x|c@gamma.co|product", "Company": "Gamma", "Website": "https://boards.greenhouse.io/gamma",
                              "Role Bucket": "product", "Status": "Enrolled", "Email": "c@gamma.co"}},
]


def test_import_twice_inserts_once_and_derives_the_legacy_keys(conn):
    first = import_airtable_rows(conn, ROWS)
    second = import_airtable_rows(conn, ROWS)
    assert first.inserted == 3 and second.inserted == 0 and second.already_present == 3
    keys = {r for (r,) in [(k,) for k in [row["key"] for row in _rows(conn)]]}
    assert "domain:acme.com|bucket:finance" in keys and "name:acme|bucket:finance" in keys and "linkedin:acme|bucket:finance" in keys
    assert "a@acme.com" in keys and "b@beta.io" in keys and "c@gamma.co" in keys
    assert not any(k.startswith("domain:greenhouse.io") for k in keys)      # an ATS host is never a company identity
    assert "name:gamma|bucket:product" in keys
    assert sql1(conn, "SELECT count(*) FROM suppressions") == first.by_kind["person_email"] + first.by_kind["company_function"] + first.by_kind["account"]


def _rows(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT kind, key FROM suppressions")
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()
    return rows


def test_instantly_import_is_idempotent(conn):
    a = import_instantly_emails(conn, ["x@a.com", "Y@A.com", "not-an-email"], campaign_id="c1")
    b = import_instantly_emails(conn, ["x@a.com"], campaign_id="c1")
    assert (a.inserted, a.skipped, b.inserted, b.already_present) == (2, 1, 0, 1)


def test_reapplying_the_schema_keeps_deliveries_and_receipts(conn, clock):
    pid, eid, oid = seed_opportunity(conn, clock)
    assert opportunity_service(conn, apollo_for("acme.com", "Acme"), clock).process(oid).outcome == "approved"
    cs = CONTROL_ID_BY_CAMPAIGN_KEY["customer_experience"]
    svc = delivery_service(conn, FakeAirtable(), FakeInstantly(campaign_status={cs: 1}), clock)
    svc.drain("airtable")
    svc.drain("instantly")
    before = (sql1(conn, "SELECT count(*) FROM delivery_receipts"), sql1(conn, "SELECT count(*) FROM approvals"), sql1(conn, "SELECT count(*) FROM page_receipts"))
    apply_schema(conn)   # a redeploy / rollback of the code re-applies the idempotent schema
    after = (sql1(conn, "SELECT count(*) FROM delivery_receipts"), sql1(conn, "SELECT count(*) FROM approvals"), sql1(conn, "SELECT count(*) FROM page_receipts"))
    assert before == after and before[0] == 4
    # and the delivered lead is now history the importer would also recognise
    counts = import_airtable_rows(conn, [{"id": "recX", "fields": {"Company": "Acme", "Website": "https://acme.com", "Role Bucket": "customer_success",
                                                                    "Status": "Approved", "Email": "good.buyer@acme.com"}}])
    assert counts.inserted == 1
    assert svc.drain("airtable") == []
