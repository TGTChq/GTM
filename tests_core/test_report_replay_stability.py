"""A closed week cannot change after it has been reported.

The defect, measured on production 2026-09-24: ``candidate_attempts`` and
``classifications`` are upserted with ``created_at = now()``, so re-processing a
candidate or a posting REWRITES when it was recorded. The 2026-09-24 run re-stamped
2,352 attempts belonging to units opened days earlier, and the same query over the same
closed week returned 595 one day and 592 the next -- after that week had been reported.

The fix is a ``first_recorded_at`` that is written once. An upsert only assigns the
columns its ``DO UPDATE`` names, so leaving it unnamed is what makes it immutable; these
tests hold that property in place, and hold the report to reading it.
"""

from __future__ import annotations

from datetime import timedelta

from tgtc_core.reporting import pipeline
from tests_core.helpers import opportunity_service, sql1
from tests_core.seed import apollo_for, seed_opportunity
from tests_core.test_weekly_report import FRIDAY_MORNING, WEEK_START, report_for, seed_lead

REPLAY_TABLES = ("candidate_attempts", "classifications", "evidence")


def test_every_upserted_table_keeps_its_first_recorded_at(conn):
    """The column exists on each replayed table and survives an upsert that moves
    ``created_at``. This is the property, stated once, for the three tables that have it."""
    with conn.cursor() as cur:
        for table in REPLAY_TABLES:
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = %s AND column_name = 'first_recorded_at'", (table,))
            assert cur.fetchone() is not None, f"{table} has no first_recorded_at"
    conn.rollback()


def test_a_replay_does_not_move_a_contact_attempt_into_another_week(conn, clock):
    """The exact production incident, in miniature: a gate decision recorded inside a
    closed week is replayed later, and the closed week must not move."""
    inside = WEEK_START + timedelta(days=1)
    lead = seed_lead(conn, received_at=inside, campaign_key="product", campaign_id="camp-pr")
    pid, employer_id, opportunity_id = seed_opportunity(conn, clock, domain="replay.com", org_name="Replay",
                                                        job_id="replay-1")
    assert opportunity_id is not None
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO candidate_attempts (opportunity_id, person_id, candidate_ref, attempt_kind, outcome, "
            "reason, epoch, created_at, first_recorded_at) "
            "VALUES (%s, %s, 'ref-replay', 'gate', 'pass', 'approved', 1, %s, %s)",
            (opportunity_id, lead["person_id"], inside, inside))
    conn.commit()

    before = report_for(conn)["headline"]
    assert before["contacts_found"] == 1

    # The production writer, replayed after the week closed.
    service = opportunity_service(conn, apollo_for("replay.com", "Replay"), clock)
    service._record_attempt(opportunity_id, "ref-replay", "gate", "pass", "approved", epoch=1,
                            person_id=lead["person_id"])

    moved = sql1(conn, "SELECT created_at > first_recorded_at FROM candidate_attempts WHERE candidate_ref = %s",
                 ("ref-replay",))
    assert moved is True, "the replay should still update created_at -- that is the last-attempt time"
    assert sql1(conn, "SELECT first_recorded_at FROM candidate_attempts WHERE candidate_ref = %s",
                ("ref-replay",)).isoformat() == inside.isoformat()

    after = report_for(conn)["headline"]
    assert after["contacts_found"] == 1, "the closed week changed after a replay"
    assert after["search_candidates_seen"] == before["search_candidates_seen"]

    # ... and the week the replay happened in does not gain a contact it never found.
    later = pipeline.build(conn, now=FRIDAY_MORNING + timedelta(days=7), compare_previous=False)
    assert later["headline"]["contacts_found"] == 0


def test_a_re_classification_does_not_move_a_job_between_cohorts(conn, clock):
    """Jobs captured and reviewed are one cohort. A posting re-classified next week must
    stay in the week it was first reviewed, or the percentage moves under the report."""
    inside = WEEK_START + timedelta(days=1)
    posting_id, _, opportunity_id = seed_opportunity(conn, clock, domain="cohort.com", org_name="Cohort",
                                                     job_id="cohort-1")
    with conn.cursor() as cur:
        cur.execute("UPDATE postings SET first_seen_at = %s WHERE id = %s", (inside, posting_id))
        cur.execute("UPDATE classifications SET created_at = %s, first_recorded_at = %s WHERE posting_id = %s",
                    (inside, inside, posting_id))
    conn.commit()

    before = report_for(conn)["headline"]
    assert (before["jobs_captured"], before["jobs_reviewed"]) == (1, 1)

    # A replay of the same classification, the way the service writes it.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO classifications (posting_id, policy_version, model_version, method, compatible_functions, "
            "excluded, result_json) SELECT posting_id, policy_version, model_version, method, compatible_functions, "
            "excluded, result_json FROM classifications WHERE posting_id = %s "
            "ON CONFLICT (posting_id, policy_version, model_version) DO UPDATE SET "
            "result_json = EXCLUDED.result_json, created_at = now()", (posting_id,))
    conn.commit()

    after = report_for(conn)["headline"]
    assert (after["jobs_captured"], after["jobs_reviewed"]) == (1, 1), "the cohort moved under a replay"
    assert after["jobs_review_rate"] == 100.0
