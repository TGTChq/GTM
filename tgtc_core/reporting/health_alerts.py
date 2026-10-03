"""Operational alerts that do not depend on anybody's laptop.

Three failures in this incident were only noticed because a human went looking:

* acquisition refused for want of Instantly slots, and the run produced nothing;
* a Challenger lead was approved whose copy could not render, which on 2026-09-21
  became 16,875 blank messages before anyone saw it;
* 25 approved deliveries sat `pending` for hours because the only thing that drains the
  outbox is a run, and no run came.

Each of those is visible in the database within minutes. This module turns them into a
check that a Railway cron can run, so the watching does not live on a workstation and
does not stop when one is closed.

Every check is a question about state, not a trend, and each carries the number that
makes it actionable. Nothing here sends email, touches a campaign or spends anything.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

#: An outbox row due this long ago that nothing has drained is stuck, not waiting.
PENDING_STUCK_HOURS = 6
#: A scheduled day with no claim by this hour UTC means the tick did not run.
TICK_EXPECTED_BY_HOUR = 6
#: Copy refusals are normal; this many in a day is a change worth seeing.
COPY_REFUSAL_ALERT = 25


def _rows(conn, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def _one(conn, sql: str, params: tuple = ()) -> Any:
    rows = _rows(conn, sql, params)
    if not rows:
        return None
    return next(iter(rows[0].values()))


def check_pending_deliveries(conn, *, now: datetime) -> Optional[Dict[str, Any]]:
    """Approved work nothing is draining."""
    cutoff = now - timedelta(hours=PENDING_STUCK_HOURS)
    # A row deferred because its campaign is not active is HELD, not stuck: FINANCE v2 is
    # deliberately kept out of sending, so its approvals pile up by design and would
    # otherwise fire this alert every hour until someone stopped believing it. Every other
    # reason still counts, including a campaign that is unexpectedly inactive, because
    # those rows carry a different last_error.
    rows = _rows(conn,
                 "SELECT channel, count(*) AS n, min(available_at) AS oldest "
                 "FROM delivery_outbox WHERE state = 'pending' AND available_at <= %s "
                 "AND coalesce(last_error, '') NOT LIKE 'campaign_status_%%' "
                 "AND coalesce(last_error, '') <> 'awaiting_instantly' "
                 "GROUP BY channel ORDER BY channel", (cutoff,))
    if not rows:
        return None
    total = sum(int(r["n"]) for r in rows)
    oldest = min(r["oldest"] for r in rows if r["oldest"])
    return {
        "check": "pending_deliveries",
        "severity": "high",
        "summary": "%d approved deliveries have been due for more than %dh"
                   % (total, PENDING_STUCK_HOURS),
        "detail": {"by_channel": {r["channel"]: int(r["n"]) for r in rows},
                   "oldest_due_at": oldest.isoformat() if hasattr(oldest, "isoformat") else str(oldest)},
        "why_it_matters": "only a run drains the outbox; if no run comes, approved contacts are never created",
    }


def check_capacity(conn, *, now: datetime) -> Optional[Dict[str, Any]]:
    """The last run refused to acquire because there were not enough Instantly slots."""
    rows = _rows(conn,
                 "SELECT run_id, details, created_at FROM run_log "
                 "WHERE stage = 'daily' AND event = 'end' ORDER BY id DESC LIMIT 1")
    if not rows:
        return None
    details = rows[0]["details"]
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except ValueError:
            details = {}
    details = details or {}
    stop = str(details.get("stop_reason") or "")
    rotation = details.get("rotation") or {}
    blocked = "instantly_slots_short_by" in stop or "slots" in stop
    deficit = rotation.get("deficit") or rotation.get("needed") or 0
    if not blocked and not (isinstance(deficit, int) and deficit > 0):
        return None
    return {
        "check": "capacity",
        "severity": "high" if blocked else "medium",
        "summary": "the last run %s (deficit %s slots)"
                   % ("was blocked for Instantly capacity" if blocked else "needed rotation", deficit),
        "detail": {"run_id": rows[0]["run_id"], "stop_reason": stop,
                   "free_before": rotation.get("free_before"), "deficit": deficit,
                   "rotated": rotation.get("rotated")},
        "why_it_matters": "a capacity refusal produces zero leads for the day and spends nothing, so it is silent",
    }


def check_copy_refusals(conn, *, now: datetime) -> Optional[Dict[str, Any]]:
    """Challenger copy that could not be rendered."""
    since = now - timedelta(hours=24)
    rows = _rows(conn,
                 "SELECT blocked_reason, count(*) AS n FROM delivery_outbox "
                 "WHERE state = 'blocked' AND updated_at >= %s "
                 "AND (blocked_reason LIKE 'challenger_copy%%' OR blocked_reason LIKE 'copy_%%') "
                 "GROUP BY 1 ORDER BY n DESC", (since,))
    total = sum(int(r["n"]) for r in rows)
    if total < COPY_REFUSAL_ALERT:
        return None
    return {
        "check": "copy_refusals",
        "severity": "medium",
        "summary": "%d Challenger leads were refused for copy in the last 24h" % total,
        "detail": {"by_reason": {r["blocked_reason"]: int(r["n"]) for r in rows[:8]}},
        "why_it_matters": "a copy refusal is correct, but a jump in them means the renderer or the title data moved",
    }


def check_scheduled_tick(conn, *, now: datetime) -> Optional[Dict[str, Any]]:
    """The day's scheduled execution never claimed."""
    if now.hour < TICK_EXPECTED_BY_HOUR:
        return None
    day = now.strftime("%Y%m%d")
    claimed = _one(conn, "SELECT count(*) FROM scheduled_executions WHERE execution_day = %s", (day,))
    if claimed:
        return None
    return {
        "check": "scheduled_tick",
        "severity": "high",
        "summary": "no scheduled execution claimed %s by %02d:00Z" % (day, now.hour),
        "detail": {"execution_day": day},
        "why_it_matters": "a cron that does not fire leaves no log at all, so silence looks like success",
    }


def check_empty_email_recovery(conn, *, now: datetime) -> Optional[Dict[str, Any]]:
    """The repair for the blank sends, and anything that says it has gone wrong.

    Reports two different things, because they need different responses. Harm -- a
    message that went out with an unapproved subject, or somebody enrolled with no role
    for the subject line -- is high and means the campaign should already be paused.
    A queue that has stopped moving is medium: it usually means storage is full and the
    daily run has not yet rotated, which is the design, but it should not be invisible
    for days.
    """
    try:
        rows = _rows(conn, "SELECT state, count(*) AS n FROM empty_email_recovery GROUP BY 1")
    except Exception:
        return None                                   # the table is not there yet
    by_state = {str(r["state"]): int(r["n"]) for r in rows}
    if not by_state:
        return None
    harm = _rows(conn,
                 "SELECT count(*) FILTER (WHERE state = 'sent' "
                 "    AND (evidence->>'subject_as_approved') IS DISTINCT FROM 'true') AS wrong_subject, "
                 # A second line, not the protection: the table's own CHECK makes a
                 # sendable row without a role impossible, and that is asserted. This
                 # stays because a future migration could relax the constraint without
                 # anybody noticing the subject line depends on it.
                 "count(*) FILTER (WHERE state IN ('reserved', 'enrolled') "
                 "    AND length(btrim(verified_role)) = 0) AS roleless "
                 "FROM empty_email_recovery")[0]
    wrong = int(harm["wrong_subject"] or 0)
    roleless = int(harm["roleless"] or 0)
    waiting = by_state.get("authorised", 0) + by_state.get("reserved", 0)
    stale = int(_one(conn,
                     "SELECT count(*) FROM empty_email_recovery WHERE state = 'enrolled' "
                     "AND enrolled_at <= %s", (now - timedelta(hours=72),)) or 0)

    # A queue waiting behind the internal test is the design working, not a condition to
    # report. It would otherwise fire every hour from Friday night until the Monday
    # window opens -- about 60 identical alerts, which is how people learn to ignore the
    # channel. The same applies to a weekend: nothing can send, so nothing is stuck.
    gate_shut = not bool(_one(
        conn, "SELECT count(*) FROM empty_email_recovery WHERE is_internal_test "
              "AND state = 'sent' AND (evidence->>'subject_as_approved') = 'true'"))

    if wrong or roleless:
        severity = "high"
        summary = ("the recovery campaign shows harm: %d message(s) that did not match "
                   "the approved email, %d contact(s) enrolled with no role"
                   % (wrong, roleless))
    elif stale and not gate_shut:
        severity = "medium"
        summary = ("%d recovery contacts have been enrolled for more than 72h with no "
                   "message recorded" % stale)
    elif waiting and not gate_shut:
        severity = "medium"
        summary = "%d recovery contacts are still waiting to be enrolled" % waiting
    else:
        return None

    return {
        "check": "empty_email_recovery",
        "severity": severity,
        "summary": summary,
        "detail": {"by_state": by_state, "unapproved_subject_sent": wrong,
                   "enrolled_without_a_role": roleless,
                   "enrolled_over_72h_with_no_message": stale,
                   "waiting_behind_the_internal_test": gate_shut},
        "why_it_matters": "this campaign exists to repair 16,875 blank messages; a fault in it would repeat them",
    }


CHECKS: List[Callable[..., Optional[Dict[str, Any]]]] = [
    check_pending_deliveries, check_capacity, check_copy_refusals, check_scheduled_tick,
    check_empty_email_recovery,
]


def evaluate(conn, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    alerts = []
    for check in CHECKS:
        try:
            result = check(conn, now=moment)
        except Exception as exc:                       # a broken check must not hide the others
            result = {"check": getattr(check, "__name__", "unknown"), "severity": "medium",
                      "summary": "the check itself failed: %s" % exc.__class__.__name__,
                      "detail": {"error": str(exc)[:200]},
                      "why_it_matters": "an alert that cannot run is an alert that will not fire"}
        if result:
            alerts.append(result)
    return {"at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"), "alerts": alerts,
            "checked": [getattr(c, "__name__", "?") for c in CHECKS]}


def blocks_for(report: Dict[str, Any]) -> Dict[str, Any]:
    alerts = report.get("alerts") or []
    header = "TGTC alert: %d condition%s at %s" % (
        len(alerts), "" if len(alerts) == 1 else "s", report.get("at"))
    blocks: List[Dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header[:150]}}]
    for alert in alerts:
        lines = ["*%s* (%s)" % (alert["check"], alert["severity"]), alert["summary"],
                 "_%s_" % alert["why_it_matters"]]
        detail = alert.get("detail") or {}
        if detail:
            lines.append("```%s```" % json.dumps(detail, default=str)[:600])
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}})
    return {"blocks": blocks, "text": header}


__all__ = ["evaluate", "blocks_for", "CHECKS", "check_empty_email_recovery",
           "PENDING_STUCK_HOURS",
           "TICK_EXPECTED_BY_HOUR", "COPY_REFUSAL_ALERT"]
