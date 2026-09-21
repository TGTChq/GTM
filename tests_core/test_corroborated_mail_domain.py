"""A company's MAIL domain is not always its website domain.

Measured, production 2026-09-21: of 976 paid Apollo person matches, 252 did not
become contacts, and 222 of those were people with an Apollo-VERIFIED email and
VERIFIED current employment, rejected only as `email:domain_not_employer`.
Northern Trust is northerntrust.com on the web and ntrs.com in the inbox; JB
Poindexter is jbpco.com; GALE is galepartners.com; OTO Development posts from
careersatoto.com and mails from otodevelopment.com. In 219 of the 222, the
person's own Apollo organisation carried exactly our employer domain.

The rule accepts such an address only on deterministic evidence, and is
stricter than the existing alternate-domain rule:

* the email is Apollo-verified and current employment is verified (unchanged);
* the PERSON's own Apollo organisation domain is one of our employer domains;
* the domain is not free mail and not a hosted-service subdomain
  (groceryoutlet.zendesk.com is a support queue, not a person);
* the domain is not a FORMER employer's: its label must not name a past
  position at a DIFFERENT Apollo organisation (a promotion inside the same
  organisation is not a former employer);
* and it is corroborated -- another verified, currently employed person at the
  same employer uses it, or it keeps the employer domain's label under another
  suffix (aeva.com / aeva.ai, dncu.com / dncu.org).

Offline on the 222: 163 accepted across 51 employers; every uncorroborated
alternate stays rejected.
"""
from __future__ import annotations

from tgtc_core.domain.gates import MAIL_DOMAIN_ALIGNMENT, corroborated_mail_domain, evaluate_email

EMPLOYER = {"northerntrust.com"}


def _person(org_domain="northerntrust.com", history=None):
    return {"organization": {"primary_domain": org_domain, "name": "Northern Trust"},
            "employment_history": history if history is not None else [
                {"organization_id": "org-nt", "organization_name": "Northern Trust", "current": True}]}


def _ok(email, person=None, siblings=frozenset(), employer=EMPLOYER):
    return corroborated_mail_domain(email=email, person=person or _person(), employer_domains=employer,
                                    sibling_domains=siblings)


def test_a_cross_person_corroborated_mail_domain_is_accepted():
    assert _ok("a.person@ntrs.com", siblings={"ntrs.com"}) == (True, "cross_person")


def test_the_same_label_under_another_suffix_is_accepted():
    assert _ok("x@aeva.ai", person=_person("aeva.com"), employer={"aeva.com"}) == (True, "same_label")
    assert _ok("x@dncu.org", person=_person("dncu.com"), employer={"dncu.com"}) == (True, "same_label")


def test_an_uncorroborated_alternate_stays_rejected():
    assert _ok("x@ntrs.com") == (False, "uncorroborated")


def test_the_persons_apollo_org_must_be_our_employer():
    assert _ok("x@ntrs.com", person=_person("someoneelse.com"), siblings={"ntrs.com"}) == (False, "apollo_org_not_employer")


def test_a_hosted_service_subdomain_is_never_a_mail_domain():
    assert _ok("help@groceryoutlet.zendesk.com", person=_person("groceryoutlet.com"),
               employer={"groceryoutlet.com"}, siblings={"groceryoutlet.zendesk.com"})[0] is False


def test_free_mail_is_never_a_mail_domain():
    assert _ok("someone@gmail.com", siblings={"gmail.com"}) == (False, "hosted_or_free")


def test_a_former_employers_domain_is_rejected():
    history = [{"organization_id": "org-oto", "organization_name": "OTO Development", "current": True},
               {"organization_id": "org-hilton", "organization_name": "Hilton", "current": False}]
    person = _person("careersatoto.com", history)
    assert _ok("x@hilton.com", person=person, employer={"careersatoto.com"}, siblings={"hilton.com"}) == \
        (False, "former_employer_domain")


def test_a_past_role_at_the_same_organisation_is_not_a_former_employer():
    """A promotion leaves a past position at the SAME Apollo organisation."""
    history = [{"organization_id": "org-oto", "organization_name": "OTO Development", "current": True},
               {"organization_id": "org-oto", "organization_name": "OTO Development", "current": False}]
    person = _person("careersatoto.com", history)
    assert _ok("x@otodevelopment.com", person=person, employer={"careersatoto.com"},
               siblings={"otodevelopment.com"}) == (True, "cross_person")


def test_the_employer_domain_itself_is_not_an_alternate():
    assert _ok("x@northerntrust.com", siblings={"northerntrust.com"}) == (False, "not_alternate")


# --- evaluate_email wiring ----------------------------------------------------


def test_evaluate_email_labels_the_new_alignment():
    r = evaluate_email(email="a@ntrs.com", email_status="verified", employer_domains=EMPLOYER,
                       mail_domains={"ntrs.com"})
    assert r.passed and r.evidence["alignment"] == MAIL_DOMAIN_ALIGNMENT


def test_evaluate_email_still_requires_verification():
    r = evaluate_email(email="a@ntrs.com", email_status="guessed", employer_domains=EMPLOYER,
                       mail_domains={"ntrs.com"})
    assert not r.passed and r.reason.startswith("email:not_verified")


def test_evaluate_email_without_mail_domains_is_unchanged():
    r = evaluate_email(email="a@ntrs.com", email_status="verified", employer_domains=EMPLOYER)
    assert not r.passed and r.reason == "email:domain_not_employer"


def test_exact_domain_still_wins():
    r = evaluate_email(email="a@northerntrust.com", email_status="verified", employer_domains=EMPLOYER,
                       mail_domains={"ntrs.com"})
    assert r.evidence["alignment"] == "EXACT_EMPLOYER_DOMAIN"
