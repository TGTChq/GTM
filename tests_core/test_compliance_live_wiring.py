"""The country gates ON the live pipeline, not beside it.

Reachability, stated plainly because this branch has already caught one false
reachability claim. What this file exercises is the real call graph:

* ``services.identity_service.resolve_employer`` -- reached from
  ``resolve_posting_identity``, i.e. ``Runner.work("resolve_identity")`` --
  stores the employer's own country, legal form and corporate-subscriber
  verdict;
* ``services.opportunity.OpportunityService.process`` -- reached from
  ``Runner.work("qualify_opportunity")`` -- refuses PAID person enrichment for
  an employer in a country where the matrix says `no`, stores the enriched
  person's own country, and writes the compliance decision onto the approval;
* ``services.delivery.DeliveryService._precheck`` -- reached from
  ``Runner.deliver`` via ``drain`` -- refuses to send a lead that is not
  outreach-eligible, including a legacy approval whose verdict is NULL.

What is NOT wired, and deliberately: ``job_acquisition_allowed`` and
``aggregate_capacity_modelling_allowed``. Both are `yes` for all five researched
geographies, so enforcing them could only ever delete rows for a country the
matrix does not cover -- and "never delete it, never silently count it" is the
rule. They are an API and a recorded field.
"""

from __future__ import annotations

import pytest

from tgtc_core.db.connection import jsonb
from tgtc_core.domain import approval as ap
from tgtc_core.domain import jurisdiction as ju
from tgtc_core.policy import compliance as c
from tgtc_core.services.metrics import ledger
from tgtc_core.testing.fakes import FakeAirtable, FakeInstantly, make_person
from tgtc_core.testing.scenario import CONTROL_ID_BY_CAMPAIGN_KEY
from tests_core.helpers import delivery_service, opportunity_service, sql1, sqlall
from tests_core.seed import apollo_for, good_buyer, seed_opportunity

CS = CONTROL_ID_BY_CAMPAIGN_KEY["customer_experience"]

#: A deployment that HAS done the UK groundwork. Absent, UK fails closed --
#: which is the default, and its own test below.
UK_CONFIGURED = dict(outreach_legal_basis="legitimate_interests",
                     outreach_legal_basis_evidence="lia-2026-09-20",
                     outreach_privacy_notice_configured=True)


# ---------------------------------------------------------------------------
# Observation helpers: provider shapes -> stored jurisdiction facts.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("countries,expected", [
    (["United States"], "US"), (["US"], "US"), (["United Kingdom"], "UK"), (["Germany"], "DE"),
    ([], ""), (["France"], ""), (["United States", "United Kingdom"], ""), (["US", "USA"], "US"),
])
def test_observe_job_country(countries, expected):
    """Two DIFFERENT matrix countries on one posting is ambiguous, which is
    unknown, which fails closed -- not "pick the first one"."""
    assert ju.observe_job_country(countries) == expected


@pytest.mark.parametrize("org,expected", [
    ({"org_linkedin_locations": ["Silverdale Road, Earley, Reading, RG6 7HS, GB"]}, "UK"),
    ({"org_linkedin_locations": ["1 Market St, San Francisco, CA, US"]}, "US"),
    ({"org_linkedin_locations": ["Berlin, DE", "Munich, DE"]}, "DE"),
    ({"org_linkedin_locations": ["Berlin, DE", "London, GB"]}, ""),
    ({"org_linkedin_locations": ["Reading"]}, ""),
    ({}, ""),
])
def test_observe_company_country(org, expected):
    assert ju.observe_company_country(org) == expected


@pytest.mark.parametrize("org,expected_class", [
    ({"organization": "Acme Ltd"}, c.CORPORATE),
    ({"organization": "Acme Limited"}, c.CORPORATE),
    ({"org_linkedin_name": "Acme LLP"}, c.CORPORATE),
    ({"organization": "Acme Trust"}, c.NOT_CORPORATE),
    ({"organization": "Acme", "org_linkedin_type": "Sole Proprietorship"}, c.NOT_CORPORATE),
    ({"organization": "Acme", "org_linkedin_type": "Privately Held"}, c.ENTITY_UNKNOWN),
    ({"organization": "Acme"}, c.ENTITY_UNKNOWN),
    ({}, c.ENTITY_UNKNOWN),
])
def test_observe_legal_entity_type(org, expected_class):
    assert c.classify_corporate_subscriber(ju.observe_legal_entity_type(org)) == expected_class


def test_an_explicit_exclusion_beats_a_corporate_sounding_name():
    """Fail closed when the two sources disagree: a declared sole
    proprietorship is not made corporate by a name that ends in "Ltd"."""
    org = {"organization": "Acme Ltd", "org_linkedin_type": "Sole Proprietorship"}
    assert c.classify_corporate_subscriber(ju.observe_legal_entity_type(org)) == c.NOT_CORPORATE


def test_observe_contact_country_reads_the_person_never_the_job():
    assert ju.observe_contact_country({"country": "Germany"}) == "DE"
    assert ju.observe_contact_country({}) == ""


# ---------------------------------------------------------------------------
# Live: the employer's jurisdiction is stored at identity resolution.
# ---------------------------------------------------------------------------

def test_identity_resolution_stores_the_employers_own_country_and_legal_form(conn, clock):
    _pid, eid, _oid = seed_opportunity(conn, clock, org_name="Acme Ltd", domain="acme.co.uk",
                                       org_linkedin_locations=["1 High St, London, GB"])
    row = sqlall(conn, "SELECT company_country, employer_legal_entity_type, corporate_subscriber_status "
                       "FROM employers WHERE id = %s", (eid,))[0]
    assert row["company_country"] == "UK"
    assert c.classify_corporate_subscriber(row["employer_legal_entity_type"]) == c.CORPORATE
    assert row["corporate_subscriber_status"] == c.CORPORATE


def test_an_employer_with_no_country_evidence_stores_none_not_a_guess(conn, clock):
    """"Stored, never inferred": the posting says the JOB is in the US, and
    that must not become the company's country."""
    _pid, eid, _oid = seed_opportunity(conn, clock, org_name="Acme", domain="acme.com")
    row = sqlall(conn, "SELECT company_country, corporate_subscriber_status FROM employers WHERE id = %s", (eid,))[0]
    assert row["company_country"] is None
    assert row["corporate_subscriber_status"] == c.ENTITY_UNKNOWN


# ---------------------------------------------------------------------------
# Live: person enrichment is refused where the matrix says `no`, BEFORE spend.
# ---------------------------------------------------------------------------

def test_a_german_employer_never_reaches_a_paid_apollo_call(conn, clock):
    _pid, _eid, oid = seed_opportunity(conn, clock, org_name="Acme GmbH", domain="acme.de",
                                       org_linkedin_locations=["Friedrichstrasse 1, Berlin, DE"])
    fake = apollo_for("acme.de", "Acme GmbH")
    out = opportunity_service(conn, fake, clock).process(oid)
    assert out.outcome == "closed"
    assert out.reason == "compliance:person_enrichment_not_permitted:DE"
    assert fake.requests == [], "a blocked jurisdiction must cost nothing"


@pytest.mark.parametrize("domain,locations,country", [
    ("acme.de", ["Berlin, DE"], "DE"),
    ("acme.ae", ["Dubai, AE"], "AE"),
    ("acme.sa", ["Riyadh, SA"], "SA"),
])
def test_every_blocked_country_closes_the_opportunity_with_a_named_reason_and_keeps_it(conn, clock, domain, locations, country):
    """Retained for capacity: the employer, the posting and the opportunity row
    all survive. Only the paid enrichment is refused, and the reason is named."""
    pid, eid, oid = seed_opportunity(conn, clock, org_name=f"Acme {country}", domain=domain,
                                     org_linkedin_locations=locations)
    fake = apollo_for(domain, f"Acme {country}")
    opportunity_service(conn, fake, clock).process(oid)
    assert sql1(conn, "SELECT close_reason FROM opportunities WHERE id = %s", (oid,)) == \
        f"compliance:person_enrichment_not_permitted:{country}"
    assert sql1(conn, "SELECT count(*) FROM opportunities WHERE id = %s", (oid,)) == 1
    assert sql1(conn, "SELECT count(*) FROM postings WHERE id = %s", (pid,)) == 1
    assert sql1(conn, "SELECT count(*) FROM employers WHERE id = %s", (eid,)) == 1


def test_an_unknown_company_country_does_not_block_enrichment(conn, clock):
    """Fail closed for SENDING, open for capacity. An employer whose country
    was never observed must not halt the pipeline -- the send gate downstream
    is the one that fails closed, on the CONTACT's own jurisdiction."""
    _pid, _eid, oid = seed_opportunity(conn, clock)
    fake = apollo_for("acme.com", "Acme")
    assert opportunity_service(conn, fake, clock).process(oid).outcome == "approved"


# ---------------------------------------------------------------------------
# Live: the compliance decision is written onto the approval.
# ---------------------------------------------------------------------------

def _approve_one(conn, clock, *, person=None, org_name="Acme", domain="acme.com", posting_country="US",
                 locations=None, **svc):
    extra = {"org_linkedin_locations": locations} if locations else {}
    _pid, _eid, oid = seed_opportunity(conn, clock, org_name=org_name, domain=domain,
                                       countries=(posting_country,), **extra)
    fake = apollo_for(domain, org_name, people=[person] if person else None)
    out = opportunity_service(conn, fake, clock, **svc).process(oid)
    rows = sqlall(conn, "SELECT * FROM approvals ORDER BY id")
    return out, rows


def test_an_approved_us_lead_carries_every_jurisdiction_field_and_is_eligible(conn, clock):
    out, rows = _approve_one(conn, clock)
    assert out.outcome == "approved" and len(rows) == 1
    row = rows[0]
    assert row["outreach_eligible"] is True
    assert row["outreach_block_reason"] is None
    assert row["compliance_rule_version"] == c.COMPLIANCE_RULE_VERSION
    assert row["job_country"] == "US"
    assert row["contact_country"] == "US"
    assert row["opt_out_status"] == c.OPT_OUT_NONE
    # and both outbox items are sendable
    assert sorted(r["state"] for r in sqlall(conn, "SELECT state FROM delivery_outbox")) == ["pending", "pending"]


def test_a_contact_with_no_country_fails_closed_but_is_still_approved_and_counted(conn, clock):
    """Requirement D exactly: not outreach-eligible, never deleted, never
    silently counted -- the approval row exists, carries the named reason, and
    neither outbox item is sendable."""
    stateless = make_person(id="p-nc", first="No", last="Country", title="VP Customer Success",
                            org_name="Acme", org_domain="acme.com", email="no.country@acme.com",
                            email_status="verified", country="")
    out, rows = _approve_one(conn, clock, person=stateless)
    assert out.outcome == "approved" and len(rows) == 1
    assert rows[0]["outreach_eligible"] is False
    assert rows[0]["contact_country"] is None
    assert rows[0]["outreach_block_reason"].startswith("compliance:unknown_jurisdiction")
    assert {r["state"] for r in sqlall(conn, "SELECT state FROM delivery_outbox")} == {"blocked"}
    assert {r["blocked_reason"] for r in sqlall(conn, "SELECT blocked_reason FROM delivery_outbox")} == \
        {rows[0]["outreach_block_reason"]}


@pytest.mark.parametrize("country", ["Germany", "United Arab Emirates", "Saudi Arabia"])
def test_a_blocked_country_contact_at_a_us_company_can_never_reach_ready_to_send(conn, clock, country):
    """The contact's jurisdiction decides, not the job's or the company's. A US
    company posting a US job with a German contact is a German contact."""
    person = make_person(id="p-de", first="Blocked", last="Buyer", title="VP Customer Success",
                         org_name="Acme", org_domain="acme.com", email="blocked.buyer@acme.com",
                         email_status="verified", country=country)
    out, rows = _approve_one(conn, clock, person=person)
    assert out.outcome == "approved"
    assert rows[0]["contact_country"] == c.normalize_country(country)
    assert rows[0]["outreach_eligible"] is False
    assert rows[0]["outreach_block_reason"] == f"compliance:cold_email_not_permitted:{c.normalize_country(country)}"
    counts = ledger(conn)
    assert counts["outreach_eligible_contacts"] == 0
    assert counts["compliance_blocked_contacts"] == 1
    # and the send path refuses it too, not merely the approval path
    svc = delivery_service(conn, FakeAirtable(), FakeInstantly(campaign_status={CS: 1}), clock)
    assert svc.drain("instantly") == [] and svc.drain("airtable") == []


def test_a_uk_contact_without_the_uk_groundwork_fails_closed(conn, clock):
    person = make_person(id="p-uk", first="UK", last="Buyer", title="VP Customer Success",
                         org_name="Acme Ltd", org_domain="acme.co.uk", email="uk.buyer@acme.co.uk",
                         email_status="verified", country="United Kingdom")
    _out, rows = _approve_one(conn, clock, person=person, org_name="Acme Ltd", domain="acme.co.uk",
                              posting_country="GB", locations=["1 High St, London, GB"])
    assert rows[0]["outreach_eligible"] is False
    assert rows[0]["outreach_block_reason"] == "compliance:uk:no_lawful_basis_record"


def test_a_uk_contact_at_an_unverifiable_entity_fails_closed_even_with_the_groundwork(conn, clock):
    """"An unknown entity type is never treated as corporate." "Acme" names no
    legal form, so it is not a verified corporate subscriber."""
    person = make_person(id="p-uk2", first="UK", last="Buyer", title="VP Customer Success",
                         org_name="Acme", org_domain="acme.co.uk", email="uk.buyer@acme.co.uk",
                         email_status="verified", country="United Kingdom")
    _out, rows = _approve_one(conn, clock, person=person, org_name="Acme", domain="acme.co.uk",
                              posting_country="GB", locations=["1 High St, London, GB"], **UK_CONFIGURED)
    assert rows[0]["corporate_subscriber_status"] == c.ENTITY_UNKNOWN
    assert rows[0]["outreach_eligible"] is False
    assert rows[0]["outreach_block_reason"] == "compliance:uk:not_a_verified_corporate_subscriber"


def test_a_uk_corporate_subscriber_with_the_groundwork_is_eligible(conn, clock):
    person = make_person(id="p-uk3", first="UK", last="Buyer", title="VP Customer Success",
                         org_name="Acme Ltd", org_domain="acme.co.uk", email="uk.buyer@acme.co.uk",
                         email_status="verified", country="United Kingdom")
    _out, rows = _approve_one(conn, clock, person=person, org_name="Acme Ltd", domain="acme.co.uk",
                              posting_country="GB", locations=["1 High St, London, GB"], **UK_CONFIGURED)
    assert rows[0]["corporate_subscriber_status"] == c.CORPORATE
    assert rows[0]["legal_basis"] == "legitimate_interests"
    assert rows[0]["legal_basis_evidence"] == "lia-2026-09-20"
    assert rows[0]["privacy_notice_due_at"] is not None
    assert rows[0]["outreach_eligible"] is True, rows[0]["outreach_block_reason"]


def test_the_job_country_is_recorded_but_never_decides_the_contact_gate(conn, clock):
    """A German job, a US contact: the recorded job_country is DE and the lead
    is still eligible, because the person gates read contact_country."""
    person = good_buyer("acme.com", "Acme")
    _out, rows = _approve_one(conn, clock, person=person, posting_country="DE")
    assert rows[0]["job_country"] == "DE" and rows[0]["contact_country"] == "US"
    assert rows[0]["outreach_eligible"] is True


# ---------------------------------------------------------------------------
# Live: the delivery pre-check is the second, independent refusal.
# ---------------------------------------------------------------------------

def test_delivery_refuses_a_legacy_approval_whose_verdict_is_null(conn, clock):
    """A lead approved before the gates existed carries NULL, which is unknown,
    which is never "yes"."""
    _out, rows = _approve_one(conn, clock)
    with conn.cursor() as cur:
        cur.execute("UPDATE approvals SET outreach_eligible = NULL, outreach_block_reason = NULL")
        cur.execute("UPDATE delivery_outbox SET state = 'pending', blocked_reason = NULL")
    conn.commit()
    svc = delivery_service(conn, FakeAirtable(), FakeInstantly(campaign_status={CS: 1}), clock)
    outcomes = svc.drain("instantly")
    assert [(o.outcome, o.reason) for o in outcomes] == [("blocked", "compliance:outreach_eligibility_unknown")]
    assert sql1(conn, "SELECT count(*) FROM delivery_receipts WHERE receipt_kind IN ('created','reconciled')") == 0


def test_the_approval_writer_and_the_delivery_precheck_share_one_predicate():
    """Share a predicate, never copy it. Both call sites ask
    ``domain.approval.outreach_blocked_reason``."""
    assert ap.outreach_blocked_reason({"outreach_eligible": True}) == ""
    assert ap.outreach_blocked_reason({"outreach_eligible": False, "outreach_block_reason": "compliance:x"}) == "compliance:x"
    assert ap.outreach_blocked_reason({}) == "compliance:outreach_eligibility_unknown"
    assert ap.outreach_blocked_reason({"outreach_eligible": None}) == "compliance:outreach_eligibility_unknown"


# ---------------------------------------------------------------------------
# Counting: separate buckets that reconcile, never one inflated total.
# ---------------------------------------------------------------------------

def test_the_ledger_reports_the_two_compliance_buckets_separately_and_they_reconcile(conn, clock):
    blocked = make_person(id="p-de2", first="Blocked", last="Buyer", title="VP Customer Success",
                          org_name="Beta", org_domain="beta.com", email="blocked@beta.com",
                          email_status="verified", country="Germany")
    _approve_one(conn, clock)
    _pid, _eid, oid = seed_opportunity(conn, clock, org_name="Beta", domain="beta.com", job_id="job-2")
    opportunity_service(conn, apollo_for("beta.com", "Beta", people=[blocked]), clock).process(oid)
    counts = ledger(conn)
    assert counts["approved_distinct"] == 2
    assert counts["outreach_eligible_contacts"] == 1
    assert counts["compliance_blocked_contacts"] == 1
    # Reconciliation: the two buckets are exact complements of the approved set.
    assert counts["outreach_eligible_contacts"] + counts["compliance_blocked_contacts"] == counts["approved_distinct"]
    assert counts["compliance_blocked_by_reason"] == {"compliance:cold_email_not_permitted:DE": 1}
    assert counts["approved_by_contact_country"] == {"US": 1, "DE": 1}
    assert counts["outreach_eligible_by_contact_country"] == {"US": 1}


def test_the_ledger_reports_every_rule_version_present_not_just_one(conn, clock):
    """A database holding approvals decided under two different rule versions
    must show both. Collapsing them to one value would let a figure be read as
    if the whole set had been decided under the newer rules."""
    _approve_one(conn, clock)
    assert ledger(conn)["compliance_rule_versions"] == {c.COMPLIANCE_RULE_VERSION: 1}
    with conn.cursor() as cur:
        cur.execute("UPDATE approvals SET compliance_rule_version = NULL")
    conn.commit()
    assert ledger(conn)["compliance_rule_versions"] == {"unversioned": 1}


def test_the_ledger_never_reports_a_ready_to_send_total_that_includes_a_blocked_country(conn, clock):
    """The one number this whole task exists to make impossible."""
    for i, country in enumerate(("Germany", "United Arab Emirates", "Saudi Arabia")):
        person = make_person(id=f"p-{i}", first="B", last="Buyer", title="VP Customer Success",
                             org_name=f"Co{i}", org_domain=f"co{i}.com", email=f"b@co{i}.com",
                             email_status="verified", country=country)
        _pid, _eid, oid = seed_opportunity(conn, clock, org_name=f"Co{i}", domain=f"co{i}.com", job_id=f"job-{i}")
        opportunity_service(conn, apollo_for(f"co{i}.com", f"Co{i}", people=[person]), clock).process(oid)
    counts = ledger(conn)
    assert counts["approved_distinct"] == 3
    assert counts["outreach_eligible_contacts"] == 0
    assert counts["compliance_blocked_contacts"] == 3
    assert set(counts["approved_by_contact_country"]) == {"DE", "AE", "SA"}
    assert counts["outreach_eligible_by_contact_country"] == {}
    svc = delivery_service(conn, FakeAirtable(), FakeInstantly(campaign_status={CS: 1}), clock)
    assert svc.drain("instantly") == []
    assert sql1(conn, "SELECT count(*) FROM delivery_receipts") == 0
