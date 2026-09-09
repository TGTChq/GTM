"""Fantastic Direct API client: one bounded page request with receipts-grade output.

Contract evidenced in ``fantastic_jobs_adapter.py`` at 5d87851 (INTEGRATION_MAP §4.1).
This client never decides approval, never persists anything, and never sends a
title or description keyword filter. It strips recruiter/hiring-manager PII from
every row before returning it.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .http import Response, Transport, TransportError, TransportTimeout

ENDPOINT_JOB_BOARDS = "/v1/active-jb"
ENDPOINT_ATS = "/v1/active-ats"
ENDPOINT_COUNT = "/v1/active-jb-count"

PII_KEYS = (
    "recruiter_name", "recruiter_title", "recruiter_url", "recruiter_email",
    "ai_hiring_manager_name", "ai_hiring_manager_email_address", "ai_hiring_manager_title",
    "ai_hiring_manager_linkedin", "hiring_manager", "hiring_manager_email",
)


class FantasticAuthError(Exception):
    """401/403: the key or its scope is rejected. Stop the source."""


class FantasticQuotaError(Exception):
    """429: rate limit or plan quota. Keep what was received; stop the source."""


class FantasticRequestError(Exception):
    def __init__(self, stage: str, code: str, status: Optional[int] = None):
        super().__init__(f"{stage}:{code}")
        self.stage, self.code, self.status = stage, code, status


@dataclass(frozen=True)
class Quota:
    jobs_limit: Optional[int] = None
    jobs_remaining: Optional[int] = None
    requests_limit: Optional[int] = None
    requests_remaining: Optional[int] = None
    next_billing_date: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"jobs_limit": self.jobs_limit, "jobs_remaining": self.jobs_remaining,
                "requests_limit": self.requests_limit, "requests_remaining": self.requests_remaining,
                "next_billing_date": self.next_billing_date}


@dataclass
class Page:
    rows: List[Dict[str, Any]]
    quota: Quota
    status: int
    pii_fields_dropped: int = 0
    retries: int = 0


def read_quota(resp: Response) -> Quota:
    def geti(name: str) -> Optional[int]:
        raw = resp.header(name)
        try:
            return int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return None
    nbd = resp.header("x-api-next-billing-date")
    return Quota(
        jobs_limit=geti("x-api-jobs-limit"), jobs_remaining=geti("x-api-jobs-remaining"),
        requests_limit=geti("x-api-requests-limit"), requests_remaining=geti("x-api-requests-remaining"),
        next_billing_date=str(nbd).strip() if nbd else None,
    )


def strip_pii(record: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    cleaned = dict(record)
    dropped = 0
    for key in list(cleaned.keys()):
        if key in PII_KEYS or key.startswith("recruiter_") or key.startswith("ai_hiring_manager"):
            cleaned.pop(key, None)
            dropped += 1
    return cleaned, dropped


class FantasticClient:
    def __init__(self, transport: Transport, *, base_url: str, api_key: str, timeout: float = 30.0,
                 max_retries: int = 2, sleep: Callable[[float], None] = time.sleep):
        self._t = transport
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout
        self._retries = max(0, max_retries)
        self._sleep = sleep

    def fetch_page(self, endpoint: str, params: Dict[str, Any]) -> Page:
        """One page. Retries 408/5xx and network errors a bounded number of times.

        A timeout on the LAST attempt is re-raised as ``TransportTimeout`` so the
        caller records the attempt as uncertain (the rows may have been billed).
        """
        url = f"{self._base}{endpoint}"
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        attempts = self._retries + 1
        for attempt in range(attempts):
            try:
                resp = self._t.request("GET", url, headers=headers, params=params, timeout=self._timeout)
            except TransportTimeout:
                if attempt + 1 < attempts:
                    self._sleep(min(4.0, (2 ** attempt) * 0.2) + random.random() * 0.05)
                    continue
                raise
            except TransportError as exc:
                if attempt + 1 < attempts:
                    self._sleep(min(4.0, (2 ** attempt) * 0.2))
                    continue
                raise FantasticRequestError("dispatch", f"network_error:{exc}", None) from None
            if resp.status in (401, 403):
                raise FantasticAuthError(f"auth_failed_status_{resp.status}")
            if resp.status == 429:
                raise FantasticQuotaError("rate_limited_429")
            if resp.status in (408, 500, 502, 503, 504) and attempt + 1 < attempts:
                self._sleep(min(4.0, (2 ** attempt) * 0.2))
                continue
            if resp.status != 200:
                raise FantasticRequestError("http_response", f"http_{resp.status}", resp.status)
            payload = resp.json()
            if payload is None:
                raise FantasticRequestError("json_parsing", "malformed_json", resp.status)
            rows = payload if isinstance(payload, list) else (
                payload.get("jobs") or payload.get("data") or payload.get("results") or [])
            if not isinstance(rows, list):
                raise FantasticRequestError("schema", "unexpected_schema", resp.status)
            cleaned: List[Dict[str, Any]] = []
            dropped = 0
            for row in rows:
                if isinstance(row, dict):
                    row2, d = strip_pii(row)
                    cleaned.append(row2)
                    dropped += d
            return Page(rows=cleaned, quota=read_quota(resp), status=resp.status,
                        pii_fields_dropped=dropped, retries=attempt)
        raise FantasticRequestError("dispatch", "exhausted_retries", None)

    def count(self, params: Dict[str, Any]) -> Quota:
        """Zero-job-credit probe: the headers are the payload."""
        page = self.fetch_page(ENDPOINT_COUNT, params)
        return page.quota


def build_window_params(*, lower_iso: str, upper_iso: str, limit: int, offset: int, time_frame: str,
                        source: Optional[str] = None, location: Optional[str] = "United States",
                        exclude_ats_duplicate: bool = True) -> Dict[str, Any]:
    """Window request WITHOUT title/description filters.

    ``date_created_gte/lt`` are proven-honoured bounds; ``time_frame`` is intersected
    by the provider and must cover the window. ``location`` is a geography filter
    whose semantics match the US-market policy; its coverage effect is recorded in
    the request params so it can be measured, not assumed.
    """
    params: Dict[str, Any] = {
        "time_frame": time_frame,
        "date_created_gte": lower_iso,
        "date_created_lt": upper_iso,
        "limit": int(limit),
        "offset": int(offset),
        "description_format": "text",
        "include_basic_organization_details": "true",
    }
    if source:
        params["source"] = source
    if exclude_ats_duplicate:
        params["exclude_ats_duplicate"] = "true"
    if location:
        params["location"] = location
    return params
