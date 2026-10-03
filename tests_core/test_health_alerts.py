"""Alerts for the three failures that were only caught because a human looked.

Each check must fire on the real condition, stay silent otherwise, and never depend on
anything outside the database -- it runs on a Railway cron, not on a workstation.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


from tgtc_core.db.connection import transaction
from tgtc_core.reporting import health_alerts as ha

NOW = datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc)


def _names(report):
    return {a["check"] for a in report["alerts"]}


def _log(conn, details, *, run_id="r1", stage="daily", event="end"):
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO run_log (run_id, stage, event, details) "
                        "VALUES (%s, %s, %s, %s::jsonb)",
                        (run_id, stage, event, json.dumps(details)))


def _claim_today(conn, day):
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO scheduled_executions (execution_day, run_id) VALUES (%s, %s) "
                        "ON CONFLICT (execution_day) DO NOTHING", (day, "r-claimed"))


def test_a_healthy_database_raises_nothing_but_still_reports_what_it_checked(conn):
    _claim_today(conn, NOW.strftime("%Y%m%d"))
    report = ha.evaluate(conn, now=NOW)
    assert report["alerts"] == []
    assert len(report["checked"]) == len(ha.CHECKS)
    assert report["at"].endswith("Z")


def test_a_missed_tick_is_an_alert_because_silence_looks_like_success(conn):
    report = ha.evaluate(conn, now=NOW)
    assert "scheduled_tick" in _names(report)
    alert = next(a for a in report["alerts"] if a["check"] == "scheduled_tick")
    assert alert["severity"] == "high"
    assert NOW.strftime("%Y%m%d") in alert["summary"]


def test_a_missed_tick_is_not_alerted_before_the_run_window_has_passed(conn):
    early = NOW.replace(hour=ha.TICK_EXPECTED_BY_HOUR - 1)
    assert "scheduled_tick" not in _names(ha.evaluate(conn, now=early))


def test_a_capacity_block_is_an_alert(conn):
    _claim_today(conn, NOW.strftime("%Y%m%d"))
    _log(conn, {"stop_reason": "target_not_reached:instantly_slots_short_by_11",
                "rotation": {"deficit": 11, "free_before": 2478, "rotated": False}})
    report = ha.evaluate(conn, now=NOW)
    assert "capacity" in _names(report)
    alert = next(a for a in report["alerts"] if a["check"] == "capacity")
    assert alert["severity"] == "high"
    assert alert["detail"]["deficit"] == 11


def test_a_run_that_needed_no_rotation_is_not_an_alert(conn):
    _claim_today(conn, NOW.strftime("%Y%m%d"))
    _log(conn, {"stop_reason": "target_reached",
                "rotation": {"deficit": 0, "needed": 0, "reason": "enough_room"}})
    assert "capacity" not in _names(ha.evaluate(conn, now=NOW))


def test_only_the_most_recent_run_decides_the_capacity_verdict(conn):
    _claim_today(conn, NOW.strftime("%Y%m%d"))
    _log(conn, {"stop_reason": "target_not_reached:instantly_slots_short_by_11",
                "rotation": {"deficit": 11}}, run_id="older")
    _log(conn, {"stop_reason": "target_reached", "rotation": {"deficit": 0}}, run_id="newer")
    assert "capacity" not in _names(ha.evaluate(conn, now=NOW))


def _approve_one(conn, clock):
    """A real approval through the real path, so the outbox rows have production shapes."""
    from tests_core.helpers import opportunity_service
    from tests_core.seed import apollo_for, seed_opportunity
    _p, _e, oid = seed_opportunity(conn, clock)
    opportunity_service(conn, apollo_for("acme.com", "Acme"), clock).process(oid)


def _set_due(conn, when):
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("UPDATE delivery_outbox SET available_at = %s WHERE state = 'pending'",
                        (when,))


def test_stuck_pending_deliveries_are_an_alert_with_their_age(conn, clock):
    _claim_today(conn, NOW.strftime("%Y%m%d"))
    _approve_one(conn, clock)
    _set_due(conn, NOW - timedelta(hours=ha.PENDING_STUCK_HOURS + 2))
    report = ha.evaluate(conn, now=NOW)
    assert "pending_deliveries" in _names(report)
    alert = next(a for a in report["alerts"] if a["check"] == "pending_deliveries")
    assert alert["detail"]["by_channel"] == {"airtable": 1, "instantly": 1}
    assert "2 approved deliveries" in alert["summary"]
    assert alert["severity"] == "high"


def test_a_delivery_not_yet_due_is_not_stuck(conn, clock):
    _claim_today(conn, NOW.strftime("%Y%m%d"))
    _approve_one(conn, clock)
    _set_due(conn, NOW + timedelta(hours=1))
    assert "pending_deliveries" not in _names(ha.evaluate(conn, now=NOW))


def test_a_broken_check_reports_itself_instead_of_hiding_the_others(conn, monkeypatch):
    def explode(conn, *, now):
        raise RuntimeError("boom")

    explode.__name__ = "check_that_breaks"
    monkeypatch.setattr(ha, "CHECKS", [explode, ha.check_scheduled_tick])
    report = ha.evaluate(conn, now=NOW)
    assert "check_that_breaks" in _names(report)
    assert "scheduled_tick" in _names(report), "the other checks must still run"


def test_the_slack_message_names_every_alert_and_fits_slacks_limits(conn):
    report = ha.evaluate(conn, now=NOW)
    message = ha.blocks_for(report)
    assert message["text"].startswith("TGTC alert")
    assert len(message["blocks"]) == len(report["alerts"]) + 1
    for block in message["blocks"]:
        rendered = json.dumps(block)
        assert len(rendered) < 3500, "a Slack section caps at 3000 characters"
