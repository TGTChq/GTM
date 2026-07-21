"""Hiring-manager identity, function and territory gate."""

from __future__ import annotations

import re
from typing import Iterable, List, Set

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
        identity_verified = False
        if person_domain:
            identity_verified = any(domains_equivalent(person_domain, domain) for domain in company_domains)
        if not identity_verified and person.organization_name:
            identity_verified = company_names_compatible(company_name, person.organization_name)
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
        bundle.add(FactValue(
            "current_employment", True, EvidenceStatus.VERIFIED_CROSS_SOURCE,
            [EvidenceItem("current_employment", True, EvidenceStatus.VERIFIED_CROSS_SOURCE, "apollo", excerpt=person.organization_name or person.organization_domain or "", confidence=0.94)]
        ))

        territory = _territory_text(person)
        if intent_market == "us_market":
            foreign = [name for name, pattern in FOREIGN_TERRITORY_PATTERNS.items() if re.search(pattern, territory, re.I)]
            has_us = any(re.search(pattern, territory, re.I) for pattern in US_TERRITORY_PATTERNS)
            if foreign and not has_us:
                bundle.add(FactValue(
                    "contact_territory", foreign, EvidenceStatus.VERIFIED_CROSS_SOURCE,
                    [EvidenceItem("contact_territory", foreign, EvidenceStatus.VERIFIED_CROSS_SOURCE, "apollo", excerpt=territory, confidence=0.95)]
                ))
                return GateDecision(
                    "contact", GateState.REROUTE, ReasonCode.REROUTE_TERRITORY_MISMATCH,
                    evidence=bundle, retryable=True, next_action="try_next_contact",
                    metadata={"detected_territories": foreign},
                )
        bundle.add(FactValue(
            "contact_territory", "compatible_or_global", EvidenceStatus.VERIFIED_CROSS_SOURCE,
            [EvidenceItem("contact_territory", "compatible_or_global", EvidenceStatus.VERIFIED_CROSS_SOURCE, "apollo", excerpt=territory, confidence=0.82)]
        ))
        return GateDecision(
            "contact", GateState.PASS, "CONTACT_PASS", evidence=bundle,
            next_action="continue_to_email_gate",
        )
