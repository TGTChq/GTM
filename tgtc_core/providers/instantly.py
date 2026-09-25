"""Instantly v2 client (INTEGRATION_MAP §4.4).

Integration truths ported from ``instantly_client.py`` at 5d87851: ``POST /leads``
returns 200 for an address already in the workspace, so a 2xx is not a delivery;
``timestamp_created`` against the request start tells created from pre-existing;
``GET /campaigns/search-by-contact`` is the only truthful membership lookup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .http import Response, Transport, TransportError, TransportTimeout

NEWLY_CREATED = "instantly_newly_created"
ALREADY_IN_TARGET_CAMPAIGN = "instantly_already_in_target_campaign"
EXISTING_OTHER_CAMPAIGN = "instantly_existing_other_campaign"
ALREADY_EXISTS_WORKSPACE = "instantly_already_exists_workspace"
MEMBERSHIP_UNKNOWN = "instantly_membership_unknown"
API_ERROR = "instantly_api_error"
DELIVERED_MEMBERSHIPS = (NEWLY_CREATED, ALREADY_IN_TARGET_CAMPAIGN)
_CREATION_SKEW = timedelta(seconds=120)

#: Only documented fields leave this module.
DOCUMENTED_LEAD_FIELDS = frozenset({
    "campaign", "email", "personalization", "website", "last_name", "first_name", "company_name",
    "job_title", "phone", "lt_interest_status", "pl_value_lead", "list_id", "assigned_to",
    "skip_if_in_workspace", "skip_if_in_campaign", "skip_if_in_list", "blocklist_id",
    "verify_leads_for_lead_finder", "verify_leads_on_import", "custom_variables",
})


@dataclass
class InstantlyResult:
    ok: bool
    status: Optional[int]
    data: Dict[str, Any] = field(default_factory=dict)
    message: str = ""
    uncertain: bool = False

    def summary(self) -> Dict[str, Any]:
        return {"ok": self.ok, "status": self.status, "message": self.message[:300], "uncertain": self.uncertain}


def _parse_ts(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def classify_membership(data: Dict[str, Any], *, target_campaign: str,
                        request_started_at: datetime) -> Tuple[str, str, str, str]:
    """Fails closed: anything not provably created by THIS request is not net-new."""
    if not isinstance(data, dict):
        return MEMBERSHIP_UNKNOWN, "", "", ""
    lead_id = str(data.get("id") or "")
    lead_campaign = str(data.get("campaign") or "")
    created_raw = str(data.get("timestamp_created") or "")
    created = _parse_ts(created_raw)
    if created is None:
        return MEMBERSHIP_UNKNOWN, lead_id, lead_campaign, created_raw
    if created >= request_started_at - _CREATION_SKEW:
        return NEWLY_CREATED, lead_id, lead_campaign, created_raw
    if not lead_campaign:
        return ALREADY_EXISTS_WORKSPACE, lead_id, lead_campaign, created_raw
    if target_campaign and lead_campaign == target_campaign:
        return ALREADY_IN_TARGET_CAMPAIGN, lead_id, lead_campaign, created_raw
    return EXISTING_OTHER_CAMPAIGN, lead_id, lead_campaign, created_raw


class InstantlyClient:
    def __init__(self, transport: Transport, *, base_url: str, api_key: str, timeout: float = 30.0):
        self._t = transport
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json", "Accept": "application/json"}

    def _call(self, method: str, path: str, *, params: Any = None, json_body: Any = None) -> InstantlyResult:
        try:
            resp = self._t.request(method, f"{self._base}{path}", headers=self._headers(), params=params,
                                   json_body=json_body, timeout=self._timeout)
        except TransportTimeout:
            return InstantlyResult(False, None, message="timeout", uncertain=method != "GET")
        except TransportError as exc:
            # A lost connection on a create may have reached the server: uncertain (R05).
            return InstantlyResult(False, None, message=str(exc)[:200], uncertain=method != "GET")
        body = resp.json()
        if 200 <= resp.status < 300:
            return InstantlyResult(True, resp.status, data=body if isinstance(body, dict) else {"items": body})
        if method != "GET" and resp.status in (408, 500, 502, 503, 504):
            return InstantlyResult(False, resp.status, data=body if isinstance(body, dict) else {}, message=(resp.text or "")[:300], uncertain=True)
        return InstantlyResult(False, resp.status, data=body if isinstance(body, dict) else {}, message=(resp.text or "")[:300])

    def create_lead(self, payload: Dict[str, Any]) -> InstantlyResult:
        unknown = set(payload) - DOCUMENTED_LEAD_FIELDS
        if unknown:
            raise ValueError(f"undocumented Instantly lead fields: {sorted(unknown)}")
        return self._call("POST", "/leads", json_body=payload)

    def move_lead(self, lead_id: str, *, from_campaign: str, to_campaign: str) -> InstantlyResult:
        """Move ONE lead from one campaign to another.

        The destination decides what the person receives next, so this is how a
        follow-up happens without replaying a finished sequence: the follow-up campaign
        holds a single step. The API refuses a move whose source and destination match,
        and refuses ``ids`` without a source, so both are always sent.
        """
        if not lead_id or not from_campaign or not to_campaign:
            raise ValueError("move_lead needs a lead id, a source campaign and a destination campaign")
        if from_campaign == to_campaign:
            raise ValueError("refusing to move a lead into the campaign it is already in")
        return self._call("POST", "/leads/move", json_body={"campaign": from_campaign, "ids": [lead_id],
                                                            "to_campaign_id": to_campaign})

    def search_by_contact(self, email: str) -> Tuple[InstantlyResult, Tuple[str, ...]]:
        result = self._call("GET", "/campaigns/search-by-contact", params={"search": email})
        if not result.ok:
            return result, ()
        items = result.data.get("items") if isinstance(result.data, dict) else None
        if not isinstance(items, list):
            return result, ()
        return result, tuple(str(c.get("id") or "") for c in items if isinstance(c, dict) and c.get("id"))

    def list_received_emails(self, *, limit: int = 100, starting_after: Optional[str] = None) -> InstantlyResult:
        """One page of replies, newest first. A GET: it changes nothing in Instantly."""
        params = {"email_type": "received", "limit": int(limit)}
        if starting_after:
            params["starting_after"] = starting_after
        return self._call("GET", "/emails", params=params)

    def emails_for(self, email: str, *, campaign_id: str = "", limit: int = 50) -> InstantlyResult:
        """Every message exchanged with this address, newest first.

        Used as the follow-up's receipt: a message that exists here was actually sent by
        Instantly, which is a different claim from "we moved the contact".
        """
        params: Dict[str, Any] = {"lead": email, "limit": int(limit)}
        if campaign_id:
            params["campaign_id"] = campaign_id
        return self._call("GET", "/emails", params=params)

    def get_lead(self, lead_id: str) -> InstantlyResult:
        """Read one lead back. This is how a move is confirmed rather than assumed.

        ``POST /leads/move`` answers 200 with a background job whose status is
        ``pending``, so the response says the work was accepted, not that it is done.
        """
        if not lead_id:
            raise ValueError("get_lead needs a lead id")
        return self._call("GET", f"/leads/{lead_id}")

    def campaign_analytics(self) -> InstantlyResult:
        """Every campaign with its stored-lead count, in ONE request.

        This is the only cheap way to know how full the workspace is. The plan caps
        STORED contacts, and the API otherwise only admits it is full by refusing a
        create with "Lead limit reached. Remaining uploads: 0" -- which is too late to
        act on. Summing `leads_count` here answers the question before the run spends.
        """
        return self._call("GET", "/campaigns/analytics")

    def list_campaigns(self, *, limit: int = 100, starting_after: Optional[str] = None) -> InstantlyResult:
        params: Dict[str, Any] = {"limit": int(limit)}
        if starting_after:
            params["starting_after"] = starting_after
        return self._call("GET", "/campaigns", params=params)

    def list_campaign_leads(self, campaign_id: str, *, limit: int = 100,
                            starting_after: Optional[str] = None) -> InstantlyResult:
        body: Dict[str, Any] = {"campaign": campaign_id, "limit": int(limit)}
        if starting_after:
            body["starting_after"] = starting_after
        return self._call("POST", "/leads/list", json_body=body)

    def delete_lead(self, lead_id: str) -> InstantlyResult:
        """Remove one contact, which returns its slot to the plan (measured: ~5.5 min).

        The API refuses a JSON content type on a body-less DELETE, so this sends no
        content type at all.
        """
        if not lead_id:
            raise ValueError("delete_lead needs a lead id")
        try:
            resp = self._t.request("DELETE", f"{self._base}/leads/{lead_id}",
                                   headers={"Authorization": f"Bearer {self._key}"},
                                   params=None, json_body=None, timeout=self._timeout)
        except TransportTimeout:
            return InstantlyResult(False, None, message="timeout", uncertain=True)
        except TransportError as exc:
            return InstantlyResult(False, None, message=str(exc)[:200], uncertain=True)
        body = resp.json()
        ok = 200 <= resp.status < 300
        return InstantlyResult(ok, resp.status, data=body if isinstance(body, dict) else {},
                               message="" if ok else (resp.text or "")[:300])

    def get_campaign(self, campaign_id: str) -> InstantlyResult:
        return self._call("GET", f"/campaigns/{campaign_id}")

    def resolve_membership(self, email: str, target_campaign: str) -> Tuple[str, Tuple[str, ...]]:
        """Authoritative membership for one email; fails closed to UNKNOWN."""
        result, campaigns = self.search_by_contact(email)
        if not result.ok:
            return MEMBERSHIP_UNKNOWN, ()
        if not campaigns:
            return ALREADY_EXISTS_WORKSPACE, ()
        if target_campaign in campaigns:
            return ALREADY_IN_TARGET_CAMPAIGN, campaigns
        return EXISTING_OTHER_CAMPAIGN, campaigns
