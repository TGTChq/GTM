"""R04 -- a cached contact is reconsidered through the same current-employer, authority,
territory and email checks as a fresh enrichment. Persisting an enriched response never
establishes buyer suitability; a contradicted employer is never attributed."""

from __future__ import annotations

from tgtc_core.testing.fakes import make_person
from tests_core.helpers import opportunity_service, sql1, sqlall
from tests_core.seed import apollo_for, good_buyer, seed_opportunity


def test_rejected_contact_reconsidered_from_cache_remains_rejected(conn, clock):
    domain, org = "acme.com", "Acme"
    emea = make_person(id="p-emea", first="Eva", last="Emea", title="VP Product EMEA", org_name=org, org_domain=domain,
                       email="eva@acme.com", email_status="verified")
    _, eid, o_product = seed_opportunity(conn, clock, function_key="product", domain=domain, org_name=org)
    fake = apollo_for(domain, org, function_key="product", people=[emea])
    out = opportunity_service(conn, fake, clock).process(o_product)
    assert out.outcome == "closed"
    assert sql1(conn, "SELECT reason FROM candidate_attempts WHERE candidate_ref = 'pid:p-emea' AND attempt_kind = 'gate'") == "contact:territory_mismatch"
    # the enriched record is stored, with evidence and WITHOUT an employer attribution
    person = sqlall(conn, "SELECT employer_id, email_status, facts_json FROM people WHERE apollo_person_id = 'p-emea'")[0]
    assert person["employer_id"] is None and person["email_status"] == "verified"
    assert person["facts_json"]["enriched"]["title"] == "VP Product EMEA"
    # a second product-family opportunity at the same employer (new evidence) must not approve
    # this person from cache: the territory gate runs again on stored evidence
    from tests_core.seed import seed_opportunity as seed2
    _, _, o2 = seed2(conn, clock, function_key="product", domain=domain, org_name=org, job_id="job-2")
    assert o2 == o_product
    out2 = opportunity_service(conn, fake, clock).process(o2)
    assert out2.outcome == "closed" and sql1(conn, "SELECT count(*) FROM approvals") == 0


def test_cached_person_is_reused_only_when_every_gate_passes_for_this_function(conn, clock):
    domain, org = "acme.com", "Acme"
    coo = good_buyer(domain, org, "operations", id="p-coo", email="coo@acme.com")
    coo["title"] = "COO"
    _, eid, o_ops = seed_opportunity(conn, clock, function_key="operations", domain=domain, org_name=org)
    fake = apollo_for(domain, org, people=[coo])
    assert opportunity_service(conn, fake, clock).process(o_ops).outcome == "approved"
    assert sql1(conn, "SELECT employer_id FROM people WHERE apollo_person_id = 'p-coo'") == eid   # attributed only after the gate passed
    # marketing at the same employer: the COO is NOT in marketing's buyer hierarchy -> reuse refused
    _, _, o_mkt = seed_opportunity(conn, clock, function_key="marketing", domain=domain, org_name=org, job_id="mkt-1")
    fake.people_by_domain[domain] = [coo]          # the search would also only return the COO
    out = opportunity_service(conn, fake, clock).process(o_mkt)
    assert out.outcome == "closed"
    assert sql1(conn, "SELECT count(*) FROM approvals") == 1


def test_stored_evidence_reuse_passes_when_the_gates_pass_and_costs_no_call(conn, clock):
    """Reuse is still valuable: a verified, employment-proven buyer can serve a second
    function whose hierarchy includes them, with zero paid calls."""
    domain, org = "acme.com", "Acme"
    coo = good_buyer(domain, org, "operations", id="p-coo", email="coo@acme.com")
    coo["title"] = "COO"
    _, eid, o_ops = seed_opportunity(conn, clock, function_key="operations", domain=domain, org_name=org)
    fake = apollo_for(domain, org, people=[coo])
    assert opportunity_service(conn, fake, clock).process(o_ops).outcome == "approved"
    paid_before = fake.served_paid
    # revoke the first approval so person-uniqueness does not decide the outcome of this test
    with conn.cursor() as cur:
        cur.execute("UPDATE approvals SET state = 'revoked', revoke_reason = 'test' ")
    conn.commit()
    _, _, o_fin = seed_opportunity(conn, clock, function_key="finance", domain=domain, org_name=org, job_id="fin-1")
    out = opportunity_service(conn, fake, clock).process(o_fin)
    assert out.outcome == "approved" and out.reason == "reused_verified_person"
    assert fake.served_paid == paid_before
