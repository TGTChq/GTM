"""Idempotent outbox consumers for Airtable and Instantly.

Per item: claim (SKIP LOCKED, expired leases reclaimable) -> re-check suppressions,
approval validity and campaign availability -> reconcile by stable identity if an
earlier attempt may have reached the provider -> attempt receipt + in_flight ->
provider call outside any transaction -> terminal receipt + delivered.

A lost response (timeout) leaves the item in_flight with an ``attempted`` receipt;
the next claim reconciles by ``Lead Key`` / email+campaign before re-sending. A
create receipt is not an email sent; both are receipts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import psycopg

from ..db.connection import jsonb, transaction
from ..providers.airtable import AirtableClient
from ..providers.instantly import (
    ALREADY_EXISTS_WORKSPACE, ALREADY_IN_TARGET_CAMPAIGN, API_ERROR, EXISTING_OTHER_CAMPAIGN, MEMBERSHIP_UNKNOWN,
    NEWLY_CREATED, InstantlyClient, classify_membership,
)
from .suppression import check as suppression_check, company_function_keys, account_keys


@dataclass
class DeliveryOutcome:
    outbox_id: int
    channel: str
    outcome: str  # delivered | reconciled | blocked | failed | deferred | uncertain
    reason: str = ""
    external_id: str = ""


@dataclass
class OutboxItem:
    id: int
    approval_id: int
    channel: str
    idempotency_key: str
    payload: Dict[str, Any]
    state: str
    attempts: int
    lease_token: Any
    version_state_before: str


class DeliveryService:
    def __init__(self, conn: psycopg.Connection, *, airtable: Optional[AirtableClient], instantly: Optional[InstantlyClient],
                 lease_seconds: int = 300, backoff_seconds: int = 120, max_attempts: int = 8,
                 check_campaign_status: bool = True, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.conn = conn
        self.airtable = airtable
        self.instantly = instantly
        self.lease_seconds = lease_seconds
        self.backoff = backoff_seconds
        self.max_attempts = max_attempts
        self.check_campaign_status = check_campaign_status
        self.now = now

    # --- claim -------------------------------------------------------------
    def claim(self, channel: str, *, limit: int = 1) -> List[OutboxItem]:
        moment = self.now()
        with self.conn.cursor() as cur:
            cur.execute(
                """
                WITH c AS (
                    SELECT id, state FROM delivery_outbox
                    WHERE channel = %(ch)s AND (
                        (state IN ('pending', 'failed') AND available_at <= %(now)s)
                        OR (state = 'in_flight' AND lease_expires_at IS NOT NULL AND lease_expires_at <= %(now)s))
                    ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %(lim)s)
                UPDATE delivery_outbox o SET lease_token = gen_random_uuid(),
                       lease_expires_at = %(now)s + make_interval(secs => %(lease)s), attempts = o.attempts + 1, updated_at = now()
                FROM c WHERE o.id = c.id
                RETURNING o.id, o.approval_id, o.channel, o.idempotency_key, o.payload_json, o.state, o.attempts, o.lease_token, c.state AS before
                """,
                {"ch": channel, "now": moment, "lim": limit, "lease": self.lease_seconds},
            )
            rows = cur.fetchall()
        self.conn.commit()
        return [OutboxItem(int(r["id"]), int(r["approval_id"]), r["channel"], r["idempotency_key"], dict(r["payload_json"]),
                           r["state"], int(r["attempts"]), r["lease_token"], r["before"]) for r in rows]

    # --- state helpers -------------------------------------------------------
    def _set(self, item: OutboxItem, state: str, *, error: str = "", blocked_reason: str = "", available_at: Optional[datetime] = None) -> bool:
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE delivery_outbox SET state = %s, last_error = %s, blocked_reason = %s,
                           available_at = COALESCE(%s, available_at),
                           lease_token = CASE WHEN %s = 'in_flight' THEN lease_token ELSE NULL END,
                           lease_expires_at = CASE WHEN %s = 'in_flight' THEN lease_expires_at ELSE NULL END, updated_at = now()
                    WHERE id = %s AND lease_token = %s RETURNING id
                    """,
                    (state, error[:500] or None, blocked_reason[:200] or None, available_at, state, state, item.id, item.lease_token),
                )
                return cur.fetchone() is not None

    def _receipt(self, item: OutboxItem, kind: str, *, external_id: str = "", external_campaign: str = "", summary: Optional[Dict[str, Any]] = None) -> None:
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO delivery_receipts (outbox_id, channel, receipt_kind, external_id, external_campaign, response_summary) VALUES (%s, %s, %s, %s, %s, %s)",
                    (item.id, item.channel, kind, external_id or None, external_campaign or None, jsonb(summary or {})),
                )

    def _mark_approval_delivered_if_complete(self, approval_id: int) -> None:
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("SELECT count(*) AS n FROM delivery_outbox WHERE approval_id = %s AND state <> 'delivered'", (approval_id,))
                if int(cur.fetchone()["n"]) == 0:
                    cur.execute("UPDATE approvals SET state = 'delivered', updated_at = now() WHERE id = %s AND state = 'approved'", (approval_id,))

    def _precheck(self, item: OutboxItem) -> Optional[Tuple[str, str]]:
        """Return (state, reason) to stop, or None to proceed."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT a.state, a.lead_json, e.canonical_name, e.domain, e.linkedin_slug FROM approvals a JOIN employers e ON e.id = a.employer_id WHERE a.id = %s", (item.approval_id,))
            row = cur.fetchone()
        self.conn.commit()
        if not row:
            return "blocked", "approval_missing"
        if row["state"] == "revoked":
            return "blocked", "approval_revoked"
        lead = dict(row["lead_json"])
        hits = suppression_check(
            self.conn, email=lead.get("email", ""),
            company_function=company_function_keys(domain=row["domain"] or "", name=row["canonical_name"], slug=row["linkedin_slug"] or "", function_key=lead["function_key"]),
            account=account_keys(domain=row["domain"] or "", name=row["canonical_name"]),
        )
        self.conn.commit()
        # A company_function hit caused by OUR OWN earlier delivery of this same approval is not a block.
        own_keys = {f"company_function:{k}" for k in company_function_keys(domain=row["domain"] or "", name=row["canonical_name"], slug=row["linkedin_slug"] or "", function_key=lead["function_key"])}
        external_hits = [h for h in hits if not (h in own_keys and self._own_suppression(h))]
        if external_hits:
            return "blocked", f"suppressed_before_delivery:{external_hits[0]}"
        return None

    def _own_suppression(self, hit: str) -> bool:
        kind, key = hit.split(":", 1)
        with self.conn.cursor() as cur:
            cur.execute("SELECT source FROM suppressions WHERE kind = %s AND key = %s", (kind, key))
            r = cur.fetchone()
        self.conn.commit()
        return bool(r and str(r["source"]).startswith("self:"))

    # --- Airtable ----------------------------------------------------------------
    def process_airtable(self, item: OutboxItem) -> DeliveryOutcome:
        if self.airtable is None:
            self._set(item, "pending", available_at=self.now() + timedelta(seconds=self.backoff))
            return DeliveryOutcome(item.id, "airtable", "deferred", "no_airtable_client")
        stop = self._precheck(item)
        if stop:
            self._set(item, stop[0], blocked_reason=stop[1])
            return DeliveryOutcome(item.id, "airtable", stop[0], stop[1])
        lead_key = item.payload["Lead Key"]
        # Reconcile first when an earlier attempt may have reached the provider.
        if item.version_state_before == "in_flight" or item.attempts > 1:
            found = self.airtable.find_by_lead_key(lead_key)
            if found.ok and found.records:
                rec = found.records[0]
                self._receipt(item, "reconciled", external_id=str(rec.get("id") or ""), summary={"reconciled_by": "Lead Key"})
                self._set(item, "delivered")
                self._mark_approval_delivered_if_complete(item.approval_id)
                return DeliveryOutcome(item.id, "airtable", "reconciled", "found_by_lead_key", str(rec.get("id") or ""))
            if not found.ok:
                self._set(item, "failed", error=f"reconcile_failed:{found.status}:{found.error_type}",
                          available_at=self.now() + timedelta(seconds=self.backoff))
                return DeliveryOutcome(item.id, "airtable", "failed", "reconcile_lookup_failed")
        self._receipt(item, "attempted", summary={"attempt": item.attempts})
        if not self._set(item, "in_flight"):
            return DeliveryOutcome(item.id, "airtable", "failed", "lease_lost")
        result = self.airtable.create_records([item.payload])
        if result.uncertain:
            # Response lost after the request was sent. Stay in_flight; the lease
            # expiry hands it to the next claim, which reconciles by Lead Key.
            return DeliveryOutcome(item.id, "airtable", "uncertain", "timeout_after_send")
        if result.ok and result.records:
            rec = result.records[0]
            self._receipt(item, "created", external_id=str(rec.get("id") or ""), summary={"status": result.status})
            self._set(item, "delivered")
            self._mark_approval_delivered_if_complete(item.approval_id)
            return DeliveryOutcome(item.id, "airtable", "delivered", "created", str(rec.get("id") or ""))
        if result.status == 422:
            self._receipt(item, "rejected", summary=result.summary())
            self._set(item, "blocked", error=result.message, blocked_reason=f"airtable_422:{result.error_type}")
            return DeliveryOutcome(item.id, "airtable", "blocked", f"airtable_422:{result.error_type}")
        state = "failed" if item.attempts < self.max_attempts else "blocked"
        self._set(item, state, error=f"{result.status}:{result.error_type}:{result.message}",
                  blocked_reason="max_attempts" if state == "blocked" else "",
                  available_at=self.now() + timedelta(seconds=self.backoff * (2 ** max(0, item.attempts - 1))))
        return DeliveryOutcome(item.id, "airtable", state, f"http_{result.status}")

    # --- Instantly ---------------------------------------------------------------
    def process_instantly(self, item: OutboxItem) -> DeliveryOutcome:
        if self.instantly is None:
            self._set(item, "pending", available_at=self.now() + timedelta(seconds=self.backoff))
            return DeliveryOutcome(item.id, "instantly", "deferred", "no_instantly_client")
        stop = self._precheck(item)
        if stop:
            self._set(item, stop[0], blocked_reason=stop[1])
            return DeliveryOutcome(item.id, "instantly", stop[0], stop[1])
        payload = item.payload
        target = str(payload["campaign"])
        email = str(payload["email"])
        if self.check_campaign_status:
            camp = self.instantly.get_campaign(target)
            if camp.ok:
                status = camp.data.get("status")
                # 1 = active per Instantly v2. Draft(0)/paused(2)/completed(3) delay delivery; they never invalidate the lead.
                if status not in (None, 1):
                    self._set(item, "pending", available_at=self.now() + timedelta(hours=1), error=f"campaign_status_{status}")
                    return DeliveryOutcome(item.id, "instantly", "deferred", f"campaign_not_active:{status}")
        if item.version_state_before == "in_flight" or item.attempts > 1:
            membership, campaigns = self.instantly.resolve_membership(email, target)
            if membership == ALREADY_IN_TARGET_CAMPAIGN:
                self._receipt(item, "reconciled", external_campaign=target, summary={"campaigns": list(campaigns)})
                self._set(item, "delivered")
                self._mark_approval_delivered_if_complete(item.approval_id)
                return DeliveryOutcome(item.id, "instantly", "reconciled", "already_in_target_campaign")
            if membership == MEMBERSHIP_UNKNOWN:
                self._set(item, "failed", error="reconcile_unknown", available_at=self.now() + timedelta(seconds=self.backoff))
                return DeliveryOutcome(item.id, "instantly", "failed", "reconcile_unknown")
        self._receipt(item, "attempted", summary={"attempt": item.attempts})
        if not self._set(item, "in_flight"):
            return DeliveryOutcome(item.id, "instantly", "failed", "lease_lost")
        started = self.now()
        result = self.instantly.create_lead(payload)
        if result.uncertain:
            return DeliveryOutcome(item.id, "instantly", "uncertain", "timeout_after_send")
        if result.ok:
            membership, lead_id, lead_campaign, created_at = classify_membership(result.data, target_campaign=target, request_started_at=started)
            if membership != NEWLY_CREATED:
                membership, campaigns = self.instantly.resolve_membership(email, target)
            if membership == NEWLY_CREATED:
                self._receipt(item, "created", external_id=lead_id, external_campaign=lead_campaign or target, summary={"created_at": created_at})
                self._set(item, "delivered")
                self._mark_approval_delivered_if_complete(item.approval_id)
                return DeliveryOutcome(item.id, "instantly", "delivered", "created", lead_id)
            if membership == ALREADY_IN_TARGET_CAMPAIGN:
                self._receipt(item, "existing", external_id=lead_id, external_campaign=target, summary={"created_at": created_at})
                self._set(item, "delivered")
                self._mark_approval_delivered_if_complete(item.approval_id)
                return DeliveryOutcome(item.id, "instantly", "delivered", "already_in_target_campaign", lead_id)
            # Pre-existing elsewhere or unknown: not delivered where intended. Block with the truth.
            self._receipt(item, "rejected", external_id=lead_id, external_campaign=lead_campaign, summary={"membership": membership})
            self._set(item, "blocked", blocked_reason=f"not_delivered:{membership}")
            return DeliveryOutcome(item.id, "instantly", "blocked", membership, lead_id)
        if result.status in (400, 422):
            self._receipt(item, "rejected", summary=result.summary())
            self._set(item, "blocked", error=result.message, blocked_reason=f"instantly_{result.status}")
            return DeliveryOutcome(item.id, "instantly", "blocked", f"instantly_{result.status}")
        state = "failed" if item.attempts < self.max_attempts else "blocked"
        self._set(item, state, error=f"{result.status}:{result.message}", blocked_reason="max_attempts" if state == "blocked" else "",
                  available_at=self.now() + timedelta(seconds=self.backoff * (2 ** max(0, item.attempts - 1))))
        return DeliveryOutcome(item.id, "instantly", state, f"http_{result.status}")

    def process(self, item: OutboxItem) -> DeliveryOutcome:
        return self.process_airtable(item) if item.channel == "airtable" else self.process_instantly(item)

    def drain(self, channel: str, *, max_items: int = 100) -> List[DeliveryOutcome]:
        out: List[DeliveryOutcome] = []
        while len(out) < max_items:
            items = self.claim(channel, limit=1)
            if not items:
                break
            out.append(self.process(items[0]))
        return out
