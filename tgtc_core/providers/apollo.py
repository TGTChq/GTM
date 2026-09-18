"""Apollo client: organization enrichment (paid), people search (0 credits), person
match (paid), with the production-proven error taxonomy.

Ported semantics from ``apollo_client.py`` / ``apollo_errors.py`` at 5d87851
(INTEGRATION_MAP §4.2). Provider outcomes are RETURNED, not raised, so the service
layer records every one of them (intent before the call, result after).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .http import Response, Transport, TransportError, TransportTimeout

CREDIT_MARKERS = (
    "shared credits", "credits are used up", "credits used up", "out of credits", "insufficient credits",
    "credit limit", "no credits remaining", "buy more credits", "you have run out of",
    "billing.limit.credits_exhausted",
)


class Outcome(str, Enum):
    SERVED = "served"
    CREDIT_EXHAUSTED = "credit_exhausted"   # explicit body marker only
    RATE_LIMITED = "rate_limited"           # 429 / long Retry-After
    UNAUTHORIZED = "unauthorized"           # 401/403
    VALIDATION = "validation"               # only person-match 404 proves record absence
    SERVER = "server"                       # 5xx / network
    TIMEOUT = "timeout"                     # uncertain: may have been charged

    @property
    def global_stop(self) -> bool:
        return self in (Outcome.CREDIT_EXHAUSTED, Outcome.RATE_LIMITED, Outcome.UNAUTHORIZED)


@dataclass
class ApolloResult:
    outcome: Outcome
    status: Optional[int] = None
    data: Dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    message: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    retry_after: Optional[float] = None

    @property
    def served(self) -> bool:
        return self.outcome is Outcome.SERVED

    def summary(self) -> Dict[str, Any]:
        summary = {"outcome": self.outcome.value, "status": self.status, "error_code": self.error_code,
                   "message": self.message[:300], "context": self.context, "retry_after": self.retry_after}
        if self.served and isinstance(self.data.get("people"), list):
            # Counts, never candidates/PII, make search coverage auditable.
            summary["returned_people"] = len(self.data["people"])
            page = self.data.get("pagination") or {}
            for name in ("page", "per_page", "total_pages", "total_entries"):
                value = page.get(name) if isinstance(page, dict) else None
                if name == "total_entries" and value is None:
                    value = self.data.get(name)
                if type(value) is int and 0 <= value <= 1_000_000_000:
                    summary[name] = value
        return summary


def _error_fields(body: str) -> Dict[str, Any]:
    # Never persist arbitrary provider text/context: it may echo credentials or PII.
    out: Dict[str, Any] = {"message": "provider_error", "error_code": "", "context": {}}
    try:
        data = json.loads(body) if body else None
    except ValueError:
        data = None
    if isinstance(data, dict):
        details = data.get("error_details") if isinstance(data.get("error_details"), dict) else {}
        code = data.get("error_code") or data.get("code") or details.get("code")
        if code == "BILLING.LIMIT.CREDITS_EXHAUSTED":
            out["error_code"] = code
        ctx = details.get("context")
        if isinstance(ctx, dict):
            extracted = {}
            for name in ("credit_balance", "credit_type", "next_billing_date"):
                entry = ctx.get(name)
                value = entry.get("value") if isinstance(entry, dict) else entry
                if name == "credit_balance" and type(value) in (int, float) and abs(value) < 1e12 and math.isfinite(value):
                    extracted[name] = value
                elif name == "credit_type" and value in ("lead credits", "email credits", "mobile credits", "export credits"):
                    extracted[name] = value
                elif name == "next_billing_date" and isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    extracted[name] = value
            out["context"] = extracted
    return out


def classify(resp: Response) -> ApolloResult:
    """Exactly one category. An explicit credit marker beats any status code."""
    body = resp.text or ""
    fields = _error_fields(body) if resp.status >= 400 else {"message": "", "error_code": "", "context": {}}
    lowered = body.lower()
    retry_after = None
    ra = resp.header("Retry-After")
    if ra:
        try:
            parsed_retry = float(ra)
            retry_after = min(parsed_retry, 900.0) if math.isfinite(parsed_retry) and parsed_retry >= 0 else None
        except ValueError:
            retry_after = None
    if resp.status >= 400 and any(m in lowered for m in CREDIT_MARKERS):
        outcome = Outcome.CREDIT_EXHAUSTED
    elif resp.status == 429 or (retry_after is not None and retry_after > 60):
        outcome = Outcome.RATE_LIMITED
    elif resp.status in (401, 403):
        outcome = Outcome.UNAUTHORIZED
    elif resp.status in (404, 422):
        outcome = Outcome.VALIDATION
    elif resp.status >= 500:
        outcome = Outcome.SERVER
    elif 200 <= resp.status < 300:
        outcome = Outcome.SERVED
    else:
        outcome = Outcome.SERVER
    data = resp.json() if outcome is Outcome.SERVED else None
    if outcome is Outcome.SERVED and not isinstance(data, dict):
        outcome = Outcome.SERVER
        fields = {"error_code": "invalid_response", "message": "invalid_response", "context": {}}
    return ApolloResult(outcome=outcome, status=resp.status, data=data if isinstance(data, dict) else {},
                        error_code=fields["error_code"], message=fields["message"], context=fields["context"],
                        retry_after=retry_after)


class ApolloClient:
    def __init__(self, transport: Transport, *, base_url: str, api_key: str, timeout: float = 30.0):
        self._t = transport
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        return {"X-Api-Key": self._key, "Content-Type": "application/json", "Accept": "application/json",
                "Cache-Control": "no-cache"}

    def _call(self, method: str, path: str, params: Any) -> ApolloResult:
        try:
            resp = self._t.request(method, f"{self._base}{path}", headers=self._headers(), params=params,
                                   timeout=self._timeout)
        except TransportTimeout:
            return ApolloResult(outcome=Outcome.TIMEOUT, message="timeout")
        except TransportError:
            return ApolloResult(outcome=Outcome.SERVER, message="transport_error")
        result = classify(resp)
        if result.served:
            key = {"/mixed_people/api_search": "people", "/organizations/enrich": "organization",
                   "/people/match": "person"}[path]
            value = result.data.get(key)
            valid = (isinstance(value, list) and all(isinstance(p, dict) for p in value)) if key == "people" else (
                key in result.data and (value is None or isinstance(value, dict)))
            if not valid:
                return ApolloResult(outcome=Outcome.SERVER, status=resp.status,
                                    error_code="invalid_response", message="invalid_response")
        return result

    # --- paid ---------------------------------------------------------------
    def enrich_organization(self, *, domain: str = "", name: str = "") -> ApolloResult:
        params: Dict[str, str] = {}
        if domain:
            params["domain"] = domain
        if name:
            params["name"] = name
        return self._call("GET", "/organizations/enrich", params)

    def match_person(self, person_id: str) -> ApolloResult:
        params = {"id": person_id, "reveal_personal_emails": "false", "reveal_phone_number": "false"}
        return self._call("POST", "/people/match", params)

    # --- documented 0 credits ---------------------------------------------
    def search_people(self, *, titles: List[str], domain: str = "", organization_id: str = "",
                      page: int = 1, per_page: int = 100, include_similar_titles: bool = False,
                      email_statuses: Optional[List[str]] = None) -> ApolloResult:
        params: List[tuple] = [("include_similar_titles", "true" if include_similar_titles else "false"),
                              ("page", str(page)), ("per_page", str(per_page))]
        if organization_id:
            params.append(("organization_ids[]", organization_id))
        elif domain:
            params.append(("q_organization_domains_list[]", domain))
        params.extend(("person_titles[]", t) for t in titles)
        params.extend(("contact_email_status[]", status) for status in (email_statuses or []))
        return self._call("POST", "/mixed_people/api_search", params)


def person_org_domain(person: Dict[str, Any]) -> str:
    from domain_utils import normalize_company_domain

    org = person.get("organization") or person.get("current_organization") or {}
    for value in (org.get("primary_domain"), org.get("domain"), org.get("website_url"), person.get("organization_domain")):
        d = normalize_company_domain(value)
        if d:
            return d
    return ""
