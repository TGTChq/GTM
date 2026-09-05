"""Read-only outcome ingestion for Outbound Wave 1.

``measurement.py`` joins outcomes; its docstring says plainly that it "never
fetches them". Nothing fetched them, so the experiment could compute a
denominator and never a numerator. This module is the missing half.

It performs listing reads only -- ``POST /leads/list`` with the singular
``campaign`` filter, the one proven to work in production -- and returns a mapping
the analyzer already understands. It cannot enroll, pause, update, or delete
anything, and it holds no write path at all.

Two design rules, both about not fabricating a numerator:

**A field we did not observe is absent, never False.** ``_accumulate`` counts a
falsy outcome as "did not happen", so writing ``positive_reply: False`` for a lead
whose interest status we could not classify would quietly deflate the treatment
arm. Unclassifiable leads carry no key at all.

**The raw provider value travels with every row.** ``instantly_status`` and
``lt_interest_status`` are carried verbatim beside the derived booleans, so a
mapping that turns out to be wrong can be re-derived from a saved collection
without re-reading Instantly -- which matters because the interest enum below is
DECLARED, not measured (see ``INTEREST_*``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

#: Hard ceiling on pagination so a misbehaving cursor cannot spin forever. Same
#: value and reasoning as ``weekly_report.external``.
_MAX_PAGES_PER_CAMPAIGN = 200
_PAGE_SIZE = 100

#: Instantly LEAD status. Observed in production: every lead sampled from a live
#: campaign carried ``3``. The negative codes are the documented terminal states.
STATUS_ACTIVE = 1
STATUS_PAUSED = 2
STATUS_COMPLETED = 3
STATUS_BOUNCED = -1
STATUS_UNSUBSCRIBED = -2
STATUS_SKIPPED = -3

#: Instantly LEAD INTEREST status (``lt_interest_status``).
#:
#: DECLARED, NOT MEASURED. Only ``0`` and ``1`` appeared in the production sample,
#: so the rest of this mapping comes from Instantly's documented enum rather than
#: from observation here. That is why every row keeps its raw value: if this is
#: wrong, a saved collection can be reclassified offline, with no second read and
#: no re-derived denominator.
#:
#: A value that is not in any set below is UNCLASSIFIED -- the row then carries no
#: ``positive_reply`` key rather than a False one.
INTEREST_POSITIVE = frozenset({1, 2, 3, 4})       # interested / meeting / met / won
INTEREST_MEETING_BOOKED = frozenset({2, 3, 4})    # booked, completed, won
INTEREST_OPPORTUNITY = frozenset({3, 4})          # meeting completed, won
INTEREST_FULFILLED = frozenset({4})               # won
INTEREST_NEGATIVE = frozenset({-1, -2, -3})       # not interested / wrong person / lost
INTEREST_NEUTRAL = frozenset({0})                 # out of office

#: Every key ``measurement._accumulate`` reads, restated so a drift between the
#: two modules is a test failure rather than a silently missing metric.
OUTCOME_FIELDS = (
    "delivered", "bounced", "replied", "positive_reply", "reply_step",
    "meeting_booked", "opportunity_created", "fulfilled",
)


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_contact_key(email: Any) -> str:
    """The join key. Lowercased, trimmed email.

    ``measurement.analyze`` joins on ``RandomizationRow.contact_key``, and
    ``randomization_row(..., contact_key=...)`` accepts an override precisely so
    the frame can be keyed on whatever the outcome source can supply. Instantly
    can supply an email and cannot supply our ``Lead Key``, so the email is the
    key on both sides. Build the frame with the same normalization.
    """
    return str(email or "").strip().lower()


def outcome_from_lead(lead: Mapping[str, Any]) -> Dict[str, Any]:
    """One Instantly lead -> one outcome row for ``measurement.analyze``.

    Derivations, each from a field the provider actually returns:

    ``delivered``  a sequence step executed for this lead
                   (``status_summary.lastStep.timestamp_executed``), OR the lead
                   replied -- you cannot reply to an email that never arrived, and
                   that second clause keeps a missing ``status_summary`` from
                   understating the guardrail.
    ``bounced``    lead status ``-1``.
    ``replied``    ``email_reply_count > 0``.
    ``reply_step`` ``email_replied_step``, which is the sequence step the reply
                   came at -- the stratum Wave 1 asks for by name.
    the rest       classified from ``lt_interest_status`` via the sets above.
    """
    out: Dict[str, Any] = {}
    status = _as_int(lead.get("status"))
    interest = _as_int(lead.get("lt_interest_status"))
    replies = _as_int(lead.get("email_reply_count")) or 0

    last_step = ((lead.get("status_summary") or {}).get("lastStep") or {}) \
        if isinstance(lead.get("status_summary"), Mapping) else {}
    executed = str((last_step or {}).get("timestamp_executed") or "").strip()

    out["contact_key"] = normalize_contact_key(
        lead.get("email") or (lead.get("payload") or {}).get("email"))
    out["campaign"] = str(lead.get("campaign") or "")
    out["timestamp_created"] = str(lead.get("timestamp_created") or "")
    # RAW, always. The derived booleans are an interpretation; these are the
    # observation, and they are what makes a wrong interpretation recoverable.
    out["instantly_status"] = status
    out["lt_interest_status"] = interest
    out["email_reply_count"] = replies
    out["email_open_count"] = _as_int(lead.get("email_open_count")) or 0
    out["email_click_count"] = _as_int(lead.get("email_click_count")) or 0

    out["delivered"] = bool(executed) or replies > 0
    if status is not None:
        out["bounced"] = status == STATUS_BOUNCED
    out["replied"] = replies > 0
    if replies > 0:
        step = _as_int(lead.get("email_replied_step"))
        if step is not None:
            out["reply_step"] = step

    # Interest-derived outcomes. Absent when we cannot classify: see the module
    # docstring -- a False here is counted as "did not happen".
    if interest is not None and (
            interest in INTEREST_POSITIVE or interest in INTEREST_NEGATIVE
            or interest in INTEREST_NEUTRAL):
        out["positive_reply"] = interest in INTEREST_POSITIVE
        out["meeting_booked"] = interest in INTEREST_MEETING_BOOKED
        out["opportunity_created"] = interest in INTEREST_OPPORTUNITY
        out["fulfilled"] = interest in INTEREST_FULFILLED
    return out


@dataclass
class OutcomeCollection:
    """What the collector observed, and what it could not.

    ``ok`` is False whenever ANY campaign failed or was truncated. A partial
    collection is still returned -- the rows it did read are real -- but the
    caller must not present a rate computed from it as complete.
    """

    rows: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    campaigns_read: List[str] = field(default_factory=list)
    campaigns_failed: List[str] = field(default_factory=list)
    campaigns_truncated: List[str] = field(default_factory=list)
    leads_scanned: int = 0
    leads_unkeyed: int = 0
    #: Replied, but the interest value was not in any mapped set. NOT "has not
    #: replied yet" -- see the collector.
    leads_unclassified_interest: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.campaigns_failed or self.campaigns_truncated or self.errors)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "contacts": len(self.rows),
            "leads_scanned": self.leads_scanned,
            "leads_without_an_email_key": self.leads_unkeyed,
            "replies_with_unclassified_interest": self.leads_unclassified_interest,
            "campaigns_read": list(self.campaigns_read),
            "campaigns_failed": list(self.campaigns_failed),
            "campaigns_truncated": list(self.campaigns_truncated),
            "errors": list(self.errors),
            "read_only_operations": ["POST /leads/list (listing only)"],
            "interest_mapping": {
                "positive": sorted(INTEREST_POSITIVE),
                "meeting_booked": sorted(INTEREST_MEETING_BOOKED),
                "opportunity_created": sorted(INTEREST_OPPORTUNITY),
                "fulfilled": sorted(INTEREST_FULFILLED),
                "negative": sorted(INTEREST_NEGATIVE),
                "neutral": sorted(INTEREST_NEUTRAL),
                "note": "declared from Instantly's documented enum, not measured; "
                        "every row carries its raw lt_interest_status so a wrong "
                        "mapping can be re-derived without re-reading",
            },
        }


def collect_outcomes(
    campaign_ids: Sequence[str],
    *,
    api_key: str,
    base_url: str = "https://api.instantly.ai/api/v2",
    requester: Optional[Any] = None,
    since: str = "",
    deadline: Optional[float] = None,
    clock: Any = None,
) -> OutcomeCollection:
    """Read outcomes for ``campaign_ids``. Listing reads only.

    ``since`` (ISO-8601) drops leads created before it, which is how the
    experiment watermark is applied -- a lead enrolled before Wave 1 started is
    not in the frame and must not be in the numerator either.

    ``deadline`` is a ``time.monotonic()`` value after which no further request is
    issued; the campaigns not reached are named rather than silently contributing
    zero.
    """
    result = OutcomeCollection()
    api_key = str(api_key or "").strip()
    if not api_key:
        result.errors.append("INSTANTLY_API_KEY is not set")
        return result
    ids = [str(c).strip() for c in (campaign_ids or []) if str(c).strip()]
    if not ids:
        result.errors.append("no campaign ids were given")
        return result

    if requester is None:
        from http_utils import request_with_retry as requester  # noqa: PLC0415
    if clock is None:
        import time  # noqa: PLC0415

        clock = time.monotonic

    base = str(base_url or "").rstrip("/")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    floor = str(since or "").strip()

    def out_of_time() -> bool:
        return deadline is not None and clock() >= deadline

    seen_campaigns: set = set()
    for campaign_id in ids:
        # Control and challenger can share a campaign id (customer_success and
        # customer_support already do). Reading it twice would double the scan
        # count and re-key the same leads.
        if campaign_id in seen_campaigns:
            continue
        seen_campaigns.add(campaign_id)
        if out_of_time():
            result.campaigns_failed.append(campaign_id)
            result.errors.append(f"{campaign_id}: deadline reached before it was read")
            continue
        cursor = ""
        pages = 0
        # Accumulate per campaign and commit only on a clean read: a campaign that
        # fails half way must contribute nothing, or the totals disagree with the
        # per-campaign breakdown.
        staged: Dict[str, Dict[str, Any]] = {}
        scanned = unkeyed = unclassified = 0
        truncated = False
        try:
            while True:
                body: Dict[str, Any] = {
                    "campaign": campaign_id,
                    "limit": _PAGE_SIZE,
                    "distinct_contacts": False,
                }
                if cursor:
                    body["starting_after"] = cursor
                response = requester("POST", f"{base}/leads/list",
                                     headers=headers, json_body=body)
                payload = _as_dict(response)
                for item in payload.get("items") or []:
                    scanned += 1
                    if not isinstance(item, Mapping):
                        continue
                    created = str(item.get("timestamp_created") or "")
                    if floor and created and created < floor:
                        continue
                    row = outcome_from_lead(item)
                    key = row.get("contact_key") or ""
                    if not key:
                        unkeyed += 1
                        continue
                    # UNCLASSIFIED means "it replied and we could not read the
                    # interest", not "it has not replied yet". A lead that has
                    # not replied has no interest status by definition, and
                    # counting all of those would report a fresh batch as 100%
                    # unclassified -- alarming, and about nothing.
                    if row.get("replied") and "positive_reply" not in row:
                        unclassified += 1
                    staged[key] = row
                next_cursor = str(payload.get("next_starting_after") or "")
                pages += 1
                if not next_cursor or next_cursor == cursor:
                    break
                if pages >= _MAX_PAGES_PER_CAMPAIGN or out_of_time():
                    truncated = True
                    break
                cursor = next_cursor
        except Exception as exc:  # noqa: BLE001 - a provider failure is a gap, not a crash
            result.campaigns_failed.append(campaign_id)
            result.errors.append(f"{campaign_id}: {type(exc).__name__}: {str(exc)[:160]}")
            logger.warning("Wave 1 outcome read failed for %s: %s",
                           campaign_id, type(exc).__name__)
            continue

        result.rows.update(staged)
        result.campaigns_read.append(campaign_id)
        result.leads_scanned += scanned
        result.leads_unkeyed += unkeyed
        result.leads_unclassified_interest += unclassified
        if truncated:
            result.campaigns_truncated.append(campaign_id)
    return result


def _as_dict(response: Any) -> Dict[str, Any]:
    if response is None:
        raise RuntimeError("no response")
    status = getattr(response, "status_code", None)
    if status is not None and int(status) != 200:
        raise RuntimeError(f"HTTP {status}")
    payload = response.json() if hasattr(response, "json") else response
    return payload if isinstance(payload, dict) else {}


def wave1_campaign_ids(cfg: Any) -> List[str]:
    """Every campaign id BOTH arms can deliver into, deduplicated.

    The experiment needs the control ids as much as the challenger ids: a lift is
    a comparison, and reading only the treatment arm measures nothing.
    """
    import os  # noqa: PLC0415

    ids: List[str] = []
    for env_name in (getattr(cfg, "CAMPAIGN_ENV_BY_BUCKET", {}) or {}).values():
        value = str(os.getenv(env_name, "") or "").strip()
        if value and value not in ids:
            ids.append(value)
    challengers = getattr(cfg, "OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS", None)
    if isinstance(challengers, dict):
        for value in challengers.values():
            value = str(value or "").strip()
            if value and value not in ids:
                ids.append(value)
    return ids


def collect_for_experiment(cfg: Any, **kwargs: Any) -> OutcomeCollection:
    """``collect_outcomes`` wired to this deployment's configuration.

    Applies ``OUTBOUND_WAVE1_MIN_RECORD_CREATED_AT`` as the ``since`` floor, so
    the numerator covers exactly the population the frame does.
    """
    kwargs.setdefault("since", str(
        getattr(cfg, "OUTBOUND_WAVE1_MIN_RECORD_CREATED_AT", "") or "").strip())
    return collect_outcomes(
        wave1_campaign_ids(cfg),
        api_key=str(getattr(cfg, "INSTANTLY_API_KEY", "") or ""),
        base_url=str(getattr(cfg, "INSTANTLY_BASE_URL", "")
                     or "https://api.instantly.ai/api/v2"),
        **kwargs,
    )


def as_outcome_map(collection: OutcomeCollection) -> Dict[str, Dict[str, Any]]:
    """The exact shape ``measurement.analyze(frame, outcomes)`` expects."""
    return dict(collection.rows)


def iter_rows(collection: OutcomeCollection) -> Iterable[Dict[str, Any]]:
    return collection.rows.values()
