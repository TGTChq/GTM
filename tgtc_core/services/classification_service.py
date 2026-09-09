"""Classify one posting and attach it to its employer × function opportunity.

Review finding R03: the four outcomes of "no answer" are kept apart.

* deterministic exclusion            -> posting closed with the commercial reason;
* insufficient evidence (content)    -> posting closed ``insufficient_evidence:*``, reopened
                                        automatically when a new policy/model version appears;
* inference not configured/authorized-> posting closed ``inference_unavailable:not_configured``,
                                        reopened automatically when inference is configured;
* transient inference failure        -> NOTHING is closed; the work item waits and resumes
                                        by itself. The posting keeps its identity; it is
                                        never re-bought or artificially modified.

A closed opportunity reopened by NEW evidence (a new or modified posting) starts its
next evidence epoch (recovery finding b); its attempt history is preserved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import psycopg

from ..db.connection import jsonb, transaction
from ..db import work_queue
from ..domain.classification import ClassificationResult, classify_posting
from ..domain.inference import UNAVAILABLE_ANSWER, UNAVAILABLE_CONFIG, UNAVAILABLE_TRANSIENT, InferencePort
from ..policy.campaigns import CAMPAIGN_BY_FUNCTION, POLICY_VERSION


@dataclass
class ClassifyOutcome:
    posting_id: int
    outcome: str  # classified | closed | wait
    reason: str = ""
    opportunity_ids: List[int] = field(default_factory=list)
    method: str = ""
    retry_after: Optional[datetime] = None


def classify_one(conn: psycopg.Connection, posting_id: int, *, inference: Optional[InferencePort],
                 now: Optional[datetime] = None, transient_backoff_minutes: int = 15,
                 work_item: Optional[work_queue.WorkItem] = None) -> ClassifyOutcome:
    moment = now or datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM postings WHERE id = %s", (posting_id,))
        posting = cur.fetchone()
    conn.commit()
    if not posting:
        raise LookupError(f"posting {posting_id} not found")
    if posting["state"] == "expired":
        return ClassifyOutcome(posting_id, "closed", "posting_expired", [], "")
    if posting["employer_id"] is None:
        return ClassifyOutcome(posting_id, "closed", posting["close_reason"] or "employer_identity_unresolved", [], "")
    org = dict(posting["org_json"] or {})
    structured = dict(posting["structured_json"] or {})
    # The model call (if any) happens here, OUTSIDE a transaction.
    result: ClassificationResult = classify_posting(
        description=posting["description_text"], title=posting["title"],
        employment_type=posting["employment_type"], ai_employment_type=structured.get("ai_employment_type"),
        location_type=posting["location_type"], countries=list(posting["countries"] or []),
        location_text=posting["location_text"], employer_name=posting["employer_name"],
        agency_flag=_bool(org.get("org_linkedin_recruitment_agency_derived")),
        org_industry=org.get("org_linkedin_industry"), content_hash=posting["content_hash"], inference=inference,
    )
    model_version = result.model_version or (getattr(inference, "model_version", "") if inference else "")
    if result.method == "unavailable":
        kind = result.unavailable_kind
        if kind == UNAVAILABLE_TRANSIENT:
            # R03: nothing is closed, nothing is bought; the work item waits and resumes.
            return ClassifyOutcome(posting_id, "wait", f"inference_transient:{result.unavailable_reason}", [], result.method,
                                   retry_after=moment + timedelta(minutes=transient_backoff_minutes))
        reason = (f"inference_unavailable:{result.unavailable_reason or 'not_configured'}" if kind == UNAVAILABLE_CONFIG
                  else f"insufficient_evidence:{result.unavailable_reason or 'no_answer'}")
        with transaction(conn):
            work_queue.assert_owned(conn, work_item, now=moment)
            _assert_snapshot(conn, posting)
            with conn.cursor() as cur:
                cur.execute("UPDATE postings SET state = 'closed', close_reason = %s, updated_at = now() WHERE id = %s",
                            (reason[:200], posting_id))
                if kind == UNAVAILABLE_ANSWER:
                    # A content-level refusal is a result for this model/policy. Without
                    # this receipt lifecycle would reopen it on every cycle forever.
                    cur.execute(
                        "INSERT INTO classifications (posting_id, policy_version, model_version, method, compatible_functions, excluded, result_json) "
                        "VALUES (%s, %s, %s, %s, %s, false, %s) "
                        "ON CONFLICT (posting_id, policy_version, model_version) DO UPDATE SET result_json = EXCLUDED.result_json",
                        (posting_id, POLICY_VERSION, model_version, result.method, [], jsonb({**result.to_dict(), "input_content_hash": posting["content_hash"]})),
                    )
        return ClassifyOutcome(posting_id, "closed", reason, [], result.method)

    opportunity_ids: List[int] = []
    with transaction(conn):
        work_queue.assert_owned(conn, work_item, now=moment)
        _assert_snapshot(conn, posting)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO classifications (posting_id, policy_version, model_version, method, compatible_functions, excluded, exclusion_reason, result_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (posting_id, policy_version, model_version) DO UPDATE SET
                    method = EXCLUDED.method, compatible_functions = EXCLUDED.compatible_functions,
                    excluded = EXCLUDED.excluded, exclusion_reason = EXCLUDED.exclusion_reason,
                    result_json = EXCLUDED.result_json, created_at = now()
                RETURNING id
                """,
                (posting_id, POLICY_VERSION, model_version or "", result.method, result.compatible_functions,
                 result.excluded, result.exclusion_reason or None, jsonb({**result.to_dict(), "input_content_hash": posting["content_hash"]})),
            )
            classification_id = int(cur.fetchone()["id"])
            if result.excluded:
                cur.execute("UPDATE postings SET state = 'closed', close_reason = %s, updated_at = now() WHERE id = %s",
                            (result.exclusion_reason[:200], posting_id))
                _detach_from_open_opportunities(cur, posting_id, moment)
                return ClassifyOutcome(posting_id, "closed", result.exclusion_reason, [], result.method)
            if not result.compatible_functions:
                reason = next((n for n in result.notes if n.startswith("insufficient_evidence")), "insufficient_evidence")
                cur.execute("UPDATE postings SET state = 'closed', close_reason = %s, updated_at = now() WHERE id = %s",
                            (reason[:200], posting_id))
                _detach_from_open_opportunities(cur, posting_id, moment)
                return ClassifyOutcome(posting_id, "closed", reason, [], result.method)
            for fn in result.compatible_functions:
                campaign = CAMPAIGN_BY_FUNCTION[fn]
                cur.execute(
                    """
                    INSERT INTO opportunities (employer_id, function_key, campaign_key, lane, first_posting_at, last_posting_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (employer_id, function_key) DO UPDATE SET
                        last_posting_at = GREATEST(opportunities.last_posting_at, EXCLUDED.last_posting_at),
                        first_posting_at = LEAST(opportunities.first_posting_at, EXCLUDED.first_posting_at),
                        lane = CASE WHEN EXCLUDED.lane = 'fresh' THEN 'fresh' ELSE opportunities.lane END,
                        updated_at = now()
                    RETURNING id, state, evidence_epoch
                    """,
                    (posting["employer_id"], fn, campaign.key, posting["lane"], posting["commercial_age_anchor"], posting["commercial_age_anchor"]),
                )
                row = cur.fetchone()
                oid = int(row["id"])
                opportunity_ids.append(oid)
                cur.execute(
                    "INSERT INTO opportunity_postings (opportunity_id, posting_id, classification_id, evidence_hash) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (opportunity_id, posting_id) DO UPDATE SET classification_id = EXCLUDED.classification_id, "
                    "evidence_hash = EXCLUDED.evidence_hash, linked_at = now() "
                    "WHERE opportunity_postings.evidence_hash IS DISTINCT FROM EXCLUDED.evidence_hash "
                    "OR opportunity_postings.classification_id IS DISTINCT FROM EXCLUDED.classification_id RETURNING posting_id",
                    (oid, posting_id, classification_id, posting["content_hash"]),
                )
                new_evidence = cur.fetchone() is not None
                if row["state"] == "open":
                    work_queue.enqueue(conn, kind="qualify_opportunity", subject_kind="opportunity", subject_id=oid,
                                       lane=posting["lane"], reopen=True, available_at=now)
                elif row["state"] == "closed" and new_evidence:
                    # New evidence reopens a closed opportunity and starts its next evidence
                    # epoch. The attempt history of earlier epochs is kept (recovery finding b).
                    cur.execute("UPDATE opportunities SET state = 'open', close_reason = NULL, evidence_epoch = evidence_epoch + 1, "
                                "reopened_at = %s, updated_at = now() WHERE id = %s", (moment, oid))
                    work_queue.enqueue(conn, kind="qualify_opportunity", subject_kind="opportunity", subject_id=oid,
                                       lane=posting["lane"], reopen=True, available_at=now)
            cur.execute("UPDATE postings SET state = 'classified', close_reason = NULL, updated_at = now() WHERE id = %s", (posting_id,))
    return ClassifyOutcome(posting_id, "classified", "", opportunity_ids, result.method)


def _detach_from_open_opportunities(cur, posting_id: int, moment: datetime) -> None:
    """An excluded/insufficient posting no longer supports its opportunities: those left
    with no active compatible posting close (R10). Delivered history is untouched."""
    cur.execute(
        """
        UPDATE opportunities o SET state = 'closed', close_reason = 'no_active_compatible_posting', updated_at = now()
        WHERE o.state = 'open' AND o.id IN (SELECT opportunity_id FROM opportunity_postings WHERE posting_id = %s)
          AND NOT EXISTS (
            SELECT 1 FROM opportunity_postings op JOIN postings p ON p.id = op.posting_id
            JOIN classifications c ON c.id = op.classification_id
            WHERE op.opportunity_id = o.id AND op.posting_id <> %s AND p.state IN ('identity_resolved', 'classified')
              AND NOT c.excluded AND o.function_key = ANY (c.compatible_functions)
              AND (p.date_valid_through IS NULL OR p.date_valid_through >= %s))
        """,
        (posting_id, posting_id, moment),
    )


def reopen_for_inference(conn: psycopg.Connection, *, model_version: str, now: Optional[datetime] = None,
                         limit: int = 5000) -> int:
    """Postings closed because inference was unavailable or gave no answer are re-entered
    once a (different) model version is configured. No re-acquisition, no modification:
    the same posting id goes back to the classify queue."""
    if not model_version:
        return 0
    moment = now or datetime.now(timezone.utc)
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.lane FROM postings p
                WHERE p.state = 'closed'
                  AND (p.close_reason LIKE 'inference_unavailable:%%' OR p.close_reason LIKE 'insufficient_evidence:%%')
                  AND p.close_reason NOT LIKE 'insufficient_evidence:description_too_short%%'
                  AND p.employer_id IS NOT NULL
                  AND (p.date_valid_through IS NULL OR p.date_valid_through >= %s)
                  AND NOT EXISTS (SELECT 1 FROM classifications c WHERE c.posting_id = p.id AND c.model_version = %s
                                  AND c.policy_version = %s)
                ORDER BY p.commercial_age_anchor DESC LIMIT %s
                """,
                (moment, model_version, POLICY_VERSION, limit),
            )
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                cur.execute("UPDATE postings SET state = 'identity_resolved', close_reason = NULL, updated_at = now() WHERE id = %s", (r["id"],))
                work_queue.enqueue(conn, kind="classify", subject_kind="posting", subject_id=int(r["id"]), lane=r["lane"],
                                   reopen=True, available_at=moment)
    return len(rows)


def _bool(value: Any) -> Optional[bool]:
    if value in (None, ""):
        return None
    return str(value).strip().lower() in {"1", "true", "yes"}


def _assert_snapshot(conn, posting) -> None:
    """Validate the model's input version under the posting row lock before writing."""
    with conn.cursor() as cur:
        cur.execute("SELECT content_hash, employer_id, state FROM postings WHERE id = %s FOR UPDATE", (posting["id"],))
        current = cur.fetchone()
        if (current is None or current["content_hash"] != posting["content_hash"]
                or current["employer_id"] != posting["employer_id"] or current["state"] == "expired"):
            raise work_queue.EvidenceChanged(f"posting {posting['id']} changed during classification")
