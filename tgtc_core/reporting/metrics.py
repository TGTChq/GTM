"""Every number in the weekly report, measured from the core database.

Three rules hold everywhere in this module, because a report that breaks them is
worse than no report:

1. **Nothing is added across units.** Records, jobs, units (company x campaign),
   contacts, Airtable records and Instantly creations are different things and are
   never summed into one another. Loss reasons carry their own unit and are shown
   beside the stage they belong to, never subtracted from a total.
2. **A counter with no underlying evidence is ``None`` (unknown), never 0.** A zero
   is a measured zero: it means the rows were looked for and were not there. When a
   production run is MISSING from a day, that is reported as a flag, not as a zero
   day of production.
3. **The KPI is defined exactly once**, in :data:`GENUINE_CREATION_PREDICATE`, and
   every view of it -- weekly, per campaign, per day, cumulative -- is that same
   predicate. "A genuinely net-new lead" is an Instantly ``created`` receipt whose
   campaign is the campaign the approval was routed to. An ``existing`` answer, a
   rejection, an Airtable row without a creation and one person counted twice are
   all excluded by construction.

The 200-person phone sidecar keeps its own SQLite store and never writes to these
tables, so no figure here can contain it; ``source.sidecar_included`` says so on
every report.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional

import psycopg

from ..policy.campaigns import CAMPAIGNS, KNOWN_CONTROL_CAMPAIGN_IDS
from .window import ReportWindow, iso_z

#: A GENUINE net-new lead: Instantly answered "created" for the campaign the approval
#: was routed to. Written once; used by every count of the business target.
GENUINE_CREATION_PREDICATE = (
    "r.channel = 'instantly' AND r.receipt_kind = 'created' AND r.external_campaign = a.campaign_id"
)

#: Gate outcomes that are only reachable AFTER both the employer gate and the
#: verified-work-email gate have passed. ``services/opportunity.py`` evaluates them in
#: that order -- contact gate, email gate, suppression, approval decision, already
#: approved elsewhere, pass -- so any of these is positive evidence that a person is a
#: usable contact, while a ``contact:`` or ``email:`` failure is evidence that they are
#: not. Counting search rows or enrichment attempts instead is what produced "12,922
#: contacts" for a week that found 4,251.
CLEARED_BOTH_GATES_SQL = (
    "ca.attempt_kind = 'gate' AND (ca.outcome = 'pass' "
    " OR ca.reason LIKE 'suppressed:%%' OR ca.reason LIKE 'approval_refused:%%' "
    " OR ca.reason = 'person_already_approved_elsewhere')"
)

#: An Airtable record that exists: created outright, or recovered by reconciliation
#: after an uncertain response (the same row, found again -- never a second row).
AIRTABLE_RECORD_PREDICATE = "r.channel = 'airtable' AND r.receipt_kind IN ('created', 'reconciled')"

_DELIVERY_JOIN = (
    "FROM delivery_receipts r "
    "JOIN delivery_outbox o ON o.id = r.outbox_id "
    "JOIN approvals a ON a.id = o.approval_id "
)


def _one(cur, sql: str, params: Dict[str, Any] | tuple = ()) -> Any:
    cur.execute(sql, params)
    row = cur.fetchone()
    if not row:
        return None
    return list(row.values())[0]


def _int(value: Any) -> int:
    return int(value or 0)

def _num(value: Any) -> Optional[float]:
    return None if value is None else float(value)


def _rows(cur, sql: str, params: Dict[str, Any] | tuple = ()) -> List[Dict[str, Any]]:
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def _breakdown(cur, sql: str, params, key: str = "k", value: str = "n") -> Dict[str, int]:
    return {str(r[key]): int(r[value]) for r in _rows(cur, sql, params)}


# --------------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------------

def coverage_section(cur, w: ReportWindow) -> Dict[str, Any]:
    """How much of this window the database can actually speak about.

    A day before the first record exists is UNAVAILABLE. It is not a day of zero
    production, and reporting it as zero would be a false statement about the business.
    """
    # The earliest evidence of ANY kind. A database that holds approvals and receipts
    # but no postings still covers those days -- coverage is about what can be seen,
    # not about which table happens to hold it.
    starts = [x for x in (
        _one(cur, "SELECT min(first_seen_at) FROM postings"),
        _one(cur, "SELECT min(created_at) FROM run_log"),
        _one(cur, "SELECT min(approved_at) FROM approvals"),
        _one(cur, "SELECT min(received_at) FROM delivery_receipts"),
    ) if x is not None]
    coverage_start = min(starts) if starts else None
    tz = w.start_local.tzinfo
    unavailable: List[str] = []
    if coverage_start is not None:
        first_covered = coverage_start.astimezone(tz).date()
        unavailable = [d.isoformat() for d in w.local_days() if d < first_covered]
    else:
        unavailable = [d.isoformat() for d in w.local_days()]
    return {
        "coverage_start_utc": iso_z(coverage_start) if coverage_start else None,
        "local_days_unavailable": unavailable,
        "fully_covered": not unavailable,
        "note": ("a day before the database's first record is reported as unavailable, never as zero production; "
                 "it cannot be fixed by waiting and does not hold back a report"),
    }


def runs_section(cur, w: ReportWindow, *, unavailable: Optional[List[str]] = None) -> Dict[str, Any]:
    """Which production runs the window contains, and which days had none.

    A day with no run is named. It is never reported as a day that produced zero,
    because those are different facts and only one of them is a problem to fix.
    """
    p = {"t0": w.start_utc, "t1": w.end_utc}
    runs = _rows(cur, """
        SELECT run_id,
               min(created_at) AS started_at,
               max(created_at) AS last_event_at,
               count(*) FILTER (WHERE stage = 'daily' AND event = 'end') AS ends,
               count(*) FILTER (WHERE stage = 'daily' AND event = 'refused') AS refusals,
               (array_agg(details ORDER BY created_at DESC)
                FILTER (WHERE stage = 'daily' AND event IN ('end', 'refused')))[1] AS outcome
        FROM run_log WHERE created_at >= %(t0)s AND created_at < %(t1)s
        GROUP BY run_id ORDER BY 2
        """, p)
    out_runs = []
    for r in runs:
        outcome = r["outcome"] or {}
        out_runs.append({
            "run_id": r["run_id"],
            "started_at": iso_z(r["started_at"]),
            "last_event_at": iso_z(r["last_event_at"]),
            "completed": bool(r["ends"]),
            "refused": bool(r["refusals"]),
            "stop_reason": outcome.get("stop_reason") or outcome.get("reason"),
            "budget_id": outcome.get("budget_id"),
        })
    tz = w.start_local.tzinfo
    days_with_run = {r["started_at"].astimezone(tz).date().isoformat() for r in runs}
    all_days = [d.isoformat() for d in w.local_days()]
    # The window's last local day is only partly elapsed on a partial report, and the
    # scheduled run for the report's own day has not happened when the report is built.
    # A day is EXPECTED to have a run only if it has already elapsed and the database
    # covers it at all; anything else is unavailable, not missing.
    blind = set(unavailable or [])
    expected = [d for d in all_days
                if d < w.data_cutoff.astimezone(tz).date().isoformat() and d not in blind]
    return {
        "runs": out_runs,
        "run_count": len(out_runs),
        "completed_runs": sum(1 for r in out_runs if r["completed"]),
        "refused_runs": sum(1 for r in out_runs if r["refused"]),
        "local_days_in_window": all_days,
        "local_days_with_a_run": sorted(days_with_run),
        "local_days_without_a_run": [d for d in expected if d not in days_with_run],
        "local_days_unavailable": sorted(blind),
    }


def acquisition_section(cur, w: ReportWindow) -> Dict[str, Any]:
    """Fantastic: what was requested, what came back, what was billed."""
    p = {"t0": w.start_utc, "t1": w.end_utc}
    attempts = _breakdown(cur, """
        SELECT status AS k, count(*) AS n FROM request_attempts
        WHERE provider = 'fantastic' AND started_at >= %(t0)s AND started_at < %(t1)s
        GROUP BY 1""", p)
    pages = _rows(cur, """
        SELECT count(*) AS pages,
               count(*) FILTER (WHERE pr.duplicate_of_receipt_id IS NOT NULL) AS duplicate_pages,
               COALESCE(sum(pr.row_count), 0) AS rows_returned
        FROM page_receipts pr JOIN request_attempts ra ON ra.id = pr.attempt_id
        WHERE ra.provider = 'fantastic' AND pr.received_at >= %(t0)s AND pr.received_at < %(t1)s""", p)[0]
    unique_ids = _one(cur, """
        SELECT count(DISTINCT x) FROM page_receipts pr, unnest(pr.row_ids) AS x
        WHERE pr.received_at >= %(t0)s AND pr.received_at < %(t1)s""", p)
    billed_estimate = _one(cur, """
        SELECT sum(estimated_credits) FROM credit_events
        WHERE provider = 'fantastic' AND created_at >= %(t0)s AND created_at < %(t1)s""", p)
    billed_confirmed = _one(cur, """
        SELECT sum(confirmed_credits) FROM credit_events
        WHERE provider = 'fantastic' AND basis <> 'estimate' AND created_at >= %(t0)s AND created_at < %(t1)s""", p)
    return {
        "fantastic_requests": sum(attempts.values()),
        "fantastic_requests_by_status": attempts,
        "fantastic_pages": _int(pages["pages"]),
        "fantastic_duplicate_pages": _int(pages["duplicate_pages"]),
        "fantastic_records_returned": _int(pages["rows_returned"]),
        "fantastic_records_unique_ids": _int(unique_ids),
        # Billing basis is named, never mixed: the provider's own confirmation where it
        # gave one, our reservation estimate otherwise.
        "fantastic_records_billed_confirmed": _num(billed_confirmed),
        "fantastic_records_billed_estimate": _num(billed_estimate),
    }


def jobs_section(cur, w: ReportWindow) -> Dict[str, Any]:
    """Jobs seen, reviewed, qualified, rejected and still pending review.

    *Reviewed* is a classification written in the window -- for a posting first seen
    in an earlier week too, which is why reviewed can exceed new jobs and is reported
    separately rather than as a percentage of them.
    """
    p = {"t0": w.start_utc, "t1": w.end_utc}
    new_jobs = _one(cur, "SELECT count(*) FROM postings WHERE first_seen_at >= %(t0)s AND first_seen_at < %(t1)s", p)
    dupes = _one(cur, """SELECT count(*) FROM postings WHERE first_seen_at >= %(t0)s AND first_seen_at < %(t1)s
                         AND duplicate_of_posting_id IS NOT NULL""", p)
    reviewed = _one(cur, """SELECT count(DISTINCT posting_id) FROM classifications
                            WHERE created_at >= %(t0)s AND created_at < %(t1)s""", p)
    qualified = _one(cur, """SELECT count(DISTINCT posting_id) FROM classifications
                             WHERE created_at >= %(t0)s AND created_at < %(t1)s
                               AND NOT excluded AND cardinality(compatible_functions) > 0""", p)
    excluded = _one(cur, """SELECT count(DISTINCT posting_id) FROM classifications
                            WHERE created_at >= %(t0)s AND created_at < %(t1)s AND excluded""", p)
    no_fit = _one(cur, """SELECT count(DISTINCT posting_id) FROM classifications
                          WHERE created_at >= %(t0)s AND created_at < %(t1)s
                            AND NOT excluded AND cardinality(compatible_functions) = 0""", p)
    reasons = _breakdown(cur, """
        SELECT COALESCE(exclusion_reason, 'unspecified') AS k, count(*) AS n FROM classifications
        WHERE created_at >= %(t0)s AND created_at < %(t1)s AND excluded GROUP BY 1 ORDER BY 2 DESC""", p)
    pending = _one(cur, """
        SELECT count(*) FROM postings p
        WHERE p.first_seen_at >= %(t0)s AND p.first_seen_at < %(t1)s
          AND NOT EXISTS (SELECT 1 FROM classifications c WHERE c.posting_id = p.id)""", p)
    methods = _breakdown(cur, """
        SELECT method AS k, count(*) AS n FROM classifications
        WHERE created_at >= %(t0)s AND created_at < %(t1)s GROUP BY 1""", p)
    return {
        "new_jobs_unique": _int(new_jobs),
        "new_jobs_duplicates_of_existing": _int(dupes),
        "jobs_reviewed": _int(reviewed),
        "jobs_qualified": _int(qualified),
        "jobs_rejected_excluded": _int(excluded),
        "jobs_no_campaign_fit": _int(no_fit),
        "jobs_pending_review": _int(pending),
        "rejection_reasons": reasons,
        "review_method": methods,
        "definitions": {
            "new_jobs_unique": "a posting first seen in this window (one row per source + provider job id)",
            "jobs_reviewed": "a classification written in this window, whenever the posting was first seen",
            "jobs_qualified": "reviewed, not excluded, and matched to at least one of the nine campaigns",
            "jobs_rejected_excluded": "reviewed and excluded by a named rule",
            "jobs_no_campaign_fit": "reviewed, not excluded, but matched to no campaign",
            "jobs_pending_review": "first seen in this window and still carrying no classification at the data cutoff",
        },
    }


def headline_section(cur, w: ReportWindow) -> Dict[str, Any]:
    """The four figures the weekly message opens with, and how each is counted.

    All of capture, review and qualification are measured on ONE cohort -- the jobs first
    seen inside this window -- so the review percentage has a numerator drawn from its own
    denominator. Where the cohort cannot be reconstructed (no jobs captured at all), the
    percentage is ``None`` and the message shows the two counts with their definitions
    instead of inventing a ratio.
    """
    p = {"t0": w.start_utc, "t1": w.end_utc, "cutoff": w.data_cutoff}
    captured = _int(_one(cur, """
        SELECT count(*) FROM postings
        WHERE first_seen_at >= %(t0)s AND first_seen_at < %(t1)s""", p))
    reviewed = _int(_one(cur, """
        SELECT count(*) FROM postings p WHERE p.first_seen_at >= %(t0)s AND p.first_seen_at < %(t1)s
          AND EXISTS (SELECT 1 FROM classifications c
                      WHERE c.posting_id = p.id AND c.created_at < %(cutoff)s)""", p))
    qualified_jobs = _int(_one(cur, """
        SELECT count(*) FROM postings p WHERE p.first_seen_at >= %(t0)s AND p.first_seen_at < %(t1)s
          AND EXISTS (SELECT 1 FROM classifications c
                      WHERE c.posting_id = p.id AND c.created_at < %(cutoff)s
                        AND NOT c.excluded AND cardinality(c.compatible_functions) > 0)""", p))
    units = _rows(cur, """
        SELECT count(DISTINCT (employer_id, campaign_key)) AS company_x_campaign,
               count(*) AS employer_x_function,
               count(DISTINCT employer_id) AS employers
        FROM opportunities WHERE created_at >= %(t0)s AND created_at < %(t1)s""", p)[0]
    opportunities = _int(units["company_x_campaign"])
    contacts = _rows(cur, f"""
        WITH cleared AS (
            SELECT DISTINCT ca.person_id FROM candidate_attempts ca
            WHERE ca.created_at >= %(t0)s AND ca.created_at < %(t1)s AND ca.person_id IS NOT NULL
              AND {CLEARED_BOTH_GATES_SQL})
        SELECT count(*) FILTER (WHERE p.email_status = 'verified') AS found,
               count(*) FILTER (WHERE p.email_status = 'verified'
                                AND p.email_verified_at >= %(t0)s AND p.email_verified_at < %(t1)s) AS in_window,
               count(*) FILTER (WHERE p.email_status = 'verified'
                                AND (p.email_verified_at IS NULL OR p.email_verified_at < %(t0)s)) AS reused,
               count(*) FILTER (WHERE p.email_status IS DISTINCT FROM 'verified') AS cleared_unverified
        FROM cleared c JOIN people p ON p.id = c.person_id""", p)[0]
    contacts_found = _int(contacts["found"])
    candidates_seen = _int(_one(cur, """
        SELECT count(DISTINCT candidate_ref) FROM candidate_attempts
        WHERE created_at >= %(t0)s AND created_at < %(t1)s""", p))
    enriched = _int(_one(cur, """
        SELECT count(DISTINCT person_id) FROM candidate_attempts
        WHERE attempt_kind = 'match' AND outcome IN ('served', 'reused_stored_evidence')
          AND created_at >= %(t0)s AND created_at < %(t1)s AND person_id IS NOT NULL""", p))
    provider_verified = _int(_one(cur, """
        SELECT count(*) FROM people WHERE email_status = 'verified'
          AND email_verified_at >= %(t0)s AND email_verified_at < %(t1)s""", p))
    not_usable = _breakdown(cur, """
        SELECT CASE WHEN reason LIKE 'email:%%' THEN 'work_email_not_usable'
                    ELSE 'employer_or_title_not_confirmed' END AS k,
               count(DISTINCT person_id) AS n
        FROM candidate_attempts
        WHERE attempt_kind = 'gate' AND outcome = 'fail' AND person_id IS NOT NULL
          AND (reason LIKE 'email:%%' OR reason LIKE 'contact:%%')
          AND created_at >= %(t0)s AND created_at < %(t1)s GROUP BY 1""", p)
    blocked_for_outreach = _int(_one(cur, """
        SELECT count(*) FROM approvals WHERE state <> 'revoked'
          AND outreach_eligible IS DISTINCT FROM TRUE
          AND approved_at >= %(t0)s AND approved_at < %(t1)s""", p))
    added = _rows(cur, f"""
        SELECT count(DISTINCT lower(o.payload_json->>'email')) AS people,
               count(DISTINCT lower(o.payload_json->>'email')) FILTER (WHERE a.approved_at >= %(t0)s) AS this_week
        {_DELIVERY_JOIN}
        WHERE {GENUINE_CREATION_PREDICATE} AND r.received_at >= %(t0)s AND r.received_at < %(t1)s""", p)[0]
    people, this_week = _int(added["people"]), _int(added["this_week"])
    return {
        "jobs_captured": captured,
        "jobs_reviewed": reviewed,
        "jobs_review_rate": round(100.0 * reviewed / captured, 1) if captured else None,
        "jobs_review_rate_omitted_because": (
            None if captured else "no jobs were captured in this window, so there is no cohort to review"),
        "qualified_jobs": qualified_jobs,
        "qualified_opportunities": opportunities,
        "qualified_opportunity_rows_employer_x_function": _int(units["employer_x_function"]),
        "qualified_opportunity_employers": _int(units["employers"]),
        "contacts_found": contacts_found,
        "contacts_found_verified_in_window": _int(contacts["in_window"]),
        "contacts_found_verified_earlier_and_reused": _int(contacts["reused"]),
        "contacts_cleared_but_unverified": _int(contacts["cleared_unverified"]),
        "search_candidates_seen": candidates_seen,
        "contacts_enriched": enriched,
        "emails_verified_by_provider": provider_verified,
        "verified_but_not_usable": not_usable,
        "verified_contacts_blocked_for_outreach": blocked_for_outreach,
        "added_to_instantly": people,
        "added_from_this_weeks_approvals": this_week,
        "added_from_earlier_approvals": people - this_week,
        "definitions": {
            "jobs_captured": "job postings first seen in this window (one per source + provider job id)",
            "jobs_reviewed": "those same jobs that had been classified by the data cutoff -- same cohort, "
                             "so the percentage divides a count by the count it came from",
            "qualified_jobs": "those same jobs whose classification matched one of the nine campaigns",
            "qualified_opportunities": "distinct company x campaign pairs opened in this window",
            "qualified_opportunity_rows_employer_x_function": (
                "the underlying units (employer + function); larger than the pairs when one company is open "
                "for two functions inside the same campaign"),
            "contacts_found": (
                "distinct people who cleared BOTH gates in this window: current employment at that employer "
                "confirmed, and a work email on the employer's domain verified by Apollo. It is not the number "
                "of search results seen (search_candidates_seen), not enrichment attempts (contacts_enriched) "
                "and not every address the provider verified (emails_verified_by_provider), because an address "
                "that is not on the employer's domain is not a usable contact"),
            "search_candidates_seen": "Apollo search rows considered, most of them discarded before any spend",
            "contacts_enriched": "people a paid Apollo match returned in this window",
            "emails_verified_by_provider": "addresses Apollo verified in this window, before the employer-domain rule",
            "verified_but_not_usable": "people rejected by the work-email or employer/title rule",
            "verified_contacts_blocked_for_outreach": (
                "approved contacts a compliance rule forbids sending to -- counted capacity, never a sent lead"),
            "added_to_instantly": "distinct people Instantly answered 'created' for, in the campaign the approval "
                                  "was routed to, receipt-confirmed inside this window. Excludes people Instantly "
                                  "already had, rejections, Control campaigns, anyone counted twice and the phone "
                                  "sidecar, which writes to none of these tables",
        },
    }


def units_section(cur, w: ReportWindow) -> Dict[str, Any]:
    """Employers and company x campaign units -- the unit paid contact work is spent on."""
    p = {"t0": w.start_utc, "t1": w.end_utc}
    employers = _one(cur, "SELECT count(*) FROM employers WHERE created_at >= %(t0)s AND created_at < %(t1)s", p)
    units = _one(cur, "SELECT count(*) FROM opportunities WHERE created_at >= %(t0)s AND created_at < %(t1)s", p)
    employers_touched = _one(cur, """
        SELECT count(DISTINCT op.employer_id) FROM opportunities op
        WHERE op.created_at >= %(t0)s AND op.created_at < %(t1)s""", p)
    worked = _one(cur, """
        SELECT count(DISTINCT opportunity_id) FROM candidate_attempts
        WHERE created_at >= %(t0)s AND created_at < %(t1)s""", p)
    closed = _breakdown(cur, """
        SELECT COALESCE(close_reason, 'unspecified') AS k, count(*) AS n FROM opportunities
        WHERE state = 'closed' AND updated_at >= %(t0)s AND updated_at < %(t1)s GROUP BY 1 ORDER BY 2 DESC""", p)
    return {
        "new_employers": _int(employers),
        "new_units": _int(units),
        "employers_behind_new_units": _int(employers_touched),
        "units_worked_for_contacts": _int(worked),
        "units_closed_by_reason": closed,
        "definitions": {
            "new_units": "a company x campaign unit opened in this window (unique employer + function)",
            "units_worked_for_contacts": "units on which a contact attempt was made in this window",
        },
    }


def contacts_section(cur, w: ReportWindow) -> Dict[str, Any]:
    """Contacts found, verified, approved -- and the ones held back, with the reason."""
    p = {"t0": w.start_utc, "t1": w.end_utc}
    found = _one(cur, """SELECT count(DISTINCT candidate_ref) FROM candidate_attempts
                         WHERE created_at >= %(t0)s AND created_at < %(t1)s""", p)
    matched = _one(cur, """SELECT count(*) FROM candidate_attempts
                           WHERE attempt_kind = 'match' AND outcome = 'served'
                             AND created_at >= %(t0)s AND created_at < %(t1)s""", p)
    verified = _one(cur, """SELECT count(*) FROM people WHERE email_status = 'verified'
                            AND email_verified_at >= %(t0)s AND email_verified_at < %(t1)s""", p)
    approved = _one(cur, """SELECT count(*) FROM approvals WHERE state <> 'revoked'
                            AND approved_at >= %(t0)s AND approved_at < %(t1)s""", p)
    revoked = _one(cur, """SELECT count(*) FROM approvals WHERE state = 'revoked'
                           AND updated_at >= %(t0)s AND updated_at < %(t1)s""", p)
    gate_fail = _breakdown(cur, """
        SELECT COALESCE(reason, outcome) AS k, count(*) AS n FROM candidate_attempts
        WHERE attempt_kind = 'gate' AND outcome <> 'pass'
          AND created_at >= %(t0)s AND created_at < %(t1)s GROUP BY 1 ORDER BY 2 DESC""", p)
    compliance = _breakdown(cur, """
        SELECT COALESCE(outreach_block_reason, 'compliance:outreach_eligibility_unknown') AS k, count(*) AS n
        FROM approvals WHERE state <> 'revoked' AND outreach_eligible IS DISTINCT FROM TRUE
          AND approved_at >= %(t0)s AND approved_at < %(t1)s GROUP BY 1 ORDER BY 2 DESC""", p)
    suppressed = _breakdown(cur, """
        SELECT kind || ':' || reason AS k, count(*) AS n FROM suppressions
        WHERE created_at >= %(t0)s AND created_at < %(t1)s GROUP BY 1 ORDER BY 2 DESC""", p)
    return {
        "candidates_found": _int(found),
        "contacts_enriched": _int(matched),
        "emails_verified": _int(verified),
        "contacts_approved": _int(approved),
        "approvals_revoked": _int(revoked),
        "contacts_rejected_by_gate": gate_fail,
        "compliance_blocked_by_reason": compliance,
        "compliance_blocked": sum(compliance.values()),
        "suppressions_added": suppressed,
        "definitions": {
            "contacts_approved": "an approval written in this window and not revoked; one active approval per person",
            "compliance_blocked": "approved, counted as capacity, but not eligible for outreach -- never merged into the sendable figure",
        },
    }


def delivery_section(cur, w: ReportWindow) -> Dict[str, Any]:
    """The business target and everything Instantly and Airtable answered.

    ``instantly_created_unique_people`` is THE number: unique net-new people created
    in the campaign they were routed to. Everything else on this section exists so a
    reader can see what the difference between the channels is made of.
    """
    p = {"t0": w.start_utc, "t1": w.end_utc}
    created = _rows(cur, f"""
        SELECT count(DISTINCT lower(o.payload_json->>'email')) AS people,
               count(DISTINCT a.id) AS approvals
        {_DELIVERY_JOIN}
        WHERE {GENUINE_CREATION_PREDICATE} AND r.received_at >= %(t0)s AND r.received_at < %(t1)s""", p)[0]
    existing = _one(cur, f"""
        SELECT count(DISTINCT a.id) {_DELIVERY_JOIN}
        WHERE r.channel = 'instantly' AND r.receipt_kind = 'existing'
          AND r.received_at >= %(t0)s AND r.received_at < %(t1)s""", p)
    rejected = _breakdown(cur, f"""
        SELECT COALESCE(r.response_summary->>'membership', r.response_summary->>'reason', 'unspecified') AS k,
               count(DISTINCT a.id) AS n {_DELIVERY_JOIN}
        WHERE r.channel = 'instantly' AND r.receipt_kind = 'rejected'
          AND r.received_at >= %(t0)s AND r.received_at < %(t1)s GROUP BY 1 ORDER BY 2 DESC""", p)
    control = _one(cur, f"""
        SELECT count(*) {_DELIVERY_JOIN}
        WHERE r.channel = 'instantly' AND r.receipt_kind IN ('created', 'reconciled')
          AND r.external_campaign = ANY(%(control)s)
          AND r.received_at >= %(t0)s AND r.received_at < %(t1)s""",
        {**p, "control": sorted(KNOWN_CONTROL_CAMPAIGN_IDS)})
    reconciled = _one(cur, f"""
        SELECT count(DISTINCT a.id) {_DELIVERY_JOIN}
        WHERE r.channel = 'instantly' AND r.receipt_kind = 'reconciled'
          AND r.received_at >= %(t0)s AND r.received_at < %(t1)s""", p)
    air = _rows(cur, f"""
        SELECT count(DISTINCT a.id) AS records,
               count(*) FILTER (WHERE r.receipt_kind = 'reconciled') AS recovered
        {_DELIVERY_JOIN}
        WHERE {AIRTABLE_RECORD_PREDICATE} AND r.received_at >= %(t0)s AND r.received_at < %(t1)s""", p)[0]
    air_existing = _one(cur, f"""
        SELECT count(DISTINCT a.id) {_DELIVERY_JOIN}
        WHERE r.channel = 'airtable' AND r.receipt_kind = 'existing'
          AND r.received_at >= %(t0)s AND r.received_at < %(t1)s""", p)
    blocked = _breakdown(cur, """
        SELECT o.channel || ':' || COALESCE(o.blocked_reason, 'unspecified') AS k, count(*) AS n
        FROM delivery_outbox o WHERE o.state = 'blocked'
          AND o.updated_at >= %(t0)s AND o.updated_at < %(t1)s GROUP BY 1 ORDER BY 2 DESC""", p)
    failed = _breakdown(cur, """
        SELECT o.channel AS k, count(*) AS n FROM delivery_outbox o
        WHERE o.state = 'failed' AND o.updated_at >= %(t0)s AND o.updated_at < %(t1)s GROUP BY 1""", p)
    return {
        "instantly_created_unique_people": _int(created["people"]),
        "instantly_created_approvals": _int(created["approvals"]),
        "instantly_recovered_by_reconciliation": _int(reconciled),
        "instantly_already_existing": _int(existing),
        "instantly_rejected_by_reason": rejected,
        "instantly_rejected": sum(rejected.values()),
        "instantly_creations_into_a_control_campaign": _int(control),
        "airtable_records_created": _int(air["records"]),
        "airtable_records_recovered_by_reconciliation": _int(air["recovered"]),
        "airtable_already_existing": _int(air_existing),
        "delivery_blocked_by_reason": blocked,
        "delivery_failed_by_channel": failed,
        "definitions": {
            "instantly_created_unique_people": (
                "THE business target: distinct people Instantly answered 'created' for, in the campaign the "
                "approval was routed to. Excludes existing, reconciled-only, rejected, Control and any person "
                "counted twice"),
            "airtable_records_created": "distinct approvals that became an Airtable record in this window",
        },
    }


def backlog_section(cur, w: ReportWindow) -> Dict[str, Any]:
    """This week's own production, separated from work carried in and carried out."""
    p = {"t0": w.start_utc, "t1": w.end_utc}
    carry_in = _one(cur, f"""
        SELECT count(DISTINCT a.id) {_DELIVERY_JOIN}
        WHERE {GENUINE_CREATION_PREDICATE} AND r.received_at >= %(t0)s AND r.received_at < %(t1)s
          AND a.approved_at < %(t0)s""", p)
    from_this_week = _one(cur, f"""
        SELECT count(DISTINCT a.id) {_DELIVERY_JOIN}
        WHERE {GENUINE_CREATION_PREDICATE} AND r.received_at >= %(t0)s AND r.received_at < %(t1)s
          AND a.approved_at >= %(t0)s""", p)
    # An approval is "waiting" only while nothing has decided it. One the compliance or
    # quality gate BLOCKED with a named reason is decided -- it is reported beside this
    # figure, never inside it, so the waiting count stays a number worth acting on.
    answered = ("EXISTS (SELECT 1 FROM delivery_outbox o JOIN delivery_receipts r ON r.outbox_id = o.id "
                "        WHERE o.approval_id = a.id AND r.channel = 'instantly' "
                "          AND r.receipt_kind IN ('created', 'existing', 'reconciled', 'rejected'))")
    blocked = ("EXISTS (SELECT 1 FROM delivery_outbox o WHERE o.approval_id = a.id "
               "        AND o.channel = 'instantly' AND o.state = 'blocked')")
    carry_out = _one(cur, f"""
        SELECT count(*) FROM approvals a
        WHERE a.state <> 'revoked' AND a.approved_at >= %(t0)s AND a.approved_at < %(t1)s
          AND a.outreach_eligible IS TRUE AND NOT {answered} AND NOT {blocked}""", p)
    held = _one(cur, f"""
        SELECT count(*) FROM approvals a
        WHERE a.state <> 'revoked' AND a.approved_at >= %(t0)s AND a.approved_at < %(t1)s
          AND NOT {answered} AND ({blocked} OR a.outreach_eligible IS DISTINCT FROM TRUE)""", p)
    pending = _breakdown(cur, """
        SELECT channel || ':' || state AS k, count(*) AS n FROM delivery_outbox
        WHERE state IN ('pending', 'claimed', 'in_flight', 'failed') GROUP BY 1 ORDER BY 2 DESC""", {})
    return {
        "created_from_this_weeks_approvals": _int(from_this_week),
        "created_from_earlier_approvals_backlog": _int(carry_in),
        "approved_this_week_not_yet_delivered": _int(carry_out),
        "approved_this_week_held_with_a_named_reason": _int(held),
        "delivery_queue_at_cutoff": pending,
        "definitions": {
            "created_from_earlier_approvals_backlog": "leads created this week from approvals made before the window opened",
            "approved_this_week_not_yet_delivered": ("eligible approvals nothing has decided yet -- a compliance or "
                                                     "quality block is a decision and is counted separately"),
            "approved_this_week_held_with_a_named_reason": "approved capacity a gate stopped; never waiting, never sendable",
            "delivery_queue_at_cutoff": "a point-in-time queue depth at the data cutoff, not a count of the window",
        },
    }


def spend_section(cur, w: ReportWindow, delivery: Dict[str, Any], unit_prices: Dict[str, float]) -> Dict[str, Any]:
    """Provider spend, and cost per final lead only where a unit price is recorded.

    An unverified price is reported as ``None`` with the reason, never as a guessed
    dollar figure: the account's Fantastic add-on price has not been confirmed from
    the contract or the provider, so this report does not invent one.
    """
    p = {"t0": w.start_utc, "t1": w.end_utc}
    rows = _rows(cur, """
        SELECT provider,
               count(*) AS events,
               COALESCE(sum(requests), 0) AS requests,
               sum(estimated_credits) AS estimated,
               sum(confirmed_credits) FILTER (WHERE basis <> 'estimate') AS confirmed
        FROM credit_events WHERE created_at >= %(t0)s AND created_at < %(t1)s
        GROUP BY 1 ORDER BY 1""", p)
    leads = int(delivery["instantly_created_unique_people"])
    providers: Dict[str, Any] = {}
    exact_usd: List[float] = []      # never rounded before it is divided by the lead count
    for r in rows:
        credits = _num(r["confirmed"]) if r["confirmed"] is not None else _num(r["estimated"])
        price = unit_prices.get(r["provider"])
        if price is not None and credits is not None:
            exact_usd.append(credits * price)
        providers[r["provider"]] = {
            "requests": _int(r["requests"]),
            "credits_confirmed": _num(r["confirmed"]),
            "credits_estimated": _num(r["estimated"]),
            "credits_per_final_lead": round(credits / leads, 3) if credits is not None and leads else None,
            "unit_price_usd": price,
            "spend_usd": round(credits * price, 2) if price is not None and credits is not None else None,
            "unit_price_basis": "configured and verified" if price is not None else "NOT VERIFIED -- no dollar figure is reported",
        }
    every_price_known = bool(providers) and all(v["unit_price_usd"] is not None for v in providers.values())
    return {
        "providers": providers,
        "final_leads": leads,
        "total_spend_usd": round(sum(exact_usd), 2) if every_price_known else None,
        "cost_per_final_lead_usd": (round(sum(exact_usd) / leads, 4) if every_price_known and leads else None),
        "note": ("cost per lead in dollars is reported only when every provider's unit price is recorded; "
                 "otherwise credits per lead is the measured figure"),
    }


def by_campaign_section(cur, w: ReportWindow) -> Dict[str, Any]:
    """The same measurements, campaign by campaign, for all nine -- a campaign with no
    production appears with its measured zero rather than being left out."""
    p = {"t0": w.start_utc, "t1": w.end_utc}
    approved = _breakdown(cur, """
        SELECT campaign_key AS k, count(*) AS n FROM approvals
        WHERE state <> 'revoked' AND approved_at >= %(t0)s AND approved_at < %(t1)s GROUP BY 1""", p)
    units = _breakdown(cur, """
        SELECT campaign_key AS k, count(*) AS n FROM opportunities
        WHERE created_at >= %(t0)s AND created_at < %(t1)s GROUP BY 1""", p)
    created = _breakdown(cur, f"""
        SELECT a.campaign_key AS k, count(DISTINCT lower(o.payload_json->>'email')) AS n {_DELIVERY_JOIN}
        WHERE {GENUINE_CREATION_PREDICATE} AND r.received_at >= %(t0)s AND r.received_at < %(t1)s
        GROUP BY 1""", p)
    existing = _breakdown(cur, f"""
        SELECT a.campaign_key AS k, count(DISTINCT a.id) AS n {_DELIVERY_JOIN}
        WHERE r.channel = 'instantly' AND r.receipt_kind IN ('existing', 'rejected')
          AND r.received_at >= %(t0)s AND r.received_at < %(t1)s GROUP BY 1""", p)
    airtable = _breakdown(cur, f"""
        SELECT a.campaign_key AS k, count(DISTINCT a.id) AS n {_DELIVERY_JOIN}
        WHERE {AIRTABLE_RECORD_PREDICATE} AND r.received_at >= %(t0)s AND r.received_at < %(t1)s GROUP BY 1""", p)
    blocked = _breakdown(cur, """
        SELECT campaign_key AS k, count(*) AS n FROM approvals
        WHERE state <> 'revoked' AND outreach_eligible IS DISTINCT FROM TRUE
          AND approved_at >= %(t0)s AND approved_at < %(t1)s GROUP BY 1""", p)
    employers = _breakdown(cur, """
        SELECT campaign_key AS k, count(DISTINCT employer_id) AS n FROM opportunities
        WHERE created_at >= %(t0)s AND created_at < %(t1)s GROUP BY 1""", p)
    out: Dict[str, Any] = {}
    for campaign in CAMPAIGNS:
        key = campaign.key
        out[key] = {
            "name": campaign.name,
            "new_units": units.get(key, 0),
            "employers": employers.get(key, 0),
            "contacts_approved": approved.get(key, 0),
            "compliance_blocked": blocked.get(key, 0),
            "instantly_created_unique_people": created.get(key, 0),
            "instantly_existing_or_rejected": existing.get(key, 0),
            "airtable_records_created": airtable.get(key, 0),
        }
    # A campaign key that is not one of the nine would mean routing went somewhere it
    # should not; it is surfaced rather than quietly dropped by the loop above.
    outside = sorted((set(approved) | set(created) | set(units)) - set(out))
    return {"campaigns": out, "campaign_keys_outside_the_nine": outside}


def _local_day_bounds(w: ReportWindow):
    """Each local day in the window as explicit UTC bounds, clamped to the window."""
    tz = w.start_local.tzinfo
    out = []
    for day in w.local_days():
        opens = datetime.combine(day, time(0, 0), tzinfo=tz).astimezone(timezone.utc)
        closes = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=tz).astimezone(timezone.utc)
        out.append((day, max(opens, w.start_utc), min(closes, w.end_utc)))
    return out


def _by_day(cur, bounds, scalar_sql: str) -> Dict[str, Any]:
    """Run one correlated count per local day. ``scalar_sql`` reads ``days.t0``/``days.t1``."""
    values = ", ".join(["(%s::date, %s::timestamptz, %s::timestamptz)"] * len(bounds))
    params: List[Any] = [value for row in bounds for value in row]
    cur.execute(f"WITH days(d, t0, t1) AS (VALUES {values}) "
                f"SELECT days.d::text AS k, ({scalar_sql}) AS n FROM days", params)
    return {str(r["k"]): r["n"] for r in cur.fetchall()}


def daily_section(cur, w: ReportWindow, *, unavailable: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Day by day in the report's own timezone, so a trend line is not silently drawn
    on UTC days that straddle two Pacific ones. A day the database does not cover
    carries nulls, never zeros."""
    blind = set(unavailable or [])
    bounds = _local_day_bounds(w)
    created = _by_day(cur, bounds, f"""
        SELECT count(DISTINCT lower(o.payload_json->>'email')) {_DELIVERY_JOIN}
        WHERE {GENUINE_CREATION_PREDICATE} AND r.received_at >= days.t0 AND r.received_at < days.t1""")
    approvals = _by_day(cur, bounds, """
        SELECT count(*) FROM approvals a WHERE a.state <> 'revoked'
          AND a.approved_at >= days.t0 AND a.approved_at < days.t1""")
    jobs = _by_day(cur, bounds, """
        SELECT count(*) FROM postings p WHERE p.first_seen_at >= days.t0 AND p.first_seen_at < days.t1""")
    apollo = _by_day(cur, bounds, """
        SELECT COALESCE(sum(COALESCE(ce.confirmed_credits, ce.estimated_credits)), 0) FROM credit_events ce
        WHERE ce.provider = 'apollo' AND ce.created_at >= days.t0 AND ce.created_at < days.t1""")
    fantastic = _by_day(cur, bounds, """
        SELECT COALESCE(sum(COALESCE(ce.confirmed_credits, ce.estimated_credits)), 0) FROM credit_events ce
        WHERE ce.provider = 'fantastic' AND ce.created_at >= days.t0 AND ce.created_at < days.t1""")
    out: List[Dict[str, Any]] = []
    for day, _, _ in bounds:
        key = day.isoformat()
        if key in blind:
            out.append({"date": key, "weekday": day.strftime("%a"), "unavailable": True,
                        "instantly_created_unique_people": None, "contacts_approved": None,
                        "new_jobs": None, "apollo_credits": None, "fantastic_records": None})
            continue
        out.append({
            "date": key,
            "weekday": day.strftime("%a"),
            "unavailable": False,
            "instantly_created_unique_people": _int(created.get(key)),
            "contacts_approved": _int(approvals.get(key)),
            "new_jobs": _int(jobs.get(key)),
            "apollo_credits": _num(apollo.get(key)) or 0.0,
            "fantastic_records": _num(fantastic.get(key)) or 0.0,
        })
    return out


def cumulative_section(cur, cutoff: datetime) -> Dict[str, Any]:
    """Since launch to the data cutoff. Clearly a DIFFERENT view from the week: it
    answers "how much exists", not "how much did this week produce"."""
    p = {"t1": cutoff}
    first = _one(cur, "SELECT min(first_seen_at) FROM postings")
    created = _rows(cur, f"""
        SELECT count(DISTINCT lower(o.payload_json->>'email')) AS people, count(DISTINCT a.id) AS approvals
        {_DELIVERY_JOIN} WHERE {GENUINE_CREATION_PREDICATE} AND r.received_at < %(t1)s""", p)[0]
    airtable = _one(cur, f"""
        SELECT count(DISTINCT a.id) {_DELIVERY_JOIN}
        WHERE {AIRTABLE_RECORD_PREDICATE} AND r.received_at < %(t1)s""", p)
    return {
        "since": iso_z(first) if first else None,
        "jobs_seen": _int(_one(cur, "SELECT count(*) FROM postings WHERE first_seen_at < %(t1)s", p)),
        "employers": _int(_one(cur, "SELECT count(*) FROM employers WHERE created_at < %(t1)s", p)),
        "units": _int(_one(cur, "SELECT count(*) FROM opportunities WHERE created_at < %(t1)s", p)),
        "contacts_approved": _int(_one(cur, """SELECT count(*) FROM approvals WHERE state <> 'revoked'
                                               AND approved_at < %(t1)s""", p)),
        "instantly_created_unique_people": _int(created["people"]),
        "instantly_created_approvals": _int(created["approvals"]),
        "airtable_records": _int(airtable),
        "apollo_credits": _num(_one(cur, """SELECT sum(COALESCE(confirmed_credits, estimated_credits))
                                            FROM credit_events WHERE provider = 'apollo' AND created_at < %(t1)s""", p)),
        "fantastic_records": _num(_one(cur, """SELECT sum(COALESCE(confirmed_credits, estimated_credits))
                                               FROM credit_events WHERE provider = 'fantastic' AND created_at < %(t1)s""", p)),
        "definition": "every measurement in this section is since launch up to the data cutoff, not this week",
    }


def legacy_airtable_review_section(cur, cutoff: datetime) -> Dict[str, Any]:
    """Airtable records that exist WITHOUT a genuine Instantly creation behind them.

    A REVIEW population, reported separately and never counted as delivered leads,
    never archived and never removed by this report. Recomputed from the database on
    every run, so the number is current rather than quoted from an old audit.
    """
    p = {"t1": cutoff}
    rows = _rows(cur, """
        SELECT COALESCE(ins.reason, 'no_instantly_row') AS k, count(*) AS n,
               min(air.t)::text AS first_seen, max(air.t)::text AS last_seen
        FROM approvals a
        JOIN LATERAL (SELECT min(r.received_at) AS t FROM delivery_outbox o
                      JOIN delivery_receipts r ON r.outbox_id = o.id
                      WHERE o.approval_id = a.id AND o.channel = 'airtable'
                        AND r.receipt_kind IN ('created', 'reconciled') AND r.received_at < %(t1)s) air ON air.t IS NOT NULL
        LEFT JOIN LATERAL (SELECT COALESCE(o.blocked_reason,
                                  (SELECT string_agg(DISTINCT r.receipt_kind, '+') FROM delivery_receipts r
                                   WHERE r.outbox_id = o.id AND r.receipt_kind <> 'attempted'),
                                  o.state) AS reason
                           FROM delivery_outbox o WHERE o.approval_id = a.id AND o.channel = 'instantly' LIMIT 1) ins ON true
        WHERE NOT EXISTS (SELECT 1 FROM delivery_outbox o JOIN delivery_receipts r ON r.outbox_id = o.id
                          WHERE o.approval_id = a.id AND o.channel = 'instantly'
                            AND r.receipt_kind IN ('created', 'reconciled'))
        GROUP BY 1 ORDER BY 2 DESC""", p)
    return {
        "records": sum(int(r["n"]) for r in rows),
        "by_reason": {str(r["k"]): int(r["n"]) for r in rows},
        "first_seen": rows[0]["first_seen"] if rows else None,
        "handling": ("separate review population: not counted as delivered leads, not archived and not removed "
                     "by this report; a person decides what happens to them"),
    }


def reconciliation_section(cur, w: ReportWindow, delivery: Dict[str, Any]) -> Dict[str, Any]:
    """Airtable records = genuine creations + records written without one.

    The identity is checked, not asserted: when it does not close, the report says so
    and names the run rather than presenting a tidy total.
    """
    p = {"t0": w.start_utc, "t1": w.end_utc}
    per_run = _rows(cur, f"""
        SELECT a.run_id,
               count(DISTINCT a.id) FILTER (WHERE {GENUINE_CREATION_PREDICATE}) AS instantly_created,
               count(DISTINCT a.id) FILTER (WHERE {AIRTABLE_RECORD_PREDICATE}) AS airtable_records
        {_DELIVERY_JOIN}
        WHERE r.received_at >= %(t0)s AND r.received_at < %(t1)s
        GROUP BY 1 ORDER BY 1""", p)
    without = _breakdown(cur, """
        SELECT COALESCE(ins.reason, 'no_instantly_row') AS k, count(DISTINCT a.id) AS n
        FROM approvals a
        JOIN delivery_outbox o ON o.approval_id = a.id AND o.channel = 'airtable'
        JOIN delivery_receipts r ON r.outbox_id = o.id AND r.receipt_kind IN ('created', 'reconciled')
        LEFT JOIN LATERAL (SELECT COALESCE(o2.blocked_reason,
                                  (SELECT string_agg(DISTINCT r2.receipt_kind, '+') FROM delivery_receipts r2
                                   WHERE r2.outbox_id = o2.id AND r2.receipt_kind <> 'attempted')) AS reason
                           FROM delivery_outbox o2 WHERE o2.approval_id = a.id AND o2.channel = 'instantly' LIMIT 1) ins ON true
        WHERE r.received_at >= %(t0)s AND r.received_at < %(t1)s
          AND NOT EXISTS (SELECT 1 FROM delivery_outbox o3 JOIN delivery_receipts r3 ON r3.outbox_id = o3.id
                          WHERE o3.approval_id = a.id AND o3.channel = 'instantly'
                            AND r3.receipt_kind IN ('created', 'reconciled'))
        GROUP BY 1 ORDER BY 2 DESC""", p)
    airtable = int(delivery["airtable_records_created"])
    genuine = int(delivery["instantly_created_approvals"])
    unexplained = airtable - genuine - sum(without.values())
    return {
        "airtable_records_created": airtable,
        "genuine_instantly_creations_approvals": genuine,
        "airtable_records_without_a_genuine_creation": sum(without.values()),
        "airtable_records_without_a_creation_by_reason": without,
        "unexplained_difference": unexplained,
        "identity_holds": unexplained == 0,
        "per_run": [{
            "run_id": r["run_id"],
            "instantly_created": int(r["instantly_created"]),
            "airtable_records": int(r["airtable_records"]),
            "difference": int(r["airtable_records"]) - int(r["instantly_created"]),
        } for r in per_run],
    }


# --------------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------------

def build_report(conn: psycopg.Connection, window: ReportWindow, *,
                 previous: Optional[Dict[str, Any]] = None,
                 unit_prices: Optional[Dict[str, float]] = None,
                 target_per_run: int = 1000) -> Dict[str, Any]:
    """Measure one window. Read-only: this function writes nothing."""
    prices = dict(unit_prices or {})
    with conn.cursor() as cur:
        coverage = coverage_section(cur, window)
        headline = headline_section(cur, window)
        runs = runs_section(cur, window, unavailable=coverage["local_days_unavailable"])
        acquisition = acquisition_section(cur, window)
        jobs = jobs_section(cur, window)
        units = units_section(cur, window)
        contacts = contacts_section(cur, window)
        delivery = delivery_section(cur, window)
        backlog = backlog_section(cur, window)
        spend = spend_section(cur, window, delivery, prices)
        campaigns = by_campaign_section(cur, window)
        daily = daily_section(cur, window, unavailable=coverage['local_days_unavailable'])
        cumulative = cumulative_section(cur, window.data_cutoff)
        legacy = legacy_airtable_review_section(cur, window.data_cutoff)
        reconciliation = reconciliation_section(cur, window, delivery)
    conn.rollback()  # a report never holds a transaction open and never writes here
    report: Dict[str, Any] = {
        "window": window.to_dict(),
        "generated_at": iso_z(datetime.now(timezone.utc)),
        "source": {
            "database": "core (Postgres Core)",
            "basis": "provider and delivery receipts recorded by the production runs themselves",
            "sidecar_included": False,
            "sidecar_note": ("the 200-person phone sidecar keeps a separate store and writes to none of these "
                             "tables, so no figure above contains it"),
        },
        "headline": headline,
        "coverage": coverage,
        "runs": runs,
        "acquisition": acquisition,
        "jobs": jobs,
        "units": units,
        "contacts": contacts,
        "delivery": delivery,
        "backlog": backlog,
        "spend": spend,
        "by_campaign": campaigns,
        "daily": daily,
        "cumulative": cumulative,
        "legacy_airtable_review": legacy,
        "reconciliation": reconciliation,
    }
    if previous is not None:
        report["previous_week"] = _comparison(report, previous)
    graded = graded_flags(report, target_per_run=target_per_run)
    report["integrity_alerts"] = [f["message"] for f in graded if f["level"] == "integrity"]
    report["alerts"] = [f["message"] for f in graded if f["level"] in ("integrity", "alert")]
    report["notes"] = [f["message"] for f in graded if f["level"] == "note"]
    report["flags"] = report["alerts"] + report["notes"]
    report["status"] = ("integrity" if report["integrity_alerts"]
                        else "attention" if report["alerts"] else "ok")
    return report


_COMPARED = (
    ("delivery", "instantly_created_unique_people"),
    ("contacts", "contacts_approved"),
    ("jobs", "new_jobs_unique"),
    ("units", "new_units"),
    ("acquisition", "fantastic_records_returned"),
    ("delivery", "airtable_records_created"),
)


def _comparison(current: Dict[str, Any], previous: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"window": previous.get("window", {}).get("window_label"), "metrics": {}}
    for section, key in _COMPARED:
        now = current.get(section, {}).get(key)
        before = previous.get(section, {}).get(key)
        change = None if now is None or before is None else now - before
        out["metrics"][key] = {"this_week": now, "previous_week": before, "change": change}
    return out


def graded_flags(report: Dict[str, Any], *, target_per_run: int = 1000) -> List[Dict[str, str]]:
    """Everything a reader must not miss, named and graded.

    ``alert``: production or data integrity needs a person. ``note``: true and worth
    printing, but not a reason to wake anybody. An empty list means every check passed,
    not that no check ran.
    """
    flags: List[Dict[str, str]] = []

    def integrity(message: str) -> None:
        flags.append({"level": "integrity", "message": message})

    def alert(message: str) -> None:
        flags.append({"level": "alert", "message": message})

    def note(message: str) -> None:
        flags.append({"level": "note", "message": message})

    runs = report["runs"]
    for day in runs["local_days_without_a_run"]:
        # An ALERT, not an integrity failure. A day whose run never happened is a fact
        # about the past that waiting cannot change and that a correct, reconciled report
        # should carry rather than be withheld for -- and a job that exits red every
        # twenty minutes for a gap nobody can now fix teaches people to ignore red.
        alert(f"no production run recorded on {day} -- this is a missing run, not a zero-production day")
    if runs["refused_runs"]:
        integrity(f"{runs['refused_runs']} run(s) were refused (budget policy) inside this window")
    recon = report["reconciliation"]
    if not recon["identity_holds"]:
        integrity(f"reconciliation does not close: {recon['unexplained_difference']} Airtable record(s) "
                  "are explained by neither a genuine creation nor a named reason")
    if recon["airtable_records_without_a_genuine_creation"]:
        alert(f"{recon['airtable_records_without_a_genuine_creation']} Airtable record(s) written IN THIS WINDOW "
              "have no genuine Instantly creation behind them (each one carries a named reason)")
    if report["delivery"]["instantly_creations_into_a_control_campaign"]:
        integrity("a lead was created in a CONTROL campaign; Challenger-only routing must be checked")
    if report["backlog"]["approved_this_week_not_yet_delivered"]:
        alert(f"{report['backlog']['approved_this_week_not_yet_delivered']} approval(s) from this window "
              "have no Instantly answer yet")
    if report["window"]["kind"] == "weekly":
        for day in report["daily"]:
            created_that_day = day["instantly_created_unique_people"]
            if created_that_day is not None and 0 < created_that_day < target_per_run:
                alert(f"{day['date']} produced {created_that_day} net-new leads, "
                      f"below the {target_per_run} minimum")
    blind = report.get("coverage", {}).get("local_days_unavailable") or []
    if blind:
        note(f"no data is available for {', '.join(blind)} (before the database's first record): "
             "reported as unavailable, not as zero production")
    if report["spend"]["cost_per_final_lead_usd"] is None:
        note("cost per lead in dollars is not reported: at least one provider unit price is not verified")
    if report["window"]["timezone_source"] != "zoneinfo:tzdata":
        note(f"the reporting timezone came from {report['window']['timezone_source']}, not the IANA database")
    return flags


def flags_for(report: Dict[str, Any], *, target_per_run: int = 1000) -> List[str]:
    """Every flag's message, most serious first."""
    graded = graded_flags(report, target_per_run=target_per_run)
    order = {"integrity": 0, "alert": 1, "note": 2}
    return [f["message"] for f in sorted(graded, key=lambda f: order.get(f["level"], 9))]
