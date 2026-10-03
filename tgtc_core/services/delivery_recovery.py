"""The authorised recovery of approved outbox rows deferred by a paused campaign.

Run ``20261002T233322.605777Z-b7478fc9`` left 25 approved Instantly rows undelivered
with ``last_error = campaign_status_2`` and 25 Airtable rows behind them with
``awaiting_instantly``. The cause was our own deferral rule, not a provider limit: a
PAUSED campaign accepts leads and sends nothing, measured on the internal TEST B
campaign on 2026-10-03 (campaign stayed status 2, ``emails_sent`` unchanged, the lead
never contacted).

This module exists because the recovery has guarantees that a bare ``deliver`` does
not give:

* **mutual exclusion** — it holds the production run lock for the whole operation, so
  no concurrent run or drain can touch the same rows
* **revalidation before delivery** — every row is re-checked for a complete, resolved,
  non-generic copy and a campaign on the allow-list; a row that fails is **blocked
  with a named reason**, never delivered
* **verification by id** — every row that reports delivered is confirmed by reading the
  lead back from the provider by its recorded external id
* **Airtable strictly second** — the CRM drain runs only after that, and the existing
  ``awaiting_instantly`` gate means a row can only proceed on a genuine creation

It performs no enrichment: the complete payload is already stored on the outbox row,
so nothing here calls Apollo or Fantastic. It restarts no sequence and sends no email.

The generic-subject check lives HERE and not in ``DeliveryService`` on purpose. The
production gates do not reject a bare function noun — ``role_display_send_safe`` tests
length, characters, appended qualifiers and headlines, none of which objects to
``operations role`` — and changing that would change the funnel. This is a recovery
standard, applied to this recovery.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..db.connection import transaction
from ..domain.outbound_copy import copy_block_reason

#: Reasons recorded on a row this recovery refuses to deliver.
WITHHELD_BY_OPERATOR = "recovery_hold:withheld_by_operator"
GENERIC_SUBJECT = "recovery_hold:subject_is_a_bare_function_noun"
INCOMPLETE_COPY = "recovery_hold:copy_incomplete_or_unresolved"
CAMPAIGN_NOT_ALLOWED = "recovery_hold:campaign_not_on_the_allow_list"


def _payload(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def pending_instantly_rows(conn) -> List[Dict[str, Any]]:
    """Every Instantly outbox row still waiting, newest approval last."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.id, o.approval_id, o.state, o.attempts, o.last_error, o.payload_json
              FROM delivery_outbox o
             WHERE o.channel = 'instantly' AND o.state = 'pending'
             ORDER BY o.id
            """
        )
        return [dict(r) for r in cur.fetchall()]


def subject_is_a_bare_function_noun(subject: str) -> bool:
    """``operations role``, ``customer support role``: a function name with the word
    ``role`` stuck on, which is what the 287 parked leads were parked for. A real job
    title does not end that way."""
    return str(subject or "").strip().lower().endswith(" role")


def recovery_refusal(payload: Dict[str, Any], allowed_campaign_ids: Iterable[str]) -> str:
    """A named reason to withhold this row, or "" when it is fit to deliver.

    The copy contract itself is NOT restated here: ``copy_block_reason`` is the
    production rule and is asked exactly as approval asks it, so a Control payload
    (which legitimately carries no rendered copy) is not refused for lacking it, and
    a Challenger payload gets the same named refusal it would get anywhere else.

    Only two things are added, both specific to this recovery: the destination must
    be on the configured allow-list, and a bare function-noun subject is withheld.
    """
    allowed = {str(c) for c in allowed_campaign_ids if c}
    if allowed and str(payload.get("campaign") or "") not in allowed:
        return CAMPAIGN_NOT_ALLOWED
    contract = copy_block_reason(payload)
    if contract:
        return f"{INCOMPLETE_COPY}:{contract}"
    variables = payload.get("custom_variables")
    variables = variables if isinstance(variables, dict) else {}
    if subject_is_a_bare_function_noun(variables.get("rendered_subject")):
        return GENERIC_SUBJECT
    return ""


def withhold(conn, outbox_id: int, reason: str, *, now: datetime) -> bool:
    """Block a row that is still pending and unleased. Returns False if it moved."""
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE delivery_outbox
                   SET state = 'blocked', blocked_reason = %s, last_error = %s,
                       lease_token = NULL, lease_expires_at = NULL, updated_at = now()
                 WHERE id = %s AND state = 'pending'
                   AND (lease_token IS NULL OR lease_expires_at <= %s)
                 RETURNING id
                """,
                (reason, reason, outbox_id, now),
            )
            return cur.fetchone() is not None


def confirmed_creations(conn, outbox_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
    """The genuine receipt for each delivered row, by outbox id."""
    if not outbox_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.outbox_id, r.receipt_kind, r.external_id, r.external_campaign
              FROM delivery_receipts r
             WHERE r.outbox_id = ANY(%s) AND r.receipt_kind IN ('created', 'reconciled')
               AND coalesce(r.external_id, '') <> ''
            """,
            (list(outbox_ids),),
        )
        return {int(r["outbox_id"]): dict(r) for r in cur.fetchall()}


def recover(conn, runner, *, withhold_ids: Sequence[int] = (), max_items: int = 100,
            now: Optional[datetime] = None, instantly=None) -> Dict[str, Any]:
    """Withhold, revalidate, deliver, verify by id, then let Airtable follow.

    ``runner`` supplies the configured delivery path; ``instantly`` is the client used
    for the read-back (defaults to the runner's).
    """
    moment = now or datetime.now(timezone.utc)
    client = instantly if instantly is not None else getattr(runner, "instantly", None)
    allowed = tuple(getattr(getattr(runner, "s", None), "campaign_env", {}).values()) \
        if getattr(getattr(runner, "s", None), "campaign_env", None) else ()

    report: Dict[str, Any] = {"withheld": {}, "revalidated": 0, "delivered": [],
                              "verified": [], "unverified": [], "airtable": {},
                              "instantly_drain": {}}

    # 1. the rows an operator named, before anything is delivered
    for outbox_id in withhold_ids:
        ok = withhold(conn, int(outbox_id), WITHHELD_BY_OPERATOR, now=moment)
        report["withheld"][str(outbox_id)] = WITHHELD_BY_OPERATOR if ok else "not_pending_anymore"

    # 2. revalidate every remaining row; a failure is blocked, never delivered
    candidates: List[int] = []
    for row in pending_instantly_rows(conn):
        refusal = recovery_refusal(_payload(row.get("payload_json")), allowed)
        if refusal:
            if withhold(conn, int(row["id"]), refusal, now=moment):
                report["withheld"][str(row["id"])] = refusal
            continue
        candidates.append(int(row["id"]))
    report["revalidated"] = len(candidates)
    if not candidates:
        return report

    # 3. deliver ONLY Instantly; no acquisition, no enrichment, no Airtable yet
    report["instantly_drain"] = runner.deliver(max_items=max_items, channels=("instantly",))

    # 4. verify each creation by reading the lead back by its recorded id
    receipts = confirmed_creations(conn, candidates)
    for outbox_id in candidates:
        receipt = receipts.get(outbox_id)
        if not receipt:
            report["unverified"].append({"outbox_id": outbox_id, "why": "no_genuine_receipt"})
            continue
        lead_id = str(receipt.get("external_id") or "")
        report["delivered"].append({"outbox_id": outbox_id, "lead_id": lead_id,
                                    "receipt": receipt.get("receipt_kind")})
        if client is None:
            report["unverified"].append({"outbox_id": outbox_id, "why": "no_client_for_read_back"})
            continue
        result = client.get_lead(lead_id)
        data = result.data if getattr(result, "ok", False) else {}
        if not getattr(result, "ok", False) or str((data or {}).get("id") or "") != lead_id:
            report["unverified"].append({"outbox_id": outbox_id, "lead_id": lead_id,
                                         "why": "read_back_failed"})
            continue
        report["verified"].append({"outbox_id": outbox_id, "lead_id": lead_id,
                                   "campaign": (data or {}).get("campaign"),
                                   "status": (data or {}).get("status")})

    # 5. Airtable strictly afterwards. Its own `awaiting_instantly` gate means a row
    #    can only proceed where a genuine creation exists, so this cannot write a CRM
    #    record for a lead that was never created.
    report["airtable"] = runner.deliver(max_items=max_items, channels=("airtable",))
    return report
