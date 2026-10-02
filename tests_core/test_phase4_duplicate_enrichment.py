"""Phase 4 audit (2026-09-20), change 4: never pay twice for the same person.

Audit item: "duplicate enrichment attempts".

The guards that existed:

* ``_excluded_refs`` returns the candidates already JUDGED -- but scoped to
  ``opportunity_id``, so it says nothing about another opportunity;
* the global approval index excludes anyone already APPROVED anywhere;
* ``_reusable_person`` reuses a stored person instead of re-buying -- but its
  query is ``WHERE email_status = 'verified'``, so it only ever catches the
  people the purchase SUCCEEDED for.

Nothing covered the gap between them: a person who was paid for and then
REJECTED (no email returned, an extrapolated email, a territory mismatch).
An opportunity is one employer x FUNCTION, so one employer hiring in two
campaigns is two opportunities -- and the SAME person can be a candidate for
both. That was already true of the shared executive titles (a COO is in the
finance and the operations hierarchies); phase 4's third persona makes it the
normal case, because the Talent/People owner is now searched for all nine
campaigns. Each of those opportunities re-bought the person the other had
already paid to rule out. Every such call is a credit spent on an answer
already in the database.

The rule: a person enriched inside ``person_evidence_ttl_days`` is not bought
again. The stored evidence is re-judged for free by the SAME gates instead, so
a candidate is never silently lost -- and if the stored record does pass, the
contact is recovered at zero credits. After the TTL the evidence is stale and
a fresh purchase is allowed again, which is what the TTL is for.
"""
from __future__ import annotations

from tgtc_core.policy.requirements import rule
from tgtc_core.testing.fakes import make_person
from tests_core.helpers import opportunity_service, sql1, sqlall
from tests_core.seed import apollo_for, seed_opportunity


DOMAIN, ORG = "acme.com", "Acme"


#: The shared candidate: one person both campaigns legitimately search for.
SHARED_TITLE = "Chief People Officer"


def _unverified(pid="p-unv"):
    return make_person(id=pid, first="Un", last="Verified", title=SHARED_TITLE,
                       org_name=ORG, org_domain=DOMAIN, email=f"un@{DOMAIN}", email_status="extrapolated")


def _two_opportunities(conn, clock):
    """Two opportunities at ONE employer: an opportunity is employer x function,
    so two jobs in the same campaign collapse into one. Two campaigns do not."""
    _, _, first = seed_opportunity(conn, clock, function_key="finance", domain=DOMAIN, org_name=ORG, job_id="job-1")
    _, _, second = seed_opportunity(conn, clock, function_key="marketing", domain=DOMAIN, org_name=ORG, job_id="job-2")
    return first, second


def test_a_person_already_paid_for_and_rejected_is_not_bought_again(conn, clock):
    first, second = _two_opportunities(conn, clock)
    assert first and second and first != second
    fake = apollo_for(DOMAIN, ORG, people=[_unverified()])
    opportunity_service(conn, fake, clock).process(first)
    after_first = sql1(conn, "SELECT count(*) FROM request_attempts WHERE operation = 'person_match'")
    assert after_first == 1, "the first opportunity should have paid for this person exactly once"

    opportunity_service(conn, fake, clock).process(second)
    after_second = sql1(conn, "SELECT count(*) FROM request_attempts WHERE operation = 'person_match'")
    assert after_second == 1, "the second opportunity re-bought a person already paid for and rejected"


def test_the_stored_verdict_is_recorded_against_the_second_opportunity(conn, clock):
    """Not buying must not mean not deciding: the second opportunity records
    its own attempt, with the reason the stored evidence gives, so the funnel
    still reconciles (input = pass + reject + unknown + duplicate + error)."""
    first, second = _two_opportunities(conn, clock)
    fake = apollo_for(DOMAIN, ORG, people=[_unverified()])
    opportunity_service(conn, fake, clock).process(first)
    opportunity_service(conn, fake, clock).process(second)
    rows = sqlall(conn, "SELECT attempt_kind, outcome, reason FROM candidate_attempts "
                        "WHERE opportunity_id = %s AND candidate_ref = 'pid:p-unv'", (second,))
    assert rows, "the second opportunity recorded nothing at all about this candidate"
    by_kind = {r["attempt_kind"]: r for r in rows}
    assert by_kind["match"]["outcome"] == "reused_stored_evidence", rows
    assert by_kind["gate"]["outcome"] == "fail" and by_kind["gate"]["reason"].startswith("email:"), rows


def test_a_stored_person_who_now_passes_is_recovered_without_a_credit(conn, clock):
    """The same reuse path, in the direction that helps: if the stored evidence
    satisfies every gate, the second opportunity approves the contact for zero
    credits rather than re-buying an answer it already holds."""
    good = make_person(id="p-good", first="Good", last="Buyer", title=SHARED_TITLE,
                       org_name=ORG, org_domain=DOMAIN, email=f"good@{DOMAIN}", email_status="verified")
    first, second = _two_opportunities(conn, clock)
    fake = apollo_for(DOMAIN, ORG, people=[good])
    opportunity_service(conn, fake, clock).process(first)
    paid_after_first = sql1(conn, "SELECT count(*) FROM request_attempts WHERE operation = 'person_match'")
    out = opportunity_service(conn, fake, clock).process(second)
    assert sql1(conn, "SELECT count(*) FROM request_attempts WHERE operation = 'person_match'") == paid_after_first
    assert out.outcome in ("approved", "wait"), out
    # ... and the person is approved ONCE globally, not once per campaign.
    assert sql1(conn, "SELECT count(*) FROM approvals") == 1


def test_after_the_evidence_ttl_the_person_may_be_bought_again(conn, clock):
    """The TTL is the whole reason this is a deferral and not a blacklist:
    Apollo's index moves, and an extrapolated email can become a verified one.
    Past ``person_evidence_ttl_days`` a fresh purchase is allowed."""
    first, second = _two_opportunities(conn, clock)
    fake = apollo_for(DOMAIN, ORG, people=[_unverified()])
    opportunity_service(conn, fake, clock).process(first)
    assert sql1(conn, "SELECT count(*) FROM request_attempts WHERE operation = 'person_match'") == 1
    clock.advance(days=int(rule("person_evidence_ttl_days")) + 1)
    opportunity_service(conn, fake, clock).process(second)
    assert sql1(conn, "SELECT count(*) FROM request_attempts WHERE operation = 'person_match'") == 2
