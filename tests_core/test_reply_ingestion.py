"""Bringing replies into the core: once, only ours, and never as a send.

The defect this path had to avoid is in the first test: the suppression set used to
contain a generic ``reply``, and 293 of the 540 most recent replies were out-of-office
auto-answers. Wiring replies in without fixing that would have permanently burned every
contact who happened to be on holiday.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tgtc_core.providers.instantly import InstantlyResult
from tgtc_core.services import replies
from tgtc_core.services.suppression import OUTCOME_SUPPRESSING_EVENTS, apply_outcome_event
from tests_core.helpers import sql1, sqlall
from tests_core.test_weekly_report import WEEK_START, seed_lead

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
OURS = "camp-pr"
THEIRS = "camp-control-legacy"


class FakeInbox:
    """Instantly's reply pages, and a record of how it was asked."""

    def __init__(self, pages, *, fail_after=None):
        self.pages = pages
        self.calls = []
        self.fail_after = fail_after

    def list_received_emails(self, *, limit=100, starting_after=None):
        self.calls.append(starting_after)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            return InstantlyResult(False, 500, message="boom")
        index = 0 if starting_after is None else int(starting_after)
        if index >= len(self.pages):
            return InstantlyResult(True, 200, data={"items": []})
        nxt = str(index + 1) if index + 1 < len(self.pages) else None
        return InstantlyResult(True, 200, data={"items": self.pages[index], "next_starting_after": nxt})


def reply(message_id, email, subject, body, *, campaign=OURS, i_status=0):
    return {"id": message_id, "from_address_email": email, "subject": subject,
            "body": {"text": body}, "campaign_id": campaign, "i_status": i_status,
            "timestamp_email": "2026-09-24T09:00:00Z"}


def lead(conn, email, *, campaign_key="product", campaign_id=OURS):
    return seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key=campaign_key,
                     campaign_id=campaign_id, email=email)


# --------------------------------------------------------------------------------
# the defect that had to be fixed first
# --------------------------------------------------------------------------------

def test_a_generic_reply_no_longer_suppresses_an_address(conn):
    assert "reply" not in OUTCOME_SUPPRESSING_EVENTS
    assert "out_of_office" not in OUTCOME_SUPPRESSING_EVENTS
    assert {"opt_out", "no_longer_here", "unsubscribe", "bounce"} <= OUTCOME_SUPPRESSING_EVENTS

    assert apply_outcome_event(conn, provider="instantly", event_type="reply", dedupe_key="k1",
                               email="someone@example.com") == "recorded"
    assert sql1(conn, "SELECT count(*) FROM suppressions") == 0
    assert apply_outcome_event(conn, provider="instantly", event_type="opt_out", dedupe_key="k2",
                               email="other@example.com") == "applied"
    assert sql1(conn, "SELECT count(*) FROM suppressions") == 1


# --------------------------------------------------------------------------------
# the poll
# --------------------------------------------------------------------------------

def test_only_the_nine_campaigns_are_ingested(conn):
    lead(conn, "ours@example.com")
    inbox = FakeInbox([[reply("m1", "ours@example.com", "Automatic reply", "Out of the office until Friday."),
                        reply("m2", "stranger@example.com", "Re:", "Not interested, please remove me.",
                              campaign=THEIRS)]])
    report = replies.poll(conn, inbox, campaign_ids=[OURS], now=NOW)
    assert report.seen == 2 and report.ours == 1 and report.skipped_other_campaigns == 1
    assert sqlall(conn, "SELECT event_type FROM outcome_events") == [{"event_type": "out_of_office"}]
    assert sql1(conn, "SELECT count(*) FROM suppressions") == 0


def test_re_polling_the_same_page_records_nothing_new(conn):
    lead(conn, "ours@example.com")
    page = [reply("m1", "ours@example.com", "Automatic reply", "Out of the office until Friday.")]
    first = replies.poll(conn, FakeInbox([page]), campaign_ids=[OURS], now=NOW)
    second = replies.poll(conn, FakeInbox([page]), campaign_ids=[OURS], now=NOW)
    assert (first.recorded, first.duplicates) == (1, 0)
    assert (second.recorded, second.duplicates) == (0, 1)
    assert sql1(conn, "SELECT count(*) FROM outcome_events") == 1
    assert sql1(conn, "SELECT count(*) FROM work_items WHERE kind = %s", (replies.FOLLOW_UP_WHEN_BACK,)) == 1


def test_an_out_of_office_waits_and_suppresses_nobody(conn):
    seeded = lead(conn, "away@example.com")
    inbox = FakeInbox([[reply("m1", "away@example.com", "Automatic reply",
                              "I am out of the office and will be back in the office on October 6.")]])
    report = replies.poll(conn, inbox, campaign_ids=[OURS], now=NOW)
    assert report.by_label == {"out_of_office": 1} and report.suppressed == 0
    assert sql1(conn, "SELECT count(*) FROM suppressions") == 0

    item = sqlall(conn, "SELECT kind, subject_kind, subject_id, available_at, waiting_on FROM work_items")[0]
    assert item["kind"] == replies.FOLLOW_UP_WHEN_BACK
    assert (item["subject_kind"], item["subject_id"]) == ("approval", seeded["approval_id"])
    assert item["available_at"].date().isoformat() == "2026-10-06", "the date in the reply is the date it waits for"
    assert item["waiting_on"] == "out_of_office"


def test_an_out_of_office_without_a_date_still_waits_rather_than_sending(conn):
    lead(conn, "away2@example.com")
    replies.poll(conn, FakeInbox([[reply("m1", "away2@example.com", "Automatic reply",
                                         "I am out of the office with limited access to email.")]]),
                 campaign_ids=[OURS], now=NOW)
    when = sql1(conn, "SELECT available_at FROM work_items WHERE kind = %s", (replies.FOLLOW_UP_WHEN_BACK,))
    assert when > NOW, "no date in the reply is not a reason to follow up immediately"


def test_a_departure_stops_that_address_and_queues_a_replacement_for_the_unit(conn):
    seeded = lead(conn, "gone@example.com")
    inbox = FakeInbox([[reply("m1", "gone@example.com", "No longer with the organization",
                              "Casey is no longer with the organization. Please reach out to Alex Moreau.")]])
    report = replies.poll(conn, inbox, campaign_ids=[OURS], now=NOW)
    assert report.by_label == {"no_longer_here": 1} and report.suppressed == 1

    suppression = sqlall(conn, "SELECT kind, key, reason, source FROM suppressions")[0]
    assert suppression == {"kind": "person_email", "key": "gone@example.com",
                           "reason": "no_longer_here", "source": "outcome:instantly"}
    item = sqlall(conn, "SELECT kind, subject_kind, subject_id, waiting_on FROM work_items")[0]
    assert item == {"kind": replies.REPLACE_DEPARTED_CONTACT, "subject_kind": "opportunity",
                    "subject_id": seeded["opportunity_id"], "waiting_on": "contact_departed"}
    # The named person is kept as evidence for whoever works the replacement, not acted on.
    payload = sql1(conn, "SELECT payload FROM outcome_events WHERE external_id = 'm1'")
    assert payload["covering_contact"] == "Alex Moreau"
    assert sql1(conn, "SELECT count(*) FROM delivery_outbox WHERE state = 'pending'") == 0


def test_an_opt_out_is_respected_immediately(conn):
    lead(conn, "stop@example.com")
    report = replies.poll(conn, FakeInbox([[reply("m1", "stop@example.com", "Re:", "Please remove me from your list.")]]),
                          campaign_ids=[OURS], now=NOW)
    assert report.by_label == {"opt_out": 1} and report.suppressed == 1
    assert sql1(conn, "SELECT reason FROM suppressions WHERE key = 'stop@example.com'") == "opt_out"


def test_a_human_reply_goes_to_review_and_never_to_a_send(conn):
    seeded = lead(conn, "human@example.com")
    report = replies.poll(conn, FakeInbox([[reply("m1", "human@example.com", "Re: hiring",
                                                  "Thanks - can you send pricing for the team?")]]),
                          campaign_ids=[OURS], now=NOW)
    assert report.by_label == {"human_reply": 1} and report.suppressed == 0
    item = sqlall(conn, "SELECT kind, subject_id, waiting_on FROM work_items")[0]
    assert item == {"kind": replies.REVIEW_REPLY, "subject_id": seeded["approval_id"], "waiting_on": "human_reply"}
    assert sql1(conn, "SELECT count(*) FROM suppressions") == 0


def test_a_reply_from_an_address_the_core_does_not_know_is_recorded_and_queues_nothing(conn):
    report = replies.poll(conn, FakeInbox([[reply("m1", "unknown@example.com", "Automatic reply",
                                                  "Out of the office.")]]),
                          campaign_ids=[OURS], now=NOW)
    assert report.unmatched_people == 1 and report.recorded == 1
    assert sql1(conn, "SELECT count(*) FROM work_items") == 0


# --------------------------------------------------------------------------------
# the cursor
# --------------------------------------------------------------------------------

def test_each_poll_starts_at_the_top_because_the_feed_is_newest_first(conn):
    """Resuming from the last cursor would walk further into history and never see the
    replies that arrived since."""
    lead(conn, "a@example.com")
    lead(conn, "b@example.com")
    pages = [[reply("m1", "a@example.com", "Automatic reply", "Out of the office.")],
             [reply("m2", "b@example.com", "Re:", "Please remove me from your list.")]]
    inbox = FakeInbox(pages)
    replies.poll(conn, inbox, campaign_ids=[OURS], now=NOW, max_pages=1)
    assert inbox.calls == [None]
    assert replies.read_cursor(conn) == "1"

    # The next hour: the top again, not page two.
    again = FakeInbox(pages)
    replies.poll(conn, again, campaign_ids=[OURS], now=NOW)
    assert again.calls[0] is None

    # ... and a sweep that IS asked to resume continues into the older page.
    deeper = FakeInbox(pages)
    resumed = replies.poll(conn, deeper, campaign_ids=[OURS], now=NOW, resume=True)
    assert deeper.calls[0] == "1" and resumed.by_label == {"opt_out": 1}


def test_a_poll_stops_as_soon_as_a_page_holds_nothing_new(conn):
    lead(conn, "a@example.com")
    lead(conn, "b@example.com")
    pages = [[reply("m1", "a@example.com", "Automatic reply", "Out of the office.")],
             [reply("m2", "b@example.com", "Re:", "Please remove me from your list.")]]
    replies.poll(conn, FakeInbox(pages), campaign_ids=[OURS], now=NOW)      # reads both pages
    inbox = FakeInbox(pages)
    second = replies.poll(conn, inbox, campaign_ids=[OURS], now=NOW)
    assert second.stopped_at_known_ground is True
    assert inbox.calls == [None], "it should not have asked for the second page"
    assert second.duplicates == 1 and second.recorded == 0


def test_a_page_that_fails_does_not_move_the_cursor(conn):
    lead(conn, "a@example.com")
    inbox = FakeInbox([[reply("m1", "a@example.com", "Automatic reply", "Out of the office.")]], fail_after=0)
    report = replies.poll(conn, inbox, campaign_ids=[OURS], now=NOW)
    assert report.seen == 0 and replies.read_cursor(conn) is None


def test_a_dry_run_reads_and_decides_but_writes_nothing(conn):
    lead(conn, "away@example.com")
    inbox = FakeInbox([[reply("m1", "away@example.com", "Automatic reply", "Out of the office until October 6."),
                        reply("m2", "away@example.com", "Re:", "Please remove me from your list.")]])
    report = replies.poll(conn, inbox, campaign_ids=[OURS], now=NOW, dry_run=True)
    assert report.dry_run is True and report.ours == 2
    assert report.by_label == {"out_of_office": 1, "opt_out": 1}
    assert sql1(conn, "SELECT count(*) FROM outcome_events") == 0
    assert sql1(conn, "SELECT count(*) FROM suppressions") == 0
    assert sql1(conn, "SELECT count(*) FROM work_items") == 0
    assert replies.read_cursor(conn) is None


def test_a_suppressed_address_is_not_re_acquired(conn):
    """The suppression a departure writes is the same one acquisition already consults,
    so the person cannot come back in through the next run."""
    from tgtc_core.services.suppression import check

    lead(conn, "gone@example.com")
    replies.poll(conn, FakeInbox([[reply("m1", "gone@example.com", "Re:", "She is no longer with the company.")]]),
                 campaign_ids=[OURS], now=NOW)
    assert check(conn, email="gone@example.com") == ["person_email:gone@example.com"]
    assert check(conn, email="someone-else@example.com") == []
