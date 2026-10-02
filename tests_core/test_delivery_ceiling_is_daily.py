"""The Instantly write ceiling is per UTC DAY, reconstructed from receipts.

Measured, production 2026-09-21: with TGTC_DELIVERY_MAX_PER_CAMPAIGN=150,
OPERATIONS received 242 new leads in one run. The counter lived in the
DeliveryService, and run-target builds a new one every round, so the ceiling
was per ROUND: four rounds, four fresh counters.

The count now starts from Instantly `created` receipts already written since
00:00 UTC, so it survives rounds, processes and restarts.
"""
from __future__ import annotations

from tgtc_core.testing.fakes import FakeAirtable, FakeInstantly
from tests_core.helpers import delivery_service, opportunity_service, sql1
from tests_core.seed import apollo_for, good_buyer, seed_opportunity

CS_CAMPAIGN = None  # resolved from the approval below


def _approve(conn, clock, domain, name, pid_suffix):
    pid, eid, oid = seed_opportunity(conn, clock, domain=domain, org_name=name, job_id=f"job-{pid_suffix}")
    buyer = good_buyer(domain, name, id=f"p-{pid_suffix}")
    out = opportunity_service(conn, apollo_for(domain, name, people=[buyer]), clock).process(oid)
    assert out.outcome == "approved", out
    return out.approval_id


def test_a_new_service_resumes_the_days_count(conn, clock):
    a1 = _approve(conn, clock, "acme.com", "Acme", "1")
    campaign = sql1(conn, "SELECT campaign_id FROM approvals WHERE id = %s", (a1,))
    env = {"TGTC_DELIVERY_MAX_PER_CAMPAIGN": "1", "TGTC_DELIVERY_MAX_TOTAL": "100"}
    ins = FakeInstantly(campaign_status={campaign: 1})
    first = delivery_service(conn, FakeAirtable(), ins, clock, env=env)
    assert [o.outcome for o in first.drain("instantly")] == ["delivered"]

    a2 = _approve(conn, clock, "globex.com", "Globex", "2")
    assert sql1(conn, "SELECT campaign_id FROM approvals WHERE id = %s", (a2,)) == campaign
    # A brand-new service -- a new round -- must still see today's one write.
    second = delivery_service(conn, FakeAirtable(), ins, clock, env=env)
    outcomes = [o.outcome for o in second.drain("instantly")]
    assert outcomes == ["deferred"], outcomes
    assert len(ins.leads) == 1


def test_the_seeded_count_is_reported(conn, clock):
    a1 = _approve(conn, clock, "acme.com", "Acme", "1")
    campaign = sql1(conn, "SELECT campaign_id FROM approvals WHERE id = %s", (a1,))
    env = {"TGTC_DELIVERY_MAX_PER_CAMPAIGN": "5", "TGTC_DELIVERY_MAX_TOTAL": "100"}
    ins = FakeInstantly(campaign_status={campaign: 1})
    delivery_service(conn, FakeAirtable(), ins, clock, env=env).drain("instantly")
    again = delivery_service(conn, FakeAirtable(), ins, clock, env=env)
    summary = again.canary_caps.summary()
    assert summary["written_total"] == 1
    assert summary["written_by_campaign"] == {campaign: 1}


def test_no_ceiling_configured_reads_nothing(conn, clock):
    svc = delivery_service(conn, FakeAirtable(), FakeInstantly(), clock, env={})
    assert svc.canary_caps.enabled is False
    assert svc.canary_caps.summary()["written_total"] == 0
