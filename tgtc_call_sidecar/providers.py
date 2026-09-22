"""Provider access for the sidecar, behind an allow-list.

Instantly and Airtable are READ-ONLY here: the only calls that can leave the
process are listed in ``ALLOWED``. Anything else raises before any network I/O,
so the sidecar cannot create, change or delete a lead or a CRM record.
Apollo: the free People API Search, People Enrichment with phone reveal
(``poll_only``), and the free webhook-result poll -- nothing else.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import requests

APOLLO_BASE = "https://api.apollo.io/api/v1"
INSTANTLY_BASE = "https://api.instantly.ai/api/v2"
AIRTABLE_BASE = "https://api.airtable.com/v0"

#: (host, method, path prefix) -- the complete list of calls the sidecar may make.
ALLOWED: Tuple[Tuple[str, str, str], ...] = (
    ("api.apollo.io", "POST", "/api/v1/mixed_people/api_search"),
    ("api.apollo.io", "POST", "/api/v1/people/match"),
    ("api.apollo.io", "GET", "/api/v1/webhook_result/"),
    ("api.instantly.ai", "POST", "/api/v2/leads/list"),
    ("api.airtable.com", "GET", "/v0/"),
)


class ForbiddenCall(RuntimeError):
    pass


def check_allowed(method: str, url: str) -> None:
    u = urlparse(url)
    for host, m, prefix in ALLOWED:
        if u.hostname == host and method.upper() == m and u.path.startswith(prefix):
            return
    raise ForbiddenCall(f"sidecar may not call {method.upper()} {u.hostname}{u.path}")


class Http:
    """requests with the allow-list and bounded 429/5xx retries."""

    def __init__(self, session=None, sleep=time.sleep):
        self.s = session or requests.Session()
        self.sleep = sleep

    def request(self, method: str, url: str, **kw) -> requests.Response:
        check_allowed(method, url)
        for attempt in range(6):
            resp = self.s.request(method, url, timeout=60, **kw)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = float(resp.headers.get("Retry-After") or 0) or min(60.0, 2.0 ** attempt)
                self.sleep(wait)
                continue
            return resp
        return resp


# --- Apollo ------------------------------------------------------------------
@dataclass
class PollResult:
    state: str                 # ready | pending | failed
    payload: Dict[str, Any]
    retry_after: float = 5.0


class ApolloPhones:
    def __init__(self, api_key: str, http: Optional[Http] = None):
        self.key = api_key
        self.http = http or Http()

    def _h(self):
        return {"X-Api-Key": self.key, "Content-Type": "application/json", "Accept": "application/json",
                "Cache-Control": "no-cache"}

    def search(self, *, domain: str = "", organization_id: str = "", titles: List[str], per_page: int = 25,
               page: int = 1, similar_titles: bool = True) -> List[Dict[str, Any]]:
        """People API Search: documented as consuming no credits. Similar titles widen the
        free search only; every pick is still gated by the core's own title predicate."""
        params: List[tuple] = [("page", str(page)), ("per_page", str(per_page)),
                               ("include_similar_titles", "true" if similar_titles else "false"),
                               ("person_locations[]", "United States")]
        if organization_id:
            params.append(("organization_ids[]", organization_id))
        elif domain:
            params.append(("q_organization_domains_list[]", domain))
        else:
            return []
        params.extend(("person_titles[]", t) for t in titles)
        resp = self.http.request("POST", f"{APOLLO_BASE}/mixed_people/api_search", headers=self._h(), params=params)
        if resp.status_code != 200:
            return []
        people = (resp.json() or {}).get("people") or []
        return [p for p in people if isinstance(p, dict)]

    def reveal(self, person_id: str) -> Dict[str, Any]:
        """People Enrichment with phone reveal, results by polling (no webhook)."""
        params = {"id": person_id, "reveal_phone_number": "true", "poll_only": "true",
                  "reveal_personal_emails": "false"}
        resp = self.http.request("POST", f"{APOLLO_BASE}/people/match", headers=self._h(), params=params)
        body = {}
        try:
            body = resp.json() or {}
        except ValueError:
            pass
        return {"status": resp.status_code, "body": body}

    def poll(self, request_id: str) -> PollResult:
        resp = self.http.request("GET", f"{APOLLO_BASE}/webhook_result/{quote(str(request_id))}", headers=self._h())
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        if resp.status_code == 200:
            return PollResult("ready", body)
        if resp.status_code == 404 and body.get("error_code") == "result_pending":
            return PollResult("pending", body, float(body.get("retry_after_seconds") or 5))
        return PollResult("failed", {"status": resp.status_code, "error_code": body.get("error_code")})


def request_id_of(match_body: Dict[str, Any]) -> str:
    for holder in (match_body, match_body.get("person") or {}, match_body.get("meta") or {}):
        v = holder.get("request_id") if isinstance(holder, dict) else None
        if v:
            return str(v)
    return ""


# --- Instantly (read-only) -----------------------------------------------------
class InstantlyReadOnly:
    def __init__(self, api_key: str, http: Optional[Http] = None):
        self.key = api_key
        self.http = http or Http()

    def _post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        resp = self.http.request("POST", f"{INSTANTLY_BASE}/leads/list",
                                 headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
                                 json=body)
        resp.raise_for_status()
        return resp.json() or {}

    def leads(self, campaign_id: str, flt: Optional[str] = None) -> List[Dict[str, Any]]:
        out, after = [], None
        for _ in range(1000):
            body: Dict[str, Any] = {"campaign": campaign_id, "limit": 100}
            if flt:
                body["filter"] = flt
            if after:
                body["starting_after"] = after
            d = self._post(body)
            items = d.get("items") or []
            out.extend(items)
            after = d.get("next_starting_after")
            if not after or not items:
                return out
        raise RuntimeError("Instantly paging did not terminate")

    def workspace_search(self, text: str) -> List[Dict[str, Any]]:
        """Leads anywhere in the workspace (any campaign) matching an email or a name."""
        if not str(text or "").strip():
            return []
        return self._post({"search": str(text).strip(), "limit": 50}).get("items") or []


# --- Airtable (read-only) ------------------------------------------------------
class AirtableReadOnly:
    def __init__(self, token: str, base_id: str, table: str, http: Optional[Http] = None):
        self.token, self.base_id, self.table = token, base_id, table
        self.http = http or Http()

    def records(self, fields: List[str]) -> List[Dict[str, Any]]:
        out, offset = [], ""
        url = f"{AIRTABLE_BASE}/{self.base_id}/{quote(self.table)}"
        for _ in range(2000):
            params: List[tuple] = [("pageSize", "100")] + [("fields[]", f) for f in fields]
            if offset:
                params.append(("offset", offset))
            resp = self.http.request("GET", url, headers={"Authorization": f"Bearer {self.token}"}, params=params)
            resp.raise_for_status()
            d = resp.json() or {}
            out.extend(d.get("records") or [])
            offset = d.get("offset") or ""
            if not offset:
                return out
        raise RuntimeError("Airtable paging did not terminate")
