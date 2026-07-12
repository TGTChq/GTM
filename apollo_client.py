"""Apollo client for organization enrichment and hiring-manager lookup."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from domain_utils import normalize_company_domain

import config
from http_utils import debug_dump, request_with_retry, safe_json

logger = logging.getLogger(__name__)
APOLLO_BASE_URL = "https://api.apollo.io/api/v1"


@dataclass
class OrgEnrichment:
    found: bool
    organization_id: Optional[str] = None
    name: Optional[str] = None
    domain: Optional[str] = None
    employee_count: Optional[int] = None
    founded_year: Optional[int] = None
    industry: Optional[str] = None
    linkedin_url: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None


@dataclass
class PersonMatch:
    person_found: bool
    email_found: bool = False
    person_id: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    title: Optional[str] = None
    linkedin_url: Optional[str] = None
    organization_name: Optional[str] = None
    organization_domain: Optional[str] = None
    email: Optional[str] = None
    email_status: Optional[str] = None
    email_source: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None


def _headers() -> Dict[str, str]:
    return {
        "X-Api-Key": config.APOLLO_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Cache-Control": "no-cache",
    }


def _domain(value: str | None) -> str:
    return normalize_company_domain(value)


def _organization_from_person(person: Dict[str, Any]) -> Dict[str, Any]:
    return person.get("organization") or person.get("current_organization") or {}


def _person_org_domain(person: Dict[str, Any]) -> str:
    org = _organization_from_person(person)
    for value in (
        org.get("primary_domain"),
        org.get("domain"),
        org.get("website_url"),
        person.get("organization_domain"),
    ):
        normalized = _domain(value)
        if normalized:
            return normalized
    return ""


def enrich_organization(
    *,
    domain: str = "",
    name: str = "",
    website: str = "",
) -> OrgEnrichment:
    if not any((domain, name, website)):
        return OrgEnrichment(found=False)

    params: Dict[str, str] = {}
    if domain:
        params["domain"] = _domain(domain)
    if name:
        params["name"] = name
    if website:
        params["website"] = website

    try:
        response = request_with_retry(
            "GET",
            f"{APOLLO_BASE_URL}/organizations/enrich",
            headers=_headers(),
            params=params,
        )
        data = safe_json(response)
        debug_dump("apollo_organization_enrich", data)
    except Exception as exc:
        logger.error("Apollo organization enrichment failed for %s/%s: %s", domain, name, exc)
        raise

    org = data.get("organization") or {}
    if not org:
        return OrgEnrichment(found=False, raw=data)

    resolved_domain = ""
    for value in (org.get("primary_domain"), org.get("domain"), org.get("website_url"), website, domain):
        resolved_domain = _domain(value)
        if resolved_domain:
            break

    requested_domain = _domain(domain)
    if requested_domain and resolved_domain and not (
        resolved_domain == requested_domain
        or resolved_domain.endswith("." + requested_domain)
        or requested_domain.endswith("." + resolved_domain)
    ):
        logger.warning(
            "Apollo organization domain mismatch for %s: resolved %s; treating enrichment as untrusted",
            requested_domain,
            resolved_domain,
        )
        return OrgEnrichment(found=False, domain=requested_domain, raw=org)

    return OrgEnrichment(
        found=True,
        organization_id=org.get("id"),
        name=org.get("name"),
        domain=resolved_domain or None,
        employee_count=org.get("estimated_num_employees") or org.get("num_employees"),
        founded_year=org.get("founded_year"),
        industry=org.get("industry"),
        linkedin_url=org.get("linkedin_url"),
        raw=org,
    )


def search_people_at_company(domain: str, titles: List[str]) -> List[Dict[str, Any]]:
    domain = _domain(domain)
    if not domain or not titles:
        return []

    # Apollo documents these as query parameters, including [] in array names.
    params: List[tuple[str, str]] = [
        ("q_organization_domains_list[]", domain),
        ("include_similar_titles", "false"),
        ("page", "1"),
        ("per_page", "25"),
    ]
    params.extend(("person_titles[]", title) for title in titles)

    try:
        response = request_with_retry(
            "POST",
            f"{APOLLO_BASE_URL}/mixed_people/api_search",
            headers=_headers(),
            params=params,
        )
        data = safe_json(response)
        debug_dump("apollo_people_search", data)
    except Exception as exc:
        logger.error("Apollo people search failed for %s: %s", domain, exc)
        raise

    people = data.get("people") or []
    if not isinstance(people, list):
        logger.warning("Apollo returned a non-list people payload for %s", domain)
        return []

    # Defensive domain validation. Search filters can be loose, and wrong-company
    # contacts are more damaging than a lower match rate.
    validated: List[Dict[str, Any]] = []
    for person in people:
        person_domain = _person_org_domain(person)
        if person_domain and person_domain != domain and not person_domain.endswith("." + domain):
            logger.warning(
                "Discarding Apollo person %s due to domain mismatch (%s != %s)",
                person.get("id") or person.get("person_id"),
                person_domain,
                domain,
            )
            continue
        validated.append(person)
    return validated


def match_person(person: Dict[str, Any]) -> PersonMatch:
    """Enrich a person while preserving search-result identity for Hunter fallback."""
    person_id = person.get("id") or person.get("person_id")
    org = _organization_from_person(person)
    base = PersonMatch(
        person_found=bool(person_id),
        person_id=person_id,
        first_name=person.get("first_name"),
        last_name=person.get("last_name"),
        title=person.get("title"),
        linkedin_url=person.get("linkedin_url"),
        organization_name=org.get("name") or person.get("organization_name"),
        organization_domain=_person_org_domain(person) or None,
        raw=person,
    )
    if not person_id:
        return base

    params = {
        "id": person_id,
        "reveal_personal_emails": "false",
        "reveal_phone_number": "false",
    }
    try:
        response = request_with_retry(
            "POST",
            f"{APOLLO_BASE_URL}/people/match",
            headers=_headers(),
            params=params,
        )
        data = safe_json(response)
        debug_dump(
            "apollo_people_match",
            data,
            redact_keys=("email", "personal_emails", "phone_numbers", "phone_number"),
        )
    except Exception as exc:
        logger.error("Apollo person enrichment failed for %s: %s", person_id, exc)
        raise

    enriched = data.get("person") or {}
    if not enriched:
        return base

    enriched_org = _organization_from_person(enriched)
    email = enriched.get("email")
    return PersonMatch(
        person_found=True,
        email_found=bool(email),
        person_id=enriched.get("id") or person_id,
        first_name=enriched.get("first_name") or base.first_name,
        last_name=enriched.get("last_name") or base.last_name,
        title=enriched.get("title") or base.title,
        linkedin_url=enriched.get("linkedin_url") or base.linkedin_url,
        organization_name=enriched_org.get("name") or base.organization_name,
        organization_domain=_person_org_domain(enriched) or base.organization_domain,
        email=email,
        email_status=enriched.get("email_status") or enriched.get("contact_email_status"),
        email_source="apollo" if email else None,
        raw=enriched,
    )
