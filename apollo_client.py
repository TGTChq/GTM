"""Apollo client for organization enrichment and hiring-manager lookup."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from company_identity import company_names_compatible
from domain_utils import normalize_company_domain

import requests

import config
from apollo_errors import (
    ApolloErrorCategory,
    ApolloErrorClassification,
    build_error_record,
    classify_apollo_error,
    write_error_artifact,
)
from http_utils import debug_dump, request_with_retry, safe_json

logger = logging.getLogger(__name__)
APOLLO_BASE_URL = "https://api.apollo.io/api/v1"


class ApolloCreditsExhaustedError(RuntimeError):
    """Raised only when Apollo's response explicitly reports exhausted credits."""


class ApolloAuthorizationError(requests.HTTPError):
    """Raised when Apollo rejects the API key or scope (HTTP 401/403).

    Subclasses ``requests.HTTPError`` so existing callers that catch that type
    still stop the run; the distinct type lets the enrichment loop open an
    Apollo-only circuit instead of mislabeling the failure as exhausted credits.
    """


class ApolloRateLimited(requests.HTTPError):
    """Raised for a genuine rate-limit stop (HTTP 429 / impractical retry window).

    A rate limit is NOT credit exhaustion. Subclasses ``requests.HTTPError`` for
    backward compatibility while remaining separately catchable.
    """


#: Failures that mean Apollo is unusable for the WHOLE run. The enrichment loop
#: opens a circuit and preserves completed work rather than crashing.
GLOBAL_FATAL_ERRORS = (
    ApolloCreditsExhaustedError,
    ApolloAuthorizationError,
    ApolloRateLimited,
)


def _apollo_error_dir():
    from pathlib import Path

    return Path(config.LOG_DIR) / "apollo_errors"


def _record_apollo_error(
    classification: ApolloErrorClassification,
    *,
    company_key: str = "",
    domain: str = "",
    retry_decision: str = "",
    final_outcome: str = "",
) -> None:
    """Persist a sanitized evidence artifact for one classified Apollo failure."""
    record = build_error_record(
        classification,
        company_key=company_key,
        domain=domain,
        retry_decision=retry_decision,
        final_outcome=final_outcome,
    )
    write_error_artifact(_apollo_error_dir(), record)


def _raise_credit_error(exc: BaseException) -> None:
    raise ApolloCreditsExhaustedError(
        "Apollo credit-consuming enrichment is unavailable. Apollo's response "
        "explicitly reports the team's shared credits are exhausted. Ask an "
        "Apollo admin to add credits, then rerun the daily pipeline."
    ) from exc


def _raise_global_fatal(classification: ApolloErrorClassification, exc: BaseException) -> None:
    """Raise the correct global-fatal Apollo exception for a global category.

    CREDIT_EXHAUSTED, AUTHORIZATION and RATE_LIMIT are kept strictly distinct so a
    422 validation error or a 429 rate limit is never reported as exhausted credits.
    """
    category = classification.category
    response = getattr(exc, "response", None)
    if category is ApolloErrorCategory.CREDIT_EXHAUSTED:
        _raise_credit_error(exc)
    if category is ApolloErrorCategory.AUTHORIZATION:
        raise ApolloAuthorizationError(
            f"Apollo rejected the request (HTTP {classification.status}): "
            "the API key or its scope is invalid. Fix the Apollo credential, "
            "then rerun the pipeline.",
            response=response,
        ) from exc
    if category is ApolloErrorCategory.RATE_LIMIT:
        window = (
            f" (requested {classification.retry_after:.0f}s retry window)"
            if classification.retry_after
            else ""
        )
        raise ApolloRateLimited(
            f"Apollo is rate limiting requests (HTTP {classification.status})"
            f"{window}. This is a throttle, not credit exhaustion; the run is "
            "preserved and Apollo calls are paused.",
            response=response,
        ) from exc


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
    headline: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    seniority: Optional[str] = None
    departments: Optional[List[str]] = None
    functions: Optional[List[str]] = None
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
        classification = classify_apollo_error(exc)
        ctx_domain = normalized_domain or domain
        # Global-fatal (credit / auth / rate limit): stop Apollo for the whole run.
        if classification.global_fatal:
            logger.error(
                "Apollo organization enrichment stopped for %s/%s: %s (HTTP %s).",
                domain, name, classification.category.value, classification.status,
            )
            _record_apollo_error(
                classification, domain=ctx_domain, retry_decision="none",
                final_outcome="apollo_global_stop",
            )
            _raise_global_fatal(classification, exc)
        # Record-specific validation (404/422): retry once with domain only, then
        # continue with unknown firmographics -- one company never aborts the run.
        if classification.category is ApolloErrorCategory.VALIDATION:
            if normalized_domain and params != {"domain": normalized_domain}:
                logger.warning(
                    "Apollo organization enrichment returned HTTP %s for %s; "
                    "retrying once with domain only.",
                    classification.status, normalized_domain,
                )
                _record_apollo_error(
                    classification, domain=normalized_domain,
                    retry_decision="retry_domain_only",
                    final_outcome="validation_retry",
                )
                try:
                    data = _organization_enrichment_request(
                        {"domain": normalized_domain},
                        debug_name="apollo_organization_enrich_domain_only",
                    )
                except requests.HTTPError as retry_exc:
                    retry_class = classify_apollo_error(retry_exc)
                    if retry_class.global_fatal:
                        _record_apollo_error(
                            retry_class, domain=normalized_domain,
                            retry_decision="none", final_outcome="apollo_global_stop",
                        )
                        _raise_global_fatal(retry_class, retry_exc)
                    if retry_class.category is ApolloErrorCategory.VALIDATION:
                        logger.warning(
                            "Apollo organization enrichment unavailable for %s "
                            "after domain-only retry (HTTP %s). Continuing with "
                            "unknown firmographics and the input domain.",
                            normalized_domain, retry_class.status,
                        )
                        _record_apollo_error(
                            retry_class, domain=normalized_domain,
                            retry_decision="none", final_outcome="unresolved_organization",
                        )
                        return _unresolved_organization(
                            domain=normalized_domain, name=name
                        )
                    _record_apollo_error(
                        retry_class, domain=normalized_domain,
                        retry_decision="none", final_outcome="record_failed",
                    )
                    logger.error(
                        "Apollo organization enrichment failed for %s/%s: %s",
                        domain, name, retry_exc,
                    )
                    raise
                except Exception as retry_exc:
                    logger.error(
                        "Apollo organization enrichment failed for %s/%s: %s",
                        domain, name, retry_exc,
                    )
                    raise
            else:
                logger.warning(
                    "Apollo organization enrichment unavailable for %s/%s "
                    "(HTTP %s). Continuing with unknown firmographics and the "
                    "input domain.",
                    ctx_domain, name, classification.status,
                )
                _record_apollo_error(
                    classification, domain=ctx_domain, retry_decision="none",
                    final_outcome="unresolved_organization",
                )
                return _unresolved_organization(domain=ctx_domain, name=name)
        else:
            # Server / unknown: retries already happened in http_utils. Mark the
            # provider failure and let the enrichment loop contain it per-company.
            _record_apollo_error(
                classification, domain=ctx_domain, retry_decision="none",
                final_outcome="record_failed",
            )
            logger.error(
                "Apollo organization enrichment failed for %s/%s: %s",
                domain, name, exc,
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
    resolved_name = org.get("name") or ""
    if not requested_domain and name and not company_names_compatible(name, resolved_name):
        logger.warning(
            "Apollo organization name mismatch for %r: resolved %r; treating "
            "name-only enrichment as untrusted",
            name,
            resolved_name,
        )
        return _unresolved_organization(domain="", name=name, raw=org)

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

    per_page = 25
    max_pages = max(1, int(getattr(config, "APOLLO_PEOPLE_SEARCH_MAX_PAGES", 1)))
    all_people: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        # Apollo documents these as query parameters, including [] in array names.
        params: List[tuple[str, str]] = [
            ("q_organization_domains_list[]", domain),
            ("include_similar_titles", "false"),
            ("page", str(page)),
            ("per_page", str(per_page)),
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
            logger.error("Apollo people search failed for %s (page %d): %s", domain, page, exc)
            if page == 1:
                raise
            # A later page failing is not fatal -- keep whatever was already
            # found rather than discarding a successful first page.
            break

        page_people = data.get("people") or []
        if not isinstance(page_people, list):
            logger.warning("Apollo returned a non-list people payload for %s (page %d)", domain, page)
            break
        all_people.extend(page_people)

        pagination = data.get("pagination") if isinstance(data.get("pagination"), dict) else {}
        total_pages = pagination.get("total_pages")
        if len(page_people) < per_page:
            break
        if isinstance(total_pages, int) and page >= total_pages:
            break

    # Defensive domain validation. Search filters can be loose, and wrong-company
    # contacts are more damaging than a lower match rate.
    validated: List[Dict[str, Any]] = []
    for person in all_people:
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
        headline=person.get("headline"),
        city=person.get("city"),
        state=person.get("state"),
        country=person.get("country"),
        seniority=person.get("seniority"),
        departments=person.get("departments") or [],
        functions=person.get("functions") or [],
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
        classification = classify_apollo_error(exc)
        if classification.global_fatal:
            logger.error(
                "Apollo person enrichment stopped: %s (HTTP %s).",
                classification.category.value, classification.status,
            )
            _record_apollo_error(
                classification, retry_decision="none",
                final_outcome="apollo_global_stop",
            )
            _raise_global_fatal(classification, exc)
        if classification.category is ApolloErrorCategory.VALIDATION:
            # Apollo can occasionally return a search-result person ID that its
            # enrichment endpoint can no longer resolve. This is a record-level
            # miss, not a pipeline-level failure. Preserve the candidate identity
            # so the existing Hunter fallback can still try first/last name +
            # company domain, and continue processing the rest of the run.
            logger.warning(
                "Apollo person enrichment skipped for %s: HTTP %s. "
                "Keeping search-result identity and continuing to Hunter fallback.",
                person_id, classification.status,
            )
            _record_apollo_error(
                classification, retry_decision="none",
                final_outcome="record_miss_hunter_fallback",
            )
            return base
        _record_apollo_error(
            classification, retry_decision="none", final_outcome="record_failed",
        )
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
        headline=enriched.get("headline") or base.headline,
        city=enriched.get("city") or base.city,
        state=enriched.get("state") or base.state,
        country=enriched.get("country") or base.country,
        seniority=enriched.get("seniority") or base.seniority,
        departments=enriched.get("departments") or base.departments or [],
        functions=enriched.get("functions") or base.functions or [],
        raw=enriched,
    )
