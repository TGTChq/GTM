"""Apollo client: organization enrichment (paid), people search (0 credits), person
match (paid), with the production-proven error taxonomy.

Ported semantics from ``apollo_client.py`` / ``apollo_errors.py`` at 5d87851
(INTEGRATION_MAP §4.2). Provider outcomes are RETURNED, not raised, so the service
layer records every one of them (intent before the call, result after).
"""

from __future__ import annotations

import json
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
_SECRET_RE = re.compile(r'("?[\w-]*(?:api[_-]?key|authorization|token|password|secret)"?\s*[:=]\s*)("?)[^"\s,}]+("?)', re.I)


class Outcome(str, Enum):
    SERVED = "served"
    CREDIT_EXHAUSTED = "credit_exhausted"   # explicit body marker only
    RATE_LIMITED = "rate_limited"           # 429 / long Retry-After
    UNAUTHORIZED = "unauthorized"           # 401/403
    VALIDATION = "validation"               # 404/422 without the marker: one record
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
        return {"outcome": self.outcome.value, "status": self.status, "error_code": self.error_code,
                "message": self.message[:300], "context": self.context, "retry_after": self.retry_after}


def _sanitize(text: str) -> str:
    return _SECRET_RE.sub(r"\1\2[REDACTED]\3", text or "")[:500].strip()


def _error_fields(body: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"message": _sanitize(body), "error_code": "", "context": {}}
    try:
        data = json.loads(body) if body else None
    except ValueError:
        data = None
    if isinstance(data, dict):
        details = data.get("error_details") if isinstance(data.get("error_details"), dict) else {}
        code = data.get("error_code") or data.get("code") or details.get("code")
        if code:
            out["error_code"] = _sanitize(str(code))
        msg = details.get("message") or data.get("error") or data.get("message") or data.get("error_message")
        if msg:
            out["message"] = _sanitize(msg if isinstance(msg, str) else json.dumps(msg))
        ctx = details.get("context")
        if isinstance(ctx, dict):
            extracted = {}
            for name, entry in ctx.items():
                value = entry.get("value") if isinstance(entry, dict) else entry
                if isinstance(value, (str, int, float, bool)) or value is None:
                    extracted[str(name)] = value
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
            retry_after = float(ra)
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
        except TransportError as exc:
            return ApolloResult(outcome=Outcome.SERVER, message=str(exc)[:200])
        return classify(resp)

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
                      page: int = 1, per_page: int = 25) -> ApolloResult:
        params: List[tuple] = [("include_similar_titles", "false"), ("page", str(page)), ("per_page", str(per_page))]
        if organization_id:
            params.append(("organization_ids[]", organization_id))
        elif domain:
            params.append(("q_organization_domains_list[]", domain))
        params.extend(("person_titles[]", t) for t in titles)
        return self._call("POST", "/mixed_people/api_search", params)


def person_org_domain(person: Dict[str, Any]) -> str:
    from domain_utils import normalize_company_domain

    org = person.get("organization") or person.get("current_organization") or {}
    for value in (org.get("primary_domain"), org.get("domain"), org.get("website_url"), person.get("organization_domain")):
        d = normalize_company_domain(value)
        if d:
            return d
    return ""
