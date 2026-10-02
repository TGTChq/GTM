"""Airtable client for the existing Leads table (INTEGRATION_MAP §4.3).

Batches of at most ten records, ``typecast: true``, 429 backoff honouring the
documented 30-second wait, and a lookup by ``Lead Key`` used for reconciliation
after a lost response. Field names are the production ones; ``Status`` written by
the core is always ``Approved``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional
from urllib.parse import quote

from .http import Response, Transport, TransportError, TransportTimeout

RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


@dataclass
class AirtableResult:
    ok: bool
    status: Optional[int]
    records: List[Dict[str, Any]] = field(default_factory=list)
    error_type: str = ""
    message: str = ""
    uncertain: bool = False   # a timeout after the request was sent

    def summary(self) -> Dict[str, Any]:
        return {"ok": self.ok, "status": self.status, "error_type": self.error_type,
                "message": self.message[:300], "uncertain": self.uncertain, "records": len(self.records)}


def _formula_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class AirtableClient:
    def __init__(self, transport: Transport, *, base_url: str, token: str, base_id: str, table: str,
                 timeout: float = 30.0, max_retries: int = 2, sleep: Callable[[float], None] = time.sleep):
        self._t = transport
        self._url = f"{base_url.rstrip('/')}/{base_id}/{quote(table, safe='')}"
        self._token = token
        self._timeout = timeout
        self._retries = max_retries
        self._sleep = sleep

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def _request(self, method: str, *, params: Any = None, json_body: Any = None) -> AirtableResult:
        """GET is retried. A create/update (POST/PATCH) is NOT retried inside the client on
        any failure that could have reached the server -- connection reset, timeout,
        408 or 5xx -- because Airtable has no idempotency key: a blind second POST after
        an accepted first one duplicates the row (review finding R05). Those outcomes
        come back ``uncertain`` so the outbox reconciles by Lead Key first. A 429 is
        rejected before processing and is retried after the documented wait."""
        mutating = method.upper() in ("POST", "PATCH", "DELETE")
        for attempt in range(self._retries + 1):
            try:
                resp = self._t.request(method, self._url, headers=self._headers(), params=params,
                                       json_body=json_body, timeout=self._timeout)
            except TransportTimeout:
                return AirtableResult(False, None, error_type="timeout", message="timeout", uncertain=mutating)
            except TransportError as exc:
                if mutating:
                    return AirtableResult(False, None, error_type="connection_lost", message=str(exc)[:200], uncertain=True)
                if attempt < self._retries:
                    self._sleep(1.0 * (attempt + 1))
                    continue
                return AirtableResult(False, None, error_type="network", message=str(exc)[:200])
            if resp.status == 429 and attempt < self._retries:
                self._sleep(30.0)
                continue
            if resp.status in RETRYABLE and resp.status != 429:
                if mutating:
                    parsed = self._parse(resp)
                    parsed.uncertain = True
                    parsed.error_type = parsed.error_type or f"ambiguous_{resp.status}"
                    return parsed
                if attempt < self._retries:
                    self._sleep(min(8.0, 2.0 ** attempt))
                    continue
            return self._parse(resp)
        return AirtableResult(False, 429, error_type="rate_limited", message="retries exhausted on 429")

    @staticmethod
    def _parse(resp: Response) -> AirtableResult:
        body = resp.json()
        if 200 <= resp.status < 300 and isinstance(body, dict):
            return AirtableResult(True, resp.status, records=list(body.get("records") or []))
        error_type, message = "", (resp.text or "")[:300]
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                error_type = str(err.get("type") or "")
                message = str(err.get("message") or "")[:300]
            elif isinstance(err, str):
                error_type = err
        return AirtableResult(False, resp.status, error_type=error_type, message=message)

    def create_records(self, fields_list: List[Dict[str, Any]]) -> AirtableResult:
        if len(fields_list) > 10:
            raise ValueError("Airtable accepts at most 10 records per create")
        body = {"records": [{"fields": f} for f in fields_list], "typecast": True}
        return self._request("POST", json_body=body)

    def update_records(self, updates: List[Dict[str, Any]]) -> AirtableResult:
        if len(updates) > 10:
            raise ValueError("Airtable accepts at most 10 records per update")
        return self._request("PATCH", json_body={"records": updates, "typecast": True})

    def find_by_lead_key(self, lead_key: str) -> AirtableResult:
        params = [("filterByFormula", f"{{Lead Key}} = '{_formula_string(lead_key)}'"), ("pageSize", 5)]
        return self._request("GET", params=params)

    def iter_records(self, fields: List[str], *, filter_formula: str = "", page_size: int = 100) -> Iterator[Dict[str, Any]]:
        offset: Optional[str] = None
        while True:
            params: List[tuple] = [("pageSize", page_size)] + [("fields[]", f) for f in fields]
            if filter_formula:
                params.append(("filterByFormula", filter_formula))
            if offset:
                params.append(("offset", offset))
            result = self._request("GET", params=params)
            if not result.ok:
                raise RuntimeError(f"airtable_list_failed:{result.status}:{result.error_type}")
            for rec in result.records:
                yield rec
            # offset lives in the body; re-read it from the last response
            offset = getattr(result, "_offset", None)
            if not offset:
                break

    def list_page(self, fields: List[str], *, filter_formula: str = "", offset: str = "", page_size: int = 100):
        """One page with the provider's continuation offset (used by the importer)."""
        params: List[tuple] = [("pageSize", page_size)] + [("fields[]", f) for f in fields]
        if filter_formula:
            params.append(("filterByFormula", filter_formula))
        if offset:
            params.append(("offset", offset))
        try:
            resp = self._t.request("GET", self._url, headers=self._headers(), params=params, timeout=self._timeout)
        except TransportError as exc:
            raise RuntimeError(f"airtable_list_failed:{exc}") from None
        body = resp.json() or {}
        if resp.status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"airtable_list_failed:{resp.status}")
        return list(body.get("records") or []), str(body.get("offset") or "")
