"""Nine routes, end to end, through the new runner with SIMULATED providers on real PostgreSQL."""

from __future__ import annotations

from tgtc_core.policy.campaigns import CAMPAIGN_BY_FUNCTION, FUNCTION_KEYS
from tgtc_core.testing.scenario import CONTROL_ID_BY_CAMPAIGN_KEY, build_nine_route_scenario
from tests_core.helpers import runner, sql1, sqlall

REQUIRED_VARIABLES = {"open_role", "open_roles", "role_focus", "role_bucket", "company_size_band", "job_url", "relevance",
                      "job_source", "job_url_status", "job_url_source"}


def test_every_route_produces_one_approved_lead_delivered_to_its_own_campaign(conn, clock):
    sc = build_nine_route_scenario(clock())
    r = runner(conn, sc, clock)
    report = r.cycle()
    assert report.stages["resolve_identity"] == {"resolved": 10}
    assert report.stages["classify"] == {"classified": 10}
    assert report.stages["qualify_opportunity"] == {"approved": 10}
    assert report.delivery == {"airtable": {"delivered": 10}, "instantly": {"delivered": 10}}
    approvals = sqlall(conn, "SELECT function_key, campaign_key, campaign_id, lead_json, state FROM approvals ORDER BY function_key")
    assert [a["function_key"] for a in approvals] == sorted(FUNCTION_KEYS)
    assert {a["campaign_key"] for a in approvals} == {c.key for c in CAMPAIGN_BY_FUNCTION.values()}   # all nine campaigns
    for a in approvals:
        assert a["campaign_id"] == CONTROL_ID_BY_CAMPAIGN_KEY[a["campaign_key"]]
        assert a["lead_json"]["email"] == sc.expected_emails[a["function_key"]]
        assert a["state"] == "delivered"
    # Instantly received exactly one lead per approval, in the right campaign, with the Control variables
    assert len(sc.instantly.leads) == 10
    for email, lead in sc.instantly.leads.items():
        fn = next(k for k, v in sc.expected_emails.items() if v == email)
        assert lead["campaign"] == CONTROL_ID_BY_CAMPAIGN_KEY[CAMPAIGN_BY_FUNCTION[fn].key]
        assert REQUIRED_VARIABLES <= set(lead["custom_variables"]) and lead["custom_variables"]["role_bucket"] == fn
    # Airtable received one Approved row per approval with the core validation version
    assert len(sc.airtable.records) == 10
    assert {r["fields"]["Status"] for r in sc.airtable.records.values()} == {"Approved"}
    assert {r["fields"]["Validation Version"] for r in sc.airtable.records.values()} == {"tgtc-core/1"}
    assert sql1(conn, "SELECT count(*) FROM delivery_receipts WHERE receipt_kind = 'created'") == 20
    # spend is reported as requests + estimates, never as one-call-one-credit facts
    L = report.ledger
    assert L["apollo_requests_by_operation_status"]["person_match:served"] == 30
    assert L["apollo_confirmed_credits"] is None and float(L["apollo_estimated_credits"]) == 30.0
    assert "organization_enrich:served" not in L["apollo_requests_by_operation_status"]   # Fantastic facts sufficed
    assert L["fantastic_rows_confirmed_by_header"] == 10


def test_a_second_cycle_is_idempotent(conn, clock):
    sc = build_nine_route_scenario(clock())
    r = runner(conn, sc, clock)
    r.cycle()
    clock.advance(minutes=5)
    second = r.cycle()
    assert second.stages == {"resolve_identity": {}, "classify": {}, "qualify_opportunity": {}}
    assert second.delivery == {"airtable": {}, "instantly": {}}
    assert sql1(conn, "SELECT count(*) FROM approvals") == 10
    assert len(sc.airtable.records) == 10 and len(sc.instantly.leads) == 10
    assert sql1(conn, "SELECT count(*) FROM postings") == 10


def test_restart_mid_cycle_resumes_without_duplicates(conn, clock):
    """A crash after acquisition and identity, before classification, loses nothing."""
    sc = build_nine_route_scenario(clock())
    r = runner(conn, sc, clock)
    r.acquire()
    r.work("resolve_identity")
    # "restart": a new runner instance on the same database
    r2 = runner(conn, sc, clock)
    report = r2.cycle(acquire=True)
    assert report.stages["classify"] == {"classified": 10} and report.stages["qualify_opportunity"] == {"approved": 10}
    assert sql1(conn, "SELECT count(*) FROM postings") == 10 and sql1(conn, "SELECT count(*) FROM approvals") == 10
