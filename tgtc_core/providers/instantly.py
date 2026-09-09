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
            return InstantlyResult(False, None, message=str(exc)[:200])
        body = resp.json()
        if 200 <= resp.status < 300:
            return InstantlyResult(True, resp.status, data=body if isinstance(body, dict) else {"items": body})
        return InstantlyResult(False, resp.status, data=body if isinstance(body, dict) else {}, message=(resp.text or "")[:300])

    def create_lead(self, payload: Dict[str, Any]) -> InstantlyResult:
        unknown = set(payload) - DOCUMENTED_LEAD_FIELDS
        if unknown:
            raise ValueError(f"undocumented Instantly lead fields: {sorted(unknown)}")
        return self._call("POST", "/leads", json_body=payload)

    def search_by_contact(self, email: str) -> Tuple[InstantlyResult, Tuple[str, ...]]:
        result = self._call("GET", "/campaigns/search-by-contact", params={"search": email})
        if not result.ok:
            return result, ()
        items = result.data.get("items") if isinstance(result.data, dict) else None
        if not isinstance(items, list):
            return result, ()
        return result, tuple(str(c.get("id") or "") for c in items if isinstance(c, dict) and c.get("id"))

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
