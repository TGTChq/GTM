"""When the report may be sent, and whether the week has actually closed.

The agreed rule, 2026-09-23: **Friday 06:00 America/Los_Angeles.** If the runs and
receipts belonging to the window are still closing, retry until **07:00 local**. If it
is still not closed at 07:00, post a clearly labelled status notice instead -- never
partial numbers presented as the final report -- and send the real report when the data
does close.

Two separate questions are kept separate on purpose:

* :func:`delivery_state` -- what the CLOCK allows, evaluated in the report's own
  timezone so 06:00 local stays 06:00 across a daylight-saving change. A cron in UTC
  cannot do that: 06:00 Pacific is 13:00 UTC in PDT and 14:00 UTC in PST.
* :func:`readiness` -- whether the DATA has closed, which is a fact about production,
  not about the hour.

A missing run is not the same as work in flight. Waiting cannot fix a run that never
happened, so it raises an alert and the report still goes out; work that is still
running or still undelivered holds the report back until the retry window ends.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List

import psycopg

from ..services import instantly_capacity
from ..services.delivery import AWAITING_INSTANTLY
from . import incidents
from .window import FRIDAY, PACIFIC_TZ_NAME, ReportWindow, resolve_timezone

#: The delivery moment and the end of the automatic-retry window, in local time.
DUE_HOUR = 6
RETRY_UNTIL_HOUR = 7

BEFORE_DUE = "before_due"
IN_RETRY_WINDOW = "in_retry_window"
AFTER_RETRY_WINDOW = "after_retry_window"
NOT_DELIVERY_DAY = "not_delivery_day"


def delivery_state(now: datetime, *, weekday: int = FRIDAY, due_hour: int = DUE_HOUR,
                   retry_until_hour: int = RETRY_UNTIL_HOUR, tz_name: str = PACIFIC_TZ_NAME) -> str:
    """Where ``now`` sits relative to this week's delivery moment, in LOCAL time."""
    tz, _ = resolve_timezone(tz_name)
    local = now.astimezone(tz)
    if local.weekday() != weekday:
        return NOT_DELIVERY_DAY
    if local.hour < due_hour:
        return BEFORE_DUE
    if local.hour < retry_until_hour:
        return IN_RETRY_WINDOW
    return AFTER_RETRY_WINDOW


#: What a single tick may do.
ACTION_FINAL = "final"
ACTION_NOTICE = "status_notice"
ACTION_SKIP = "skip"


def decide(state: str, ready: bool) -> tuple:
    """What this tick does, given the clock and whether the data has closed.

    Written as one pure function so the rule can be read in five lines and tested
    without a database, a clock or a network:

    * before the delivery moment, or on any other day: do nothing;
    * the data has closed: publish the report (once per channel);
    * still closing, inside the retry window: wait for the next tick;
    * still closing after the retry window: post the labelled status notice (once).
    """
    if state in (NOT_DELIVERY_DAY, BEFORE_DUE):
        return ACTION_SKIP, state
    if ready:
        return ACTION_FINAL, "data_closed"
    if state == IN_RETRY_WINDOW:
        return ACTION_SKIP, "waiting_for_the_data_to_close"
    return ACTION_NOTICE, "data_did_not_close_within_the_retry_window"


#: Waiting states whose meaning is already settled: the week's figures are correct
#: without them, and nothing that is still running will change them today.
NAMED_WAITING_STATES = [instantly_capacity.DEFERRED_REASON, AWAITING_INSTANTLY]


@dataclass(frozen=True)
class Readiness:
    """Has everything belonging to this window finished closing?"""

    ready: bool
    blockers: List[str]
    detail: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"ready": self.ready, "blockers": self.blockers, **self.detail}


def readiness(conn: psycopg.Connection, window: ReportWindow, report: Dict[str, Any]) -> Readiness:
    """Whether the week's production has closed.

    Blocking (the data may still change): a run inside the window that never logged an
    end, delivery rows still queued or in flight for approvals in the window, a
    production run holding the run lock right now, or a reconciliation difference that
    nothing explains.

    Not blocking (waiting will not change them): a day that had no run at all, days
    before the database's coverage begins, the historical Airtable records that already
    carry a named reason, and a contact waiting because the DESTINATION is full. That
    last one is named, not hidden: the week's figures are already correct without it
    (nothing counts it as created), and a full Instantly workspace can stay full for
    days. Holding Friday's report hostage to it would make the report less true, not
    more.
    """
    blockers: List[str] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(DISTINCT run_id) AS n FROM run_log l
            WHERE l.stage = 'daily' AND l.event = 'start'
              AND l.created_at >= %(t0)s AND l.created_at < %(t1)s
              AND NOT EXISTS (SELECT 1 FROM run_log e WHERE e.run_id = l.run_id
                              AND e.stage = 'daily' AND e.event IN ('end', 'refused'))
            """, {"t0": window.start_utc, "t1": window.end_utc})
        open_runs = int(cur.fetchone()["n"])
        interrupted = incidents.settled(cur, window_start=window.start_utc, window_end=window.end_utc)
        cur.execute(
            """
            SELECT
              count(*) FILTER (WHERE COALESCE(o.last_error, '') <> ALL(%(named)s)) AS queued,
              count(*) FILTER (WHERE COALESCE(o.last_error, '') = ANY(%(named)s)) AS waiting_capacity
            FROM delivery_outbox o JOIN approvals a ON a.id = o.approval_id
            WHERE o.state IN ('pending', 'claimed', 'in_flight')
              AND a.approved_at >= %(t0)s AND a.approved_at < %(t1)s
            """, {"t0": window.start_utc, "t1": window.end_utc, "named": NAMED_WAITING_STATES})
        row = cur.fetchone()
        queued, waiting_capacity = int(row["queued"]), int(row["waiting_capacity"])
        cur.execute("SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND objid = %s AND granted",
                    (0x74677463,))
        run_in_progress = int(cur.fetchone()["n"])
    conn.rollback()
    unexplained = int(report["reconciliation"]["unexplained_difference"])

    # An interrupted run stops blocking ONLY when every proof holds: its own (a later run
    # took the exclusive lock and completed, and it has been silent since the incident was
    # filed -- checked in `incidents.settled`), and the window's (nothing holds the lock
    # now, and the receipts reconcile). If the week does not reconcile, or a run is going
    # right now, no record dismisses anything: the gate blocks and the incident waits.
    proofs_hold = not run_in_progress and not unexplained
    settled_ids = [i["run_id"] for i in interrupted] if proofs_hold else []
    unfinished = open_runs - len(settled_ids)

    if unfinished:
        blockers.append(f"{unfinished} production run(s) inside the window logged no end event yet")
    if queued:
        blockers.append(f"{queued} delivery row(s) for this window's approvals are still queued or in flight")
    if run_in_progress:
        blockers.append("a production run is holding the run lock right now")
    if unexplained:
        blockers.append(f"{unexplained} Airtable record(s) are explained by neither a creation nor a named reason")
    return Readiness(ready=not blockers, blockers=blockers, detail={
        "unfinished_runs": unfinished,
        "open_runs_without_an_end_event": open_runs,
        "interrupted_runs_settled_as_incidents": settled_ids,
        "interrupted_runs": interrupted,
        "queued_delivery_rows": queued,
        "waiting_on_a_named_condition": waiting_capacity,
        "waiting_for_destination_capacity": waiting_capacity,
        "production_run_in_progress": bool(run_in_progress),
        "unexplained_reconciliation_difference": unexplained,
        "known_exceptions_do_not_block": (
            "days with no run, days before the database's coverage begins, and historical Airtable records "
            "carrying a named reason are reported, not waited for"),
    })
