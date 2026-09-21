"""Stored, already-paid people that NOW pass the mail-domain rule are re-judged
without waiting out a 24-hour buyer-search deferral, and without paying again.

Only an opportunity whose employer holds such a person is released; any other
wait keeps its schedule. Guarded against churn: a unit released in the last six
hours is not released again.
"""
from __future__ import annotations

from datetime import timedelta

from tgtc_core.db import work_queue
from tgtc_core.domain.gates import MAIL_DOMAIN_FLAG_ENV
from tgtc_core.services.mail_domain_recheck import release_mail_domain_recoverable
from tests_core.helpers import sql1
from tests_core.seed import seed_opportunity

ON = {MAIL_DOMAIN_FLAG_ENV: "1"}


def _unit(conn, clock, *, candidate_domain, sibling_domains, waiting_on="buyer_search_pending:no_verified_buyer_in_candidates"):
    pid, eid, oid = seed_opportunity(conn, clock, domain="acme.com", org_name="Acme")
    work_queue.enqueue(conn, kind="qualify_opportunity", subject_kind="opportunity", subject_id=oid)
    with conn.cursor() as cur:
        cur.execute("UPDATE work_items SET state = 'waiting', waiting_on = %s, available_at = %s, updated_at = %s "
                    "WHERE subject_id = %s AND kind = 'qualify_opportunity'",
                    (waiting_on, clock() + timedelta(hours=20), clock() - timedelta(hours=7), oid))
        cur.execute(
            "INSERT INTO people (apollo_person_id, first_name, last_name, title, employer_id, email, email_status, "
            "organization_domain, facts_json) VALUES ('p-cand', 'C', 'Andidate', 'Director', %s, %s, 'verified', "
            "'acme.com', %s::jsonb)",
            (eid, f"c@{candidate_domain}",
             '{"email_alignment": "", "employment_verified": true, "enriched": {"organization": '
             '{"primary_domain": "acme.com", "name": "Acme"}, "employment_history": '
             '[{"organization_id": "o1", "organization_name": "Acme", "current": true}]}}'))
        for i, d in enumerate(sibling_domains):
            cur.execute("INSERT INTO people (apollo_person_id, first_name, last_name, title, employer_id, email, "
                        "email_status, organization_domain) VALUES (%s, 'S', 'Ib', 'VP', %s, %s, 'verified', 'acme.com')",
                        (f"p-sib-{i}", eid, f"s{i}@{d}"))
    conn.commit()
    return oid


def _available(conn, clock, oid):
    return sql1(conn, "SELECT available_at <= %s FROM work_items WHERE subject_id = %s AND kind = 'qualify_opportunity'",
                (clock(), oid))


def test_a_unit_with_a_now_recoverable_person_is_released(conn, clock):
    oid = _unit(conn, clock, candidate_domain="acmemail.com", sibling_domains=["acmemail.com", "acmemail.com"])
    assert release_mail_domain_recoverable(conn, now=clock(), env=ON) == 1
    assert _available(conn, clock, oid)


def test_nothing_is_released_with_the_flag_off(conn, clock):
    oid = _unit(conn, clock, candidate_domain="acmemail.com", sibling_domains=["acmemail.com", "acmemail.com"])
    assert release_mail_domain_recoverable(conn, now=clock(), env={}) == 0
    assert not _available(conn, clock, oid)


def test_an_uncorroborated_person_does_not_release_the_unit(conn, clock):
    oid = _unit(conn, clock, candidate_domain="acmemail.com", sibling_domains=["acmemail.com"])
    assert release_mail_domain_recoverable(conn, now=clock(), env=ON) == 0
    assert not _available(conn, clock, oid)


def test_a_wait_on_another_dependency_is_not_released(conn, clock):
    oid = _unit(conn, clock, candidate_domain="acmemail.com", sibling_domains=["acmemail.com", "acmemail.com"],
                waiting_on="spend_budget_exhausted:apollo:credits")
    assert release_mail_domain_recoverable(conn, now=clock(), env=ON) == 0


def test_a_unit_released_recently_is_not_released_again(conn, clock):
    oid = _unit(conn, clock, candidate_domain="acmemail.com", sibling_domains=["acmemail.com", "acmemail.com"])
    with conn.cursor() as cur:
        cur.execute("UPDATE work_items SET updated_at = %s WHERE subject_id = %s", (clock() - timedelta(hours=1), oid))
    conn.commit()
    assert release_mail_domain_recoverable(conn, now=clock(), env=ON) == 0
