"""Delivery: the clock, whether the week has closed, and the message people read.

The agreed rule is Friday 06:00 America/Los_Angeles, retry until 07:00, and -- if the
data still has not closed -- a labelled status notice rather than partial numbers
dressed as the final report. Three things can go wrong with that, and each has tests
here:

* the hour drifts across a daylight-saving change, because a UTC cron is not a local
  clock;
* the report goes out while production is still writing to the window it measures;
* the retry posts the report a second time.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tgtc_core.reporting import pipeline, slack, store
from tgtc_core.reporting.schedule import (ACTION_FINAL, ACTION_NOTICE, ACTION_SKIP, AFTER_RETRY_WINDOW,
                                          BEFORE_DUE, IN_RETRY_WINDOW, NOT_DELIVERY_DAY, decide,
                                          delivery_state, readiness)
from tests_core.test_weekly_report import FRIDAY_MORNING, WEEK_START, report_for, seed_lead, seed_run

UTC = timezone.utc


# --------------------------------------------------------------------------------
# the clock
# --------------------------------------------------------------------------------

def test_06_00_local_stays_06_00_across_a_daylight_saving_change():
    """In PDT the delivery moment is 13:00 UTC; in PST it is 14:00 UTC. A schedule that
    only knew UTC would deliver an hour early or an hour late for half the year."""
    # Friday 2026-09-25, PDT (UTC-7)
    assert delivery_state(datetime(2026, 9, 25, 12, 59, tzinfo=UTC)) == BEFORE_DUE          # 05:59 local
    assert delivery_state(datetime(2026, 9, 25, 13, 0, tzinfo=UTC)) == IN_RETRY_WINDOW      # 06:00 local
    assert delivery_state(datetime(2026, 9, 25, 13, 59, tzinfo=UTC)) == IN_RETRY_WINDOW     # 06:59 local
    assert delivery_state(datetime(2026, 9, 25, 14, 0, tzinfo=UTC)) == AFTER_RETRY_WINDOW   # 07:00 local
    # Friday 2026-11-06, PST (UTC-8) -- one hour later in UTC, the same hour locally
    assert delivery_state(datetime(2026, 11, 6, 13, 59, tzinfo=UTC)) == BEFORE_DUE          # 05:59 local
    assert delivery_state(datetime(2026, 11, 6, 14, 0, tzinfo=UTC)) == IN_RETRY_WINDOW      # 06:00 local
    assert delivery_state(datetime(2026, 11, 6, 15, 0, tzinfo=UTC)) == AFTER_RETRY_WINDOW   # 07:00 local


def test_no_other_day_is_a_delivery_day():
    for day in range(19, 25):        # Sat 2026-09-19 .. Thu 2026-09-24
        assert delivery_state(datetime(2026, 9, day, 14, 0, tzinfo=UTC)) == NOT_DELIVERY_DAY
    assert delivery_state(datetime(2026, 9, 25, 14, 0, tzinfo=UTC)) != NOT_DELIVERY_DAY


def test_one_tick_does_exactly_one_of_three_things():
    assert decide(NOT_DELIVERY_DAY, True) == (ACTION_SKIP, NOT_DELIVERY_DAY)
    assert decide(BEFORE_DUE, True) == (ACTION_SKIP, BEFORE_DUE)
    assert decide(IN_RETRY_WINDOW, True) == (ACTION_FINAL, "data_closed")
    assert decide(IN_RETRY_WINDOW, False) == (ACTION_SKIP, "waiting_for_the_data_to_close")
    assert decide(AFTER_RETRY_WINDOW, False) == (ACTION_NOTICE, "data_did_not_close_within_the_retry_window")
    # And once the data closes, even long after the retry window, the real report goes.
    assert decide(AFTER_RETRY_WINDOW, True) == (ACTION_FINAL, "data_closed")


# --------------------------------------------------------------------------------
# whether the week has closed
# --------------------------------------------------------------------------------

def _ready(conn, report=None):
    window = pipeline.window_for(now=FRIDAY_MORNING)
    return readiness(conn, window, report or report_for(conn))


def test_a_run_that_never_logged_an_end_holds_the_report_back(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO run_log (run_id, stage, event, details, created_at) "
                    "VALUES ('run-open', 'daily', 'start', '{}'::jsonb, %s)", (WEEK_START + timedelta(days=1),))
    conn.commit()
    state = _ready(conn)
    assert state.ready is False
    assert "logged no end event yet" in state.blockers[0]


def test_delivery_rows_still_in_flight_hold_the_report_back(conn):
    lead = seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product",
                     campaign_id="camp-pr", approved_at=WEEK_START + timedelta(days=1),
                     instantly_kind=None, airtable=False)
    conn.execute("INSERT INTO delivery_outbox (approval_id, channel, idempotency_key, payload_json, state) "
                 "VALUES (%s, 'instantly', 'queued-1', '{}'::jsonb, 'in_flight')", (lead["approval_id"],))
    conn.commit()
    state = _ready(conn)
    assert state.ready is False
    assert any("still queued or in flight" in b for b in state.blockers)


def test_a_creation_in_the_wrong_campaign_is_an_unexplained_difference_and_blocks(conn):
    """An Airtable record whose Instantly creation went to a campaign the approval was
    not routed to is explained by nothing. That is exactly the case a report must not
    quietly average away."""
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product",
              campaign_id="camp-pr", instantly_campaign="camp-somewhere-else")
    report = report_for(conn)
    assert report["reconciliation"]["unexplained_difference"] == 1
    assert report["reconciliation"]["identity_holds"] is False
    state = _ready(conn, report)
    assert state.ready is False
    assert any("neither a creation nor a named reason" in b for b in state.blockers)


def test_a_missing_run_is_an_alert_but_never_holds_the_report_back(conn):
    """Waiting cannot fix a run that did not happen. The week still gets reported --
    with the missing day named."""
    seed_run(conn, run_id="run-a", started_at=WEEK_START + timedelta(days=1, hours=3))
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1, hours=4), campaign_key="product",
              campaign_id="camp-pr", run_id="run-a")
    report = report_for(conn)
    assert any("missing run" in alert for alert in report["alerts"])
    state = _ready(conn, report)
    assert state.ready is True and state.blockers == []


def test_the_historical_airtable_exception_does_not_hold_the_report_back(conn):
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="finance", campaign_id="camp-fi",
              instantly_kind="rejected", blocked_reason="not_delivered:instantly_existing_other_campaign")
    report = report_for(conn)
    assert report["reconciliation"]["airtable_records_without_a_genuine_creation"] == 1
    assert report["reconciliation"]["identity_holds"] is True
    assert _ready(conn, report).ready is True


# --------------------------------------------------------------------------------
# the message
# --------------------------------------------------------------------------------

def _blocks_text(blocks) -> str:
    out = []
    for block in blocks:
        if block["type"] == "section":
            out.append(block["text"]["text"])
        elif block["type"] == "header":
            out.append(block["text"]["text"])
        elif block["type"] == "context":
            out.extend(element["text"] for element in block["elements"])
    return "\n".join(out)


def test_the_slack_message_carries_the_seven_sections_with_their_denominators(conn):
    seed_run(conn, run_id="run-a", started_at=WEEK_START + timedelta(days=1, hours=3))
    for i in range(3):
        seed_lead(conn, received_at=WEEK_START + timedelta(days=1, hours=4), campaign_key="product",
                  campaign_id="camp-pr", run_id="run-a")
    report = report_for(conn)
    blocks = slack.blocks_for(report)
    text = _blocks_text(blocks)
    assert len(blocks) <= 50                                   # Slack's own limit
    # 1 window, cutoff, status
    assert "end exclusive" in text and "Data cutoff" in text and "America/Los_Angeles" in text
    # 2 captured and reviewed, each percentage next to the count it came from
    assert "Records captured from the provider" in text and "of the" in text
    assert "New unique jobs" in text and "Jobs reviewed" in text
    # 3 qualified, employers, units
    assert "Qualified jobs" in text and "Unique employers" in text and "Company × campaign units" in text
    # 4 three separate delivery measurements
    assert "Verified contacts" in text and "Airtable records created" in text
    assert "Genuine new Instantly creations" in text
    # 5 trend, previous week, nine campaigns
    assert "Daily trend" in text and "All nine Challenger campaigns" in text
    assert sum(campaign["name"][:12] in text for campaign in report["by_campaign"]["campaigns"].values()) == 9
    # 6 provider consumption and cost per final new lead
    assert "Provider consumption" in text and "Cost per final new lead" in text
    # 7 exceptions and where the detail lives
    assert "Exceptions" in text and "Legacy exception" in text and "Reconciliation:" in text
    assert "Lead-level detail is not published" in text


def test_the_slack_message_never_carries_personal_data(conn):
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product", campaign_id="camp-pr",
              email="jane.doe@acme-corp.com", person="jane.doe", employer="Acme Corp")
    text = _blocks_text(slack.blocks_for(report_for(conn)))
    assert "@" not in text and "jane" not in text.lower() and "acme" not in text.lower()


def test_a_day_the_database_cannot_speak_about_says_so_in_slack(conn):
    seed_lead(conn, received_at=WEEK_START + timedelta(days=3), campaign_key="product", campaign_id="camp-pr")
    text = _blocks_text(slack.blocks_for(report_for(conn)))
    assert "unavailable — before this database begins" in text
    assert "reported as unavailable, not as zero" in text


def test_the_detail_link_is_used_when_one_is_published(conn):
    text = _blocks_text(slack.blocks_for(report_for(conn), detail_url="https://example.invalid/detail"))
    assert "<https://example.invalid/detail|Secure lead-level detail>" in text


def test_the_status_notice_publishes_no_pipeline_numbers(conn):
    """A delayed week must not leave a half-number in anybody's head."""
    for _ in range(4):
        seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product", campaign_id="camp-pr")
    report = report_for(conn)
    assert report["delivery"]["instantly_created_unique_people"] == 4
    text = _blocks_text(slack.status_notice_blocks(report, {"blockers": ["2 delivery row(s) are still in flight"]},
                                                   retry_until="07:00 America/Los_Angeles"))
    assert "not yet final" in text and "Partial figures are deliberately not shown" in text
    assert "still in flight" in text
    for word in ("net-new", "Genuine new Instantly creations", "Qualified jobs", "Airtable records created"):
        assert word not in text


def test_the_notification_line_says_the_number_and_whether_it_reconciled(conn):
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product", campaign_id="camp-pr")
    line = slack.text_for(report_for(conn))
    assert "TGTC weekly pipeline" in line and "1 net-new Challenger leads created" in line


# --------------------------------------------------------------------------------
# the destination
# --------------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


def test_the_channel_is_resolved_by_name_so_the_destination_is_a_fact(monkeypatch):
    import requests

    pages = [
        {"ok": True, "channels": [{"id": "C111", "name": "random", "is_member": True}],
         "response_metadata": {"next_cursor": "page2"}},
        {"ok": True, "channels": [{"id": "C999", "name": "gtm-engineering", "is_member": True}]},
    ]
    seen = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        assert url.endswith("/conversations.list")
        page = pages[seen["n"]]
        seen["n"] += 1
        return _FakeResponse(page)

    monkeypatch.setattr(requests, "get", fake_get)
    found = slack.resolve_channel("xoxb-test", "#gtm-engineering")
    assert found == {"id": "C999", "name": "gtm-engineering", "is_private": False, "is_member": True}


def test_a_channel_that_cannot_be_found_refuses_rather_than_guessing(monkeypatch):
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse({"ok": True, "channels": []}))
    with pytest.raises(slack.SlackError, match="could not be verified"):
        slack.resolve_channel("xoxb-test", "#gtm-engineering")


def test_the_api_sender_addresses_the_resolved_channel_id(monkeypatch):
    import requests

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, payload=json, auth=bool(headers.get("Authorization")))
        return _FakeResponse({"ok": True, "ts": "1790000000.1"})

    monkeypatch.setattr(requests, "post", fake_post)
    receipt = slack.api_sender("xoxb-test", "C999")("#gtm-engineering", {"blocks": [], "text": "x"})
    assert captured["payload"]["channel"] == "C999" and captured["auth"] is True
    assert receipt["transport"] == "slack_api" and receipt["channel_id"] == "C999"
    assert receipt["message_ts"] == "1790000000.1"


def test_slack_refusing_is_an_error_not_a_silent_success(monkeypatch):
    import requests

    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse({"ok": False, "error": "not_in_channel"}))
    with pytest.raises(slack.SlackError, match="not_in_channel"):
        slack.api_sender("xoxb-test", "C999")("#gtm-engineering", {"blocks": [], "text": "x"})


# --------------------------------------------------------------------------------
# the command
# --------------------------------------------------------------------------------

def _argv(pg_url, *extra):
    return ["weekly-report", "--database-url", pg_url, "--week", "last", "--now", FRIDAY_MORNING.isoformat(),
            "--print-format", "none", "--no-compare", *extra]


def test_the_command_refuses_to_send_without_a_named_channel(conn, pg_url, capsys):
    from tgtc_core.__main__ import main

    assert main(_argv(pg_url, "--send", "slack")) == 5
    assert "--slack-channel names the confirmed destination" in capsys.readouterr().err


def test_the_command_refuses_to_send_with_no_verifiable_destination(conn, pg_url, capsys, monkeypatch):
    from tgtc_core.__main__ import main

    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("SLACK_WEEKLY_REPORT_WEBHOOK_URL", "https://hooks.invalid/whatever")
    # A webhook alone proves nothing about where it posts, so the default refuses -- and
    # it refuses AT THE DELIVERY MOMENT (Friday 06:05 Pacific), where it is a real
    # failure rather than daily noise.
    friday = datetime(2026, 9, 25, 13, 5, tzinfo=UTC)
    argv = ["weekly-report", "--database-url", pg_url, "--week", "last", "--now", friday.isoformat(),
            "--print-format", "none", "--no-compare", "--send", "slack", "--slack-channel", "#gtm-engineering"]
    assert main(argv) == 5
    err = capsys.readouterr().err
    assert "no verified destination" in err and "hooks.invalid" not in err      # never the URL
    assert store.delivery_record(conn, "weekly-2026-09-18", "#gtm-engineering", store.FINAL) is None


def test_the_command_can_render_the_message_without_sending_it(conn, pg_url, capsys, monkeypatch):
    from tgtc_core.__main__ import main

    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product", campaign_id="camp-pr")
    monkeypatch.setenv("SLACK_WEEKLY_REPORT_WEBHOOK_URL", "https://hooks.invalid/whatever")
    assert main(_argv(pg_url, "--send", "slack", "--slack-channel", "#gtm-engineering",
                      "--destination-basis", "webhook-declared", "--dry-run-send")) == 0
    out = capsys.readouterr().out
    preview, summary = out.split(chr(10), 1)
    # The whole message on one line, so it survives out-of-order container logs.
    message = json.loads(preview[len("SLACK_PREVIEW "):])
    assert preview.startswith("SLACK_PREVIEW ") and chr(10) not in preview
    assert message["blocks"] and message["text"].startswith("TGTC weekly pipeline")
    summary = json.loads(summary)
    assert summary["delivery"]["reason"] == "dry_run" and summary["delivery"]["sent"] is False
    assert summary["delivery"]["blocks"] == len(message["blocks"]) >= 10
    assert summary["delivery"]["destination_basis"] == "webhook_declared"
    assert store.delivery_record(conn, "weekly-2026-09-11", "#gtm-engineering", store.FINAL) is None


def test_a_weekday_tick_measures_the_week_in_progress_and_is_not_an_error(conn, pg_url, capsys, monkeypatch):
    """The job runs daily so a problem is visible before Friday. On a Tuesday it holds a
    week-to-date report and simply has nowhere to send it -- that is not a failure."""
    from tgtc_core.__main__ import main

    tuesday = datetime(2026, 9, 22, 20, 0, tzinfo=UTC)          # 13:00 Pacific, Tuesday
    assert main(["weekly-report", "--database-url", pg_url, "--week", "auto", "--now", tuesday.isoformat(),
                 "--print-format", "none", "--no-compare", "--send", "slack",
                 "--slack-channel", "#gtm-engineering", "--fail-on", "never"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["kind"] == "partial"
    assert summary["delivery"]["reason"] == "not_delivery_day" and "error" not in summary["delivery"]


def test_the_command_does_not_send_before_the_delivery_moment(conn, pg_url, capsys, monkeypatch):
    from tgtc_core.__main__ import main

    monkeypatch.setenv("SLACK_WEEKLY_REPORT_WEBHOOK_URL", "https://hooks.invalid/whatever")
    thursday = datetime(2026, 9, 24, 14, 0, tzinfo=UTC)
    argv = ["weekly-report", "--database-url", pg_url, "--week", "last", "--now", thursday.isoformat(),
            "--print-format", "none", "--no-compare", "--send", "slack", "--slack-channel", "#gtm-engineering",
            "--destination-basis", "webhook-declared"]
    assert main(argv) == 0
    summary = json.loads(capsys.readouterr().out)
    # A tick that could not send anything asks nothing of Slack and nothing of the
    # database: no credential is needed, and no readiness query is run.
    assert summary["delivery"] == {"sent": False, "channel": "#gtm-engineering",
                                   "state": "not_delivery_day", "reason": "not_delivery_day",
                                   "schedule": "friday 06:00 America/Los_Angeles, retry until 07:00"}
    assert store.delivery_record(conn, "weekly-2026-09-11", "#gtm-engineering", store.FINAL) is None
