"""Hiring-manager identity, function and territory gate."""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Set, Tuple

import config

from apollo_client import PersonMatch
from company_identity import company_names_compatible, domains_equivalent, safe_company_domain
from decision_types import GateDecision, GateState
from evidence_types import EvidenceBundle, EvidenceItem, EvidenceStatus, FactValue
from job_filter import normalize_text
from reason_codes import ReasonCode


FOREIGN_TERRITORY_PATTERNS = {
    "EMEA": r"\b(?:emea|europe|european|uk & ireland|united kingdom)\b",
    "APAC": r"\b(?:apac|asia pacific|asia-pacific|anz|australia|new zealand)\b",
    "INDIA_MIDDLE_EAST": r"\b(?:india|middle east|mena|gcc)\b",
    "CEE": r"\b(?:cee|central and eastern europe|central & eastern europe)\b",
    "LATAM": r"\b(?:latam|latin america|south america)\b",
    "CANADA": r"\bcanada|canadian\b",
}
US_TERRITORY_PATTERNS = [r"\b(?:us|u\.s\.|usa|united states|north america|americas)\b"]
GLOBAL_SCOPE_PATTERN = r"\b(?:global|worldwide|chief (?:executive|technology|marketing|revenue|operating|people|product) officer|ceo|cto|cmo|cro|coo|founder|co[- ]?founder)\b"


def _title_match(title: str, targets: Iterable[str]) -> bool:
    normalized = normalize_text(title)
    for target in targets:
        candidate = normalize_text(target)
        if candidate and (
            normalized == candidate
            or re.search(r"\b" + re.escape(candidate) + r"\b", normalized)
            or candidate in normalized
        ):
            return True
    return False


def _territory_text(person: PersonMatch) -> str:
    raw = person.raw or {}
    location = raw.get("location") or {}
    if not isinstance(location, dict):
        location = {}
    values = [
        person.title,
        getattr(person, "headline", None),
        getattr(person, "city", None),
        getattr(person, "state", None),
        getattr(person, "country", None),
        raw.get("headline"),
        raw.get("city"),
        raw.get("state"),
        raw.get("country"),
        location.get("city"),
        location.get("state"),
        location.get("country"),
    ]
    return " | ".join(str(value) for value in values if value)


def _organization_matches(
    *, name: str, domain: str, company_name: str, company_domains: Set[str]
) -> bool:
    normalized_domain = safe_company_domain(domain or "", [])
    if normalized_domain and any(domains_equivalent(normalized_domain, item) for item in company_domains):
        return True
    return bool(name and company_names_compatible(company_name, name))


def _current_employment_evidence(
    person: PersonMatch, company_name: str, company_domains: Set[str]
) -> Tuple[bool, str]:
    raw = person.raw or {}
    current_org = raw.get("current_organization") or raw.get("organization") or {}
    if isinstance(current_org, dict) and _organization_matches(
        name=str(current_org.get("name") or ""),
        domain=str(current_org.get("primary_domain") or current_org.get("domain") or current_org.get("website_url") or ""),
        company_name=company_name,
        company_domains=company_domains,
    ):
        return True, "apollo_current_organization"

    histories = raw.get("employment_history") or raw.get("employment_histories") or []
    if isinstance(histories, dict):
        histories = [histories]
    for item in histories:
        if not isinstance(item, dict):
            continue
        is_current = item.get("current") is True or (not item.get("end_date") and not item.get("ended_at"))
        organization = item.get("organization") or {}
        name = str(item.get("organization_name") or organization.get("name") or "")
        domain = str(
            item.get("organization_domain")
            or organization.get("primary_domain")
            or organization.get("domain")
            or ""
        )
        if is_current and _organization_matches(
            name=name, domain=domain, company_name=company_name, company_domains=company_domains
        ):
            return True, "apollo_current_employment_history"

    # Apollo's enriched top-level organization is explicitly the current org.
    # Require a LinkedIn profile as an additional stable identity anchor in
    # strict mode instead of trusting a bare name/domain pair.
    top_level_match = _organization_matches(
        name=str(person.organization_name or ""),
        domain=str(person.organization_domain or ""),
        company_name=company_name,
        company_domains=company_domains,
    )
    if top_level_match and (person.linkedin_url or not config.REQUIRE_CONTACT_LINKEDIN):
        return True, "apollo_top_level_current_org_with_identity_anchor"
    return False, "current_employment_not_positively_verified"


class ContactGate:
    def evaluate(
        self,
        *,
        person: PersonMatch,
        target_titles: List[str],
        company_domains: Set[str],
        company_name: str,
        intent_market: str = "us_market",
        founder_allowed: bool = True,
    ) -> GateDecision:
        bundle = EvidenceBundle()
        if not person.person_found:
            return GateDecision(
                "contact", GateState.REROUTE, ReasonCode.REROUTE_NOT_CURRENT_EMPLOYEE,
                retryable=True, next_action="try_next_contact",
            )

        title = str(person.title or "").strip()
        bundle.add(FactValue(
            "contact_title", title, EvidenceStatus.VERIFIED_CROSS_SOURCE,
            [EvidenceItem("contact_title", title, EvidenceStatus.VERIFIED_CROSS_SOURCE, "apollo", excerpt=title, confidence=0.9)]
        ))
        if not _title_match(title, target_titles):
            return GateDecision(
                "contact", GateState.REROUTE, ReasonCode.REROUTE_FUNCTION_MISMATCH,
                evidence=bundle, retryable=True, next_action="try_next_contact",
            )

        if re.search(r"\b(?:founder|co[- ]?founder|owner|chief executive officer|ceo|president)\b", title, re.I) and not founder_allowed:
            return GateDecision(
                "contact", GateState.REROUTE, ReasonCode.REROUTE_SENIORITY_MISMATCH,
                evidence=bundle, retryable=True, next_action="try_next_contact",
            )

        person_domain = safe_company_domain(person.organization_domain or "", [])
        identity_verified = _organization_matches(
            name=str(person.organization_name or ""),
            domain=str(person.organization_domain or ""),
            company_name=company_name,
            company_domains=company_domains,
        )
        if not identity_verified:
            reason = (
                ReasonCode.REROUTE_WRONG_ORGANIZATION
                if person_domain or person.organization_name
                else ReasonCode.REROUTE_NOT_CURRENT_EMPLOYEE
            )
            return GateDecision(
                "contact", GateState.REROUTE, reason,
                evidence=bundle, retryable=True, next_action="try_next_contact",
            )
        current_verified, current_reason = _current_employment_evidence(
            person, company_name, company_domains
        )
        if config.REQUIRE_CURRENT_EMPLOYMENT_EVIDENCE and not current_verified:
            return GateDecision(
                "contact", GateState.NEEDS_CHECK, ReasonCode.REROUTE_NOT_CURRENT_EMPLOYEE,
                evidence=bundle, retryable=False, next_action="write_review",
                metadata={"current_employment_reason": current_reason},
            )
        bundle.add(FactValue(
            "current_employment", True, EvidenceStatus.VERIFIED_CROSS_SOURCE,
            [EvidenceItem(
                "current_employment", True, EvidenceStatus.VERIFIED_CROSS_SOURCE,
                "apollo_current_employment", person.linkedin_url or "",
                excerpt=f"{person.organization_name or person.organization_domain or ''} | {current_reason}",
                confidence=0.96,
            )]
        ))

        territory = _territory_text(person)
        role_scope = " | ".join(
            str(value) for value in (person.title, person.headline, (person.raw or {}).get("headline"))
            if value
        )
        if intent_market == "us_market":
            foreign = [name for name, pattern in FOREIGN_TERRITORY_PATTERNS.items() if re.search(pattern, territory, re.I)]
            role_foreign = [name for name, pattern in FOREIGN_TERRITORY_PATTERNS.items() if re.search(pattern, role_scope, re.I)]
            role_has_us = any(re.search(pattern, role_scope, re.I) for pattern in US_TERRITORY_PATTERNS)
            role_global = bool(re.search(GLOBAL_SCOPE_PATTERN, role_scope, re.I))
            has_us = any(re.search(pattern, territory, re.I) for pattern in US_TERRITORY_PATTERNS)
            # A person's physical location is not proof of territory ownership.
            # Reroute only when the role itself explicitly owns a conflicting
            # geography (for example, "VP Sales EMEA").
            if role_foreign and not role_has_us and not role_global:
                bundle.add(FactValue(
                    "contact_territory", role_foreign, EvidenceStatus.VERIFIED_CROSS_SOURCE,
                    [EvidenceItem("contact_territory", role_foreign, EvidenceStatus.VERIFIED_CROSS_SOURCE, "apollo", excerpt=role_scope, confidence=0.95)]
                ))
                return GateDecision(
                    "contact", GateState.REROUTE, ReasonCode.REROUTE_TERRITORY_MISMATCH,
                    evidence=bundle, retryable=True, next_action="try_next_contact",
                    metadata={"detected_territories": role_foreign},
                )
        if intent_market == "us_market" and config.REQUIRE_US_CONTACT_TERRITORY and not has_us and not role_global:
            return GateDecision(
                "contact", GateState.NEEDS_CHECK, ReasonCode.REROUTE_TERRITORY_UNVERIFIED,
                evidence=bundle, retryable=False, next_action="write_review",
                metadata={"territory_text": territory},
            )
        territory_value = (
            "us_or_americas_verified" if has_us
            else "global_scope_verified" if role_global
            else "compatible_global"
        )
        bundle.add(FactValue(
            "contact_territory", territory_value,
            EvidenceStatus.VERIFIED_CROSS_SOURCE,
            [EvidenceItem(
                "contact_territory", territory_value,
                EvidenceStatus.VERIFIED_CROSS_SOURCE, "apollo", excerpt=territory,
                confidence=0.95 if has_us else 0.9 if role_global else 0.75,
            )]
        ))
        return GateDecision(
            "contact", GateState.PASS, "CONTACT_PASS", evidence=bundle,
            next_action="continue_to_email_gate",
        )
