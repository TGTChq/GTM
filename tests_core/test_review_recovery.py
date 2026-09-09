"""Recovery problems named by the review: the organization-id fallback must be tried when
the domain search leaves no USABLE candidate; a reopen on new evidence permits meaningful
reconsideration without erasing history or allowing unlimited retries."""

from __future__ import annotations

from tgtc_core.policy.requirements import rule
from tests_core.helpers import opportunity_service, sql1, sqlall
from tests_core.seed import apollo_for, good_buyer, seed_opportunity


def test_org_id_fallback_runs_when_domain_results_are_all_already_judged(conn, clock):
    domain, org = "acme.com", "Acme"
    pid, eid, oid = seed_opportunity(conn, clock, headcount=None)       # forces org enrich -> apollo_org_id known
    judged = good_buyer(domain, org, id="p-judged", email="judged@acme.com", status="extrapolated")
    fresh = good_buyer(domain, org, id="p-fresh", email="fresh@acme.com")
    fake = apollo_for(domain, org, people=[judged])
    fake.people_by_org_id[f"org-{domain}"] = [judged, fresh]            # the org-id selector knows one more person
    out = opportunity_service(conn, fake, clock).process(oid)
    assert out.outcome == "closed" and out.reason.startswith("no_verified_buyer")
    # new evidence reopens; the domain selector returns only the judged person -> fallback by org id
    from tests_core.seed import seed_opportunity as seed2
    seed2(conn, clock, domain=domain, org_name=org, job_id="job-2")
    out2 = opportunity_service(conn, fake, clock).process(oid)
    assert out2.outcome == "approved" and sql1(conn, "SELECT lead_json->>'email' FROM approvals") == "fresh@acme.com"
    selectors = [("organization_ids[]" in r["params"], "q_organization_domains_list[]" in r["params"])
                 for r in fake.requests if r["path"].endswith("/mixed_people/api_search")]
    assert (False, True) in selectors and (True, False) in selectors    # both selectors were used
    assert sql1(conn, "SELECT count(*) FROM request_attempts WHERE operation = 'person_match'") == 2   # judged once, fresh once


def test_reopen_budget_is_per_epoch_and_bounded(conn, clock):
    domain, org = "acme.com", "Acme"
    cap = int(rule("max_match_attempts_per_evidence_epoch"))
    epochs = int(rule("max_evidence_epochs"))
    people = [good_buyer(domain, org, id=f"p-{i}", email=f"p{i}@{domain}", status="extrapolated") for i in range(cap * epochs + 2)]
    pid, eid, oid = seed_opportunity(conn, clock)
    fake = apollo_for(domain, org, people=people)
    from tests_core.seed import seed_opportunity as seed2
    total_matches = 0
    for epoch in range(1, epochs + 1):
        if epoch > 1:
            seed2(conn, clock, domain=domain, org_name=org, job_id=f"job-{epoch}")
            assert sql1(conn, "SELECT evidence_epoch FROM opportunities WHERE id = %s", (oid,)) == epoch
        out = opportunity_service(conn, fake, clock).process(oid)
        assert out.outcome == "closed" and out.reason == f"no_verified_buyer_after_max_attempts:epoch_{epoch}"
        total_matches += cap
        assert sql1(conn, "SELECT count(*) FROM request_attempts WHERE operation = 'person_match'") == total_matches
    # one more piece of evidence: the epoch bound stops further spend, with a named reason
    seed2(conn, clock, domain=domain, org_name=org, job_id="job-final")
    out = opportunity_service(conn, fake, clock).process(oid)
    assert out.outcome == "closed" and out.reason == "evidence_epochs_exhausted"
    assert sql1(conn, "SELECT count(*) FROM request_attempts WHERE operation = 'person_match'") == total_matches
    # the full history is still there, one row per judged candidate
    assert sql1(conn, "SELECT count(*) FROM candidate_attempts WHERE attempt_kind = 'match'") == total_matches
    assert sorted({r["epoch"] for r in sqlall(conn, "SELECT epoch FROM candidate_attempts")}) == list(range(1, epochs + 1))
