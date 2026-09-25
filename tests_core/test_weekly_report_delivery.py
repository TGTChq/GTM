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

from tgtc_core.reporting import pipeline, render, slack, store
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


def test_a_contact_waiting_for_destination_capacity_does_not_hold_the_report_back(conn):
    """Measured 2026-09-24: 107 approved contacts sat behind a full Instantly workspace.
    Their week's figures are already correct -- nothing counts them as created -- and a
    full workspace stays full for days. Waiting would publish nothing at all."""
    lead = seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product",
                     campaign_id="camp-pr", approved_at=WEEK_START + timedelta(days=1),
                     instantly_kind=None, airtable=False)
    conn.execute("INSERT INTO delivery_outbox (approval_id, channel, idempotency_key, payload_json, state, last_error) "
                 "VALUES (%s, 'instantly', 'full-1', '{}'::jsonb, 'pending', 'instantly_capacity_blocked')",
                 (lead["approval_id"],))
    conn.commit()

    state = _ready(conn)

    assert state.ready is True, "a full destination is a named condition, not an open question"
    assert state.to_dict()["waiting_for_destination_capacity"] == 1
    assert state.to_dict()["queued_delivery_rows"] == 0

    # The Airtable row the gate is holding for that same contact is the same kind of
    # named condition: it is not counted anywhere, and it cannot change until Instantly
    # accepts the contact.
    conn.execute("INSERT INTO delivery_outbox (approval_id, channel, idempotency_key, payload_json, state, last_error) "
                 "VALUES (%s, 'airtable', 'held-1', '{}'::jsonb, 'pending', 'awaiting_instantly')",
                 (lead["approval_id"],))
    conn.commit()
    again = _ready(conn)
    assert again.ready is True and again.to_dict()["waiting_on_a_named_condition"] == 2

    # ... while an ordinary in-flight row, for another contact, still holds it back.
    other = seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="finance",
                      campaign_id="camp-fi", approved_at=WEEK_START + timedelta(days=1),
                      instantly_kind=None, airtable=False)
    conn.execute("INSERT INTO delivery_outbox (approval_id, channel, idempotency_key, payload_json, state) "
                 "VALUES (%s, 'instantly', 'inflight-1', '{}'::jsonb, 'in_flight')", (other["approval_id"],))
    conn.commit()
    assert _ready(conn).ready is False


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


def test_the_message_opens_with_the_four_figures_in_the_order_they_were_asked_for(conn):
    seed_run(conn, run_id="run-a", started_at=WEEK_START + timedelta(days=1, hours=3))
    for _ in range(3):
        seed_lead(conn, received_at=WEEK_START + timedelta(days=1, hours=4), campaign_key="product",
                  campaign_id="camp-pr", run_id="run-a")
    report = report_for(conn)
    lines = slack.headline_lines(report)
    assert [line.split(":*")[0].lstrip("*") for line in lines] == [
        "Jobs", "Qualified opportunities", "Contacts found", "Added to Instantly"]
    text = _blocks_text(slack.blocks_for(report))
    assert text.index("Jobs:") < text.index("Qualified opportunities:") \
        < text.index("Contacts found:") < text.index("Added to Instantly:")
    assert "captured /" in text and "reviewed" in text


def test_the_review_percentage_divides_a_cohort_by_itself(conn):
    """Two jobs captured this week, one of them reviewed: 50%. A classification written
    this week for a job first seen LAST week changes neither count."""
    inside = WEEK_START + timedelta(days=1)
    with conn.cursor() as cur:
        for i, (first_seen, classified) in enumerate(((inside, True), (inside, False),
                                                      (WEEK_START - timedelta(days=3), True))):
            cur.execute("INSERT INTO postings (source, provider_job_id, content_hash, commercial_age_anchor, "
                        "first_seen_at) VALUES ('linkedin', %s, %s, %s, %s) RETURNING id",
                        (f"job-{i}", f"hash-{i}", first_seen, first_seen))
            posting_id = int(cur.fetchone()["id"])
            if classified:
                cur.execute("INSERT INTO classifications (posting_id, policy_version, method, "
                            "compatible_functions, excluded, created_at, first_recorded_at) "
                            "VALUES (%s, 'v', 'deterministic', '{product}', false, %s, %s)",
                            (posting_id, inside, inside))
    conn.commit()
    h = report_for(conn)["headline"]
    assert (h["jobs_captured"], h["jobs_reviewed"], h["jobs_review_rate"]) == (2, 1, 50.0)
    assert h["qualified_jobs"] == 1
    assert "same cohort" in h["definitions"]["jobs_reviewed"]


def test_the_percentage_is_omitted_with_its_reason_when_there_is_no_cohort(conn):
    h = report_for(conn)["headline"]
    assert (h["jobs_captured"], h["jobs_reviewed"]) == (0, 0)
    assert h["jobs_review_rate"] is None
    assert "no cohort" in h["jobs_review_rate_omitted_because"]
    line = slack.headline_lines(report_for(conn))[0]
    assert "% omitted" in line and "no cohort" in line


def test_added_to_instantly_counts_only_confirmed_unique_creations(conn):
    """Existing contacts, rejections and Control campaigns are not additions, and a
    creation from an earlier week's approval is shown as such rather than hidden."""
    inside = WEEK_START + timedelta(days=2)
    seed_lead(conn, received_at=inside, campaign_key="product", campaign_id="camp-pr")
    seed_lead(conn, received_at=inside, campaign_key="finance", campaign_id="camp-fi", instantly_kind="existing")
    seed_lead(conn, received_at=inside, campaign_key="finance", campaign_id="camp-fi",
              instantly_kind="rejected", blocked_reason="instantly_existing_other_campaign")
    seed_lead(conn, received_at=inside, campaign_key="ecommerce", campaign_id="camp-ec",
              approved_at=WEEK_START - timedelta(days=2))
    h = report_for(conn)["headline"]
    assert h["added_to_instantly"] == 2
    assert (h["added_from_this_weeks_approvals"], h["added_from_earlier_approvals"]) == (1, 1)
    line = [x for x in slack.headline_lines(report_for(conn)) if x.startswith("*Added")][0]
    assert "2" in line and "includes 1 from approvals made before this week" in line


def test_the_message_stays_short_and_leaves_the_detail_to_the_file(conn):
    """Trends, the nine-campaign table and the full exception list are review material."""
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product", campaign_id="camp-pr")
    blocks = slack.blocks_for(report_for(conn))
    text = _blocks_text(blocks)
    assert len(blocks) <= 6 and len(text) < 1600
    for absent in ("Daily trend", "All nine Challenger campaigns", "Provider consumption", "```"):
        assert absent not in text
    # ... but the week's own identity and the counting rules are still on the message.
    assert "end exclusive" in text and "data cutoff" in text and "same cohort" in text
    assert "phone sidecar" in text


def test_only_one_line_of_attention_reaches_the_channel(conn):
    seed_run(conn, run_id="run-a", started_at=WEEK_START + timedelta(days=1, hours=3))
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1, hours=4), campaign_key="product",
              campaign_id="camp-pr", run_id="run-a")
    report = report_for(conn)
    assert len(report["alerts"]) > 1
    text = _blocks_text(slack.blocks_for(report))
    attention = [line for line in text.splitlines() if line.startswith(("⚠️", "🟥"))]
    assert len(attention) == 1 and "more in the detail" in attention[0]


def test_the_detail_is_linked_when_published_and_pending_when_not(conn):
    report = report_for(conn)
    linked = _blocks_text(slack.blocks_for(report, detail_url="https://drive.example/file", detail_rows=3623))
    assert "<https://drive.example/file|this week's file>" in linked
    assert "3,623 rows, one per lead" in linked and "authorised team" in linked

    pending = _blocks_text(slack.blocks_for(report, detail_rows=3623))
    assert "_pending_" in pending and "no private destination and reader list has been verified" in pending
    assert "3,623 rows" in pending and "personal data" in pending


def test_a_day_the_database_cannot_speak_about_is_kept_in_the_detail_not_the_channel(conn):
    """Unavailable coverage is a real caveat and it belongs in the file, not in four
    lines at 06:00 -- but it must never be read as a zero."""
    seed_lead(conn, received_at=WEEK_START + timedelta(days=3), campaign_key="product", campaign_id="camp-pr")
    report = report_for(conn)
    assert report["coverage"]["local_days_unavailable"]
    assert any("unavailable, not as zero production" in note for note in report["notes"])
    assert all(day["instantly_created_unique_people"] is None
               for day in report["daily"] if day.get("unavailable"))
    assert "UNAVAILABLE" in render.render_text(report)
    assert "unavailable" not in _blocks_text(slack.blocks_for(report)).lower()


def test_the_notification_line_leads_with_the_figures(conn):
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product", campaign_id="camp-pr")
    line = slack.text_for(report_for(conn))
    assert line.startswith("TGTC weekly pipeline")
    assert "1 added to Instantly" in line and "qualified opportunities" in line


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


# --------------------------------------------------------------------------------
# the channel test
# --------------------------------------------------------------------------------

def test_the_connectivity_test_says_what_it_is_and_carries_no_pipeline_data():
    """A test that looks like a report teaches people to read a test as a report."""
    blocks = slack.connectivity_test_blocks(channel="#gtm-engineering",
                                            schedule="every Friday at 06:00 America/Los_Angeles",
                                            window_rule="Friday 00:00 to the following Friday 00:00 (end exclusive)")
    text = _blocks_text(blocks)
    assert "connectivity test" in text.lower()
    assert "carries no pipeline numbers" in text
    assert "#gtm-engineering" in text and "06:00 America/Los_Angeles" in text
    assert "status notice" in text                      # the reader learns what a delay looks like
    for word in ("net-new", "Airtable", "Qualified jobs", "campaigns\n"):
        assert word not in text
    assert len(blocks) == 2 and len(text) < 1200         # short by construction
    assert "no pipeline data" in slack.connectivity_test_text("#gtm-engineering")


def test_the_channel_test_is_sent_once_per_day_per_channel(conn):
    """The same rule the Friday retries depend on, proved on the cheapest message there
    is: a second attempt with the same key sends nothing."""
    store.ensure_schema(conn)
    sent = []
    sender = lambda channel, message: sent.append(channel) or {"transport": "incoming_webhook"}  # noqa: E731
    key = "connectivity-test-2026-09-23"

    first = store.deliver_guarded(conn, key=key, channel="#gtm-engineering",
                                  kind=store.CONNECTIVITY_TEST, sender=sender,
                                  message={"blocks": [], "text": "t"}, destination_basis="webhook_declared")
    second = store.deliver_guarded(conn, key=key, channel="#gtm-engineering",
                                   kind=store.CONNECTIVITY_TEST, sender=sender,
                                   message={"blocks": [], "text": "t"}, destination_basis="webhook_declared")
    assert first["sent"] is True and second["sent"] is False and second["reason"] == "already_delivered"
    assert sent == ["#gtm-engineering"]
    row = store.delivery_record(conn, key, "#gtm-engineering", store.CONNECTIVITY_TEST)
    assert row["destination_basis"] == "webhook_declared" and row["receipt"]["transport"] == "incoming_webhook"


def test_the_channel_test_refuses_the_same_way_the_report_does(conn, pg_url, capsys, monkeypatch):
    from tgtc_core.__main__ import main

    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_WEEKLY_REPORT_WEBHOOK_URL", raising=False)
    assert main(["slack-test", "--database-url", pg_url, "--channel", "#gtm-engineering"]) == 5
    assert "no verified destination" in capsys.readouterr().err


# --------------------------------------------------------------------------------
# a known historical gap
# --------------------------------------------------------------------------------

def test_a_missing_run_is_reported_but_neither_withholds_nor_fails_the_report(conn, pg_url, capsys):
    """2026-09-19 had no production run. The week that contains it must still publish:
    the gap is an exception in the message, not a reason to hold the report or to exit
    red every twenty minutes."""
    from tgtc_core.__main__ import main

    seed_run(conn, run_id="run-a", started_at=WEEK_START + timedelta(days=1, hours=3))
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1, hours=4), campaign_key="product",
              campaign_id="camp-pr", run_id="run-a")
    report = report_for(conn)

    assert any("missing run" in item for item in report["alerts"])      # said out loud
    assert report["integrity_alerts"] == []                             # but not an integrity failure
    assert _ready(conn, report).ready is True                           # and never withheld
    assert decide(IN_RETRY_WINDOW, _ready(conn, report).ready)[0] == ACTION_FINAL
    assert "missing run" in _blocks_text(slack.blocks_for(report))      # visible to the readers

    argv = ["weekly-report", "--database-url", pg_url, "--week", "last", "--now", FRIDAY_MORNING.isoformat(),
            "--print-format", "none", "--no-compare", "--fail-on", "integrity"]
    assert main(argv) == 0                                              # the tick is green
    capsys.readouterr()


def test_an_unexplained_difference_still_withholds_and_fails(conn, pg_url, capsys):
    """The other half of the same rule: what nothing explains is still blocking."""
    from tgtc_core.__main__ import main

    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product",
              campaign_id="camp-pr", instantly_campaign="camp-somewhere-else")
    report = report_for(conn)
    assert report["integrity_alerts"] and _ready(conn, report).ready is False
    assert decide(IN_RETRY_WINDOW, False)[0] == ACTION_SKIP
    assert decide(AFTER_RETRY_WINDOW, False)[0] == ACTION_NOTICE
    argv = ["weekly-report", "--database-url", pg_url, "--week", "last", "--now", FRIDAY_MORNING.isoformat(),
            "--print-format", "none", "--no-compare", "--fail-on", "integrity"]
    assert main(argv) == 4
    capsys.readouterr()


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
    assert summary["delivery"]["blocks"] == len(message["blocks"]) <= 6
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


# --------------------------------------------------------------------------------
# a run the container replacement killed, told apart from a run still working
# --------------------------------------------------------------------------------
# Measured 2026-09-25: run 20260924T030130...-91bada58 logged a start and died ten
# seconds later when a push replaced the container. Nothing can ever close it, so the
# readiness gate would have held Friday's report back for ever. Recording a `daily/end`
# would have fixed that by lying; this records what happened and re-checks it instead.

def _log(conn, run_id, event, at, stage="daily"):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO run_log (run_id, stage, event, details, created_at) "
                    "VALUES (%s, %s, %s, '{}'::jsonb, %s)", (run_id, stage, event, at))
    conn.commit()


def _killed_and_superseded(conn):
    """The shape of the real incident: one run starts and dies, a later one completes."""
    from tgtc_core.reporting import incidents

    _log(conn, "run-killed", "start", WEEK_START + timedelta(days=1))
    _log(conn, "run-recovery", "start", WEEK_START + timedelta(days=1, hours=2))
    _log(conn, "run-recovery", "end", WEEK_START + timedelta(days=1, hours=5))
    return incidents


def test_a_run_still_working_keeps_blocking_even_with_an_incident_on_file(conn):
    """The distinction has to survive the case it exists to exclude."""
    incidents = _killed_and_superseded(conn)
    incidents.record(conn, run_id="run-killed", superseded_by="run-recovery",
                     kind=incidents.KIND_CONTAINER_REPLACED, recorded_by="test")
    assert _ready(conn).ready is True, "the settled incident should not block"

    # now a genuinely active run: started, no end, and no incident claims otherwise
    _log(conn, "run-live", "start", WEEK_START + timedelta(days=2))
    state = _ready(conn)
    assert state.ready is False
    assert any("logged no end event yet" in b for b in state.blockers)
    assert state.detail["unfinished_runs"] == 1, "only the live run should be counted as in flight"
    assert state.detail["interrupted_runs_settled_as_incidents"] == ["run-killed"]


def test_an_interrupted_run_without_a_record_still_blocks(conn):
    _killed_and_superseded(conn)
    state = _ready(conn)
    assert state.ready is False and state.detail["unfinished_runs"] == 1


def test_it_blocks_again_the_moment_any_proof_stops_holding(conn):
    """A record is a claim, never a dismissal."""
    incidents = _killed_and_superseded(conn)
    incidents.record(conn, run_id="run-killed", superseded_by="run-recovery",
                     kind=incidents.KIND_CONTAINER_REPLACED, recorded_by="test")
    assert _ready(conn).ready is True

    # the run speaks again after the incident was filed: it was never gone. A live run
    # stamps its own events with the current time, which is what makes this detectable.
    with conn.cursor() as cur:
        cur.execute("INSERT INTO run_log (run_id, stage, event, details, created_at) "
                    "VALUES ('run-killed', 'daily', 'round', '{}'::jsonb, now())")
    conn.commit()
    state = _ready(conn)
    assert state.ready is False and state.detail["interrupted_runs_settled_as_incidents"] == []


def test_a_week_that_does_not_reconcile_is_never_dismissed_by_a_record(conn):
    incidents = _killed_and_superseded(conn)
    incidents.record(conn, run_id="run-killed", superseded_by="run-recovery",
                     kind=incidents.KIND_CONTAINER_REPLACED, recorded_by="test")
    report = report_for(conn)
    report["reconciliation"] = {**report["reconciliation"], "unexplained_difference": 3}
    window = pipeline.window_for(now=FRIDAY_MORNING)
    state = readiness(conn, window, report)
    assert state.ready is False
    assert state.detail["interrupted_runs_settled_as_incidents"] == []
    assert any("logged no end event yet" in b for b in state.blockers)


def test_the_record_refuses_what_it_can_see_is_untrue(conn):
    import pytest as _pytest
    from tgtc_core.reporting import incidents

    _log(conn, "run-closed", "start", WEEK_START + timedelta(days=1))
    _log(conn, "run-closed", "end", WEEK_START + timedelta(days=1, hours=1))
    _log(conn, "run-open", "start", WEEK_START + timedelta(days=2))
    _log(conn, "run-never-ended", "start", WEEK_START + timedelta(days=3))

    with _pytest.raises(ValueError, match="closed itself"):
        incidents.record(conn, run_id="run-closed", superseded_by="run-open", kind="container_replaced")
    with _pytest.raises(ValueError, match="has not completed"):
        incidents.record(conn, run_id="run-open", superseded_by="run-never-ended", kind="container_replaced")
    with _pytest.raises(ValueError, match="never logged a start"):
        incidents.record(conn, run_id="ghost", superseded_by="run-closed", kind="container_replaced")
    with _pytest.raises(ValueError, match="cannot supersede itself"):
        incidents.record(conn, run_id="run-open", superseded_by="run-open", kind="container_replaced")


def test_a_successor_that_ran_BEFORE_it_proves_nothing(conn):
    import pytest as _pytest
    from tgtc_core.reporting import incidents

    _log(conn, "earlier", "start", WEEK_START + timedelta(days=1))
    _log(conn, "earlier", "end", WEEK_START + timedelta(days=1, hours=1))
    _log(conn, "later-killed", "start", WEEK_START + timedelta(days=3))
    with _pytest.raises(ValueError, match="did not run after"):
        incidents.record(conn, run_id="later-killed", superseded_by="earlier", kind="container_replaced")


def test_the_incident_is_visible_in_the_report_not_silently_dropped(conn):
    incidents = _killed_and_superseded(conn)
    incidents.record(conn, run_id="run-killed", superseded_by="run-recovery",
                     kind=incidents.KIND_CONTAINER_REPLACED, recorded_by="test")
    report = report_for(conn)
    listed = report["runs"]["interrupted_runs"]
    assert [i["run_id"] for i in listed] == ["run-killed"]
    assert any("was interrupted" in f and "run-recovery" in f
               for f in report["alerts"]), "the week must say an incident happened"
