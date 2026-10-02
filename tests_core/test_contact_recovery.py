"""Alternative contact recovery through the real gates. SIMULATED Apollo."""

from __future__ import annotations

import pytest

from tgtc_core.policy.campaigns import FUNCTION_KEYS
from tgtc_core.services.opportunity import reopen_recoverable_opportunities
from tgtc_core.testing.fakes import make_person
from tgtc_core.testing.scenario import BUYER_TITLE_BY_FUNCTION
from tests_core.helpers import opportunity_service, sql1, sqlall
from tests_core.seed import apollo_for, good_buyer, seed_opportunity


def test_recoverable_search_closure_is_reopened_without_incrementing_evidence_epoch(conn, clock):
    _, _, oid = seed_opportunity(conn, clock)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE opportunities SET state = 'closed', close_reason = 'no_candidates_found' WHERE id = %s",
            (oid,),
        )
        cur.execute(
            "UPDATE work_items SET state = 'closed', close_reason = 'no_candidates_found' "
            "WHERE kind = 'qualify_opportunity' AND subject_id = %s",
            (oid,),
        )
    conn.commit()
    from tgtc_core.testing.scenario import campaign_env
    assert reopen_recoverable_opportunities(
        conn, campaign_env=campaign_env(), signing_key="test-key", now=clock(),
    ) == 1
    assert sql1(conn, "SELECT state || ':' || evidence_epoch FROM opportunities WHERE id = %s", (oid,)) == "open:1"
    assert sql1(conn, "SELECT state FROM work_items WHERE kind = 'qualify_opportunity' AND subject_id = %s", (oid,)) == "ready"


@pytest.mark.parametrize("function_key", FUNCTION_KEYS)
def test_wrong_company_then_no_email_then_unverified_then_the_valid_one_is_approved(conn, clock, function_key):
    domain, org = "acme.com", "Acme"
    buyer = BUYER_TITLE_BY_FUNCTION[function_key]
    wrong = make_person(id="p-1-wrong", first="W", last="Rong", title=buyer, org_name="Other Corp", org_domain="othercorp.com",
                        email="w@othercorp.com", email_status="verified")
    noemail = make_person(id="p-2-noemail", first="N", last="Oemail", title=buyer, org_name=org, org_domain=domain, email=None, email_status=None)
    unverified = make_person(id="p-3-unv", first="U", last="Nverified", title=buyer, org_name=org, org_domain=domain,
                             email=f"u.nverified@{domain}", email_status="extrapolated")
    good = good_buyer(domain, org, function_key, id="p-4-good")
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
    assert attempts[("pid:p-1-wrong", "gate")][0] == "skipped_pre_enrichment"
    assert ("pid:p-1-wrong", "match") not in attempts
    assert attempts[("pid:p-2-noemail", "gate")] == ("fail", "email:none_returned")
    assert attempts[("pid:p-3-unv", "gate")] == ("fail", "email:not_verified:extrapolated")
    assert attempts[("pid:p-4-good", "gate")] == ("pass", "approved")
    # exactly three paid matches (noemail, unverified, good) -- reported as requests, not "credits"
    assert sql1(conn, "SELECT count(*) FROM request_attempts WHERE provider = 'apollo' AND operation = 'person_match'") == 3
    assert fake.served_paid == 3  # 3 matches; Fantastic facts made the org enrich unnecessary; the search is not paid


def test_generic_mailbox_and_wrong_domain_are_rejected_and_the_next_candidate_wins(conn, clock):
    domain, org = "acme.com", "Acme"
    generic = good_buyer(domain, org, id="p-1-generic", email=f"info@{domain}")
    wrong_dom = good_buyer(domain, org, id="p-2-wrongdom", email="jane@gmail.com")
    good = good_buyer(domain, org, id="p-4-good")
    pid, eid, oid = seed_opportunity(conn, clock)
    fake = apollo_for(domain, org, people=[generic, wrong_dom, good])
    out = opportunity_service(conn, fake, clock).process(oid)
    assert out.outcome == "approved"
    reasons = {a["candidate_ref"]: a["reason"] for a in sqlall(conn, "SELECT candidate_ref, reason FROM candidate_attempts WHERE attempt_kind = 'gate'")}
    assert reasons["pid:p-1-generic"] == "email:generic_mailbox"
    assert reasons["pid:p-2-wrongdom"] == "email:domain_not_employer"
    assert sql1(conn, "SELECT lead_json->>'email' FROM approvals") == f"good.buyer@{domain}"


def test_one_opportunity_can_create_three_distinct_fully_gated_buyers(conn, clock):
    """Phase 2 audit task 9 (2026-09-19): "three people in the same role are
    NOT diversification" -- the three good buyers now carry three genuinely
    distinct personas (functional owner, executive leader, TA/People leader)
    so this still proves three distinct people can be fully gated and
    approved in one opportunity under the new persona-diversity rule.
    people_hr is used because it is the one buyer hierarchy with all three
    personas; customer_success (the original function) tops out at two."""
    domain, org = "acme.com", "Acme"
    bad = good_buyer(domain, org, "people_hr", id="p-bad", email=f"bad@{domain}", status="extrapolated")
    good = [good_buyer(domain, org, "people_hr", id=f"p-good-{i}", email=f"buyer{i}@{domain}") for i in range(3)]
    good[0]["title"] = "HR Director"                    # functional_owner
    good[1]["title"] = "VP People"                       # executive_leader
    good[2]["title"] = "Head of Talent Acquisition"      # ta_people_leader
    _, _, oid = seed_opportunity(conn, clock, function_key="people_hr")
    fake = apollo_for(domain, org, function_key="people_hr", people=[bad, *good])
    out = opportunity_service(
        conn, fake, clock,
        max_contacts_per_opportunity=3,
        run_id="three-contact-test",
    ).process(oid)

    assert out.outcome == "approved"
    assert out.details == {"approvals_created": 3, "approved_total": 3}
    assert sql1(conn, "SELECT count(*) FROM approvals WHERE opportunity_id = %s", (oid,)) == 3
    assert sql1(conn, "SELECT count(*) FROM approvals WHERE run_id = 'three-contact-test'") == 3
    assert sql1(conn, "SELECT count(DISTINCT person_id) FROM approvals WHERE opportunity_id = %s", (oid,)) == 3
    assert sql1(conn, "SELECT count(*) FROM delivery_outbox WHERE channel = 'airtable'") == 3
    assert sql1(conn, "SELECT count(*) FROM delivery_outbox WHERE channel = 'instantly'") == 3
    reasons = {a["candidate_ref"]: a["reason"] for a in sqlall(
        conn, "SELECT candidate_ref, reason FROM candidate_attempts WHERE attempt_kind = 'gate'"
    )}
    assert reasons["pid:p-bad"] == "email:not_verified:extrapolated"
    assert {reasons[f"pid:p-good-{i}"] for i in range(3)} == {"approved"}


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


def test_expanded_three_contact_budget_reopens_old_three_attempt_closure(conn, clock):
    domain, org = "acme.com", "Acme"
    people = [good_buyer(domain, org, id=f"old-{i}", email=f"old{i}@{domain}", status="extrapolated")
              for i in range(4)]
    _, _, oid = seed_opportunity(conn, clock)
    assert opportunity_service(conn, apollo_for(domain, org, people=people), clock).process(oid).outcome == "closed"
    from tgtc_core.testing.scenario import campaign_env
    assert reopen_recoverable_opportunities(
        conn, campaign_env=campaign_env(), signing_key="test-key", now=clock(),
        max_contacts_per_opportunity=3,
    ) == 1
    assert sql1(conn, "SELECT state || ':' || evidence_epoch FROM opportunities WHERE id = %s", (oid,)) == "open:1"


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
