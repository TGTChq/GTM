"""A company's MAIL domain is not always its website domain -- accepted only on
NON-CIRCULAR evidence.

Measured, production 2026-09-21: 222 of 976 paid Apollo matches were
Apollo-verified, currently employed people rejected only as
`email:domain_not_employer` (Northern Trust mails from ntrs.com, GALE from
galepartners.com).

Safety correction (Luis, 2026-09-21), before any live canary:

* same company label under another TLD is NOT sufficient by itself;
* corroboration cannot be circular: a contact accepted by this rule never
  validates another contact under this rule;
* the seed must be one of --
  1. provider_confirmed: the person's Apollo organisation record itself carries
     the domain (primary_domain, website_url, or a suborganisation's domain);
  2. strict_seed: another contact at the employer that passed the STRICT rule
     (EXACT or CORROBORATED_ALTERNATE alignment) on that domain;
  3. independent_employees: at least TWO OTHER Apollo-verified current employees
     at the employer on that domain, each with an Apollo organisation matching
     the employer, none of them accepted by this rule;
* free mail, SaaS support, ATS / job-board and former-employer domains stay
  rejected, and so does an explicit current-employer disagreement.
"""
from __future__ import annotations

from tgtc_core.domain.gates import MAIL_DOMAIN_ALIGNMENT, corroborated_mail_domain, evaluate_email

EMPLOYER = {"northerntrust.com"}
NAME = "Northern Trust"


def _person(org_domain="northerntrust.com", history=None, **org_extra):
    org = {"primary_domain": org_domain, "name": NAME, **org_extra}
    return {"organization": org,
            "employment_history": history if history is not None else [
                {"organization_id": "org-nt", "organization_name": NAME, "current": True}]}


def _sib(domain, alignment="", org_domain="northerntrust.com"):
    return {"email_domain": domain, "alignment": alignment, "org_domain": org_domain}


def _ok(email, person=None, siblings=(), employer=EMPLOYER, name=NAME):
    return corroborated_mail_domain(email=email, person=person or _person(), employer_domains=employer,
                                    employer_name=name, siblings=siblings)


# --- the three admissible seeds -----------------------------------------------


def test_provider_confirmed_by_the_org_record_website():
    person = _person(website_url="https://www.ntrs.com")
    assert _ok("a@ntrs.com", person=person) == (True, "provider_confirmed")


def test_provider_confirmed_by_a_suborganisation():
    person = _person(suborganizations=[{"name": "NT Securities", "website_url": "http://ntrs.com"}])
    assert _ok("a@ntrs.com", person=person) == (True, "provider_confirmed")


def test_a_strict_seed_is_enough():
    assert _ok("a@ntrs.com", siblings=[_sib("ntrs.com", "CORROBORATED_ALTERNATE_EMPLOYER_DOMAIN")]) == \
        (True, "strict_seed")


def test_two_independent_current_employees_are_enough():
    sibs = [_sib("ntrs.com"), _sib("ntrs.com")]
    assert _ok("a@ntrs.com", siblings=sibs) == (True, "independent_employees")


def test_one_independent_employee_is_not_enough():
    assert _ok("a@ntrs.com", siblings=[_sib("ntrs.com")]) == (False, "uncorroborated")


# --- non-circularity ------------------------------------------------------------


def test_contacts_accepted_by_this_rule_never_corroborate():
    sibs = [_sib("ntrs.com", MAIL_DOMAIN_ALIGNMENT), _sib("ntrs.com", MAIL_DOMAIN_ALIGNMENT)]
    assert _ok("a@ntrs.com", siblings=sibs) == (False, "uncorroborated")


def test_a_relaxed_contact_does_not_top_up_an_independent_one():
    sibs = [_sib("ntrs.com"), _sib("ntrs.com", MAIL_DOMAIN_ALIGNMENT)]
    assert _ok("a@ntrs.com", siblings=sibs) == (False, "uncorroborated")


def test_siblings_whose_apollo_org_is_not_the_employer_do_not_count():
    sibs = [_sib("ntrs.com", org_domain="other.com"), _sib("ntrs.com", org_domain="other.com")]
    assert _ok("a@ntrs.com", siblings=sibs) == (False, "uncorroborated")


# --- same label is insufficient by itself ---------------------------------------


def test_same_label_other_tld_alone_is_rejected_as_insufficient():
    assert _ok("x@aeva.ai", person=_person("aeva.com"), employer={"aeva.com"}, name="Aeva") == \
        (False, "same_label_insufficient")


def test_same_label_with_real_corroboration_is_accepted_on_that_corroboration():
    sibs = [_sib("aeva.ai", org_domain="aeva.com"), _sib("aeva.ai", org_domain="aeva.com")]
    assert _ok("x@aeva.ai", person=_person("aeva.com"), employer={"aeva.com"}, name="Aeva", siblings=sibs) == \
        (True, "independent_employees")


# --- domains that are never a person's mail domain ------------------------------


def test_hosted_support_subdomain_is_rejected():
    sibs = [_sib("groceryoutlet.zendesk.com", org_domain="groceryoutlet.com")] * 2
    assert _ok("help@groceryoutlet.zendesk.com", person=_person("groceryoutlet.com"),
               employer={"groceryoutlet.com"}, siblings=sibs)[1] == "hosted_or_free"


def test_free_mail_is_rejected():
    assert _ok("someone@gmail.com", siblings=[_sib("gmail.com")] * 2) == (False, "hosted_or_free")


def test_ats_and_job_board_domains_are_rejected():
    for d in ("greenhouse.io", "myworkdayjobs.com", "lever.co", "indeed.com", "acmecareers.com"):
        assert _ok(f"x@{d}", siblings=[_sib(d)] * 2)[1] == "ats_or_job_board", d


def test_a_former_employers_domain_is_rejected():
    history = [{"organization_id": "org-oto", "organization_name": "OTO Development", "current": True},
               {"organization_id": "org-hilton", "organization_name": "Hilton", "current": False}]
    person = _person("careersatoto.com", history)
    assert _ok("x@hilton.com", person=person, employer={"careersatoto.com"}, name="OTO Development",
               siblings=[_sib("hilton.com", org_domain="careersatoto.com")] * 2) == (False, "former_employer_domain")


def test_a_past_role_at_the_same_organisation_is_not_a_former_employer():
    history = [{"organization_id": "org-oto", "organization_name": "OTO Development", "current": True},
               {"organization_id": "org-oto", "organization_name": "OTO Development", "current": False}]
    person = _person("careersatoto.com", history)
    sibs = [_sib("otodevelopment.com", org_domain="careersatoto.com")] * 2
    assert _ok("x@otodevelopment.com", person=person, employer={"careersatoto.com"}, name="OTO Development",
               siblings=sibs) == (True, "independent_employees")


def test_an_explicit_current_employer_disagreement_is_rejected():
    """A second CURRENT position at another organisation names the domain."""
    history = [{"organization_id": "org-nt", "organization_name": NAME, "current": True},
               {"organization_id": "org-x", "organization_name": "Ntrs Advisory LLC", "current": True}]
    assert _ok("a@ntrs.com", person=_person(history=history), siblings=[_sib("ntrs.com")] * 2) == \
        (False, "current_employer_disagreement")


def test_the_persons_apollo_org_must_be_our_employer():
    assert _ok("x@ntrs.com", person=_person("someoneelse.com"), siblings=[_sib("ntrs.com")] * 2) == \
        (False, "apollo_org_not_employer")


def test_the_employer_domain_itself_is_not_an_alternate():
    assert _ok("x@northerntrust.com") == (False, "not_alternate")


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
