"""SIMULATED provider behaviour for tests and the offline demo.

Everything in this module is a fake. It reproduces the documented/observed shape of
each provider's responses so the REAL client code (``tgtc_core.providers.*``) runs
unchanged, but nothing here is evidence about the live services. Each fake is
stateful and scriptable: pages can overlap, repeat or fail; Apollo can exhaust
credits, rate-limit, time out or refuse auth; Airtable/Instantly can accept and then
lose the response, reset the connection or answer 5xx after creating.

Apollo People Search is served in its DOCUMENTED limited shape (review finding R01):
no email, no LinkedIn URL, no employment history -- those arrive only from
``people/match``. The search carries id, names, title, headline and a basic
organization block.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from ..providers.http import Response, TransportError, TransportTimeout

#: Fields Apollo's People Search returns (limited data); everything else needs enrichment.
SEARCH_FIELDS = ("id", "first_name", "last_name", "name", "title", "headline", "organization", "seniority")


def _params_dict(params: Any) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    items = list(params.items()) if isinstance(params, dict) else list(params or [])
    for k, v in items:
        out.setdefault(str(k), []).append(str(v))
    return out


def _json(status: int, body: Any, headers: Optional[Dict[str, str]] = None) -> Response:
    return Response(status=status, headers=headers or {}, text=json.dumps(body, default=str))


# ---------------------------------------------------------------------------
# Fantastic
# ---------------------------------------------------------------------------

def make_posting_row(*, id: str, title: str, organization: str, domain: str, description: str,
                     date_created: datetime, source: str = "linkedin", employment_type: str = "FULL_TIME",
                     countries: Tuple[str, ...] = ("US",), slug: str = "", headcount: Optional[int] = 120,
                     industry: str = "Software Development", agency: bool = False, location_type: str = "remote",
                     url: str = "", ats_duplicate: bool = False, source_type: str = "jobboard",
                     date_valid_through: Optional[datetime] = None, **extra: Any) -> Dict[str, Any]:
    row = {
        "id": id, "title": title, "organization": organization,
        "organization_url": f"https://{domain}" if domain else "", "domain_derived": domain,
        "org_linkedin_name": organization, "org_linkedin_slug": slug or re.sub(r"[^a-z0-9]+", "", organization.lower()),
        "org_linkedin_website": f"https://www.{domain}" if domain else "", "org_linkedin_headcount": headcount,
        "org_linkedin_size": "51-200", "org_linkedin_industry": industry,
        "org_linkedin_recruitment_agency_derived": agency,
        "countries_derived": list(countries), "locations_derived": ["United States"], "location_type": location_type,
        "date_created": date_created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date_posted": (date_created - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "description_text": description, "url": url or f"https://jobs.example/{id}",
        "source": source, "source_type": source_type, "employment_type": employment_type,
        "ai_employment_type": employment_type, "ats_duplicate": ats_duplicate,
        # PII the client must strip
        "recruiter_name": "Recruiter Person", "ai_hiring_manager_email_address": "hm@" + (domain or "example.com"),
    }
    if date_valid_through is not None:
        row["date_valid_through"] = date_valid_through.strftime("%Y-%m-%dT%H:%M:%SZ")
    row.update(extra)
    return row


@dataclass
class FakeFantastic:
    """Serves a window's rows ordered by date_posted DESC and paginates by offset.

    ``rows`` feeds ``/v1/active-jb``; ``ats_rows`` feeds ``/v1/active-ats``. When the
    job-board request carries ``exclude_ats_duplicate=true`` rows flagged
    ``ats_duplicate`` are dropped from the job-board feed (documented behaviour).
    """

    rows: List[Dict[str, Any]] = field(default_factory=list)
    ats_rows: List[Dict[str, Any]] = field(default_factory=list)
    jobs_remaining: int = 20000
    requests_remaining: int = 10000
    next_billing_date: str = "2026-09-17"
    fail_offsets: Dict[int, Any] = field(default_factory=dict)  # offset -> status | 'timeout'
    repeat_page_at_offset: Optional[int] = None                 # serve the previous page again once
    requests: List[Dict[str, Any]] = field(default_factory=list)
    auth_ok: bool = True
    quota_exhausted: bool = False
    on_request: Optional[Callable[[Dict[str, Any]], None]] = None   # hook (used by concurrency tests)
    _repeated: bool = field(default=False, repr=False)

    def request(self, method: str, url: str, *, headers=None, params=None, json_body=None, timeout=30.0) -> Response:
        p = _params_dict(params)
        path = urlsplit(url).path
        record = {"path": path, "params": {k: v[0] if len(v) == 1 else v for k, v in p.items()},
                  "auth_header_present": bool((headers or {}).get("Authorization"))}
        self.requests.append(record)
        if self.on_request is not None:
            self.on_request(record)
        if not self.auth_ok:
            return _json(401, {"error": "unauthorized"})
        if self.quota_exhausted:
            return _json(429, {"error": "quota exhausted"})
        limit = int(p.get("limit", ["100"])[0])
        offset = int(p.get("offset", ["0"])[0])
        fail = self.fail_offsets.pop(offset, None)
        if fail == "timeout":
            raise TransportTimeout("simulated timeout")
        if isinstance(fail, int):
            return _json(fail, {"error": f"simulated_{fail}"})
        lower = datetime.fromisoformat(p["date_created_gte"][0].replace("Z", "+00:00"))
        upper = datetime.fromisoformat(p["date_created_lt"][0].replace("Z", "+00:00"))
        source_rows = self.ats_rows if path.endswith("/active-ats") else self.rows
        window = [r for r in source_rows if lower <= datetime.fromisoformat(r["date_created"].replace("Z", "+00:00")) < upper]
        if path.endswith("/active-jb") and p.get("exclude_ats_duplicate", ["false"])[0] == "true":
            window = [r for r in window if not r.get("ats_duplicate")]
        window.sort(key=lambda r: r["date_posted"], reverse=True)
        if self.repeat_page_at_offset is not None and offset == self.repeat_page_at_offset and not self._repeated:
            self._repeated = True
            page = window[max(0, offset - limit):offset]
        else:
            page = window[offset:offset + limit]
        if path.endswith("-count"):
            page = []
        else:
            self.jobs_remaining -= len(page)
        self.requests_remaining -= 1
        headers = {"x-api-jobs-limit": "20000", "x-api-jobs-remaining": str(self.jobs_remaining),
                   "x-api-requests-limit": "10000", "x-api-requests-remaining": str(self.requests_remaining),
                   "x-api-next-billing-date": self.next_billing_date}
        return _json(200, page, headers)


# ---------------------------------------------------------------------------
# Apollo
# ---------------------------------------------------------------------------

CREDIT_BODY = {
    "error": "You have insufficient credits! <a href=\"#\">Upgrade</a>",
    "error_details": {"code": "BILLING.LIMIT.CREDITS_EXHAUSTED",
                      "message": "Your team has used all of its credits for this billing cycle.",
                      "context": {"credit_type": {"value": "lead credits"}, "credit_balance": {"value": 0},
                                  "next_billing_date": {"value": "2026-09-18"}}},
}


def make_person(*, id: str, first: str, last: str, title: str, org_name: str, org_domain: str,
                email: Optional[str], email_status: Optional[str], linkedin: Optional[str] = None,
                org_id: str = "", headline: str = "", current: bool = True) -> Dict[str, Any]:
    """A FULL person record as ``people/match`` returns it. The search fake projects it
    to the documented limited shape."""
    return {
        "id": id, "first_name": first, "last_name": last, "name": f"{first} {last}", "title": title, "headline": headline,
        "linkedin_url": linkedin if linkedin is not None else f"https://www.linkedin.com/in/{first.lower()}-{last.lower()}-{id}",
        "organization": {"id": org_id or f"org-{org_domain}", "name": org_name, "primary_domain": org_domain},
        "employment_history": [{"organization_name": org_name, "current": current, "end_date": None if current else "2024-01-01"}],
        "_email": email, "_email_status": email_status,
    }


def search_projection(person: Dict[str, Any]) -> Dict[str, Any]:
    """Apollo People Search: limited data (no email, no LinkedIn, no history)."""
    out = {k: v for k, v in person.items() if k in SEARCH_FIELDS}
    org = out.get("organization")
    if isinstance(org, dict):
        out["organization"] = {k: org[k] for k in ("id", "name", "primary_domain") if k in org}
    return out


@dataclass
class FakeApollo:
    organizations: Dict[str, Dict[str, Any]] = field(default_factory=dict)     # domain -> org
    people_by_domain: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    people_by_org_id: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    credits: Optional[int] = None            # None = unlimited; 0 = exhausted
    fail_next: List[Any] = field(default_factory=list)  # 'timeout' | 429 | 401 | 500 (consumed in order for paid calls)
    requests: List[Dict[str, Any]] = field(default_factory=list)
    served_paid: int = 0
    #: Legacy-style rich search output. Off by default: the documented shape is sparse.
    rich_search: bool = False

    def _people(self, p: Dict[str, List[str]]) -> List[Dict[str, Any]]:
        if "organization_ids[]" in p:
            return list(self.people_by_org_id.get(p["organization_ids[]"][0], []))
        domain = p.get("q_organization_domains_list[]", [""])[0]
        return list(self.people_by_domain.get(domain, []))

    def _all_people(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for people in list(self.people_by_domain.values()) + list(self.people_by_org_id.values()):
            out.extend(people)
        return out

    def request(self, method: str, url: str, *, headers=None, params=None, json_body=None, timeout=30.0) -> Response:
        p = _params_dict(params)
        path = urlsplit(url).path
        self.requests.append({"path": path, "method": method, "params": {k: v for k, v in p.items()},
                              "key_present": bool((headers or {}).get("X-Api-Key"))})
        paid = path.endswith("/organizations/enrich") or path.endswith("/people/match")
        if paid and self.fail_next:
            f = self.fail_next.pop(0)
            if f == "timeout":
                raise TransportTimeout("simulated timeout")
            if f == 429:
                return _json(429, {"error": "rate limited"}, {"Retry-After": "30"})
            if f == 401:
                return _json(401, {"error": "invalid api key"})
            if f == 500:
                return _json(500, {"error": "server error"})
        if paid and self.credits is not None and self.credits <= 0:
            return _json(422, CREDIT_BODY)
        if path.endswith("/organizations/enrich"):
            domain = p.get("domain", [""])[0]
            org = self.organizations.get(domain)
            if org is None and not domain:
                name = p.get("name", [""])[0].lower()
                org = next((o for o in self.organizations.values() if str(o.get("name", "")).lower() == name), None)
            self._charge()
            return _json(200, {"organization": org} if org else {"organization": None})
        if path.endswith("/mixed_people/api_search"):
            titles = [t.lower() for t in p.get("person_titles[]", [])]
            people = [x for x in self._people(p) if any(t in str(x.get("title", "")).lower() or str(x.get("title", "")).lower() in t for t in titles)]
            page = int(p.get("page", ["1"])[0])
            per_page = int(p.get("per_page", ["25"])[0])
            chunk = people[(page - 1) * per_page: page * per_page]
            if self.rich_search:
                public = [{k: v for k, v in x.items() if not k.startswith("_")} for x in chunk]
            else:
                public = [search_projection(x) for x in chunk]
            return _json(200, {"people": public, "pagination": {"page": page, "per_page": per_page,
                                                                  "total_pages": max(1, (len(people) + per_page - 1) // per_page)}})
        if path.endswith("/people/match"):
            pid = p.get("id", [""])[0]
            person = next((x for x in self._all_people() if x["id"] == pid), None)
            self._charge()
            if not person:
                return _json(404, {"error": "not found"})
            enriched = {k: v for k, v in person.items() if not k.startswith("_")}
            enriched["email"] = person.get("_email")
            enriched["email_status"] = person.get("_email_status")
            return _json(200, {"person": enriched})
        return _json(404, {"error": "unknown endpoint"})

    def _charge(self) -> None:
        self.served_paid += 1
        if self.credits is not None:
            self.credits -= 1


# ---------------------------------------------------------------------------
# Airtable
# ---------------------------------------------------------------------------

@dataclass
class FakeAirtable:
    records: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # record id -> record
    lose_response_once: bool = False
    #: Accept the create, store the row, then fail the response: 'reset' | 500 | 502 | 503
    fail_after_create_once: Optional[Any] = None
    fail_next_status: Optional[int] = None
    requests: List[Dict[str, Any]] = field(default_factory=list)
    _counter: int = 0

    def request(self, method: str, url: str, *, headers=None, params=None, json_body=None, timeout=30.0) -> Response:
        p = _params_dict(params)
        self.requests.append({"method": method, "params": p, "body": json_body})
        if method == "GET":
            formula = p.get("filterByFormula", [""])[0]
            m = re.search(r"\{Lead Key\} = '(.*)'", formula)
            recs = list(self.records.values())
            if m:
                key = m.group(1).replace("\\'", "'")
                recs = [r for r in recs if r["fields"].get("Lead Key") == key]
            return _json(200, {"records": recs[:100]})
        if method == "POST":
            if self.fail_next_status:
                status, self.fail_next_status = self.fail_next_status, None
                return _json(status, {"error": {"type": "SIMULATED", "message": f"simulated {status}"}})
            created = []
            for rec in (json_body or {}).get("records", []):
                fields = dict(rec.get("fields") or {})
                self._counter += 1
                rid = f"rec{self._counter:06d}"
                self.records[rid] = {"id": rid, "fields": fields, "createdTime": datetime.now(timezone.utc).isoformat()}
                created.append(self.records[rid])
            if self.lose_response_once:
                self.lose_response_once = False
                raise TransportTimeout("simulated lost response after create")
            if self.fail_after_create_once is not None:
                mode, self.fail_after_create_once = self.fail_after_create_once, None
                if mode == "reset":
                    raise TransportError("ConnectionResetError")
                return _json(int(mode), {"error": {"type": "SERVER_ERROR", "message": f"simulated {mode} after create"}})
            return _json(200, {"records": created})
        if method == "PATCH":
            updated = []
            for rec in (json_body or {}).get("records", []):
                rid = rec.get("id")
                if rid in self.records:
                    self.records[rid]["fields"].update(rec.get("fields") or {})
                    updated.append(self.records[rid])
            return _json(200, {"records": updated})
        return _json(405, {"error": {"type": "METHOD", "message": "no"}})


# ---------------------------------------------------------------------------
# Instantly
# ---------------------------------------------------------------------------

@dataclass
class FakeInstantly:
    leads: Dict[str, Dict[str, Any]] = field(default_factory=dict)   # email -> lead
    campaign_status: Dict[str, int] = field(default_factory=dict)    # campaign id -> status (1 active)
    lose_response_once: bool = False
    fail_after_create_once: Optional[Any] = None                     # 'reset' | 500
    requests: List[Dict[str, Any]] = field(default_factory=list)
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def request(self, method: str, url: str, *, headers=None, params=None, json_body=None, timeout=30.0) -> Response:
        path = urlsplit(url).path
        p = _params_dict(params)
        self.requests.append({"method": method, "path": path, "params": p, "body": json_body})
        if path.endswith("/leads") and method == "POST":
            email = str((json_body or {}).get("email") or "").lower()
            campaign = str((json_body or {}).get("campaign") or "")
            existing = self.leads.get(email)
            if existing is None:
                lead = {"id": str(uuid.uuid4()), "email": email, "campaign": campaign,
                        "timestamp_created": self.clock().isoformat(), "custom_variables": (json_body or {}).get("custom_variables", {})}
                self.leads[email] = lead
                if self.lose_response_once:
                    self.lose_response_once = False
                    raise TransportTimeout("simulated lost response after create")
                if self.fail_after_create_once is not None:
                    mode, self.fail_after_create_once = self.fail_after_create_once, None
                    if mode == "reset":
                        raise TransportError("ConnectionResetError")
                    return _json(int(mode), {"error": f"simulated {mode} after create"})
                return _json(200, lead)
            # 200 for an existing email, with the ORIGINAL timestamp (integration truth)
            return _json(200, existing)
        if path.endswith("/campaigns/search-by-contact"):
            email = p.get("search", [""])[0].lower()
            lead = self.leads.get(email)
            items = [{"id": lead["campaign"]}] if lead and lead.get("campaign") else []
            return _json(200, {"items": items})
        if "/campaigns/" in path and method == "GET":
            cid = path.rsplit("/", 1)[1]
            return _json(200, {"id": cid, "status": self.campaign_status.get(cid, 1)})
        return _json(404, {"error": "unknown"})
