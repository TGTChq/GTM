"""Public suffix, ATS host, aggregator, shortener, alias corroboration, employer mismatch."""

from __future__ import annotations

from tgtc_core.domain import identity as ident
from tgtc_core.services.identity_service import resolve_employer
from tests_core.helpers import sqlall


def test_public_suffix_and_subdomains_reduce_to_registrable_domain():
    assert ident.safe_employer_domain("https://careers.acme.co.uk/jobs") == "acme.co.uk"
    assert ident.safe_employer_domain("https://investor.capitalone.com/news") == "capitalone.com"


def test_ats_aggregator_and_shortener_hosts_are_never_an_employer():
    for host in ("https://boards.greenhouse.io/acme", "https://acme.myworkdayjobs.com/x", "https://www.linkedin.com/company/acme",
                 "https://bit.ly/abc", "https://wellfound.com/company/acme", "https://gmail.com"):
        assert ident.safe_employer_domain(host) == "", host


def test_placeholder_names_are_not_identities():
    assert ident.employer_anchors({"organization": "Confidential Company"}) == ("", "", "")
    assert ident.employer_anchors({"organization": "Acme Inc.", "organization_url": "https://acme.com"}) == ("acme.com", "", "acme")


def test_email_rules():
    assert ident.is_generic_mailbox("info@acme.com") and not ident.is_generic_mailbox("jane.doe@acme.com")
    assert ident.email_on_domains("jane@acme.com", {"acme.com"})
    assert not ident.email_on_domains("jane@gmail.com", {"gmail.com"})
    assert not ident.email_on_domains("jane@acme.com", {"other.com"})


def test_domain_name_consistency_is_constructive_not_similarity():
    assert ident.domain_name_consistent("BS&B Safety Systems", "bsbsystems.com") or ident.domain_name_consistent("BS&B Safety Systems", "bsbsafetysystems.com")
    assert ident.domain_name_consistent("Acme Robotics", "acmerobotics.com")
    assert not ident.domain_name_consistent("Great Minds DC", "californiaconstructores.com")


def test_resolve_employer_matches_by_domain_then_slug_never_by_similar_name(conn):
    a, _, r = resolve_employer(conn, org={"organization": "Acme", "organization_url": "https://acme.com", "org_linkedin_slug": "acme"}, source="t")
    conn.commit()
    assert r == "employer_created"
    # same slug, careers domain -> same employer (domain normalised), no second employer
    b, _, r2 = resolve_employer(conn, org={"organization": "Acme", "organization_url": "https://careers.acme.com", "org_linkedin_slug": "acme"}, source="t")
    conn.commit()
    assert b == a and r2 == "employer_domain"
    # a different company with a similar name is a different employer
    c, _, _ = resolve_employer(conn, org={"organization": "Acme Group", "organization_url": "https://acmegroup.io", "org_linkedin_slug": "acmegroup"}, source="t")
    conn.commit()
    assert c != a
    assert len(sqlall(conn, "SELECT id FROM employers")) == 2


def test_second_domain_under_one_slug_needs_name_consistency(conn):
    a, _, _ = resolve_employer(conn, org={"organization": "Kai Security", "organization_url": "https://kai.security", "org_linkedin_slug": "kaisecurity"}, source="t")
    conn.commit()
    b, _, r = resolve_employer(conn, org={"organization": "Kai Security", "organization_url": "https://kaisecurity.com", "org_linkedin_slug": "kaisecurity"}, source="t")
    conn.commit()
    assert b == a and r == "employer_linkedin_slug"
    aliases = {x["alias_value"] for x in sqlall(conn, "SELECT alias_value FROM employer_aliases WHERE employer_id = %s AND alias_kind = 'domain'", (a,))}
    assert aliases == {"kai.security", "kaisecurity.com"}
    # an unrelated domain under the same slug is recorded as evidence, NOT an alias
    c, _, _ = resolve_employer(conn, org={"organization": "Kai Security", "organization_url": "https://totallydifferent.com", "org_linkedin_slug": "kaisecurity"}, source="t")
    conn.commit()
    assert c == a
    aliases2 = {x["alias_value"] for x in sqlall(conn, "SELECT alias_value FROM employer_aliases WHERE employer_id = %s AND alias_kind = 'domain'", (a,))}
    assert "totallydifferent.com" not in aliases2
    ev = sqlall(conn, "SELECT fact FROM evidence WHERE subject_kind = 'employer' AND subject_id = %s", (a,))
    assert any(e["fact"] == "domain_disagreement" for e in ev)


def test_parent_and_subsidiary_stay_separate_employers(conn):
    p, _, _ = resolve_employer(conn, org={"organization": "Alphabet", "organization_url": "https://abc.xyz", "org_linkedin_slug": "alphabet"}, source="t")
    s, _, _ = resolve_employer(conn, org={"organization": "Google", "organization_url": "https://google.com", "org_linkedin_slug": "google"}, source="t")
    conn.commit()
    assert p != s


def test_no_anchor_is_unresolved(conn):
    eid, key, reason = resolve_employer(conn, org={"organization": "Undisclosed", "organization_url": "https://www.linkedin.com/company/x"}, source="t")
    conn.commit()
    assert eid is None and key == "" and reason == "employer_identity_unresolved"
