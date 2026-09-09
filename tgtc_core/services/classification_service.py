"""Classify one posting and attach it to its employer × function opportunity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

import psycopg

from ..db.connection import jsonb, transaction
from ..db import work_queue
from ..domain.classification import ClassificationResult, classify_posting
from ..domain.inference import InferencePort
from ..policy.campaigns import CAMPAIGN_BY_FUNCTION, POLICY_VERSION


@dataclass
class ClassifyOutcome:
    posting_id: int
    outcome: str  # classified | closed
    reason: str = ""
    opportunity_ids: List[int] = None  # type: ignore[assignment]
    method: str = ""


def classify_one(conn: psycopg.Connection, posting_id: int, *, inference: Optional[InferencePort],
                 now: Optional[datetime] = None) -> ClassifyOutcome:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM postings WHERE id = %s", (posting_id,))
        posting = cur.fetchone()
    conn.commit()
    if not posting:
        raise LookupError(f"posting {posting_id} not found")
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
    opportunity_ids: List[int] = []
    with transaction(conn):
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
                 result.excluded, result.exclusion_reason or None, jsonb(result.to_dict())),
            )
            classification_id = int(cur.fetchone()["id"])
            if result.excluded:
                cur.execute("UPDATE postings SET state = 'closed', close_reason = %s, updated_at = now() WHERE id = %s",
                            (result.exclusion_reason[:200], posting_id))
                return ClassifyOutcome(posting_id, "closed", result.exclusion_reason, [], result.method)
            if not result.compatible_functions:
                reason = next((n for n in result.notes if n.startswith("insufficient_evidence")), "insufficient_evidence")
                cur.execute("UPDATE postings SET state = 'closed', close_reason = %s, updated_at = now() WHERE id = %s",
                            (reason[:200], posting_id))
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
                    RETURNING id, state
                    """,
                    (posting["employer_id"], fn, campaign.key, posting["lane"], posting["commercial_age_anchor"], posting["commercial_age_anchor"]),
                )
                row = cur.fetchone()
                oid = int(row["id"])
                opportunity_ids.append(oid)
                cur.execute(
                    "INSERT INTO opportunity_postings (opportunity_id, posting_id, classification_id) VALUES (%s, %s, %s) "
                    "ON CONFLICT (opportunity_id, posting_id) DO UPDATE SET classification_id = EXCLUDED.classification_id",
                    (oid, posting_id, classification_id),
                )
                if row["state"] == "open":
                    work_queue.enqueue(conn, kind="qualify_opportunity", subject_kind="opportunity", subject_id=oid,
                                       lane=posting["lane"], reopen=True, available_at=now)
                elif row["state"] == "closed":
                    # New evidence reopens a closed opportunity (closed-with-reason is reopenable by design).
                    cur.execute("UPDATE opportunities SET state = 'open', close_reason = NULL, updated_at = now() WHERE id = %s", (oid,))
                    work_queue.enqueue(conn, kind="qualify_opportunity", subject_kind="opportunity", subject_id=oid,
                                       lane=posting["lane"], reopen=True, available_at=now)
            cur.execute("UPDATE postings SET state = 'classified', close_reason = NULL, updated_at = now() WHERE id = %s", (posting_id,))
    return ClassifyOutcome(posting_id, "classified", "", opportunity_ids, result.method)


def _bool(value: Any) -> Optional[bool]:
    if value in (None, ""):
        return None
    return str(value).strip().lower() in {"1", "true", "yes"}
