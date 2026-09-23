"""The weekly report: the window, the KPI, the reconciliation and the delivery rule.

What these tests protect, in the order a reader of the report would care about it:

* the window is the agreed one -- Friday 00:00 to Friday 00:00 America/Los_Angeles,
  end exclusive -- and it stays that way across a DST change;
* the number Brett and Roman read is genuine net-new people, so an ``existing``
  answer, a rejection, a Control campaign and the same person twice are all excluded;
* the Airtable/Instantly difference is explained by name rather than smoothed over,
  which is the 2026-09-23 case (1,045 records, 1,013 creations) written down as a test;
* a missing production day is FLAGGED, never reported as a day that produced zero;
* a week is delivered at most once however many times the job fires;
* the summary carries no personal data, and the lead-level export refuses to be
  written inside the repository.
"""

from __future__ import annotations

import json
import itertools
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tgtc_core.db.connection import jsonb
from tgtc_core.policy.campaigns import CAMPAIGN_BY_KEY, CAMPAIGNS
from tgtc_core.reporting import export, pipeline, render, store
from tgtc_core.reporting.window import (PACIFIC_TZ_NAME, explicit_window, is_due, partial_window,
                                        resolve_timezone, weekly_window)

UTC = timezone.utc

#: Friday 2026-09-18, 07:00 Pacific (14:00 UTC): the moment a Friday-morning report is
#: produced. The week that just closed is 2026-09-11 -> 2026-09-18.
FRIDAY_MORNING = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)
WEEK_START = datetime(2026, 9, 11, 7, 0, tzinfo=UTC)   # Fri 00:00 PDT
WEEK_END = datetime(2026, 9, 18, 7, 0, tzinfo=UTC)


# --------------------------------------------------------------------------------
# seeding: a complete lead chain with timestamps a test chooses
# --------------------------------------------------------------------------------

_SEQ = itertools.count(1)


def seed_lead(conn, *, received_at, campaign_key="customer_experience", campaign_id="camp-cx",
              instantly_kind="created", instantly_campaign=None, airtable=True, approved_at=None,
              email=None, run_id="run-1", employer=None, person=None, outreach_eligible=True,
              blocked_reason=None, employer_id=None, person_id=None):
    """One approval with the delivery receipts a report reads. Returns its ids."""
    campaign = CAMPAIGN_BY_KEY[campaign_key]
    function_key = campaign.functions[0]
    approved_at = approved_at or (received_at - timedelta(minutes=5))
    serial = next(_SEQ)
    email = email or f"person{serial}@example.com"
    employer = employer or f"Employer {serial}"
    person = person or f"person{serial}"
    domain = f"employer{serial}.example.com"
    with conn.cursor() as cur:
        if employer_id is None:
            cur.execute("INSERT INTO employers (canonical_name, name_key, domain) VALUES (%s, %s, %s) RETURNING id",
                        (employer, employer.lower().replace(" ", "-"), domain))
            employer_id = int(cur.fetchone()["id"])
        if person_id is None:
            cur.execute("INSERT INTO people (first_name, last_name, title, employer_id, email, email_status, "
                        "email_verified_at, contact_country) VALUES (%s, 'Doe', 'Head of Support', %s, %s, "
                        "'verified', %s, 'US') RETURNING id", (person, employer_id, email, approved_at))
            person_id = int(cur.fetchone()["id"])
        cur.execute("INSERT INTO opportunities (employer_id, function_key, campaign_key, created_at) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (employer_id, function_key) DO UPDATE SET updated_at = now() "
                    "RETURNING id", (employer_id, function_key, campaign_key, approved_at))
        opportunity_id = int(cur.fetchone()["id"])
        lead_key = f"{opportunity_id}-{person_id}-{campaign_id}"
        cur.execute(
            "INSERT INTO approvals (opportunity_id, person_id, employer_id, campaign_key, function_key, campaign_id, "
            "policy_version, lead_key, fingerprint, lead_json, run_id, approved_at, outreach_eligible, contact_country) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'tgtc-core/3-exhaustive-nine', %s, 'fp', %s, %s, %s, %s, 'US') RETURNING id",
            (opportunity_id, person_id, employer_id, campaign_key, function_key, campaign_id, lead_key,
             jsonb({"email": email}), run_id, approved_at, outreach_eligible))
        approval_id = int(cur.fetchone()["id"])
        if instantly_kind is not None:
            state = "delivered" if instantly_kind in ("created", "reconciled") else "blocked"
            cur.execute("INSERT INTO delivery_outbox (approval_id, channel, idempotency_key, payload_json, state, "
                        "blocked_reason) VALUES (%s, 'instantly', %s, %s, %s, %s) RETURNING id",
                        (approval_id, f"i-{lead_key}", jsonb({"email": email}), state, blocked_reason))
            outbox_id = int(cur.fetchone()["id"])
            cur.execute("INSERT INTO delivery_receipts (outbox_id, channel, receipt_kind, external_id, "
                        "external_campaign, received_at, response_summary) VALUES (%s, 'instantly', %s, %s, %s, %s, %s)",
                        (outbox_id, instantly_kind, f"lead-{approval_id}", instantly_campaign or campaign_id,
                         received_at, jsonb({"membership": blocked_reason} if blocked_reason else {})))
        if airtable:
            cur.execute("INSERT INTO delivery_outbox (approval_id, channel, idempotency_key, payload_json, state) "
                        "VALUES (%s, 'airtable', %s, %s, 'delivered') RETURNING id",
                        (approval_id, f"a-{lead_key}", jsonb({"email": email})))
            air_id = int(cur.fetchone()["id"])
            cur.execute("INSERT INTO delivery_receipts (outbox_id, channel, receipt_kind, external_id, received_at) "
                        "VALUES (%s, 'airtable', 'created', %s, %s)", (air_id, f"rec{approval_id}", received_at))
    conn.commit()
    return {"approval_id": approval_id, "person_id": person_id, "employer_id": employer_id,
            "opportunity_id": opportunity_id, "email": email}


def seed_run(conn, *, run_id, started_at, stop_reason="target_reached"):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO run_log (run_id, stage, event, details, created_at) VALUES (%s, 'daily', 'start', "
                    "'{}'::jsonb, %s)", (run_id, started_at))
        cur.execute("INSERT INTO run_log (run_id, stage, event, details, created_at) VALUES (%s, 'daily', 'end', %s, %s)",
                    (run_id, jsonb({"stop_reason": stop_reason}), started_at + timedelta(hours=2)))
    conn.commit()


def report_for(conn, *, now=FRIDAY_MORNING, **kwargs):
    return pipeline.build(conn, now=now, compare_previous=False, **kwargs)


# --------------------------------------------------------------------------------
# the window
# --------------------------------------------------------------------------------

def test_the_week_is_friday_to_friday_pacific_and_end_exclusive():
    w = weekly_window(FRIDAY_MORNING)
    assert (w.start_utc, w.end_utc) == (WEEK_START, WEEK_END)
    assert w.start_local.hour == 0 and w.end_local.hour == 0
    assert w.start_local.strftime("%a") == "Fri" and w.end_local.strftime("%a") == "Fri"
    assert w.contains(WEEK_START) and not w.contains(WEEK_END)      # half-open
    assert w.timezone_name == PACIFIC_TZ_NAME
    assert w.report_id == "weekly-2026-09-11"


def test_a_report_written_on_friday_morning_covers_the_week_that_just_closed():
    for hour in (7, 8, 12, 23):     # any time on the delivery day resolves to the same week
        w = weekly_window(datetime(2026, 9, 18, hour, 0, tzinfo=UTC))
        assert w.report_id == "weekly-2026-09-11"
    # ... and one minute before the boundary it is still the week before that.
    assert weekly_window(datetime(2026, 9, 18, 6, 59, tzinfo=UTC)).report_id == "weekly-2026-09-04"


def test_the_window_holds_its_local_hour_across_a_dst_change():
    # The week containing 2026-11-01 (PDT -> PST) is 169 real hours, both ends 00:00 local.
    w = weekly_window(datetime(2026, 11, 6, 16, 0, tzinfo=UTC))
    assert w.start_local.hour == 0 and w.end_local.hour == 0
    assert w.duration_hours == 169.0
    assert w.start_utc.hour == 7 and w.end_utc.hour == 8      # PDT then PST


def test_this_module_and_the_legacy_window_agree_instant_for_instant():
    """The legacy report defined the agreed window; this one must not drift from it."""
    legacy = pytest.importorskip("weekly_report.timewindow")
    moment = datetime(2026, 1, 2, 9, 0, tzinfo=UTC)
    for _ in range(80):
        mine = weekly_window(moment)
        theirs = legacy.weekly_window(moment)
        assert (mine.start_utc, mine.end_utc) == (theirs.start_utc, theirs.end_utc), moment
        moment += timedelta(days=4, hours=5)


def test_a_partial_week_never_reaches_past_now_and_says_it_is_partial():
    now = datetime(2026, 9, 23, 5, 30, tzinfo=UTC)      # Wednesday
    w = partial_window(now)
    assert w.kind == "partial" and w.report_id == "partial-2026-09-18"
    assert w.end_utc == now and w.start_utc == datetime(2026, 9, 18, 7, 0, tzinfo=UTC)


def test_due_is_decided_in_the_reports_timezone_not_in_utc():
    # Saturday 02:00 UTC is still FRIDAY 19:00 in Pacific: due.
    assert is_due(datetime(2026, 9, 19, 2, 0, tzinfo=UTC), hour=7)
    # Friday 06:00 UTC is Thursday 23:00 Pacific: not due.
    assert not is_due(datetime(2026, 9, 18, 6, 0, tzinfo=UTC), hour=7)
    assert is_due(datetime(2026, 9, 18, 14, 0, tzinfo=UTC), hour=7)
    assert not is_due(datetime(2026, 9, 18, 13, 0, tzinfo=UTC), hour=7)   # 06:00 PDT, before 07:00


def test_an_explicit_week_can_be_re_issued_unchanged():
    w = explicit_window(date(2026, 9, 11), now=FRIDAY_MORNING)
    assert (w.start_utc, w.end_utc) == (WEEK_START, WEEK_END)
    assert w.report_id == "weekly-2026-09-11"


# --------------------------------------------------------------------------------
# the KPI
# --------------------------------------------------------------------------------

def test_only_genuine_creations_in_the_routed_campaign_count(conn):
    inside = WEEK_START + timedelta(days=1)
    seed_lead(conn, received_at=inside, campaign_key="customer_experience", campaign_id="camp-cx")
    seed_lead(conn, received_at=inside, campaign_key="product", campaign_id="camp-pr")
    seed_lead(conn, received_at=inside, campaign_key="finance", campaign_id="camp-fi",
              instantly_kind="existing", blocked_reason=None)
    seed_lead(conn, received_at=inside, campaign_key="finance", campaign_id="camp-fi",
              instantly_kind="rejected", blocked_reason="instantly_existing_other_campaign",
              instantly_campaign="camp-other")
    seed_lead(conn, received_at=WEEK_END + timedelta(hours=1), campaign_key="product", campaign_id="camp-pr")
    report = report_for(conn)
    d = report["delivery"]
    assert d["instantly_created_unique_people"] == 2        # the two created, inside the window
    assert d["instantly_already_existing"] == 1
    assert d["instantly_rejected"] == 1
    assert d["instantly_rejected_by_reason"] == {"instantly_existing_other_campaign": 1}


def test_one_person_in_two_campaigns_is_one_net_new_lead(conn):
    """The schema already allows only one ACTIVE approval per person, so the way a
    person can reach two campaigns is a revoked approval followed by a new one. Both
    were genuinely created in Instantly; the target counts the PERSON once."""
    inside = WEEK_START + timedelta(days=2)
    first = seed_lead(conn, received_at=inside, campaign_key="product", campaign_id="camp-pr",
                      email="dup@example.com")
    conn.execute("UPDATE approvals SET state = 'revoked' WHERE id = %s", (first["approval_id"],))
    conn.commit()
    seed_lead(conn, received_at=inside + timedelta(hours=1), campaign_key="finance", campaign_id="camp-fi",
              email="dup@example.com", person_id=first["person_id"])
    report = report_for(conn)
    assert report["delivery"]["instantly_created_unique_people"] == 1
    assert report["delivery"]["instantly_created_approvals"] == 2     # two rows, one PERSON


def test_a_creation_in_a_control_campaign_is_excluded_and_flagged(conn):
    from tgtc_core.policy.campaigns import KNOWN_CONTROL_CAMPAIGN_IDS

    control = sorted(KNOWN_CONTROL_CAMPAIGN_IDS)[0]
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product", campaign_id=control,
              instantly_campaign=control)
    report = report_for(conn)
    assert report["delivery"]["instantly_creations_into_a_control_campaign"] == 1
    assert any("CONTROL" in flag for flag in report["flags"])


def test_every_one_of_the_nine_campaigns_is_reported(conn):
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="gtm_systems", campaign_id="camp-gtm")
    campaigns = report_for(conn)["by_campaign"]["campaigns"]
    assert set(campaigns) == {c.key for c in CAMPAIGNS} and len(campaigns) == 9
    assert campaigns["gtm_systems"]["instantly_created_unique_people"] == 1
    assert campaigns["finance"]["instantly_created_unique_people"] == 0     # a measured zero, still reported


# --------------------------------------------------------------------------------
# the reconciliation the 2026-09-23 run needed
# --------------------------------------------------------------------------------

def test_airtable_records_reconcile_to_creations_plus_named_reasons(conn):
    """1,045 Airtable records and 1,013 creations, in miniature: the difference is
    named, and the identity closes."""
    inside = WEEK_START + timedelta(days=1)
    seed_lead(conn, received_at=inside, campaign_key="product", campaign_id="camp-pr")
    seed_lead(conn, received_at=inside, campaign_key="product", campaign_id="camp-pr",
              instantly_kind="rejected", blocked_reason="not_delivered:instantly_existing_other_campaign",
              instantly_campaign="camp-other")
    seed_lead(conn, received_at=inside, campaign_key="finance", campaign_id="camp-fi", instantly_kind="existing")
    recon = report_for(conn)["reconciliation"]
    assert recon["airtable_records_created"] == 3
    assert recon["genuine_instantly_creations_approvals"] == 1
    assert recon["airtable_records_without_a_genuine_creation"] == 2
    assert recon["unexplained_difference"] == 0 and recon["identity_holds"] is True
    assert set(recon["airtable_records_without_a_creation_by_reason"]) == {
        "not_delivered:instantly_existing_other_campaign", "existing"}
    assert any("no genuine Instantly creation behind them" in flag for flag in report_for(conn)["flags"])


def test_the_legacy_review_population_is_separate_and_never_counted_as_delivered(conn):
    inside = WEEK_START + timedelta(days=1)
    seed_lead(conn, received_at=inside, campaign_key="product", campaign_id="camp-pr")
    seed_lead(conn, received_at=datetime(2026, 8, 1, 12, tzinfo=UTC), campaign_key="finance", campaign_id="camp-fi",
              instantly_kind="rejected", blocked_reason="failed_compliance_gate")
    report = report_for(conn)
    legacy = report["legacy_airtable_review"]
    assert legacy["records"] == 1 and legacy["by_reason"] == {"failed_compliance_gate": 1}
    assert "not counted as delivered leads" in legacy["handling"]
    # It is NOT in this week's delivered figure, and this week's own count is untouched.
    assert report["delivery"]["instantly_created_unique_people"] == 1
    assert report["cumulative"]["instantly_created_unique_people"] == 1


# --------------------------------------------------------------------------------
# runs, backlog and flags
# --------------------------------------------------------------------------------

def test_a_day_with_no_run_is_flagged_as_missing_not_as_zero_production(conn):
    seed_run(conn, run_id="run-a", started_at=WEEK_START + timedelta(days=1, hours=3))
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1, hours=4), campaign_key="product",
              campaign_id="camp-pr", run_id="run-a")
    report = report_for(conn)
    assert report["runs"]["run_count"] == 1 and report["runs"]["completed_runs"] == 1
    missing = report["runs"]["local_days_without_a_run"]
    # The run started 03:00 Pacific on Saturday 2026-09-12. 09-11 is before this
    # database holds anything at all, so it is UNAVAILABLE rather than missing -- the
    # five days after the run are the ones that genuinely had none.
    assert "2026-09-12" not in missing and "2026-09-11" not in missing
    assert missing == ["2026-09-13", "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17"]
    assert report["coverage"]["local_days_unavailable"] == ["2026-09-11"]
    assert report["daily"][0] == {"date": "2026-09-11", "weekday": "Fri", "unavailable": True,
                                  "instantly_created_unique_people": None, "contacts_approved": None,
                                  "new_jobs": None, "apollo_credits": None, "fantastic_records": None}
    assert any("reported as unavailable, not as zero production" in note for note in report["notes"])
    assert any("missing run, not a zero-production day" in flag for flag in report["flags"])
    # A missing run is an ALERT: the readers must see it, and a reconciled report is
    # neither withheld nor failed for a gap that waiting cannot fix. Integrity is kept
    # for what is genuinely unexplained.
    assert report["integrity_alerts"] == []
    assert len([a for a in report["alerts"] if "missing run" in a]) == 5
    assert report["status"] == "attention"


def test_backlog_creations_are_separated_from_this_weeks_own_production(conn):
    inside = WEEK_START + timedelta(days=1)
    seed_lead(conn, received_at=inside, campaign_key="product", campaign_id="camp-pr",
              approved_at=WEEK_START - timedelta(days=3))          # approved before the window: backlog
    seed_lead(conn, received_at=inside, campaign_key="finance", campaign_id="camp-fi",
              approved_at=inside - timedelta(hours=1))             # this week's own
    b = report_for(conn)["backlog"]
    assert b["created_from_earlier_approvals_backlog"] == 1
    assert b["created_from_this_weeks_approvals"] == 1


def test_an_approval_with_no_instantly_answer_is_reported_as_carried_out(conn):
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product", campaign_id="camp-pr",
              instantly_kind=None, airtable=False, approved_at=WEEK_START + timedelta(days=1))
    report = report_for(conn)
    assert report["backlog"]["approved_this_week_not_yet_delivered"] == 1
    assert any("no Instantly answer yet" in flag for flag in report["flags"])


def test_a_blocked_approval_is_held_with_a_reason_not_counted_as_waiting(conn):
    """The first production rehearsal counted 594 approvals as "waiting" when 446 of
    them had already been blocked by the compliance gate. A decision is not a wait."""
    inside = WEEK_START + timedelta(days=1)
    seed_lead(conn, received_at=inside, campaign_key="product", campaign_id="camp-pr",
              instantly_kind=None, airtable=False, approved_at=inside)                    # genuinely waiting
    seed_lead(conn, received_at=inside, campaign_key="finance", campaign_id="camp-fi",
              instantly_kind=None, airtable=False, approved_at=inside, outreach_eligible=False)
    third = seed_lead(conn, received_at=inside, campaign_key="ecommerce", campaign_id="camp-ec",
                      instantly_kind=None, airtable=False, approved_at=inside)
    conn.execute("INSERT INTO delivery_outbox (approval_id, channel, idempotency_key, payload_json, state, "
                 "blocked_reason) VALUES (%s, 'instantly', 'blocked-1', '{}'::jsonb, 'blocked', "
                 "'compliance:unknown_jurisdiction:absent')", (third["approval_id"],))
    conn.commit()
    b = report_for(conn)["backlog"]
    assert b["approved_this_week_not_yet_delivered"] == 1
    assert b["approved_this_week_held_with_a_named_reason"] == 2


def test_a_day_below_the_minimum_is_named_in_the_flags(conn):
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product", campaign_id="camp-pr")
    report = pipeline.build(conn, now=FRIDAY_MORNING, compare_previous=False, target_per_run=1000)
    assert any("below the 1000 minimum" in flag for flag in report["flags"])


def test_dollar_cost_is_not_invented_when_a_unit_price_is_not_verified(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO credit_events (provider, operation, requests, estimated_credits, basis, created_at) "
                    "VALUES ('apollo', 'match', 10, 14.0, 'estimate', %s)", (WEEK_START + timedelta(days=1),))
    conn.commit()
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product", campaign_id="camp-pr")
    report = report_for(conn)
    apollo = report["spend"]["providers"]["apollo"]
    assert apollo["credits_per_final_lead"] == 14.0
    assert apollo["spend_usd"] is None and "NOT VERIFIED" in apollo["unit_price_basis"]
    assert report["spend"]["cost_per_final_lead_usd"] is None
    priced = pipeline.build(conn, now=FRIDAY_MORNING, compare_previous=False, unit_prices={"apollo": 0.016})
    assert priced["spend"]["providers"]["apollo"]["spend_usd"] == 0.22
    assert priced["spend"]["cost_per_final_lead_usd"] == 0.224


# --------------------------------------------------------------------------------
# the funnel stages
# --------------------------------------------------------------------------------

def test_jobs_reviewed_qualified_rejected_and_pending_are_counted_in_their_own_unit(conn):
    inside = WEEK_START + timedelta(days=1)
    with conn.cursor() as cur:
        for i, (excluded, functions, reason) in enumerate((
                (False, ["product"], None), (True, [], "agency"), (True, [], "agency"), (False, [], None))):
            cur.execute("INSERT INTO postings (source, provider_job_id, content_hash, commercial_age_anchor, "
                        "first_seen_at, title) VALUES ('linkedin', %s, %s, %s, %s, 'Head of Product') RETURNING id",
                        (f"job-{i}", f"hash-{i}", inside, inside))
            posting_id = int(cur.fetchone()["id"])
            cur.execute("INSERT INTO classifications (posting_id, policy_version, method, compatible_functions, "
                        "excluded, exclusion_reason, created_at) VALUES (%s, 'tgtc-core/3-exhaustive-nine', "
                        "'deterministic', %s, %s, %s, %s)", (posting_id, functions, excluded, reason, inside))
        cur.execute("INSERT INTO postings (source, provider_job_id, content_hash, commercial_age_anchor, "
                    "first_seen_at) VALUES ('linkedin', 'job-pending', 'h', %s, %s)", (inside, inside))
    conn.commit()
    j = report_for(conn)["jobs"]
    assert j["new_jobs_unique"] == 5
    assert j["jobs_reviewed"] == 4
    assert j["jobs_qualified"] == 1
    assert j["jobs_rejected_excluded"] == 2
    assert j["jobs_no_campaign_fit"] == 1
    assert j["jobs_pending_review"] == 1
    assert j["rejection_reasons"] == {"agency": 2}
    assert "first seen in this window" in j["definitions"]["new_jobs_unique"]


def test_compliance_blocked_contacts_are_reported_beside_approvals_never_inside_them(conn):
    inside = WEEK_START + timedelta(days=1)
    seed_lead(conn, received_at=inside, campaign_key="product", campaign_id="camp-pr")
    seed_lead(conn, received_at=inside, campaign_key="finance", campaign_id="camp-fi", instantly_kind=None,
              airtable=False, outreach_eligible=False)
    c = report_for(conn)["contacts"]
    assert c["contacts_approved"] == 2          # both are approved capacity
    assert c["compliance_blocked"] == 1         # one of them can never be sent to
    assert c["compliance_blocked_by_reason"] == {"compliance:outreach_eligibility_unknown": 1}


def test_the_cumulative_view_is_labelled_and_larger_than_the_week(conn):
    seed_lead(conn, received_at=datetime(2026, 8, 20, 12, tzinfo=UTC), campaign_key="product", campaign_id="camp-pr")
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="finance", campaign_id="camp-fi")
    report = report_for(conn)
    assert report["delivery"]["instantly_created_unique_people"] == 1
    assert report["cumulative"]["instantly_created_unique_people"] == 2
    assert "not this week" in report["cumulative"]["definition"]


def test_the_previous_week_is_compared_when_asked(conn):
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product", campaign_id="camp-pr")
    seed_lead(conn, received_at=WEEK_START - timedelta(days=2), campaign_key="finance", campaign_id="camp-fi")
    report = pipeline.build(conn, now=FRIDAY_MORNING, compare_previous=True)
    comparison = report["previous_week"]["metrics"]["instantly_created_unique_people"]
    assert comparison == {"this_week": 1, "previous_week": 1, "change": 0}


# --------------------------------------------------------------------------------
# privacy, export and delivery
# --------------------------------------------------------------------------------

def test_the_summary_carries_no_personal_data(conn):
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product", campaign_id="camp-pr",
              email="jane.doe@acme-corp.com", person="jane.doe")
    text = render.render_text(report_for(conn))
    assert "@" not in text and "jane" not in text.lower()
    assert "no personal data appears in this summary" in text.lower()


def test_the_lead_export_traces_each_lead_and_is_deduplicated_by_person(conn, tmp_path):
    inside = WEEK_START + timedelta(days=1)
    first = seed_lead(conn, received_at=inside, campaign_key="product", campaign_id="camp-pr",
                      email="dup@example.com")
    conn.execute("UPDATE approvals SET state = 'revoked' WHERE id = %s", (first["approval_id"],))
    conn.commit()
    seed_lead(conn, received_at=inside + timedelta(hours=2), campaign_key="finance", campaign_id="camp-fi",
              email="dup@example.com", person_id=first["person_id"])
    seed_lead(conn, received_at=inside, campaign_key="gtm_systems", campaign_id="camp-gtm")
    rows = export.lead_rows(conn, weekly_window(FRIDAY_MORNING))
    assert len(rows) == 2                                   # one row per PERSON
    assert len({r["email"] for r in rows}) == 2
    row = [r for r in rows if r["email"] == "dup@example.com"][0]
    assert row["campaign"] in {"product", "finance"} and row["employer"] and row["approval_id"]
    assert row["instantly_lead_id"] and row["airtable_record_id"]
    written = export.write_csv(tmp_path / "leads.csv", rows)
    assert written.exists() and "dup@example.com" in written.read_text(encoding="utf-8")


def test_the_lead_export_refuses_to_be_written_inside_the_repository(conn):
    with pytest.raises(ValueError, match="must not be written inside the repository"):
        export.write_csv(Path(export.REPO_ROOT) / "leads.csv", [])


MESSAGE = {"blocks": [{"type": "section"}], "text": "TGTC weekly pipeline"}


def test_a_week_is_delivered_at_most_once_per_channel_however_often_the_job_fires(conn):
    """The retry fires every twenty minutes between 06:00 and 07:00 Pacific. That is
    only safe because a second attempt after a successful one sends nothing."""
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product", campaign_id="camp-pr")
    store.ensure_schema(conn)
    report = report_for(conn)
    store.save(conn, report)
    sent = []

    def sender(channel, message):
        sent.append((channel, message))
        return {"transport": "test", "channel": channel}

    first = store.deliver_once(conn, report, sender=sender, channel="#gtm-engineering", message=MESSAGE)
    for _ in range(3):          # the 06:20, 06:40 and 07:00 retries
        again = store.deliver_once(conn, report, sender=sender, channel="#gtm-engineering", message=MESSAGE)
    assert first["sent"] is True and again["sent"] is False
    assert again["reason"] == "already_delivered"
    assert len(sent) == 1
    # Re-measuring the same week never erases the fact that it was sent.
    store.save(conn, report_for(conn))
    assert store.delivery_record(conn, report["window"]["report_id"], "#gtm-engineering", store.FINAL) is not None
    assert store.delivered(conn, report["window"]["report_id"]) is not None
    # An explicit resend is possible, and is never automatic.
    assert store.deliver_once(conn, report, sender=sender, channel="#gtm-engineering", message=MESSAGE,
                              resend=True)["sent"] is True
    assert len(sent) == 2


def test_the_same_week_may_still_reach_a_different_channel(conn):
    """Idempotency is per week AND per destination: a channel that never received the
    report has not received it."""
    store.ensure_schema(conn)
    report = report_for(conn)
    store.save(conn, report)
    sender = lambda channel, message: {"channel": channel}          # noqa: E731
    assert store.deliver_once(conn, report, sender=sender, channel="#gtm-engineering", message=MESSAGE)["sent"]
    assert store.deliver_once(conn, report, sender=sender, channel="#another", message=MESSAGE)["sent"]
    assert not store.deliver_once(conn, report, sender=sender, channel="#gtm-engineering", message=MESSAGE)["sent"]


def test_a_status_notice_never_stands_in_for_the_final_report(conn):
    """The 07:00 notice says the data has not closed. When it closes, the real report
    still goes out -- and the notice itself is not posted twice."""
    store.ensure_schema(conn)
    report = report_for(conn)
    store.save(conn, report)
    sent = []
    sender = lambda channel, message: sent.append((channel, message["text"])) or {"channel": channel}  # noqa: E731

    notice = store.deliver_once(conn, report, sender=sender, channel="#gtm-engineering",
                                message={"blocks": [], "text": "delayed"}, kind=store.STATUS_NOTICE)
    repeat = store.deliver_once(conn, report, sender=sender, channel="#gtm-engineering",
                                message={"blocks": [], "text": "delayed"}, kind=store.STATUS_NOTICE)
    final = store.deliver_once(conn, report, sender=sender, channel="#gtm-engineering", message=MESSAGE)
    assert [notice["sent"], repeat["sent"], final["sent"]] == [True, False, True]
    assert [text for _, text in sent] == ["delayed", "TGTC weekly pipeline"]
    assert store.delivered(conn, report["window"]["report_id"]) is not None       # only the FINAL marks the week


def test_the_destination_basis_is_recorded_on_every_delivery(conn):
    """A webhook's target cannot be introspected, so how the destination was
    established is part of the receipt rather than an assumption."""
    store.ensure_schema(conn)
    report = report_for(conn)
    store.save(conn, report)
    store.deliver_once(conn, report, sender=lambda c, m: {"ok": True}, channel="#gtm-engineering",
                       message=MESSAGE, destination_basis="slack_api:C0123456789")
    row = store.delivery_record(conn, report["window"]["report_id"], "#gtm-engineering", store.FINAL)
    assert row["destination_basis"] == "slack_api:C0123456789"


def test_re_measuring_a_week_to_date_moves_its_stored_end(conn):
    """A partial window ends at its cutoff. Storing it twice must not leave the first
    run's end behind on a row whose payload covers more."""
    store.ensure_schema(conn)
    first = pipeline.build(conn, now=datetime(2026, 9, 22, 12, tzinfo=UTC), kind="partial", compare_previous=False)
    store.save(conn, first)
    later = pipeline.build(conn, now=datetime(2026, 9, 23, 5, tzinfo=UTC), kind="partial", compare_previous=False)
    store.save(conn, later)
    row = store.get(conn, "partial-2026-09-18")
    assert row["window_end"].astimezone(UTC) == datetime(2026, 9, 23, 5, tzinfo=UTC)
    assert row["payload_json"]["window"]["window_end_utc"] == "2026-09-23T05:00:00Z"


def test_a_partial_week_is_never_delivered(conn):
    store.ensure_schema(conn)
    report = pipeline.build(conn, now=datetime(2026, 9, 23, 5, 30, tzinfo=UTC), kind="partial", compare_previous=False)
    store.save(conn, report)
    with pytest.raises(store.DeliveryRefused, match="closed week"):
        store.deliver_once(conn, report, sender=lambda c, m: {}, channel="#gtm-engineering", message=MESSAGE)


def test_a_report_that_was_never_stored_is_not_sent(conn):
    store.ensure_schema(conn)
    report = report_for(conn)
    with pytest.raises(store.DeliveryRefused, match="saved before it is sent"):
        store.deliver_once(conn, report, sender=lambda c, m: {}, channel="#gtm-engineering", message=MESSAGE)


def test_a_failed_send_is_not_recorded_as_delivered(conn):
    store.ensure_schema(conn)
    report = report_for(conn)
    store.save(conn, report)

    def refuse(channel, message):
        raise RuntimeError("HTTP 500")

    with pytest.raises(RuntimeError):
        store.deliver_once(conn, report, sender=refuse, channel="#gtm-engineering", message=MESSAGE)
    assert store.delivered(conn, report["window"]["report_id"]) is None
    assert store.delivery_record(conn, report["window"]["report_id"], "#gtm-engineering", store.FINAL) is None
    assert store.get(conn, report["window"]["report_id"])["attempts"] == 1
    # The next attempt therefore really does send: a duplicate is recoverable, a
    # silently skipped report is not.
    assert store.deliver_once(conn, report, sender=lambda c, m: {"ok": True}, channel="#gtm-engineering",
                              message=MESSAGE)["sent"] is True


def test_generating_a_report_writes_nothing_but_its_own_row(conn):
    """A report is a measurement: it must never change what it measures."""
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product", campaign_id="camp-pr")
    store.ensure_schema(conn)
    counts = lambda: {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]        # noqa: E731
                      for t in ("approvals", "delivery_outbox", "delivery_receipts", "people", "employers",
                                "opportunities", "postings", "run_log", "suppressions")}
    before = counts()
    conn.rollback()
    pipeline.generate_and_store(conn, now=FRIDAY_MORNING, compare_previous=False)
    conn.rollback()
    assert counts() == before


def test_the_artifacts_carry_the_checksum_of_the_text_that_was_sent(conn, tmp_path):
    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product", campaign_id="camp-pr")
    report = report_for(conn)
    written = pipeline.artifacts(tmp_path, report)
    payload = json.loads(Path(written["json"]).read_text(encoding="utf-8"))
    import hashlib

    text = Path(written["text"]).read_text(encoding="utf-8")
    assert payload["text_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest() == written["text_sha256"]
    assert Path(written["json"]).name == "weekly-2026-09-11.json"


def test_the_report_says_the_phone_sidecar_is_not_in_it(conn):
    report = report_for(conn)
    assert report["source"]["sidecar_included"] is False
    assert "sidecar" in render.render_text(report).lower()


def test_the_reporting_package_never_reads_the_sidecar(conn):
    """Isolation, checked mechanically rather than promised in prose."""
    import pkgutil

    import tgtc_core.reporting as package

    for module in pkgutil.iter_modules(package.__path__):
        source = (Path(package.__path__[0]) / f"{module.name}.py").read_text(encoding="utf-8")
        assert "tgtc_call_sidecar" not in source


# --------------------------------------------------------------------------------
# the command
# --------------------------------------------------------------------------------

def test_the_command_reports_measures_and_refuses_to_send_without_a_confirmed_destination(conn, pg_url, capsys, tmp_path):
    from tgtc_core.__main__ import main

    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product", campaign_id="camp-pr")
    argv = ["weekly-report", "--database-url", pg_url, "--now", FRIDAY_MORNING.isoformat(),
            "--print-format", "none", "--no-compare", "--out-dir", str(tmp_path)]
    assert main(argv) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["report_id"] == "weekly-2026-09-11"
    assert summary["instantly_created_unique_people"] == 1
    assert summary["delivery"] == {"sent": False, "reason": "not requested"}
    assert Path(summary["artifacts"]["text"]).exists()
    # A send without a confirmed destination is refused, and nothing is delivered.
    assert main(argv + ["--send", "slack"]) == 5
    assert "refused to send" in capsys.readouterr().err
    assert store.delivered(conn, "weekly-2026-09-11") is None


def test_the_command_exits_non_zero_on_an_alert_but_never_on_a_note(conn, pg_url, capsys):
    from tgtc_core.__main__ import main
    from tgtc_core.reporting.metrics import graded_flags

    seed_lead(conn, received_at=WEEK_START + timedelta(days=1), campaign_key="product", campaign_id="camp-pr")
    argv = ["weekly-report", "--database-url", pg_url, "--week", "last", "--now", FRIDAY_MORNING.isoformat(),
            "--print-format", "none", "--no-compare"]
    assert main(argv) == 0
    summary = json.loads(capsys.readouterr().out)
    assert any("below the 1000 minimum" in alert for alert in summary["alerts"])
    assert any("unit price is not verified" in note for note in summary["notes"])
    assert main(argv + ["--fail-on-alerts"]) == 4
    assert main(argv + ["--fail-on", "integrity"]) == 0       # missing runs are not integrity
    assert main(argv + ["--fail-on", "never"]) == 0           # a report always still prints

    # The unverified price is graded a NOTE, so it can never by itself fail a run.
    graded = graded_flags(report_for(conn))
    assert [f["level"] for f in graded if "unit price" in f["message"]] == ["note"]


def test_the_command_does_nothing_before_its_scheduled_moment(conn, pg_url, capsys):
    from tgtc_core.__main__ import main

    thursday = datetime(2026, 9, 17, 14, 0, tzinfo=UTC)
    assert main(["weekly-report", "--database-url", pg_url, "--now", thursday.isoformat(),
                 "--if-due-hour", "7", "--print-format", "none"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["skipped"] == "not_due" and "friday 07:00 America/Los_Angeles" in out["schedule"]
    assert store.get(conn, "weekly-2026-09-11") is None       # nothing was generated or stored


def test_the_timezone_resolver_is_recorded_on_every_report(conn):
    tz, source = resolve_timezone()
    assert source in ("zoneinfo:tzdata", "builtin:us_federal_dst_rule")
    assert report_for(conn)["window"]["timezone_source"] == source
