"""Apollo client for organization enrichment and hiring-manager lookup."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from domain_utils import normalize_company_domain

import requests

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


def _organization_enrichment_request(
    params: Dict[str, str],
    *,
    debug_name: str,
) -> Dict[str, Any]:
    response = request_with_retry(
        "GET",
        f"{APOLLO_BASE_URL}/organizations/enrich",
        headers=_headers(),
        params=params,
    )
    data = safe_json(response)
    debug_dump(debug_name, data)
    return data


def _unresolved_organization(
    *,
    domain: str,
    name: str,
    raw: Optional[Dict[str, Any]] = None,
) -> OrgEnrichment:
    """Preserve safe input identity when Apollo cannot enrich one company."""
    return OrgEnrichment(
        found=False,
        name=name or None,
        domain=_domain(domain) or None,
        raw=raw,
    )


def enrich_organization(
    *,
    domain: str = "",
    name: str = "",
    website: str = "",
) -> OrgEnrichment:
    if not any((domain, name, website)):
        return OrgEnrichment(found=False)

    normalized_domain = _domain(domain)
    params: Dict[str, str] = {}
    if normalized_domain:
        params["domain"] = normalized_domain
    if name:
        params["name"] = name
    if website:
        params["website"] = website

    try:
        data = _organization_enrichment_request(
            params, debug_name="apollo_organization_enrich"
        )
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in {404, 422}:
            # A company-level enrichment miss must not abort the whole daily run.
            # When a multi-identifier request is rejected, retry once with the
            # normalized domain only. This removes any stale/noisy name or website
            # value while keeping the strongest company identifier.
            if normalized_domain and params != {"domain": normalized_domain}:
                logger.warning(
                    "Apollo organization enrichment returned HTTP %s for %s; "
                    "retrying once with domain only.",
                    status,
                    normalized_domain,
                )
                try:
                    data = _organization_enrichment_request(
                        {"domain": normalized_domain},
                        debug_name="apollo_organization_enrich_domain_only",
                    )
                except requests.HTTPError as retry_exc:
                    retry_status = (
                        retry_exc.response.status_code
                        if retry_exc.response is not None
                        else None
                    )
                    if retry_status in {404, 422}:
                        logger.warning(
                            "Apollo organization enrichment unavailable for %s "
                            "after domain-only retry (HTTP %s). Continuing with "
                            "unknown firmographics and the input domain.",
                            normalized_domain,
                            retry_status,
                        )
                        return _unresolved_organization(
                            domain=normalized_domain, name=name
                        )
                    logger.error(
                        "Apollo organization enrichment failed for %s/%s: %s",
                        domain,
                        name,
                        retry_exc,
                    )
                    raise
                except Exception as retry_exc:
                    logger.error(
                        "Apollo organization enrichment failed for %s/%s: %s",
                        domain,
                        name,
                        retry_exc,
                    )
                    raise
            else:
                logger.warning(
                    "Apollo organization enrichment unavailable for %s/%s "
                    "(HTTP %s). Continuing with unknown firmographics and the "
                    "input domain.",
                    normalized_domain or domain,
                    name,
                    status,
                )
                return _unresolved_organization(
                    domain=normalized_domain or domain, name=name
                )
        else:
            logger.error(
                "Apollo organization enrichment failed for %s/%s: %s",
                domain,
                name,
                exc,
            )
            raise
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
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in {404, 422}:
            # Apollo can occasionally return a search-result person ID that its
            # enrichment endpoint can no longer resolve. This is a record-level
            # miss, not a pipeline-level failure. Preserve the candidate identity
            # so the existing Hunter fallback can still try first/last name +
            # company domain, and continue processing the rest of the run.
            logger.warning(
                "Apollo person enrichment skipped for %s: HTTP %s. "
                "Keeping search-result identity and continuing to Hunter fallback.",
                person_id,
                status,
            )
            return base
        logger.error("Apollo person enrichment failed for %s: %s", person_id, exc)
        raise
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
