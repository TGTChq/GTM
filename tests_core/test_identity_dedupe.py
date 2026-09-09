"""Same job from two sources -> no duplicate; distinct jobs survive; one person is never
approved twice across functions."""

from __future__ import annotations

from tests_core.helpers import opportunity_service, sql1, sqlall
from tests_core.seed import apollo_for, example_for, good_buyer, seed_opportunity


def test_same_job_from_two_sources_is_one_opportunity_and_one_lead(conn, clock):
    ex = example_for("customer_success")
    seed_opportunity(conn, clock, job_id="li-1", source="fantastic:active-jb")
    seed_opportunity(conn, clock, job_id="ats-1", source="fantastic:active-ats")
    postings = sqlall(conn, "SELECT id, source, duplicate_of_posting_id FROM postings ORDER BY id")
    assert len(postings) == 2 and postings[1]["duplicate_of_posting_id"] == postings[0]["id"]
    assert sql1(conn, "SELECT count(*) FROM opportunities") == 1
    assert sql1(conn, "SELECT count(*) FROM opportunity_postings") == 2
    oid = sql1(conn, "SELECT id FROM opportunities")
    out = opportunity_service(conn, apollo_for("acme.com", "Acme"), clock).process(oid)
    assert out.outcome == "approved"
    assert sql1(conn, "SELECT count(*) FROM approvals") == 1
    assert sql1(conn, "SELECT count(*) FROM delivery_outbox") == 2   # one airtable + one instantly, not four


def test_distinct_jobs_at_one_employer_are_kept_and_grouped_by_function(conn, clock):
    seed_opportunity(conn, clock, function_key="customer_success", job_id="j1")
    seed_opportunity(conn, clock, function_key="customer_success", job_id="j2", title="CSM II",
                     description=example_for("customer_success").description + " You will also own onboarding playbooks.")
    seed_opportunity(conn, clock, function_key="finance", job_id="j3")
    assert sql1(conn, "SELECT count(*) FROM postings WHERE duplicate_of_posting_id IS NULL") == 3
    opps = sqlall(conn, "SELECT function_key, (SELECT count(*) FROM opportunity_postings op WHERE op.opportunity_id = o.id) AS n FROM opportunities o ORDER BY function_key")
    assert opps == [{"function_key": "customer_success", "n": 2}, {"function_key": "finance", "n": 1}]
    assert sql1(conn, "SELECT count(DISTINCT employer_id) FROM opportunities") == 1


def test_one_person_is_not_approved_twice_across_functions(conn, clock):
    """The COO is the best candidate for both operations and finance; only one approval may name them."""
    _, _, o_ops = seed_opportunity(conn, clock, function_key="operations", job_id="ops-1")
    _, _, o_fin = seed_opportunity(conn, clock, function_key="finance", job_id="fin-1")
    coo = good_buyer("acme.com", "Acme", "operations", id="p-coo", email="coo@acme.com")
    coo["title"] = "COO"
    controller = good_buyer("acme.com", "Acme", "finance", id="p-ctrl", email="controller@acme.com")
    fake = apollo_for("acme.com", "Acme", people=[coo, controller])
    svc = opportunity_service(conn, fake, clock)
    assert svc.process(o_ops).outcome == "approved"
    assert sql1(conn, "SELECT lead_json->>'email' FROM approvals WHERE opportunity_id = %s", (o_ops,)) == "coo@acme.com"
    out = svc.process(o_fin)
    assert out.outcome == "approved"
    # finance must pick a DIFFERENT person even though the COO is in finance's hierarchy too
    assert sql1(conn, "SELECT lead_json->>'email' FROM approvals WHERE opportunity_id = %s", (o_fin,)) == "controller@acme.com"
    assert sql1(conn, "SELECT count(DISTINCT person_id) FROM approvals") == 2
    # and if the COO had been the only candidate for finance, finance closes rather than double-approving
    _, _, o_hr = seed_opportunity(conn, clock, function_key="people_hr", job_id="hr-1")
    fake.people_by_domain["acme.com"] = [dict(coo, title="Chief People Officer")]
    out2 = svc.process(o_hr)
    assert out2.outcome == "closed" and sql1(conn, "SELECT count(*) FROM approvals") == 2
