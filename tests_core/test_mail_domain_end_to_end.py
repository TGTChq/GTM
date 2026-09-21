"""End to end on a real PostgreSQL: a corroborated mail domain approves only
with its flag on, and nothing else about the gate changes."""
from __future__ import annotations

from tests_core.helpers import opportunity_service, sql1
from tests_core.seed import apollo_for, good_buyer, seed_opportunity
from tgtc_core.domain.gates import MAIL_DOMAIN_ALIGNMENT, MAIL_DOMAIN_FLAG_ENV


def _setup(conn, clock, *, sibling_domain):
    pid, eid, oid = seed_opportunity(conn, clock, domain="acme.com", org_name="Acme")
    if sibling_domain:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO people (apollo_person_id, first_name, last_name, title, employer_id, email, email_status) "
                        "VALUES ('p-sibling', 'Other', 'Employee', 'Director', %s, %s, 'verified')",
                        (eid, f"other.employee@{sibling_domain}"))
        conn.commit()
    buyer = good_buyer("acme.com", "Acme", email="good.buyer@acmemail.com")
    return oid, apollo_for("acme.com", "Acme", people=[buyer])


def test_flag_off_the_alternate_mail_domain_is_still_rejected(conn, clock, monkeypatch):
    monkeypatch.delenv(MAIL_DOMAIN_FLAG_ENV, raising=False)
    oid, apollo = _setup(conn, clock, sibling_domain="acmemail.com")
    out = opportunity_service(conn, apollo, clock).process(oid)
    assert out.outcome != "approved"


def test_flag_on_a_cross_person_corroborated_mail_domain_approves(conn, clock, monkeypatch):
    monkeypatch.setenv(MAIL_DOMAIN_FLAG_ENV, "1")
    oid, apollo = _setup(conn, clock, sibling_domain="acmemail.com")
    out = opportunity_service(conn, apollo, clock).process(oid)
    assert out.outcome == "approved", out
    alignment = sql1(conn, "SELECT p.facts_json->>'email_alignment' FROM approvals a JOIN people p ON p.id = a.person_id "
                           "WHERE a.id = %s", (out.approval_id,))
    assert alignment == MAIL_DOMAIN_ALIGNMENT


def test_flag_on_without_corroboration_stays_rejected(conn, clock, monkeypatch):
    monkeypatch.setenv(MAIL_DOMAIN_FLAG_ENV, "1")
    oid, apollo = _setup(conn, clock, sibling_domain=None)
    out = opportunity_service(conn, apollo, clock).process(oid)
    assert out.outcome != "approved"


def test_flag_on_a_sibling_on_a_different_domain_does_not_corroborate(conn, clock, monkeypatch):
    monkeypatch.setenv(MAIL_DOMAIN_FLAG_ENV, "1")
    oid, apollo = _setup(conn, clock, sibling_domain="unrelated.com")
    out = opportunity_service(conn, apollo, clock).process(oid)
    assert out.outcome != "approved"
