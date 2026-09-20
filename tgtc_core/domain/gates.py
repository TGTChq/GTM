"""Contact and email gates: deterministic checks over Apollo evidence.

Ported semantics from ``contact_gate.py`` / ``email_gate.py`` /
``alternate_contact_recovery.py`` at 5d87851 (INTEGRATION_MAP §7). Every outcome
is PASS or a named negative that sends discovery to the NEXT candidate; there is no
review state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .facts import RULE_VERSION
from .identity import (
    company_names_compatible, email_domain, email_on_domains, is_generic_mailbox,
    safe_employer_domain,
)
from .title_norm import is_department_label, not_an_employee_in_role, title_matches as _title_matches
from ..policy.campaigns import is_founder_tier

FOREIGN_TERRITORY = {
    "EMEA": r"\b(?:emea|europe|european|uk & ireland|united kingdom)\b",
    "APAC": r"\b(?:apac|asia pacific|asia-pacific|anz|australia|new zealand)\b",
    "INDIA_MIDDLE_EAST": r"\b(?:india|middle east|mena|gcc)\b",
    "CEE": r"\b(?:cee|central and eastern europe|central & eastern europe)\b",
    "LATAM": r"\b(?:latam|latin america|south america)\b",
    "CANADA": r"\bcanada|canadian\b",
}
US_TERRITORY = r"\b(?:us|u\.s\.|usa|united states|north america|americas)\b"
GLOBAL_SCOPE = r"\b(?:global|worldwide|chief (?:executive|technology|marketing|revenue|operating|people|product) officer|ceo|cto|cmo|cro|coo|founder|co[- ]?founder)\b"


@dataclass
class GateResult:
    passed: bool
    reason: str
    evidence: Dict[str, Any] = field(default_factory=dict)


#: Phase 4 audit (2026-09-20): the buyer-title predicate now lives in ONE place,
#: ``domain/title_norm.py``, because three callers need it (this gate, the
#: post-enrichment gate, and candidate ranking) and phase 2's version was already
#: being read by a fourth (``services/opportunity.contact_persona``). It is
#: imported, not re-implemented; the abbreviation/variant tables that used to sit
#: here moved there with it. See that module for what changed and why the change
#: is strictly additive.
title_matches = _title_matches


def person_organization(person: Dict[str, Any]) -> Dict[str, Any]:
    org = person.get("organization") or person.get("current_organization") or {}
    return org if isinstance(org, dict) else {}


def _org_domain(org: Dict[str, Any]) -> str:
    for key in ("primary_domain", "domain", "website_url"):
        d = safe_employer_domain(org.get(key))
        if d:
            return d
    return ""


def organization_matches(*, name: str, domain: str, employer_name: str, employer_domains: Set[str]) -> bool:
    d = safe_employer_domain(domain)
    if d and d in employer_domains:
        return True
    return bool(name and company_names_compatible(employer_name, name))


def current_employment_evidence(person: Dict[str, Any], employer_name: str, employer_domains: Set[str],
                                *, require_linkedin: bool) -> Optional[str]:
    org = person_organization(person)
    if org and organization_matches(name=str(org.get("name") or ""), domain=_org_domain(org),
                                    employer_name=employer_name, employer_domains=employer_domains):
        return "apollo_current_organization"
    histories = person.get("employment_history") or []
    if isinstance(histories, dict):
        histories = [histories]
    for item in histories:
        if not isinstance(item, dict):
            continue
        current = item.get("current") is True or (not item.get("end_date") and not item.get("ended_at"))
        h_org = item.get("organization") or {}
        if current and organization_matches(
                name=str(item.get("organization_name") or h_org.get("name") or ""),
                domain=str(item.get("organization_domain") or h_org.get("primary_domain") or ""),
                employer_name=employer_name, employer_domains=employer_domains):
            return "apollo_current_employment_history"
    if organization_matches(name=str(person.get("organization_name") or ""),
                            domain=str(person.get("organization_domain") or ""),
                            employer_name=employer_name, employer_domains=employer_domains) and (
            person.get("linkedin_url") or not require_linkedin):
        return "apollo_top_level_current_org"
    return None


def evaluate_contact(
    *,
    person: Dict[str, Any],
    employer_name: str,
    employer_domains: Set[str],
    buyer_titles: Sequence[str],
    founder_allowed: bool,
    require_linkedin: bool = True,
    require_current_employment: bool = True,
    intent_market: str = "us_market",
) -> GateResult:
    if not (person.get("id") or person.get("person_id")):
        return GateResult(False, "contact:no_person_identity")
    title = str(person.get("title") or "").strip()
    if not title_matches(title, buyer_titles):
        return GateResult(False, "contact:function_or_authority_mismatch", {"title": title, "rule_version": RULE_VERSION})
    if is_founder_tier(title) and not founder_allowed:
        return GateResult(False, "contact:founder_tier_not_allowed_for_size", {"title": title, "rule_version": RULE_VERSION})
    if require_linkedin and not str(person.get("linkedin_url") or "").strip():
        return GateResult(False, "contact:no_linkedin_identity_anchor")
    org = person_organization(person)
    identity_ok = organization_matches(name=str(org.get("name") or person.get("organization_name") or ""),
                                       domain=_org_domain(org) or str(person.get("organization_domain") or ""),
                                       employer_name=employer_name, employer_domains=employer_domains)
    if not identity_ok:
        return GateResult(False, "contact:wrong_organization", {"org": org.get("name")})
    current = current_employment_evidence(person, employer_name, employer_domains, require_linkedin=require_linkedin)
    if require_current_employment and not current:
        return GateResult(False, "contact:current_employment_not_verified")
    headline = str(person.get("headline") or "")
    role_scope = f"{title} | {headline}"
    if intent_market == "us_market":
        foreign = [k for k, p in FOREIGN_TERRITORY.items() if re.search(p, role_scope, re.I)]
        if foreign and not re.search(US_TERRITORY, role_scope, re.I) and not re.search(GLOBAL_SCOPE, role_scope, re.I):
            return GateResult(False, "contact:territory_mismatch", {"territories": foreign})
    return GateResult(True, "contact:pass", {"title": title, "current_employment": current})


def corroborated_alternate_domains(*, employer_name: str, employer_domains: Set[str],
                                   person: Dict[str, Any], apollo_org: Optional[Dict[str, Any]]) -> Set[str]:
    """Domains proven to belong to the SAME employer by Apollo organization evidence.

    A domain is corroborated only when the organization carrying it is the resolved
    employer (name-compatible or the same Apollo organization id). Similarity alone
    never adds a domain.
    """
    out: Set[str] = set()
    candidates: List[Dict[str, Any]] = []
    if apollo_org:
        candidates.append(apollo_org)
    org = person_organization(person)
    if org:
        candidates.append(org)
    for c in candidates:
        d = _org_domain(c)
        if not d:
            continue
        if company_names_compatible(employer_name, str(c.get("name") or "")) or (
                apollo_org and c.get("id") and c.get("id") == apollo_org.get("id")):
            out.add(d)
    return out - set(employer_domains)


def evaluate_email(
    *,
    email: Optional[str],
    email_status: Optional[str],
    employer_domains: Set[str],
    corroborated_domains: Set[str] = frozenset(),
) -> GateResult:
    addr = str(email or "").strip().lower()
    if not addr:
        return GateResult(False, "email:none_returned")
    if is_generic_mailbox(addr):
        return GateResult(False, "email:generic_mailbox", {"email_domain": email_domain(addr)})
    alignment = ""
    if email_on_domains(addr, employer_domains):
        alignment = "EXACT_EMPLOYER_DOMAIN"
    elif email_on_domains(addr, corroborated_domains):
        alignment = "CORROBORATED_ALTERNATE_EMPLOYER_DOMAIN"
    else:
        return GateResult(False, "email:domain_not_employer", {"email_domain": email_domain(addr)})
    status = str(email_status or "").strip().lower()
    if status != "verified":
        # Apollo is the single authority; nothing promotes unverified/extrapolated/unknown.
        return GateResult(False, f"email:not_verified:{status or 'missing'}", {"alignment": alignment})
    return GateResult(True, "email:pass", {"alignment": alignment, "authority": "apollo"})


def pre_enrichment_check(*, person: Dict[str, Any], employer_name: str, employer_domains: Set[str],
                         buyer_titles: Sequence[str], founder_allowed: bool) -> GateResult:
    """What can be decided from a SEARCH result, which Apollo documents as limited data
    (no email, no LinkedIn; organization details may be partial).

    Decides only on evidence the search carries: a title outside the buyer hierarchy,
    a founder-tier title at an employer too large for founders, or an organization that
    is provably a different company. Everything that arrives only with enrichment
    (LinkedIn, employment history, email) is checked AFTER enrichment by
    ``evaluate_contact`` / ``evaluate_email``. A negative here is cheap and re-evaluable;
    it never blacklists the candidate.
    """
    if not (person.get("id") or person.get("person_id")):
        return GateResult(False, "contact:no_person_identity")
    title = str(person.get("title") or "").strip()
    if not title_matches(title, buyer_titles):
        # Phase 4: "the person's title is a department name" and "the person's
        # title is a different job" are two different losses. Both were counted
        # as ``function_or_authority_mismatch``, which made the loss table say
        # "wrong function" for a record that carries no function evidence at
        # all. Same outcome, separate reason, so the accounting stops lying.
        if not_an_employee_in_role(title):
            return GateResult(False, "contact:title_not_current_employee_in_role", {"title": title, "rule_version": RULE_VERSION})
        if is_department_label(title):
            return GateResult(False, "contact:title_is_department_not_role", {"title": title, "rule_version": RULE_VERSION})
        return GateResult(False, "contact:function_or_authority_mismatch", {"title": title, "rule_version": RULE_VERSION})
    if is_founder_tier(title) and not founder_allowed:
        return GateResult(False, "contact:founder_tier_not_allowed_for_size", {"title": title, "rule_version": RULE_VERSION})
    org = person_organization(person)
    org_name = str(org.get("name") or person.get("organization_name") or "")
    org_domain = _org_domain(org) or safe_employer_domain(person.get("organization_domain"))
    if org_domain or org_name:
        if not organization_matches(name=org_name, domain=org_domain, employer_name=employer_name, employer_domains=employer_domains):
            return GateResult(False, "contact:wrong_organization", {"org": org_name or org_domain})
    return GateResult(True, "contact:eligible_for_enrichment", {"title": title})
