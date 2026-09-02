"""Read-only collectors for the two systems the pipeline delivers into.

Both collectors are **opt-in** and perform list/GET operations only. Nothing here
creates, updates, pauses, enrolls or deletes anything.

Why they exist
--------------

``sent_to_instantly`` cannot be reconstructed from the orchestrator's run
artifacts at all. Enrollment is performed by a *different* Railway service
(``run_approved.py`` on "GTM Approved Sync"), that service writes no run artifact,
and its volume-less container leaves nothing behind but log lines. Airtable's
``Status = Enrolled`` is no substitute: the base carries no enrolled-at timestamp,
so a status column cannot be attributed to a week.

Instantly itself does carry the fact, on the lead: ``timestamp_created`` is the
instant the lead entered the workspace. Counting leads whose ``timestamp_created``
falls inside the window is therefore the only defensible measurement of "sent to
Instantly this week", and it is also the *right* one -- Instantly answers 200 for
an email that already exists, so an accepted API call is not a delivery, but a new
``timestamp_created`` is.

Leads are listed per campaign with ``POST /leads/list`` and the singular
``campaign`` filter, which is the filter proven to work in production (the plural
``campaign_ids`` filter is ignored by the API and must never be relied on).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from weekly_report.timewindow import ReportingWindow, parse_instant

logger = logging.getLogger(__name__)

#: Hard ceiling on pagination so a misbehaving cursor can never spin forever.
_MAX_PAGES_PER_CAMPAIGN = 200
_PAGE_SIZE = 100


@dataclass
class CollectorResult:
    """What a collector managed to observe, and what it could not."""

    name: str
    enabled: bool
    ok: bool = False
    count: Optional[int] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    operations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "ok": self.ok,
            "count": self.count,
            "detail": dict(self.detail),
            "errors": list(self.errors),
            "read_only_operations": list(self.operations),
        }


def configured_campaign_ids(cfg: Any) -> List[str]:
    """Distinct Instantly campaign ids reachable from configuration.

    Reads every ``INSTANTLY_CAMPAIGN_*`` env name the router knows about, plus any
    size-band variant that is set, plus the default campaign. Order is stable.
    """
    import os

    names: List[str] = []
    for base in getattr(cfg, "CAMPAIGN_ENV_BY_BUCKET", {}).values():
        names.append(base)
        names.extend(f"{base}_{band}" for band in ("SMALL", "MID", "LARGE"))
    ids: List[str] = []
    for name in names:
        value = str(os.getenv(name, "") or "").strip()
        if value and value not in ids:
            ids.append(value)
    default = str(getattr(cfg, "INSTANTLY_CAMPAIGN_ID", "") or "").strip()
    if default and default not in ids:
        ids.append(default)
    return ids


def collect_instantly(
    window: ReportingWindow,
    *,
    cfg: Any,
    campaign_ids: Optional[Sequence[str]] = None,
    requester: Optional[Any] = None,
) -> CollectorResult:
    """Count leads whose ``timestamp_created`` falls inside ``window``.

    ``requester`` is injected in tests; in production it is
    ``http_utils.request_with_retry``.
    """
    result = CollectorResult(
        name="instantly",
        enabled=True,
        operations=["POST /leads/list (read-only listing)"],
    )
    api_key = str(getattr(cfg, "INSTANTLY_API_KEY", "") or "").strip()
    if not api_key:
        result.errors.append("INSTANTLY_API_KEY is not set")
        return result

    ids = list(campaign_ids) if campaign_ids else configured_campaign_ids(cfg)
    if not ids:
        result.errors.append(
            "no Instantly campaign ids are configured; set INSTANTLY_CAMPAIGN_* or pass "
            "campaign ids explicitly"
        )
        return result

    if requester is None:
        from http_utils import request_with_retry as requester  # noqa: PLC0415

    base = str(getattr(cfg, "INSTANTLY_BASE_URL", "") or "https://api.instantly.ai/api/v2").rstrip("/")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    per_campaign: Dict[str, int] = {}
    scanned = 0
    in_window = 0
    undated = 0
    for campaign_id in ids:
        cursor = ""
        pages = 0
        campaign_hits = 0
        try:
            while True:
                body: Dict[str, Any] = {
                    "campaign": campaign_id,
                    "limit": _PAGE_SIZE,
                    "distinct_contacts": False,
                }
                if cursor:
                    body["starting_after"] = cursor
                response = requester("POST", f"{base}/leads/list", headers=headers, json_body=body)
                payload = _as_dict(response)
                items = payload.get("items") or []
                for item in items:
                    scanned += 1
                    created = parse_instant((item or {}).get("timestamp_created"))
                    if created is None:
                        undated += 1
                        continue
                    if window.contains(created):
                        in_window += 1
                        campaign_hits += 1
                next_cursor = str(payload.get("next_starting_after") or "")
                pages += 1
                if not next_cursor or next_cursor == cursor or pages >= _MAX_PAGES_PER_CAMPAIGN:
                    if pages >= _MAX_PAGES_PER_CAMPAIGN and next_cursor:
                        result.errors.append(
                            f"campaign {campaign_id}: stopped at the {_MAX_PAGES_PER_CAMPAIGN}-page "
                            "safety ceiling; the count for this campaign is a floor, not a total"
                        )
                    break
                cursor = next_cursor
        except Exception as exc:  # noqa: BLE001 - a provider failure is a gap, not a crash
            result.errors.append(f"campaign {campaign_id}: {str(exc)[:200]}")
            continue
        per_campaign[campaign_id] = campaign_hits

    result.detail = {
        "campaigns_requested": ids,
        "campaigns_read": sorted(per_campaign),
        "leads_scanned": scanned,
        "leads_without_timestamp_created": undated,
        "per_campaign_in_window": per_campaign,
        "timestamp_field": "lead.timestamp_created",
    }
    if not per_campaign:
        return result
    result.ok = True
    result.count = in_window
    if undated:
        result.errors.append(
            f"{undated} lead(s) carry no timestamp_created and could not be attributed"
        )
    return result


def collect_airtable(
    window: ReportingWindow,
    *,
    cfg: Any,
    requester: Optional[Any] = None,
) -> CollectorResult:
    """Count Airtable rows *created* inside the window, by current Status.

    Airtable stamps every record with ``createdTime``, which is the row's own
    creation instant and is therefore attributable to a week. ``Status`` is a
    *current* value with no transition history, so it is reported as a snapshot of
    those rows -- never as "N rows were enrolled during the window".
    """
    result = CollectorResult(
        name="airtable",
        enabled=True,
        operations=["GET records (read-only listing)"],
    )
    token = str(getattr(cfg, "AIRTABLE_TOKEN", "") or "").strip()
    base_id = str(getattr(cfg, "AIRTABLE_BASE_ID", "") or "").strip()
    table = str(getattr(cfg, "AIRTABLE_TABLE_NAME", "") or "").strip()
    if not (token and base_id and table):
        result.errors.append("AIRTABLE_TOKEN / AIRTABLE_BASE_ID / AIRTABLE_TABLE_NAME are required")
        return result

    if requester is None:
        from http_utils import request_with_retry as requester  # noqa: PLC0415

    from urllib.parse import quote

    url = f"https://api.airtable.com/v0/{base_id}/{quote(table, safe='')}"
    headers = {"Authorization": f"Bearer {token}"}
    offset = ""
    scanned = 0
    created_in_window = 0
    by_status: Dict[str, int] = {}
    by_decision: Dict[str, int] = {}
    by_day: Dict[str, int] = {}
    pages = 0
    try:
        while True:
            params: List[tuple] = [("pageSize", 100)]
            if offset:
                params.append(("offset", offset))
            response = requester("GET", url, headers=headers, params=params)
            payload = _as_dict(response)
            for record in payload.get("records") or []:
                scanned += 1
                created = parse_instant((record or {}).get("createdTime"))
                if created is None or not window.contains(created):
                    continue
                created_in_window += 1
                fields = (record or {}).get("fields") or {}
                status = str(fields.get("Status") or "(blank)")
                decision = str(fields.get("Final Decision") or "(blank)")
                by_status[status] = by_status.get(status, 0) + 1
                by_decision[decision] = by_decision.get(decision, 0) + 1
                day = created.astimezone(window.start_local.tzinfo).strftime("%Y-%m-%d")
                by_day[day] = by_day.get(day, 0) + 1
            offset = str(payload.get("offset") or "")
            pages += 1
            if not offset or pages >= _MAX_PAGES_PER_CAMPAIGN:
                if pages >= _MAX_PAGES_PER_CAMPAIGN and offset:
                    result.errors.append(
                        "stopped at the pagination safety ceiling; the Airtable count is a floor"
                    )
                break
    except Exception as exc:  # noqa: BLE001
        result.errors.append(str(exc)[:200])
        return result

    result.ok = True
    result.count = created_in_window
    result.detail = {
        "records_scanned": scanned,
        "created_in_window": created_in_window,
        "by_current_status": dict(sorted(by_status.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_final_decision": dict(sorted(by_decision.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_local_day": dict(sorted(by_day.items())),
        "timestamp_field": "record.createdTime",
        "status_caveat": (
            "Status is the row's CURRENT value, not a transition that happened during "
            "the window; Airtable stores no status-change timestamp."
        ),
    }
    return result


def disabled(name: str, reason: str) -> CollectorResult:
    """A collector the operator did not switch on."""
    return CollectorResult(name=name, enabled=False, ok=False, errors=[reason])


def _as_dict(response: Any) -> Dict[str, Any]:
    """Accept either a ``requests.Response`` or an already-decoded mapping."""
    if isinstance(response, dict):
        return response
    from http_utils import safe_json  # noqa: PLC0415

    return safe_json(response)


def iter_nonempty(values: Iterable[Optional[str]]) -> List[str]:
    return [v for v in (str(x or "").strip() for x in values) if v]
