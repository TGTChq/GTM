"""Fantastic acquisition: independent bounded partitions, receipts before cursor.

Invariants (PRODUCT_CONTRACT §5 / blueprint §5), tightened after review a090a2b:

* intent (``request_attempts``) is committed BEFORE the HTTP call; the result after;
* rows, their receipt and the cursor advance are ONE transaction, so a failed later
  page never loses an earlier one and a crash between call and commit leaves the
  cursor where it was (the page may be re-billed; it can never be lost);
* a page whose id set equals the previous page is a duplicate page: recorded, the
  cursor does NOT move, the same offset is requested again; a loop stalls the partition;
* a partition is complete only when a short, non-duplicate page is received;
* R07 -- a partition is acquired under an exclusive lease; every cursor commit is
  fenced by the lease token AND the expected offset. A page received after the lease
  was lost is kept (billed, auditable, ``fenced=true``) but never moves the cursor;
* R02 -- every source maps to its own endpoint and parameter set; ``exclude_ats_duplicate``
  is sent on the job-board feed only when the ATS feed is also scheduled;
* R09 -- stalled partitions are reopened by the scheduler when the provider is no
  longer on record as refusing; a quota reading older than the staleness window or
  past its billing date never blocks a request;
* R10 -- the content hash covers every decision-relevant provider field; a change
  records a version with the changed field names and re-enters the posting;
* R11 -- a posting whose ``date_valid_through`` has passed is expired at observation.
"""

from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import psycopg

from ..db.connection import jsonb, transaction
from ..db import work_queue
from ..domain.identity import content_hash
from ..providers.fantastic import (
    ENDPOINT_ATS, ENDPOINT_JOB_BOARDS, FantasticAuthError, FantasticClient, FantasticQuotaError,
    FantasticRequestError, Page, build_window_params,
)
from ..providers.http import TransportTimeout
from . import provider_state

SOURCE_JOB_BOARDS = "fantastic:active-jb"
SOURCE_ATS = "fantastic:active-ats"
PROVIDER = "fantastic"


@dataclass(frozen=True)
class SourceSpec:
    key: str
    endpoint: str
    #: The job-board feed can drop rows that also appear in the ATS feed. Doing so is
    #: only correct when the ATS feed is acquired too.
    supports_exclude_ats_duplicate: bool


SOURCE_SPECS: Dict[str, SourceSpec] = {
    SOURCE_JOB_BOARDS: SourceSpec(SOURCE_JOB_BOARDS, ENDPOINT_JOB_BOARDS, True),
    SOURCE_ATS: SourceSpec(SOURCE_ATS, ENDPOINT_ATS, False),
}
DEFAULT_SOURCES: Tuple[str, ...] = (SOURCE_JOB_BOARDS, SOURCE_ATS)

#: The same page served this many times in a row at one offset stalls the partition
#: (kept for a later resume) instead of ever declaring it complete.
MAX_CONSECUTIVE_DUPLICATE_PAGES = 3

#: Every provider field that can change a decision (eligibility, identity, destination).
#: Any change here is a new posting version and re-enters the posting (R10).
TRACKED_ROW_FIELDS: Tuple[str, ...] = (
    "title", "description_text", "url", "employment_type", "ai_employment_type", "location_type",
    "locations_derived", "countries_derived", "date_valid_through", "organization", "organization_url",
    "domain_derived", "org_linkedin_slug", "org_linkedin_website", "org_linkedin_name",
    "org_linkedin_headcount", "org_linkedin_industry", "org_linkedin_recruitment_agency_derived",
    "ai_taxonomies_a", "ats_duplicate", "source", "source_type",
)
#: Changes to these re-resolve the employer identity before reclassifying.
IDENTITY_FIELDS: Tuple[str, ...] = (
    "organization", "organization_url", "domain_derived", "org_linkedin_slug", "org_linkedin_website", "org_linkedin_name",
)


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
    expired_postings: int = 0
    duplicate_pages: int = 0
    stop_reason: str = ""
    quota: Dict[str, Any] = field(default_factory=dict)
    new_posting_ids: List[int] = field(default_factory=list)


@dataclass
class UpsertResult:
    state: str            # new | unchanged | modified | expired
    posting_id: int
    changes: List[str] = field(default_factory=list)
    identity_changed: bool = False
    expired: bool = False

    def __iter__(self):  # keeps ``state, pid = upsert_posting(...)`` working
        yield self.state
        yield self.posting_id


def tracked_snapshot(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: row.get(k) for k in TRACKED_ROW_FIELDS if row.get(k) not in (None, "", [])}


def posting_content_hash(row: Dict[str, Any]) -> str:
    return content_hash(json.dumps(tracked_snapshot(row), sort_keys=True, default=str))


def org_block(row: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("organization", "organization_url", "org_linkedin_name", "org_linkedin_slug", "org_linkedin_website",
            "org_linkedin_headcount", "org_linkedin_size", "org_linkedin_industry",
            "org_linkedin_recruitment_agency_derived", "domain_derived", "linkedin_url")
    return {k: row.get(k) for k in keys if row.get(k) not in (None, "", [])}


def structured_block(row: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("ai_employment_type", "ai_taxonomies_a", "ai_key_skills", "ai_salary_min_value", "ai_salary_max_value",
            "ai_salary_currency", "source", "source_type", "ats_duplicate", "remote_derived", "cities_derived",
            "regions_derived")
    out = {k: row.get(k) for k in keys if row.get(k) not in (None, "", [])}
    out["tracked"] = tracked_snapshot(row)
    return out


def _int(value: Any) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> Optional[bool]:
    if value in (None, ""):
        return None
    return str(value).strip().lower() in {"1", "true", "yes"}


def refresh_employer_facts(cur, employer_id: int, org: Dict[str, Any]) -> None:
    """Provider organization facts changed (R10): keep the latest observation. Headcount
    and industry from Apollo stay authoritative once an enrichment exists; the agency
    flag is always the latest provider value."""
    cur.execute(
        """
        UPDATE employers SET
            employee_count = CASE WHEN enriched_at IS NULL THEN COALESCE(%s, employee_count) ELSE employee_count END,
            industry = CASE WHEN enriched_at IS NULL THEN COALESCE(%s, industry) ELSE industry END,
            agency_flag = COALESCE(%s, agency_flag),
            facts_json = facts_json || %s, updated_at = now()
        WHERE id = %s
        """,
        (_int(org.get("org_linkedin_headcount")), org.get("org_linkedin_industry") or None,
         _bool(org.get("org_linkedin_recruitment_agency_derived")), jsonb({"fantastic": org}), employer_id),
    )


def upsert_posting(conn: psycopg.Connection, *, source: str, row: Dict[str, Any], lane: str,
                   now: datetime) -> UpsertResult:
    """Insert or refresh one provider row.

    ``commercial_age_anchor`` is assigned once (never reset by a re-observation).
    A changed evidence hash records a ``posting_versions`` row naming the changed
    fields. A posting whose ``date_valid_through`` has passed is expired.
    """
    provider_id = str(row.get("id") or "").strip()
    if not provider_id:
        raise ValueError("row without provider id")
    h = posting_content_hash(row)
    snapshot = tracked_snapshot(row)
    date_created = _parse_dt(row.get("date_created"))
    date_posted = _parse_dt(row.get("date_posted"))
    valid_through = _parse_dt(row.get("date_valid_through"))
    expired = bool(valid_through and valid_through < now)
    anchor = min(d for d in (date_posted, date_created, now) if d is not None)
    countries = [str(c) for c in (row.get("countries_derived") or []) if c]
    locations = row.get("locations_derived") or []
    location_text = ", ".join(str(x) for x in locations[:3]) if isinstance(locations, list) else str(locations or "")
    org = org_block(row)
    with conn.cursor() as cur:
        cur.execute("SELECT id, content_hash, state, employer_id, structured_json FROM postings WHERE source = %s AND provider_job_id = %s FOR UPDATE",
                    (source, provider_id))
        existing = cur.fetchone()
        if existing is None:
            cur.execute(
                """
                INSERT INTO postings (source, provider_job_id, content_hash, title, employer_name, description_text, url,
                    employment_type, location_type, location_text, countries, provider_date_created, date_posted,
                    date_valid_through, first_seen_at, last_confirmed_active_at, commercial_age_anchor, org_json,
                    structured_json, lane, state, expired_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (source, provider_id, h, row.get("title"), row.get("organization") or row.get("org_linkedin_name"),
                 row.get("description_text"), row.get("url"), row.get("employment_type"), row.get("location_type"),
                 location_text, countries, date_created, date_posted, valid_through,
                 now, now, anchor, jsonb(org), jsonb(structured_block(row)), lane,
                 "expired" if expired else "new", now if expired else None),
            )
            pid = int(cur.fetchone()["id"])
            cur.execute("INSERT INTO posting_versions (posting_id, version, content_hash, changes) VALUES (%s, 1, %s, %s)",
                        (pid, h, jsonb({"event": "first_seen", "expired_on_arrival": expired})))
            return UpsertResult("expired" if expired else "new", pid, expired=expired)
        pid = int(existing["id"])
        if existing["content_hash"] == h:
            if expired and existing["state"] not in ("expired", "closed"):
                cur.execute("UPDATE postings SET state = 'expired', expired_at = %s, last_confirmed_active_at = %s, updated_at = now() WHERE id = %s",
                            (now, now, pid))
                return UpsertResult("expired", pid, expired=True)
            cur.execute("UPDATE postings SET last_confirmed_active_at = %s, updated_at = now() WHERE id = %s", (now, pid))
            return UpsertResult("unchanged", pid)
        old_snapshot = dict((existing["structured_json"] or {}).get("tracked") or {})
        changes = sorted(k for k in set(old_snapshot) | set(snapshot) if old_snapshot.get(k) != snapshot.get(k))
        identity_changed = any(k in IDENTITY_FIELDS for k in changes)
        new_state = "expired" if expired else ("new" if identity_changed else "identity_resolved")
        if existing["state"] == "closed" and not expired:
            new_state = "new" if identity_changed else "identity_resolved"
        cur.execute(
            """
            UPDATE postings SET content_hash = %s, title = %s, employer_name = %s, description_text = %s, url = %s, employment_type = %s,
                   location_type = %s, location_text = %s, countries = %s, date_valid_through = %s,
                   last_confirmed_active_at = %s, date_modified = %s, org_json = %s, structured_json = %s,
                   state = CASE WHEN state = 'expired' AND NOT %s THEN 'identity_resolved' ELSE %s END,
                   close_reason = NULL, expired_at = CASE WHEN %s THEN %s ELSE NULL END, updated_at = now()
            WHERE id = %s
            """,
            (h, row.get("title"), row.get("organization") or row.get("org_linkedin_name"), row.get("description_text"),
             row.get("url"), row.get("employment_type"), row.get("location_type"), location_text, countries, valid_through,
             now, now, jsonb(org), jsonb(structured_block(row)), expired, new_state, expired, now, pid),
        )
        if existing["employer_id"] is not None and not identity_changed:
            refresh_employer_facts(cur, int(existing["employer_id"]), org)
        cur.execute("SELECT COALESCE(MAX(version), 0) AS v FROM posting_versions WHERE posting_id = %s", (pid,))
        version = int(cur.fetchone()["v"]) + 1
        cur.execute("INSERT INTO posting_versions (posting_id, version, content_hash, changes) VALUES (%s, %s, %s, %s)",
                    (pid, version, h, jsonb({"event": "modified", "fields": changes, "identity_changed": identity_changed,
                                             "expired": expired})))
        return UpsertResult("expired" if expired else "modified", pid, changes=changes,
                            identity_changed=identity_changed, expired=expired)


class AcquisitionService:
    def __init__(self, conn: psycopg.Connection, client: FantasticClient, *, page_limit: int, time_frame: str,
                 fresh_window_minutes: int, fresh_lag_minutes: int, backfill_window_hours: int,
                 max_pages_per_partition: int, min_jobs_quota_remaining: int, min_requests_quota_remaining: int,
                 location: Optional[str] = "United States", sources: Sequence[str] = DEFAULT_SOURCES,
                 lease_seconds: int = 900, quota_max_age_hours: float = 24.0, provider_retry_hours: float = 6.0,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
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
        self.sources = tuple(sources)
        for s in self.sources:
            if s not in SOURCE_SPECS:
                raise ValueError(f"unknown Fantastic source {s!r}; known: {sorted(SOURCE_SPECS)}")
        self.lease_seconds = lease_seconds
        self.quota_max_age = timedelta(hours=quota_max_age_hours)
        self.provider_retry_hours = provider_retry_hours
        self.now = now

    # --- request shape per source (R02) -------------------------------------
    def request_params(self, source: str, *, lower: datetime, upper: datetime, offset: int) -> Tuple[str, Dict[str, Any]]:
        spec = SOURCE_SPECS[source]
        exclude = spec.supports_exclude_ats_duplicate and SOURCE_ATS in self.sources
        params = build_window_params(lower_iso=_iso(lower), upper_iso=_iso(upper), limit=self.page_limit, offset=offset,
                                     time_frame=self.time_frame, location=self.location, exclude_ats_duplicate=exclude)
        return spec.endpoint, params

    # --- planning ---------------------------------------------------------
    def plan_fresh_partitions(self, source: str = SOURCE_JOB_BOARDS, *, max_new: int = 24) -> List[int]:
        """Create hourly fresh windows from the last window end up to now - lag.

        Partitions are contiguous; a window that did not complete keeps its own state
        and new windows are still scheduled (blueprint: a historical gap never blocks
        fresh coverage).
        """
        moment = self.now()
        upper_bound = (moment - self.fresh_lag).replace(minute=0, second=0, microsecond=0)
        created: List[int] = []
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("SELECT MAX(window_end) AS e FROM source_partitions WHERE source = %s AND lane = 'fresh'", (source,))
                row = cur.fetchone()
                start = row["e"] if row and row["e"] else upper_bound - self.fresh_window
                while start < upper_bound and len(created) < max_new:
                    end = start + self.fresh_window
                    cur.execute(
                        "INSERT INTO source_partitions (source, lane, window_start, window_end) VALUES (%s, 'fresh', %s, %s) "
                        "ON CONFLICT (source, lane, window_start, window_end) DO NOTHING RETURNING id",
                        (source, start, end),
                    )
                    r = cur.fetchone()
                    if r:
                        created.append(int(r["id"]))
                    start = end
        return created

    def plan_backfill_partition(self, source: str = SOURCE_JOB_BOARDS) -> Optional[int]:
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("SELECT MIN(window_start) AS s FROM source_partitions WHERE source = %s AND lane = 'backfill'", (source,))
                oldest_backfill = cur.fetchone()["s"]
                cur.execute("SELECT MIN(window_start) AS s FROM source_partitions WHERE source = %s AND lane = 'fresh'", (source,))
                oldest_fresh = cur.fetchone()["s"]
                end = oldest_backfill or oldest_fresh or (self.now() - self.fresh_lag)
                start = end - self.backfill_window
                cur.execute(
                    "INSERT INTO source_partitions (source, lane, window_start, window_end) VALUES (%s, 'backfill', %s, %s) "
                    "ON CONFLICT (source, lane, window_start, window_end) DO NOTHING RETURNING id",
                    (source, start, end),
                )
                r = cur.fetchone()
                return int(r["id"]) if r else None

    def open_partitions(self, lane: str, source: str = SOURCE_JOB_BOARDS, limit: int = 10) -> List[int]:
        """Open partitions not under an active lease (R07)."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM source_partitions WHERE source = %s AND lane = %s AND state = 'open' "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= %s) ORDER BY window_start DESC LIMIT %s",
                (source, lane, self.now(), limit),
            )
            ids = [int(r["id"]) for r in cur.fetchall()]
        self.conn.commit()
        return ids

    # --- ownership (R07) --------------------------------------------------
    def claim_partition(self, partition_id: int) -> Optional[str]:
        moment = self.now()
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE source_partitions SET lease_token = gen_random_uuid(),
                           lease_expires_at = %s + make_interval(secs => %s), updated_at = now()
                    WHERE id = %s AND state = 'open' AND (lease_expires_at IS NULL OR lease_expires_at <= %s)
                    RETURNING lease_token
                    """,
                    (moment, self.lease_seconds, partition_id, moment),
                )
                row = cur.fetchone()
        return str(row["lease_token"]) if row else None

    def release_partition(self, partition_id: int, token: str) -> None:
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("UPDATE source_partitions SET lease_token = NULL, lease_expires_at = NULL, updated_at = now() "
                            "WHERE id = %s AND lease_token = %s", (partition_id, token))

    # --- recovery (R09) ---------------------------------------------------
    def recover_partitions(self, source: str = SOURCE_JOB_BOARDS) -> Dict[str, Any]:
        """Reopen stalled partitions when the provider is not on record as refusing, or
        the retry interval has elapsed (the next page request IS the controlled probe:
        a refusal stalls them again and records the refusal). Cursors are untouched."""
        gate = provider_state.reserve_probe(self.conn, PROVIDER, retry_hours=self.provider_retry_hours, now=self.now())
        if not gate["allowed"]:
            return {"reopened": 0, "reason": f"provider_{gate['state']}", "next_attempt_after": gate.get("next_attempt_after")}
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("UPDATE source_partitions SET state = 'open', updated_at = now() WHERE source = %s AND state = 'stalled' RETURNING id",
                            (source,))
                ids = [int(r["id"]) for r in cur.fetchall()]
        return {"reopened": len(ids), "partition_ids": ids, "reason": "provider_probe" if gate.get("reserved") else "provider_available"}

    def reopen_stalled(self, source: str = SOURCE_JOB_BOARDS) -> int:
        return int(self.recover_partitions(source).get("reopened", 0))

    # --- quota (R09) ------------------------------------------------------
    def _latest_quota(self, source: str) -> Dict[str, Any]:
        """The most recent quota reading for this source across ALL its partitions, or
        {} when it is stale (older than the staleness window or past its billing date)."""
        moment = self.now()
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.quota_jobs_remaining, r.quota_requests_remaining, r.quota_next_billing_date, r.received_at
                FROM page_receipts r JOIN source_partitions p ON p.id = r.partition_id
                WHERE p.source = %s AND r.quota_jobs_remaining IS NOT NULL ORDER BY r.id DESC LIMIT 1
                """,
                (source,),
            )
            r = cur.fetchone()
        self.conn.commit()
        if not r:
            return {}
        if r["received_at"] < moment - self.quota_max_age:
            return {}
        nbd = _parse_dt(r["quota_next_billing_date"])
        if nbd is None and r["quota_next_billing_date"]:
            try:
                nbd = datetime.fromisoformat(str(r["quota_next_billing_date"])[:10]).replace(tzinfo=timezone.utc)
            except ValueError:
                nbd = None
        if nbd is not None and nbd <= moment:
            return {}   # the billing cycle has turned over since this reading
        return {"jobs_remaining": r["quota_jobs_remaining"], "requests_remaining": r["quota_requests_remaining"]}

    def _quota_breach(self, quota: Dict[str, Any]) -> str:
        jr, rr = quota.get("jobs_remaining"), quota.get("requests_remaining")
        if rr is not None and rr - 1 < self.min_requests_quota:
            return "quota_reserve:requests"
        if jr is not None and jr - self.page_limit < self.min_jobs_quota:
            return "quota_reserve:jobs"
        return ""

    # --- execution --------------------------------------------------------
    def _load_partition(self, partition_id: int) -> Dict[str, Any]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM source_partitions WHERE id = %s", (partition_id,))
            row = cur.fetchone()
        self.conn.commit()
        if not row:
            raise LookupError(f"partition {partition_id} not found")
        return dict(row)

    def _last_receipt_fingerprint(self, partition_id: int) -> Tuple[str, Optional[int]]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT id, fingerprint FROM page_receipts WHERE partition_id = %s AND NOT fenced ORDER BY id DESC LIMIT 1",
                        (partition_id,))
            r = cur.fetchone()
        self.conn.commit()
        return (str(r["fingerprint"]), int(r["id"])) if r else ("", None)

    def run_partition(self, partition_id: int, *, max_pages: Optional[int] = None) -> PartitionRun:
        run = PartitionRun(partition_id=partition_id)
        token = self.claim_partition(partition_id)
        if token is None:
            run.stop_reason = "partition_not_owned"
            return run
        try:
            return self._run_owned(partition_id, token, run, max_pages if max_pages is not None else self.max_pages)
        finally:
            self.release_partition(partition_id, token)

    def _run_owned(self, partition_id: int, token: str, run: PartitionRun, pages_budget: int) -> PartitionRun:
        consecutive_duplicates = 0
        stall = False
        for _ in range(pages_budget):
            part = self._load_partition(partition_id)
            if part["state"] != "open":
                run.stop_reason = f"partition_{part['state']}"
                break
            if str(part.get("lease_token") or "") != token:
                run.stop_reason = "lease_lost"
                break
            source = part["source"]
            prev_fp, prev_receipt_id = self._last_receipt_fingerprint(partition_id)
            breach = self._quota_breach(self._latest_quota(source))
            if breach:
                run.stop_reason = breach
                break
            offset = int(part["next_offset"])
            endpoint, params = self.request_params(source, lower=part["window_start"], upper=part["window_end"], offset=offset)
            # 1) intent, committed before the call
            with transaction(self.conn):
                with self.conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO request_attempts (provider, operation, partition_id, params_json, estimated_credits) "
                        "VALUES ('fantastic', 'page', %s, %s, %s) RETURNING id",
                        (partition_id, jsonb({"endpoint": endpoint, **params}), self.page_limit),
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
                provider_state.record_refusal(self.conn, PROVIDER, str(exc), state=provider_state.UNAUTHORIZED, now=self.now())
                run.stop_reason = "auth_refused"
                break
            except FantasticQuotaError as exc:
                self._finish_attempt(attempt_id, "refused", 429, "quota", str(exc))
                self._stall(partition_id, f"quota:{exc}")
                provider_state.record_refusal(self.conn, PROVIDER, str(exc), now=self.now())
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
            # 3) rows + receipt + fenced cursor advance: one transaction
            moment = self.now()
            ids = [str(r.get("id") or "") for r in page.rows]
            fp = hashlib.sha256("\x1f".join(ids).encode()).hexdigest()
            duplicate_page = bool(ids) and fp == prev_fp
            complete = (not duplicate_page) and len(page.rows) < self.page_limit
            advance = 0 if duplicate_page else len(page.rows)
            consecutive_duplicates = consecutive_duplicates + 1 if duplicate_page else 0
            stall = duplicate_page and consecutive_duplicates >= MAX_CONSECUTIVE_DUPLICATE_PAGES
            fenced = False
            with transaction(self.conn):
                with self.conn.cursor() as cur:
                    cur.execute(
                        "UPDATE request_attempts SET status = 'served', http_status = %s, finished_at = now(), response_summary = %s WHERE id = %s",
                        (page.status, jsonb({"rows": len(page.rows), "quota": page.quota.to_dict(),
                                             "pii_fields_dropped": page.pii_fields_dropped}), attempt_id),
                    )
                    new_here = modified_here = expired_here = 0
                    if not duplicate_page:
                        for row in page.rows:
                            if not str(row.get("id") or "").strip():
                                continue
                            res = upsert_posting(self.conn, source=source, row=row, lane=part["lane"], now=moment)
                            if res.state == "new":
                                new_here += 1
                                run.new_posting_ids.append(res.posting_id)
                                work_queue.enqueue(self.conn, kind="resolve_identity", subject_kind="posting",
                                                   subject_id=res.posting_id, lane=part["lane"], available_at=moment)
                            elif res.state == "modified":
                                modified_here += 1
                                kind = "resolve_identity" if res.identity_changed else "classify"
                                work_queue.enqueue(self.conn, kind=kind, subject_kind="posting", subject_id=res.posting_id,
                                                   lane=part["lane"], reopen=True, available_at=moment)
                            elif res.state == "expired":
                                expired_here += 1
                    # Fenced cursor advance: only the lease holder, only from the expected offset.
                    cur.execute(
                        """
                        UPDATE source_partitions SET next_offset = next_offset + %s, pages_received = pages_received + 1,
                               rows_received = rows_received + %s, rows_new = rows_new + %s,
                               duplicate_pages = duplicate_pages + %s,
                               state = CASE WHEN %s THEN 'complete' WHEN %s THEN 'stalled' ELSE state END,
                               complete_coverage = CASE WHEN %s THEN true ELSE complete_coverage END,
                               stall_reason = CASE WHEN %s THEN 'duplicate_page_loop' ELSE NULL END,
                               last_error = NULL, lease_expires_at = %s + make_interval(secs => %s), updated_at = now()
                        WHERE id = %s AND lease_token = %s AND next_offset = %s AND state = 'open'
                        RETURNING id
                        """,
                        (advance, len(page.rows), new_here, 1 if duplicate_page else 0, complete, stall, complete, stall,
                         moment, self.lease_seconds, partition_id, token, offset),
                    )
                    fenced = cur.fetchone() is None
                    cur.execute(
                        """
                        INSERT INTO page_receipts (partition_id, attempt_id, page_offset, page_limit, row_ids, row_count,
                            fingerprint, duplicate_of_receipt_id, quota_jobs_remaining, quota_requests_remaining,
                            quota_next_billing_date, rows_compressed, fenced)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (partition_id, attempt_id, offset, self.page_limit, ids, len(ids), fp,
                         prev_receipt_id if duplicate_page else None, page.quota.jobs_remaining,
                         page.quota.requests_remaining, page.quota.next_billing_date,
                         zlib.compress(json.dumps(page.rows, default=str).encode("utf-8")), fenced),
                    )
                    cur.execute(
                        "INSERT INTO credit_events (provider, operation, attempt_id, requests, estimated_credits, confirmed_credits, basis) "
                        "VALUES ('fantastic', 'page', %s, 1, %s, %s, %s)",
                        (attempt_id, len(page.rows), len(page.rows) if page.quota.jobs_remaining is not None else None,
                         "provider_header" if page.quota.jobs_remaining is not None else "estimate"),
                    )
            provider_state.record_served(self.conn, PROVIDER, now=moment)
            run.pages += 1
            run.rows += len(page.rows)
            run.new_postings += new_here
            run.modified_postings += modified_here
            run.expired_postings += expired_here
            run.duplicate_pages += 1 if duplicate_page else 0
            run.quota = page.quota.to_dict()
            if fenced:
                run.stop_reason = "lease_lost"
                break
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
                cur.execute("UPDATE source_partitions SET state = 'stalled', stall_reason = %s, last_error = %s, updated_at = now() WHERE id = %s",
                            (error[:120], error[:300], partition_id))
