"""End to end on a real PostgreSQL: a corroborated mail domain approves only
with its flag on, only on non-circular evidence, and nothing else about the gate
changes."""
from __future__ import annotations

from tests_core.helpers import opportunity_service, sql1
from tests_core.seed import apollo_for, good_buyer, seed_opportunity
from tgtc_core.domain.gates import MAIL_DOMAIN_ALIGNMENT, MAIL_DOMAIN_FLAG_ENV


def _setup(conn, clock, siblings):
    """siblings: list of (email_domain, alignment, organization_domain)."""
    pid, eid, oid = seed_opportunity(conn, clock, domain="acme.com", org_name="Acme")
    with conn.cursor() as cur:
        for i, (domain, alignment, org_domain) in enumerate(siblings):
            cur.execute(
                "INSERT INTO people (apollo_person_id, first_name, last_name, title, employer_id, email, email_status, "
                "organization_domain, facts_json) VALUES (%s, 'Other', 'Employee', 'Director', %s, %s, 'verified', %s, "
                "jsonb_build_object('email_alignment', %s::text))",
                (f"p-sib-{i}", eid, f"other.employee{i}@{domain}", org_domain, alignment))
    conn.commit()
    buyer = good_buyer("acme.com", "Acme", email="good.buyer@acmemail.com")
    return oid, apollo_for("acme.com", "Acme", people=[buyer])


TWO_INDEPENDENT = [("acmemail.com", "", "acme.com"), ("acmemail.com", "", "acme.com")]


def test_flag_off_the_alternate_mail_domain_is_still_rejected(conn, clock, monkeypatch):
    monkeypatch.delenv(MAIL_DOMAIN_FLAG_ENV, raising=False)
    oid, apollo = _setup(conn, clock, TWO_INDEPENDENT)
    assert opportunity_service(conn, apollo, clock).process(oid).outcome != "approved"


def test_two_independent_employees_approve_with_the_flag_on(conn, clock, monkeypatch):
    monkeypatch.setenv(MAIL_DOMAIN_FLAG_ENV, "1")
    oid, apollo = _setup(conn, clock, TWO_INDEPENDENT)
    out = opportunity_service(conn, apollo, clock).process(oid)
    assert out.outcome == "approved", out
    assert sql1(conn, "SELECT p.facts_json->>'email_alignment' FROM approvals a JOIN people p ON p.id = a.person_id "
                      "WHERE a.id = %s", (out.approval_id,)) == MAIL_DOMAIN_ALIGNMENT


def test_one_strict_seed_approves(conn, clock, monkeypatch):
    monkeypatch.setenv(MAIL_DOMAIN_FLAG_ENV, "1")
    oid, apollo = _setup(conn, clock, [("acmemail.com", "CORROBORATED_ALTERNATE_EMPLOYER_DOMAIN", "acme.com")])
    assert opportunity_service(conn, apollo, clock).process(oid).outcome == "approved"


def test_one_independent_employee_is_not_enough(conn, clock, monkeypatch):
    monkeypatch.setenv(MAIL_DOMAIN_FLAG_ENV, "1")
    oid, apollo = _setup(conn, clock, [("acmemail.com", "", "acme.com")])
    assert opportunity_service(conn, apollo, clock).process(oid).outcome != "approved"


def test_contacts_accepted_by_the_rule_never_corroborate(conn, clock, monkeypatch):
    monkeypatch.setenv(MAIL_DOMAIN_FLAG_ENV, "1")
    oid, apollo = _setup(conn, clock, [("acmemail.com", MAIL_DOMAIN_ALIGNMENT, "acme.com")] * 2)
    assert opportunity_service(conn, apollo, clock).process(oid).outcome != "approved"


def test_siblings_on_a_different_domain_do_not_corroborate(conn, clock, monkeypatch):
    monkeypatch.setenv(MAIL_DOMAIN_FLAG_ENV, "1")
    oid, apollo = _setup(conn, clock, [("unrelated.com", "", "acme.com")] * 2)
    assert opportunity_service(conn, apollo, clock).process(oid).outcome != "approved"


def test_the_reuse_path_records_the_relaxed_alignment_so_it_cannot_seed_others(conn, clock, monkeypatch):
    """A person first rejected, then recovered on the REUSE path (no second
    payment), must be stored as CORROBORATED_MAIL_DOMAIN. Otherwise the record
    would still read as an unaligned independent employee and could validate
    another contact under the same rule -- exactly the circularity forbidden."""
    monkeypatch.delenv(MAIL_DOMAIN_FLAG_ENV, raising=False)
    oid, apollo = _setup(conn, clock, [])
    first = opportunity_service(conn, apollo, clock).process(oid)
    assert first.outcome != "approved"
    matches_before = len([r for r in apollo.requests if "match" in str(r)]) if hasattr(apollo, "requests") else None

    eid = sql1(conn, "SELECT employer_id FROM opportunities WHERE id = %s", (oid,))
    with conn.cursor() as cur:
        for i in range(2):
            cur.execute("INSERT INTO people (apollo_person_id, first_name, last_name, title, employer_id, email, "
                        "email_status, organization_domain) VALUES (%s, 'S', 'Ib', 'VP', %s, %s, 'verified', 'acme.com')",
                        (f"p-late-{i}", eid, f"s{i}@acmemail.com"))
    conn.commit()
    monkeypatch.setenv(MAIL_DOMAIN_FLAG_ENV, "1")
    second = opportunity_service(conn, apollo, clock).process(oid)
    assert second.outcome == "approved", second
    assert sql1(conn, "SELECT p.facts_json->>'email_alignment' FROM approvals a JOIN people p ON p.id = a.person_id "
                      "WHERE a.id = %s", (second.approval_id,)) == MAIL_DOMAIN_ALIGNMENT
    if matches_before is not None:
        assert len([r for r in apollo.requests if "match" in str(r)]) == matches_before, "paid twice"
