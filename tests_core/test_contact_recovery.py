"""Alternative contact recovery through the real gates. SIMULATED Apollo."""

from __future__ import annotations

import pytest

from tgtc_core.policy.campaigns import FUNCTION_KEYS
from tgtc_core.testing.fakes import make_person
from tgtc_core.testing.scenario import BUYER_TITLE_BY_FUNCTION
from tests_core.helpers import opportunity_service, sql1, sqlall
from tests_core.seed import apollo_for, good_buyer, seed_opportunity


@pytest.mark.parametrize("function_key", FUNCTION_KEYS)
def test_wrong_company_then_no_email_then_unverified_then_the_valid_one_is_approved(conn, clock, function_key):
    domain, org = "acme.com", "Acme"
    buyer = BUYER_TITLE_BY_FUNCTION[function_key]
    wrong = make_person(id="p-wrong", first="W", last="Rong", title=buyer, org_name="Other Corp", org_domain="othercorp.com",
                        email="w@othercorp.com", email_status="verified")
    noemail = make_person(id="p-noemail", first="N", last="Oemail", title=buyer, org_name=org, org_domain=domain, email=None, email_status=None)
    unverified = make_person(id="p-unv", first="U", last="Nverified", title=buyer, org_name=org, org_domain=domain,
                             email=f"u.nverified@{domain}", email_status="extrapolated")
    good = good_buyer(domain, org, function_key, id="p-good")
    pid, eid, oid = seed_opportunity(conn, clock, function_key=function_key, domain=domain, org_name=org)
    assert oid is not None, f"seed produced no opportunity for {function_key}"
    fake = apollo_for(domain, org, function_key=function_key, people=[wrong, noemail, unverified, good])
    svc = opportunity_service(conn, fake, clock)
    out = svc.process(oid)
    assert out.outcome == "approved", out
    assert sql1(conn, "SELECT lead_json->>'email' FROM approvals WHERE opportunity_id = %s", (oid,)) == good["_email"]
    attempts = {(a["candidate_ref"], a["attempt_kind"]): (a["outcome"], a["reason"]) for a in
                sqlall(conn, "SELECT candidate_ref, attempt_kind, outcome, reason FROM candidate_attempts WHERE opportunity_id = %s", (oid,))}
    # wrong company never costs a paid match: rejected before enrichment
    assert attempts[("pid:p-wrong", "gate")][0] == "skipped_pre_enrichment"
    assert ("pid:p-wrong", "match") not in attempts
    assert attempts[("pid:p-noemail", "gate")] == ("fail", "email:none_returned")
    assert attempts[("pid:p-unv", "gate")] == ("fail", "email:not_verified:extrapolated")
    assert attempts[("pid:p-good", "gate")] == ("pass", "approved")
    # exactly three paid matches (noemail, unverified, good) -- reported as requests, not "credits"
    assert sql1(conn, "SELECT count(*) FROM request_attempts WHERE provider = 'apollo' AND operation = 'person_match'") == 3
    assert fake.served_paid == 3  # 3 matches; Fantastic facts made the org enrich unnecessary; the search is not paid


def test_generic_mailbox_and_wrong_domain_are_rejected_and_the_next_candidate_wins(conn, clock):
    domain, org = "acme.com", "Acme"
    generic = good_buyer(domain, org, id="p-generic", email=f"info@{domain}")
    wrong_dom = good_buyer(domain, org, id="p-wrongdom", email="jane@gmail.com")
    good = good_buyer(domain, org, id="p-good")
    pid, eid, oid = seed_opportunity(conn, clock)
    fake = apollo_for(domain, org, people=[generic, wrong_dom, good])
    out = opportunity_service(conn, fake, clock).process(oid)
    assert out.outcome == "approved"
    reasons = {a["candidate_ref"]: a["reason"] for a in sqlall(conn, "SELECT candidate_ref, reason FROM candidate_attempts WHERE attempt_kind = 'gate'")}
    assert reasons["pid:p-generic"] == "email:generic_mailbox"
    assert reasons["pid:p-wrongdom"] == "email:domain_not_employer"
    assert sql1(conn, "SELECT lead_json->>'email' FROM approvals") == f"good.buyer@{domain}"


def test_max_attempts_closes_with_reason_and_a_new_candidate_reopens_later(conn, clock):
    """Review (recovery b): the per-epoch cap closes the opportunity; NEW evidence reopens it
    into the next epoch with a fresh budget, the earlier history is kept, and candidates
    already judged on evidence are not retried."""
    domain, org = "acme.com", "Acme"
    people = [good_buyer(domain, org, id=f"p-{i}", email=f"p{i}@{domain}", status="extrapolated") for i in range(5)]
    pid, eid, oid = seed_opportunity(conn, clock)
    fake = apollo_for(domain, org, people=people)
    out = opportunity_service(conn, fake, clock).process(oid)
    assert out.outcome == "closed" and out.reason == "no_verified_buyer_after_max_attempts:epoch_1"
    assert sql1(conn, "SELECT count(*) FROM request_attempts WHERE operation = 'person_match'") == 3   # the legacy cap, explicit
    assert sql1(conn, "SELECT count(*) FROM approvals") == 0
    # new evidence: a new posting for the same employer x function reopens it into epoch 2
    from tests_core.seed import seed_opportunity as seed2
    fake.people_by_domain[domain].append(good_buyer(domain, org, id="p-new", email=f"new@{domain}"))
    pid2, _, oid2 = seed2(conn, clock, domain=domain, org_name=org, job_id="job-2")
    assert oid2 == oid and sql1(conn, "SELECT state || ':' || evidence_epoch FROM opportunities WHERE id = %s", (oid,)) == "open:2"
    out2 = opportunity_service(conn, fake, clock).process(oid)
    assert out2.outcome == "approved", out2
    assert sql1(conn, "SELECT lead_json->>'email' FROM approvals") == f"new@{domain}"
    # p-3 and p-4 were never enriched in epoch 1. Their email status is unknown to
    # search, so epoch 2 must evaluate them before p-new; none of p-0..p-2 is retried.
    assert sql1(conn, "SELECT count(*) FROM request_attempts WHERE operation = 'person_match'") == 6
    matches = sqlall(conn, "SELECT epoch, candidate_ref FROM candidate_attempts "
                    "WHERE opportunity_id = %s AND attempt_kind = 'match' ORDER BY id", (oid,))
    assert [(m["epoch"], m["candidate_ref"]) for m in matches] == [
        (1, "pid:p-0"), (1, "pid:p-1"), (1, "pid:p-2"),
        (2, "pid:p-3"), (2, "pid:p-4"), (2, "pid:p-new"),
    ]
    assert fake.served_paid == 6
    # history is kept: every epoch-1 attempt row is still there
    assert sql1(conn, "SELECT count(*) FROM candidate_attempts WHERE opportunity_id = %s", (oid,)) >= 8


def test_corroborated_alternate_domain_email_passes_but_unrelated_domain_does_not(conn, clock):
    """Apollo's organization for the same employer carries a brand domain; a verified email there is aligned."""
    domain, org = "acme.com", "Acme"
    pid, eid, oid = seed_opportunity(conn, clock, domain=domain, org_name=org)
    fake = apollo_for(domain, org)
    fake.organizations[domain]["primary_domain"] = "acmebrand.com"          # Apollo knows the brand domain
    alt = good_buyer(domain, org, id="p-alt", email="jane@acmebrand.com")
    alt["organization"] = {"id": f"org-{domain}", "name": org, "primary_domain": "acmebrand.com"}
    fake.people_by_domain[domain] = [alt]
    out = opportunity_service(conn, fake, clock).process(oid)
    assert out.outcome == "approved", out
    assert sql1(conn, "SELECT lead_json->>'email_alignment' FROM approvals") == "CORROBORATED_ALTERNATE_EMPLOYER_DOMAIN"
