"""Idempotent outbox consumers for Airtable and Instantly.

Per item: exclusive claim (R06: an atomic ``pending/failed -> claimed`` transition under
a lease; a second worker gets nothing while the lease is active) -> re-check
suppressions, approval validity, the supporting posting's lifecycle (R11) and campaign
availability -> reconcile by stable identity if an earlier attempt may have reached the
provider -> attempt receipt + in_flight -> provider call outside any transaction ->
terminal receipt + delivered.

Every state change and every receipt is fenced by the lease token, so a worker that
lost its lease can neither record a delivery nor move the item (R06). An uncertain
outcome (timeout, connection reset, ambiguous 5xx after the request was sent -- R05)
leaves the item in_flight; the next claim reconciles by ``Lead Key`` / email+campaign
BEFORE any second create.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import psycopg

from ..db.connection import jsonb, transaction
from ..providers.airtable import AirtableClient
from ..providers.instantly import ALREADY_IN_TARGET_CAMPAIGN, MEMBERSHIP_UNKNOWN, NEWLY_CREATED, InstantlyClient, classify_membership
from .lifecycle import posting_is_active
from .suppression import check as suppression_check, company_function_keys, account_keys


@dataclass
class DeliveryOutcome:
    outbox_id: int
    channel: str
    outcome: str  # delivered | reconciled | blocked | failed | deferred | uncertain | lease_lost
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

    # --- claim (R06) ----------------------------------------------------------
    def claim(self, channel: str, *, limit: int = 1) -> List[OutboxItem]:
        moment = self.now()
        with self.conn.cursor() as cur:
            cur.execute(
                """
                WITH c AS (
                    SELECT id, state FROM delivery_outbox
                    WHERE channel = %(ch)s AND (
                        (state IN ('pending', 'failed') AND available_at <= %(now)s
                             AND (lease_expires_at IS NULL OR lease_expires_at <= %(now)s))
                        OR (state IN ('claimed', 'in_flight') AND lease_expires_at IS NOT NULL AND lease_expires_at <= %(now)s))
                    ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %(lim)s)
                UPDATE delivery_outbox o SET state = 'claimed', lease_token = gen_random_uuid(),
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

    # --- fenced state helpers ----------------------------------------------------
    def _set(self, item: OutboxItem, state: str, *, error: str = "", blocked_reason: str = "", available_at: Optional[datetime] = None) -> bool:
        """Move the item; only the lease holder can. Terminal/idle states release the lease."""
        keep_lease = state in ("claimed", "in_flight")
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE delivery_outbox SET state = %s, last_error = %s, blocked_reason = %s,
                           available_at = COALESCE(%s, available_at),
                           lease_token = CASE WHEN %s THEN lease_token ELSE NULL END,
                           lease_expires_at = CASE WHEN %s THEN lease_expires_at ELSE NULL END, updated_at = now()
                    WHERE id = %s AND lease_token = %s RETURNING id
                    """,
                    (state, error[:500] or None, blocked_reason[:200] or None, available_at, keep_lease, keep_lease, item.id, item.lease_token),
                )
                return cur.fetchone() is not None

    def _receipt(self, item: OutboxItem, kind: str, *, external_id: str = "", external_campaign: str = "",
                 summary: Optional[Dict[str, Any]] = None) -> bool:
        """A receipt is written only while the item still carries this worker's lease."""
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO delivery_receipts (outbox_id, channel, receipt_kind, external_id, external_campaign, response_summary)
                    SELECT o.id, o.channel, %s, %s, %s, %s FROM delivery_outbox o
                    WHERE o.id = %s AND o.lease_token = %s
                    RETURNING id
                    """,
                    (kind, external_id or None, external_campaign or None, jsonb(summary or {}), item.id, item.lease_token),
                )
                return cur.fetchone() is not None

    def _delivered(self, item: OutboxItem, kind: str, *, external_id: str = "", external_campaign: str = "",
                   summary: Optional[Dict[str, Any]] = None) -> bool:
        if not self._receipt(item, kind, external_id=external_id, external_campaign=external_campaign, summary=summary):
            return False
        if not self._set(item, "delivered"):
            return False
        self._mark_approval_delivered_if_complete(item.approval_id)
        return True

    def _mark_approval_delivered_if_complete(self, approval_id: int) -> None:
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("SELECT count(*) AS n FROM delivery_outbox WHERE approval_id = %s AND state <> 'delivered'", (approval_id,))
                if int(cur.fetchone()["n"]) == 0:
                    cur.execute("UPDATE approvals SET state = 'delivered', updated_at = now() WHERE id = %s AND state = 'approved'", (approval_id,))

    def _revoke(self, approval_id: int, reason: str) -> None:
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("UPDATE approvals SET state = 'revoked', revoke_reason = %s, updated_at = now() WHERE id = %s AND state = 'approved'",
                            (reason[:200], approval_id))
                cur.execute("UPDATE delivery_outbox SET state = 'blocked', blocked_reason = %s, lease_token = NULL, lease_expires_at = NULL, updated_at = now() "
                            "WHERE approval_id = %s AND state IN ('pending', 'failed', 'claimed')", (reason[:200], approval_id))

    def _precheck(self, item: OutboxItem) -> Optional[Tuple[str, str]]:
        """Return (state, reason) to stop, or None to proceed."""
        moment = self.now()
        with self.conn.cursor() as cur:
            cur.execute("SELECT a.state, a.lead_json, e.canonical_name, e.domain, e.linkedin_slug FROM approvals a JOIN employers e ON e.id = a.employer_id WHERE a.id = %s", (item.approval_id,))
            row = cur.fetchone()
            posting = None
            if row:
                pid = (row["lead_json"] or {}).get("posting_id")
                if pid:
                    cur.execute("SELECT id, state, date_valid_through, content_hash FROM postings WHERE id = %s", (pid,))
                    posting = cur.fetchone()
        self.conn.commit()
        if not row:
            return "blocked", "approval_missing"
        if row["state"] == "revoked":
            return "blocked", "approval_revoked"
        lead = dict(row["lead_json"])
        # R11: the supporting vacancy must still be active and unchanged since approval.
        if posting is not None:
            active, why = posting_is_active(dict(posting), now=moment)
            if not active:
                self._revoke(item.approval_id, f"posting_no_longer_active:{why}")
                return "blocked", f"posting_no_longer_active:{why}"
            if lead.get("posting_content_hash") and posting["content_hash"] != lead.get("posting_content_hash"):
                self._revoke(item.approval_id, "posting_evidence_changed_since_approval")
                return "blocked", "posting_evidence_changed_since_approval"
        hits = suppression_check(
            self.conn, email=lead.get("email", ""),
            company_function=company_function_keys(domain=row["domain"] or "", name=row["canonical_name"], slug=row["linkedin_slug"] or "", function_key=lead["function_key"]),
            account=account_keys(domain=row["domain"] or "", name=row["canonical_name"]),
        )
        self.conn.commit()
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

    def _backoff_at(self, item: OutboxItem) -> datetime:
        return self.now() + timedelta(seconds=self.backoff * (2 ** max(0, item.attempts - 1)))

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
        # Reconcile first whenever an earlier attempt may have reached the provider (R05).
        if item.version_state_before in ("in_flight", "claimed") or item.attempts > 1:
            found = self.airtable.find_by_lead_key(lead_key)
            if found.ok and found.records:
                rec = found.records[0]
                if self._delivered(item, "reconciled", external_id=str(rec.get("id") or ""), summary={"reconciled_by": "Lead Key"}):
                    return DeliveryOutcome(item.id, "airtable", "reconciled", "found_by_lead_key", str(rec.get("id") or ""))
                return DeliveryOutcome(item.id, "airtable", "lease_lost", "reconciled_but_lease_lost")
            if not found.ok:
                self._set(item, "failed", error=f"reconcile_failed:{found.status}:{found.error_type}", available_at=self._backoff_at(item))
                return DeliveryOutcome(item.id, "airtable", "failed", "reconcile_lookup_failed")
        if not self._receipt(item, "attempted", summary={"attempt": item.attempts}) or not self._set(item, "in_flight"):
            return DeliveryOutcome(item.id, "airtable", "lease_lost", "lease_lost_before_send")
        result = self.airtable.create_records([item.payload])
        if result.uncertain:
            # The request may have created the row. Stay in_flight under the lease; the
            # next claim (after expiry) reconciles by Lead Key before any second create.
            return DeliveryOutcome(item.id, "airtable", "uncertain", f"uncertain:{result.error_type}")
        if result.ok and result.records:
            rec = result.records[0]
            if self._delivered(item, "created", external_id=str(rec.get("id") or ""), summary={"status": result.status}):
                return DeliveryOutcome(item.id, "airtable", "delivered", "created", str(rec.get("id") or ""))
            return DeliveryOutcome(item.id, "airtable", "lease_lost", "created_but_lease_lost", str(rec.get("id") or ""))
        if result.status == 422:
            self._receipt(item, "rejected", summary=result.summary())
            self._set(item, "blocked", error=result.message, blocked_reason=f"airtable_422:{result.error_type}")
            return DeliveryOutcome(item.id, "airtable", "blocked", f"airtable_422:{result.error_type}")
        state = "failed" if item.attempts < self.max_attempts else "blocked"
        self._set(item, state, error=f"{result.status}:{result.error_type}:{result.message}",
                  blocked_reason="max_attempts" if state == "blocked" else "", available_at=self._backoff_at(item))
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
                if status not in (None, 1):
                    self._set(item, "pending", available_at=self.now() + timedelta(hours=1), error=f"campaign_status_{status}")
                    return DeliveryOutcome(item.id, "instantly", "deferred", f"campaign_not_active:{status}")
        if item.version_state_before in ("in_flight", "claimed") or item.attempts > 1:
            membership, campaigns = self.instantly.resolve_membership(email, target)
            if membership == ALREADY_IN_TARGET_CAMPAIGN:
                if self._delivered(item, "reconciled", external_campaign=target, summary={"campaigns": list(campaigns)}):
                    return DeliveryOutcome(item.id, "instantly", "reconciled", "already_in_target_campaign")
                return DeliveryOutcome(item.id, "instantly", "lease_lost", "reconciled_but_lease_lost")
            if membership == MEMBERSHIP_UNKNOWN:
                self._set(item, "failed", error="reconcile_unknown", available_at=self._backoff_at(item))
                return DeliveryOutcome(item.id, "instantly", "failed", "reconcile_unknown")
        if not self._receipt(item, "attempted", summary={"attempt": item.attempts}) or not self._set(item, "in_flight"):
            return DeliveryOutcome(item.id, "instantly", "lease_lost", "lease_lost_before_send")
        started = self.now()
        result = self.instantly.create_lead(payload)
        if result.uncertain:
            return DeliveryOutcome(item.id, "instantly", "uncertain", f"uncertain:{result.message[:40]}")
        if result.ok:
            membership, lead_id, lead_campaign, created_at = classify_membership(result.data, target_campaign=target, request_started_at=started)
            if membership != NEWLY_CREATED:
                membership, campaigns = self.instantly.resolve_membership(email, target)
            if membership == NEWLY_CREATED:
                if self._delivered(item, "created", external_id=lead_id, external_campaign=lead_campaign or target, summary={"created_at": created_at}):
                    return DeliveryOutcome(item.id, "instantly", "delivered", "created", lead_id)
                return DeliveryOutcome(item.id, "instantly", "lease_lost", "created_but_lease_lost", lead_id)
            if membership == ALREADY_IN_TARGET_CAMPAIGN:
                if self._delivered(item, "existing", external_id=lead_id, external_campaign=target, summary={"created_at": created_at}):
                    return DeliveryOutcome(item.id, "instantly", "delivered", "already_in_target_campaign", lead_id)
                return DeliveryOutcome(item.id, "instantly", "lease_lost", "existing_but_lease_lost", lead_id)
            self._receipt(item, "rejected", external_id=lead_id, external_campaign=lead_campaign, summary={"membership": membership})
            self._set(item, "blocked", blocked_reason=f"not_delivered:{membership}")
            return DeliveryOutcome(item.id, "instantly", "blocked", membership, lead_id)
        if result.status in (400, 422):
            self._receipt(item, "rejected", summary=result.summary())
            self._set(item, "blocked", error=result.message, blocked_reason=f"instantly_{result.status}")
            return DeliveryOutcome(item.id, "instantly", "blocked", f"instantly_{result.status}")
        state = "failed" if item.attempts < self.max_attempts else "blocked"
        self._set(item, state, error=f"{result.status}:{result.message}", blocked_reason="max_attempts" if state == "blocked" else "",
                  available_at=self._backoff_at(item))
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
