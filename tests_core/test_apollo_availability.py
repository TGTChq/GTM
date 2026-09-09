"""Zero balance, 422, 429, 401, timeout, restart and recovery are distinct. No storms.
Intent is written before the call. SIMULATED Apollo."""

from __future__ import annotations

from datetime import timedelta

from tgtc_core.db import connect
from tgtc_core.providers.http import Response
from tgtc_core.testing.fakes import CREDIT_BODY, FakeApollo
from tests_core.helpers import opportunity_service, sql1, sqlall
from tests_core.seed import apollo_for, seed_opportunity


def test_credit_exhaustion_waits_records_refusal_and_does_not_storm(conn, clock):
    pid, eid, oid = seed_opportunity(conn, clock)
    fake = apollo_for("acme.com", "Acme")
    fake.credits = 0
    svc = opportunity_service(conn, fake, clock)
    out = svc.process(oid)
    assert out.outcome == "wait" and out.reason == "apollo_credit_exhausted"
    state = sqlall(conn, "SELECT state, last_error_code, consecutive_refusals FROM provider_state WHERE provider = 'apollo'")[0]
    assert state["state"] == "refusing" and state["last_error_code"] == "BILLING.LIMIT.CREDITS_EXHAUSTED" and state["consecutive_refusals"] == 1
    attempts = sqlall(conn, "SELECT operation, status, error_class, response_summary FROM request_attempts WHERE provider = 'apollo' ORDER BY id")
    # Fantastic already supplied headcount/industry/domain, so NO organization enrich was bought;
    # the 0-credit search ran, and the first PAID call (person_match) was refused.
    assert [(a["operation"], a["status"]) for a in attempts] == [("people_search", "served"), ("person_match", "refused")]
    assert attempts[1]["response_summary"]["context"]["credit_balance"] == 0
    assert sql1(conn, "SELECT count(*) FROM candidate_attempts WHERE attempt_kind = 'match' AND outcome = 'refused'") == 1
    assert sql1(conn, "SELECT state FROM opportunities WHERE id = %s", (oid,)) == "open"   # not closed, not approved
    # within the retry interval: NO new request is made, not even the free search
    calls = len(fake.requests)
    out2 = svc.process(oid)
    assert out2.outcome == "wait" and len(fake.requests) == calls
    # after the interval: exactly ONE controlled paid attempt, still refusing -> still waiting
    paid = lambda: len([r for r in fake.requests if r["path"].endswith("/people/match") or r["path"].endswith("/organizations/enrich")])
    paid_before = paid()
    clock.advance(hours=6, seconds=1)
    out3 = svc.process(oid)
    assert out3.outcome == "wait" and paid() == paid_before + 1
    assert sql1(conn, "SELECT consecutive_refusals FROM provider_state WHERE provider = 'apollo'") == 2
    # recovery: the provider serves again -> approved, state serving, without any operator action
    fake.credits = None
    clock.advance(hours=6, seconds=1)
    out4 = svc.process(oid)
    assert out4.outcome == "approved", out4
    assert sql1(conn, "SELECT state FROM provider_state WHERE provider = 'apollo'") == "serving"


def test_rate_limit_auth_timeout_and_server_are_distinct(conn, clock):
    pid, eid, oid = seed_opportunity(conn, clock)
    fake = apollo_for("acme.com", "Acme")
    svc = opportunity_service(conn, fake, clock)
    fake.fail_next = [429]
    out = svc.process(oid)
    assert out.outcome == "wait" and out.reason == "apollo_rate_limited"
    assert sql1(conn, "SELECT state FROM provider_state WHERE provider = 'apollo'") is None   # a throttle is not a refusal
    fake.fail_next = ["timeout"]
    out = svc.process(oid)
    assert out.outcome == "retry" and out.reason == "apollo_timeout"
    assert sql1(conn, "SELECT status FROM request_attempts WHERE provider = 'apollo' ORDER BY id DESC LIMIT 1") == "uncertain"
    fake.fail_next = [500]
    out = svc.process(oid)
    assert out.outcome == "retry" and out.reason == "apollo_server"
    assert sql1(conn, "SELECT state FROM provider_state WHERE provider = 'apollo'") is None   # transient, not a refusal
    fake.fail_next = [401]
    out = svc.process(oid)
    assert out.outcome == "wait" and out.reason == "apollo_unauthorized"
    assert sql1(conn, "SELECT state FROM provider_state WHERE provider = 'apollo'") == "unauthorized"
    # still unauthorized within the interval: no call at all
    calls = len(fake.requests)
    assert svc.process(oid).outcome == "wait" and len(fake.requests) == calls
    # after the interval one controlled attempt is made; the provider serves -> approved, serving
    clock.advance(hours=7)
    out = svc.process(oid)
    assert out.outcome == "approved"
    assert sql1(conn, "SELECT state FROM provider_state WHERE provider = 'apollo'") == "serving"
    paid_statuses = [a["status"] for a in sqlall(conn, "SELECT status FROM request_attempts WHERE provider = 'apollo' AND operation <> 'people_search' ORDER BY id")]
    assert paid_statuses == ["refused", "uncertain", "failed", "refused", "served"]


def test_record_level_validation_is_not_a_global_stop(conn, clock):
    """A 404 on people/match for one candidate moves to the next candidate; nothing is recorded as refusing."""
    pid, eid, oid = seed_opportunity(conn, clock)
    from tests_core.seed import good_buyer

    ghost = good_buyer("acme.com", "Acme", id="p-ghost", email="ghost@acme.com")
    real = good_buyer("acme.com", "Acme", id="p-real", email="real@acme.com")
    fake = apollo_for("acme.com", "Acme", people=[ghost, real])
    # the fake matches by id over its people lists; remove the ghost from match resolution by renaming its id there
    fake.people_by_domain["acme.com"][0] = dict(ghost)
    orig_request = fake.request

    def request(method, url, **kw):
        if url.endswith("/people/match") and dict(kw.get("params") or {}).get("id") == "p-ghost":
            return Response(status=404, text='{"error":"not found"}')
        return orig_request(method, url, **kw)

    fake.request = request  # type: ignore[assignment]
    svc = opportunity_service(conn, fake, clock)
    out = svc.process(oid)
    assert out.outcome == "approved"
    assert sql1(conn, "SELECT lead_json->>'email' FROM approvals") == "real@acme.com"
    assert sql1(conn, "SELECT outcome FROM candidate_attempts WHERE candidate_ref = 'pid:p-ghost' AND attempt_kind = 'match'") == "not_found"
    assert sql1(conn, "SELECT state FROM provider_state WHERE provider = 'apollo'") == "serving"


def test_intent_is_committed_before_the_provider_is_called(conn, conn2, clock, pg_url):
    pid, eid, oid = seed_opportunity(conn, clock)
    fake = apollo_for("acme.com", "Acme")
    seen = []
    orig = fake.request

    def request(method, url, **kw):
        # observed from an INDEPENDENT connection while the call is in flight
        with conn2.cursor() as cur:
            cur.execute("SELECT operation, status FROM request_attempts WHERE provider = 'apollo' ORDER BY id DESC LIMIT 1")
            seen.append(dict(cur.fetchone() or {}))
        conn2.commit()
        return orig(method, url, **kw)

    fake.request = request  # type: ignore[assignment]
    svc = opportunity_service(conn, fake, clock)
    assert svc.process(oid).outcome == "approved"
    assert seen and all(s["status"] == "intended" for s in seen)
    assert [s["operation"] for s in seen] == ["people_search", "person_match"]   # facts from Fantastic: no org enrich


def test_missing_employer_facts_are_enriched_once_from_apollo_and_reused(conn, clock):
    pid, eid, oid = seed_opportunity(conn, clock, headcount=None)
    fake = apollo_for("acme.com", "Acme", headcount=140)
    out = opportunity_service(conn, fake, clock).process(oid)
    assert out.outcome == "approved"
    ops = [a["operation"] for a in sqlall(conn, "SELECT operation FROM request_attempts WHERE provider = 'apollo' ORDER BY id")]
    assert ops[0] == "organization_enrich" and ops.count("organization_enrich") == 1
    assert sql1(conn, "SELECT employee_count FROM employers WHERE id = %s", (eid,)) == 140
    assert sql1(conn, "SELECT alias_value FROM employer_aliases WHERE employer_id = %s AND alias_kind = 'apollo_org_id'", (eid,)) == "org-acme.com"
