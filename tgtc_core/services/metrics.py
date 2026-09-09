"""Reconciled ledger from tables. Nothing here subtracts reasons from a total, and a
counter with no underlying evidence is reported as ``None`` (unknown), never 0.
"""

from __future__ import annotations

from typing import Any, Dict

import psycopg


def _one(cur, sql: str, params=()) -> Any:
    cur.execute(sql, params)
    row = cur.fetchone()
    if not row:
        return None
    return list(row.values())[0]


def ledger(conn: psycopg.Connection) -> Dict[str, Any]:
    with conn.cursor() as cur:
        out: Dict[str, Any] = {}
        # acquisition
        out["fantastic_requests"] = _one(cur, "SELECT count(*) FROM request_attempts WHERE provider = 'fantastic'")
        out["fantastic_rows_billed_estimate"] = _one(cur, "SELECT COALESCE(sum(estimated_credits), 0) FROM credit_events WHERE provider = 'fantastic'")
        out["fantastic_rows_confirmed_by_header"] = _one(cur, "SELECT sum(confirmed_credits) FROM credit_events WHERE provider = 'fantastic' AND basis = 'provider_header'")
        out["fantastic_uncertain_attempts"] = _one(cur, "SELECT count(*) FROM request_attempts WHERE provider = 'fantastic' AND status = 'uncertain'")
        out["page_receipts"] = _one(cur, "SELECT count(*) FROM page_receipts")
        out["duplicate_pages"] = _one(cur, "SELECT count(*) FROM page_receipts WHERE duplicate_of_receipt_id IS NOT NULL")
        out["unique_provider_ids_received"] = _one(cur, "SELECT count(DISTINCT x) FROM page_receipts, unnest(row_ids) AS x")
        out["postings"] = _one(cur, "SELECT count(*) FROM postings")
        out["postings_cross_source_duplicates"] = _one(cur, "SELECT count(*) FROM postings WHERE duplicate_of_posting_id IS NOT NULL")
        out["postings_modified_versions"] = _one(cur, "SELECT count(*) FROM posting_versions WHERE version > 1")
        cur.execute("SELECT state, count(*) AS n FROM postings GROUP BY state")
        out["postings_by_state"] = {r["state"]: int(r["n"]) for r in cur.fetchall()}
        cur.execute("SELECT close_reason, count(*) AS n FROM postings WHERE state = 'closed' GROUP BY close_reason ORDER BY n DESC")
        out["postings_close_reasons"] = {str(r["close_reason"]): int(r["n"]) for r in cur.fetchall()}
        # identity / classification
        out["employers"] = _one(cur, "SELECT count(*) FROM employers")
        out["classifications"] = _one(cur, "SELECT count(*) FROM classifications")
        cur.execute("SELECT method, count(*) AS n FROM classifications GROUP BY method")
        out["classifications_by_method"] = {r["method"]: int(r["n"]) for r in cur.fetchall()}
        out["postings_compatible"] = _one(cur, "SELECT count(DISTINCT posting_id) FROM classifications WHERE NOT excluded AND cardinality(compatible_functions) > 0")
        out["opportunities"] = _one(cur, "SELECT count(*) FROM opportunities")
        cur.execute("SELECT state, count(*) AS n FROM opportunities GROUP BY state")
        out["opportunities_by_state"] = {r["state"]: int(r["n"]) for r in cur.fetchall()}
        cur.execute("SELECT close_reason, count(*) AS n FROM opportunities WHERE state = 'closed' GROUP BY close_reason ORDER BY n DESC")
        out["opportunities_close_reasons"] = {str(r["close_reason"]): int(r["n"]) for r in cur.fetchall()}
        cur.execute("SELECT campaign_key, count(*) AS n FROM opportunities GROUP BY campaign_key")
        out["opportunities_by_campaign"] = {r["campaign_key"]: int(r["n"]) for r in cur.fetchall()}
        # people / spend
        out["apollo_requests"] = _one(cur, "SELECT count(*) FROM request_attempts WHERE provider = 'apollo'")
        cur.execute("SELECT operation, status, count(*) AS n FROM request_attempts WHERE provider = 'apollo' GROUP BY operation, status")
        out["apollo_requests_by_operation_status"] = {f"{r['operation']}:{r['status']}": int(r["n"]) for r in cur.fetchall()}
        out["apollo_estimated_credits"] = _one(cur, "SELECT sum(estimated_credits) FROM credit_events WHERE provider = 'apollo'")
        out["apollo_confirmed_credits"] = _one(cur, "SELECT sum(confirmed_credits) FROM credit_events WHERE provider = 'apollo' AND basis <> 'estimate'")
        out["candidates_found"] = _one(cur, "SELECT count(DISTINCT candidate_ref) FROM candidate_attempts")
        out["people_enriched"] = _one(cur, "SELECT count(*) FROM candidate_attempts WHERE attempt_kind = 'match' AND outcome = 'served'")
        out["people_verified_email"] = _one(cur, "SELECT count(*) FROM people WHERE email_status = 'verified'")
        # approvals / delivery
        out["approved_distinct"] = _one(cur, "SELECT count(*) FROM approvals WHERE state <> 'revoked'")
        cur.execute("SELECT campaign_key, count(*) AS n FROM approvals WHERE state <> 'revoked' GROUP BY campaign_key")
        out["approved_by_campaign"] = {r["campaign_key"]: int(r["n"]) for r in cur.fetchall()}
        out["airtable_receipts_created"] = _one(cur, "SELECT count(*) FROM delivery_receipts WHERE channel = 'airtable' AND receipt_kind IN ('created','reconciled')")
        out["instantly_receipts_created"] = _one(cur, "SELECT count(*) FROM delivery_receipts WHERE channel = 'instantly' AND receipt_kind IN ('created','reconciled')")
        out["instantly_receipts_existing"] = _one(cur, "SELECT count(*) FROM delivery_receipts WHERE channel = 'instantly' AND receipt_kind = 'existing'")
        cur.execute("SELECT channel, state, count(*) AS n FROM delivery_outbox GROUP BY channel, state")
        out["outbox_by_channel_state"] = {f"{r['channel']}:{r['state']}": int(r["n"]) for r in cur.fetchall()}
        out["suppressions"] = _one(cur, "SELECT count(*) FROM suppressions")
        out["outcome_events"] = _one(cur, "SELECT count(*) FROM outcome_events")
        out["replies_recorded"] = _one(cur, "SELECT count(*) FROM outcome_events WHERE event_type IN ('reply','replied')") or None
        out["meetings_recorded"] = _one(cur, "SELECT count(*) FROM outcome_events WHERE event_type = 'meeting_booked'") or None
        cur.execute("SELECT kind AS k, state, count(*) AS n FROM work_items GROUP BY kind, state")
        out["work_items"] = {f"{r['k']}:{r['state']}": int(r["n"]) for r in cur.fetchall()}
        cur.execute("SELECT provider, state, last_error_code FROM provider_state")
        out["provider_state"] = {r["provider"]: {"state": r["state"], "last_error_code": r["last_error_code"]} for r in cur.fetchall()}
        # capacity identity, from linked cohorts only
        opp = out["opportunities"] or 0
        with_candidates = _one(cur, "SELECT count(DISTINCT opportunity_id) FROM candidate_attempts")
        with_verified = _one(cur, "SELECT count(DISTINCT opportunity_id) FROM candidate_attempts WHERE attempt_kind = 'gate' AND outcome = 'pass'")
        out["capacity_budget"] = {
            "opportunities": opp,
            "buyer_coverage": (with_candidates / opp) if opp else None,
            "valid_email_given_candidates": (with_verified / with_candidates) if with_candidates else None,
            "approved": out["approved_distinct"],
        }
        return out
