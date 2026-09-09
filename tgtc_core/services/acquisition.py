"""Fantastic acquisition: independent bounded partitions, receipts before cursor.

Invariants (PRODUCT_CONTRACT §5 / blueprint §5):

* intent (``request_attempts``) is committed BEFORE the HTTP call; the result after;
* rows, their receipt and the cursor advance are ONE transaction, so a failed later
  page never loses an earlier one and a crash between call and commit leaves the
  cursor where it was (the page may be re-billed; it can never be lost);
* a page whose id set equals the previous page is a duplicate page: recorded, offset
  advanced, partition NOT marked complete;
* a partition is complete only when a short page (fewer rows than ``limit``) is
  received; ``complete_coverage`` is set then and only then;
* fresh and backfill partitions are separate rows with separate cursors;
* provider auth/quota refusals stall the partition and record provider state; the
  rows already received stay.
"""

from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import psycopg

from ..db.connection import jsonb, transaction
from ..db import work_queue
from ..domain.identity import content_hash
from ..providers.fantastic import (
    ENDPOINT_JOB_BOARDS, FantasticAuthError, FantasticClient, FantasticQuotaError,
    FantasticRequestError, Page, build_window_params,
)
from ..providers.http import TransportTimeout
from .provider_state import record_refusal, record_served

SOURCE_JOB_BOARDS = "fantastic:active-jb"
SOURCE_ATS = "fantastic:active-ats"
#: The same page served this many times in a row at one offset stalls the partition
#: (kept for a later resume) instead of ever declaring it complete.
MAX_CONSECUTIVE_DUPLICATE_PAGES = 3


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class PartitionRun:
    partition_id: int
    pages: int = 0
    rows: int = 0
    new_postings: int = 0
    modified_postings: int = 0
    duplicate_pages: int = 0
    stop_reason: str = ""
    quota: Dict[str, Any] = field(default_factory=dict)
    new_posting_ids: List[int] = field(default_factory=list)


def posting_content_hash(row: Dict[str, Any]) -> str:
    return content_hash(
        row.get("title"), row.get("description_text"), row.get("employment_type"), row.get("location_type"),
        row.get("url"), row.get("date_valid_through"), row.get("organization"), row.get("organization_url"),
        row.get("org_linkedin_slug"), json.dumps(row.get("locations_derived") or [], sort_keys=True),
    )


def org_block(row: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("organization", "organization_url", "org_linkedin_name", "org_linkedin_slug", "org_linkedin_website",
            "org_linkedin_headcount", "org_linkedin_size", "org_linkedin_industry",
            "org_linkedin_recruitment_agency_derived", "domain_derived", "linkedin_url")
    return {k: row.get(k) for k in keys if row.get(k) not in (None, "", [])}


def structured_block(row: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("ai_employment_type", "ai_taxonomies_a", "ai_key_skills", "ai_salary_min_value", "ai_salary_max_value",
            "ai_salary_currency", "source", "source_type", "ats_duplicate", "remote_derived", "cities_derived",
            "regions_derived")
    return {k: row.get(k) for k in keys if row.get(k) not in (None, "", [])}


def upsert_posting(conn: psycopg.Connection, *, source: str, row: Dict[str, Any], lane: str,
                   now: datetime) -> Tuple[str, int]:
    """Insert or refresh one provider row. Returns ('new'|'unchanged'|'modified', posting_id).

    ``commercial_age_anchor`` is assigned once (never reset by a re-observation).
    A changed content hash records a ``posting_versions`` row.
    """
    provider_id = str(row.get("id") or "").strip()
    if not provider_id:
        raise ValueError("row without provider id")
    h = posting_content_hash(row)
    date_created = _parse_dt(row.get("date_created"))
    date_posted = _parse_dt(row.get("date_posted"))
    anchor = min(d for d in (date_posted, date_created, now) if d is not None)
    countries = [str(c) for c in (row.get("countries_derived") or []) if c]
    locations = row.get("locations_derived") or []
    location_text = ", ".join(str(x) for x in locations[:3]) if isinstance(locations, list) else str(locations or "")
    with conn.cursor() as cur:
        cur.execute("SELECT id, content_hash FROM postings WHERE source = %s AND provider_job_id = %s FOR UPDATE",
                    (source, provider_id))
        existing = cur.fetchone()
        if existing is None:
            cur.execute(
                """
                INSERT INTO postings (source, provider_job_id, content_hash, title, employer_name, description_text, url,
                    employment_type, location_type, location_text, countries, provider_date_created, date_posted,
                    date_valid_through, first_seen_at, last_confirmed_active_at, commercial_age_anchor, org_json,
                    structured_json, lane, state)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'new')
                RETURNING id
                """,
                (source, provider_id, h, row.get("title"), row.get("organization") or row.get("org_linkedin_name"),
                 row.get("description_text"), row.get("url"), row.get("employment_type"), row.get("location_type"),
                 location_text, countries, date_created, date_posted, _parse_dt(row.get("date_valid_through")),
                 now, now, anchor, jsonb(org_block(row)), jsonb(structured_block(row)), lane),
            )
            pid = int(cur.fetchone()["id"])
            cur.execute("INSERT INTO posting_versions (posting_id, version, content_hash, changes) VALUES (%s, 1, %s, %s)",
                        (pid, h, jsonb({"event": "first_seen"})))
            return "new", pid
        pid = int(existing["id"])
        if existing["content_hash"] == h:
            cur.execute("UPDATE postings SET last_confirmed_active_at = %s, updated_at = now() WHERE id = %s", (now, pid))
            return "unchanged", pid
        cur.execute(
            """
            UPDATE postings SET content_hash = %s, title = %s, description_text = %s, url = %s, employment_type = %s,
                   location_type = %s, location_text = %s, countries = %s, date_valid_through = %s,
                   last_confirmed_active_at = %s, date_modified = %s, org_json = %s, structured_json = %s, updated_at = now()
            WHERE id = %s
            """,
            (h, row.get("title"), row.get("description_text"), row.get("url"), row.get("employment_type"),
             row.get("location_type"), location_text, countries, _parse_dt(row.get("date_valid_through")), now, now,
             jsonb(org_block(row)), jsonb(structured_block(row)), pid),
        )
        cur.execute("SELECT COALESCE(MAX(version), 0) AS v FROM posting_versions WHERE posting_id = %s", (pid,))
        version = int(cur.fetchone()["v"]) + 1
        cur.execute("INSERT INTO posting_versions (posting_id, version, content_hash, changes) VALUES (%s, %s, %s, %s)",
                    (pid, version, h, jsonb({"event": "modified"})))
        return "modified", pid


class AcquisitionService:
    def __init__(self, conn: psycopg.Connection, client: FantasticClient, *, page_limit: int, time_frame: str,
                 fresh_window_minutes: int, fresh_lag_minutes: int, backfill_window_hours: int,
                 max_pages_per_partition: int, min_jobs_quota_remaining: int, min_requests_quota_remaining: int,
                 location: Optional[str] = "United States", now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.conn = conn
        self.client = client
        self.page_limit = page_limit
        self.time_frame = time_frame
        self.fresh_window = timedelta(minutes=fresh_window_minutes)
        self.fresh_lag = timedelta(minutes=fresh_lag_minutes)
        self.backfill_window = timedelta(hours=backfill_window_hours)
        self.max_pages = max_pages_per_partition
        self.min_jobs_quota = min_jobs_quota_remaining
        self.min_requests_quota = min_requests_quota_remaining
        self.location = location
        self.now = now

    # --- planning ---------------------------------------------------------
    def plan_fresh_partitions(self, source: str = SOURCE_JOB_BOARDS, *, max_new: int = 24) -> List[int]:
        """Create hourly fresh windows from the last window end up to now - lag.

        A missing (failed) window is never skipped: partitions are contiguous, and a
        partition that did not complete keeps its own state; new windows are
        scheduled regardless (blueprint: 'las nuevas particiones se programan aunque
        quede un hueco histórico').
        """
        moment = self.now()
        upper_bound = moment - self.fresh_lag
        upper_bound = upper_bound.replace(minute=0, second=0, microsecond=0)
        created: List[int] = []
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("SELECT MAX(window_end) AS e FROM source_partitions WHERE source = %s AND lane = 'fresh'", (source,))
                row = cur.fetchone()
                start = row["e"] if row and row["e"] else upper_bound - self.fresh_window
                while start < upper_bound and len(created) < max_new:
                    end = start + self.fresh_window
                    cur.execute(
                        """
                        INSERT INTO source_partitions (source, lane, window_start, window_end)
                        VALUES (%s, 'fresh', %s, %s)
                        ON CONFLICT (source, lane, window_start, window_end) DO NOTHING RETURNING id
                        """,
                        (source, start, end),
                    )
                    r = cur.fetchone()
                    if r:
                        created.append(int(r["id"]))
                    start = end
        return created

    def plan_backfill_partition(self, source: str = SOURCE_JOB_BOARDS) -> Optional[int]:
        """One more backfill window, walking backwards from the oldest fresh window."""
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("SELECT MIN(window_start) AS s FROM source_partitions WHERE source = %s AND lane = 'backfill'", (source,))
                oldest_backfill = cur.fetchone()["s"]
                cur.execute("SELECT MIN(window_start) AS s FROM source_partitions WHERE source = %s AND lane = 'fresh'", (source,))
                oldest_fresh = cur.fetchone()["s"]
                end = oldest_backfill or oldest_fresh or (self.now() - self.fresh_lag)
                start = end - self.backfill_window
                cur.execute(
                    """
                    INSERT INTO source_partitions (source, lane, window_start, window_end)
                    VALUES (%s, 'backfill', %s, %s)
                    ON CONFLICT (source, lane, window_start, window_end) DO NOTHING RETURNING id
                    """,
                    (source, start, end),
                )
                r = cur.fetchone()
                return int(r["id"]) if r else None

    def open_partitions(self, lane: str, source: str = SOURCE_JOB_BOARDS, limit: int = 10) -> List[int]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM source_partitions WHERE source = %s AND lane = %s AND state = 'open' ORDER BY window_start %s LIMIT %s"
                % ("%s", "%s", "DESC" if lane == "fresh" else "DESC", "%s"),
                (source, lane, limit),
            )
            return [int(r["id"]) for r in cur.fetchall()]

    # --- execution --------------------------------------------------------
    def _load_partition(self, partition_id: int) -> Dict[str, Any]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM source_partitions WHERE id = %s", (partition_id,))
            row = cur.fetchone()
        if not row:
            raise LookupError(f"partition {partition_id} not found")
        return dict(row)

    def _last_receipt_fingerprint(self, partition_id: int) -> Tuple[str, Optional[int], Dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id, fingerprint, quota_jobs_remaining, quota_requests_remaining FROM page_receipts "
                "WHERE partition_id = %s ORDER BY id DESC LIMIT 1", (partition_id,))
            r = cur.fetchone()
        if not r:
            return "", None, {}
        return str(r["fingerprint"]), int(r["id"]), {"jobs_remaining": r["quota_jobs_remaining"],
                                                        "requests_remaining": r["quota_requests_remaining"]}

    def _quota_breach(self, quota: Dict[str, Any]) -> str:
        jr, rr = quota.get("jobs_remaining"), quota.get("requests_remaining")
        if rr is not None and rr - 1 < self.min_requests_quota:
            return "quota_reserve:requests"
        if jr is not None and jr - self.page_limit < self.min_jobs_quota:
            return "quota_reserve:jobs"
        return ""

    def run_partition(self, partition_id: int, *, max_pages: Optional[int] = None) -> PartitionRun:
        run = PartitionRun(partition_id=partition_id)
        pages_budget = max_pages if max_pages is not None else self.max_pages
        endpoint = ENDPOINT_JOB_BOARDS
        consecutive_duplicates = 0
        for _ in range(pages_budget):
            part = self._load_partition(partition_id)
            if part["state"] != "open":
                run.stop_reason = f"partition_{part['state']}"
                break
            prev_fp, prev_receipt_id, last_quota = self._last_receipt_fingerprint(partition_id)
            breach = self._quota_breach(last_quota)
            if breach:
                run.stop_reason = breach
                break
            offset = int(part["next_offset"])
            params = build_window_params(
                lower_iso=_iso(part["window_start"]), upper_iso=_iso(part["window_end"]), limit=self.page_limit,
                offset=offset, time_frame=self.time_frame, location=self.location,
            )
            # 1) intent, committed before the call
            with transaction(self.conn):
                with self.conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO request_attempts (provider, operation, partition_id, params_json, estimated_credits)
                        VALUES ('fantastic', 'page', %s, %s, %s) RETURNING id
                        """,
                        (partition_id, jsonb(params), self.page_limit),
                    )
                    attempt_id = int(cur.fetchone()["id"])
            # 2) the call, outside any transaction
            try:
                page: Page = self.client.fetch_page(endpoint, params)
            except TransportTimeout:
                self._finish_attempt(attempt_id, "uncertain", None, "timeout", "TransportTimeout")
                self._credit_event(attempt_id, "page", requests=1, estimated=self.page_limit, confirmed=None)
                run.stop_reason = "timeout_uncertain"
                break
            except FantasticAuthError as exc:
                self._finish_attempt(attempt_id, "refused", 401, "auth", str(exc))
                self._stall(partition_id, f"auth:{exc}")
                record_refusal(self.conn, "fantastic", str(exc), state="unauthorized")
                run.stop_reason = "auth_refused"
                break
            except FantasticQuotaError as exc:
                self._finish_attempt(attempt_id, "refused", 429, "quota", str(exc))
                self._stall(partition_id, f"quota:{exc}")
                record_refusal(self.conn, "fantastic", str(exc))
                run.stop_reason = "quota_refused"
                break
            except FantasticRequestError as exc:
                self._finish_attempt(attempt_id, "failed", exc.status, exc.stage, exc.code)
                with transaction(self.conn):
                    with self.conn.cursor() as cur:
                        cur.execute("UPDATE source_partitions SET last_error = %s, updated_at = now() WHERE id = %s",
                                    (f"{exc.stage}:{exc.code}", partition_id))
                run.stop_reason = f"request_error:{exc.code}"
                break
            # 3) rows + receipt + cursor: one transaction
            moment = self.now()
            ids = [str(r.get("id") or "") for r in page.rows]
            fp = hashlib.sha256("\x1f".join(ids).encode()).hexdigest()
            duplicate_page = bool(ids) and fp == prev_fp
            with transaction(self.conn):
                with self.conn.cursor() as cur:
                    cur.execute(
                        "UPDATE request_attempts SET status = 'served', http_status = %s, finished_at = now(), "
                        "response_summary = %s, confirmed_credits = NULL WHERE id = %s",
                        (page.status, jsonb({"rows": len(page.rows), "quota": page.quota.to_dict(),
                                             "pii_fields_dropped": page.pii_fields_dropped}), attempt_id),
                    )
                    new_here = 0
                    modified_here = 0
                    if not duplicate_page:
                        for row in page.rows:
                            if not str(row.get("id") or "").strip():
                                continue
                            state, pid = upsert_posting(self.conn, source=part["source"], row=row, lane=part["lane"], now=moment)
                            if state == "new":
                                new_here += 1
                                run.new_posting_ids.append(pid)
                                work_queue.enqueue(self.conn, kind="resolve_identity", subject_kind="posting",
                                                   subject_id=pid, lane=part["lane"], available_at=moment)
                            elif state == "modified":
                                modified_here += 1
                                work_queue.enqueue(self.conn, kind="classify", subject_kind="posting", subject_id=pid,
                                                   lane=part["lane"], reopen=True, available_at=moment)
                    cur.execute(
                        """
                        INSERT INTO page_receipts (partition_id, attempt_id, page_offset, page_limit, row_ids, row_count,
                            fingerprint, duplicate_of_receipt_id, quota_jobs_remaining, quota_requests_remaining,
                            quota_next_billing_date, rows_compressed)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (partition_id, attempt_id, offset, self.page_limit, ids, len(ids), fp,
                         prev_receipt_id if duplicate_page else None, page.quota.jobs_remaining,
                         page.quota.requests_remaining, page.quota.next_billing_date,
                         zlib.compress(json.dumps(page.rows, default=str).encode("utf-8"))),
                    )
                    # A duplicate page was billed but proved nothing about THIS offset: the cursor
                    # does not move and the same offset is requested again. Only a genuinely
                    # short, non-duplicate page completes the partition.
                    complete = (not duplicate_page) and len(page.rows) < self.page_limit
                    advance = 0 if duplicate_page else len(page.rows)
                    consecutive_duplicates = consecutive_duplicates + 1 if duplicate_page else 0
                    stall = duplicate_page and consecutive_duplicates >= MAX_CONSECUTIVE_DUPLICATE_PAGES
                    cur.execute(
                        """
                        UPDATE source_partitions SET next_offset = next_offset + %s, pages_received = pages_received + 1,
                               rows_received = rows_received + %s, rows_new = rows_new + %s,
                               duplicate_pages = duplicate_pages + %s,
                               state = CASE WHEN %s THEN 'complete' WHEN %s THEN 'stalled' ELSE state END,
                               complete_coverage = CASE WHEN %s THEN true ELSE complete_coverage END,
                               last_error = CASE WHEN %s THEN 'duplicate_page_loop' ELSE NULL END, updated_at = now()
                        WHERE id = %s
                        """,
                        (advance, len(page.rows), new_here, 1 if duplicate_page else 0, complete, stall, complete, stall, partition_id),
                    )
                    cur.execute(
                        "INSERT INTO credit_events (provider, operation, attempt_id, requests, estimated_credits, confirmed_credits, basis) "
                        "VALUES ('fantastic', 'page', %s, 1, %s, %s, %s)",
                        (attempt_id, len(page.rows), len(page.rows) if page.quota.jobs_remaining is not None else None,
                         "provider_header" if page.quota.jobs_remaining is not None else "estimate"),
                    )
            record_served(self.conn, "fantastic")
            run.pages += 1
            run.rows += len(page.rows)
            run.new_postings += new_here
            run.modified_postings += modified_here
            run.duplicate_pages += 1 if duplicate_page else 0
            run.quota = page.quota.to_dict()
            if complete:
                run.stop_reason = "complete"
                break
            if stall:
                run.stop_reason = "duplicate_page_loop"
                break
        else:
            run.stop_reason = run.stop_reason or "page_budget"
        return run

    def _finish_attempt(self, attempt_id: int, status: str, http_status: Optional[int], error_class: str, error_code: str) -> None:
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE request_attempts SET status = %s, http_status = %s, error_class = %s, error_code = %s, finished_at = now() WHERE id = %s",
                    (status, http_status, error_class[:80], error_code[:200], attempt_id),
                )

    def _credit_event(self, attempt_id: int, operation: str, *, requests: int, estimated: Optional[int], confirmed: Optional[int]) -> None:
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO credit_events (provider, operation, attempt_id, requests, estimated_credits, confirmed_credits, basis) "
                    "VALUES ('fantastic', %s, %s, %s, %s, %s, 'estimate')",
                    (operation, attempt_id, requests, estimated, confirmed),
                )

    def _stall(self, partition_id: int, error: str) -> None:
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("UPDATE source_partitions SET state = 'stalled', last_error = %s, updated_at = now() WHERE id = %s",
                            (error[:300], partition_id))

    def reopen_stalled(self, source: str = SOURCE_JOB_BOARDS) -> int:
        """After the provider serves again, stalled partitions resume from their own cursor."""
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("UPDATE source_partitions SET state = 'open', updated_at = now() WHERE source = %s AND state = 'stalled'", (source,))
                return cur.rowcount
