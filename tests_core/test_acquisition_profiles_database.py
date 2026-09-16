"""Full local PostgreSQL gate. Never skip when DB is unavailable. No real providers."""
from datetime import timedelta

import pytest

from tgtc_core.db import apply_schema
from tgtc_core.domain.acquisition_query import PRIORITY_PROFILE as P, DISCOVERY_PROFILE as D
from tgtc_core.runner import Runner
from tgtc_core.services.acquisition import SOURCE_JOB_BOARDS as JB, SOURCE_ATS as ATS
from tgtc_core.services.metrics import ledger
from tgtc_core.services.spend_budget import BudgetLimits, create_budget
from tgtc_core.testing.fakes import FakeFantastic
from tests_core.helpers import acquisition, make_fresh_partition, rows_in_window, settings, sql1, sqlall


def scoped_partition(conn, clock, profile):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO source_partitions (source,lane,window_start,window_end,query_profile) "
                    "VALUES (%s,'fresh',%s,%s,%s) RETURNING id",
                    (JB, clock() - timedelta(hours=5), clock() - timedelta(hours=4), profile))
        pid = cur.fetchone()["id"]
    conn.commit()
    return pid


def candidates(clock, n=4, **kwargs):
    rows = rows_in_window(clock, n, **kwargs)
    for row in rows:
        row["ai_taxonomies_a"] = ["Software"]
    return rows


def test_parallel_profiles_have_independent_cursors_and_deduplicate_jobs(conn, clock):
    rows = candidates(clock)
    rows[1]["org_linkedin_headcount"] = None
    rows[2]["ai_taxonomies_a"] = None
    rows[3]["ai_employment_type"] = ["PART_TIME"]
    fake = FakeFantastic(rows=rows)
    svc = acquisition(conn, fake, clock, page_limit=2)
    p, d = scoped_partition(conn, clock, P), scoped_partition(conn, clock, D)
    assert svc.run_partition(p).new_postings == 1
    first = svc.run_partition(d, max_pages=1)
    assert first.stop_reason == "page_budget" and first.query_profile == D
    second = svc.run_partition(d)
    assert first.new_postings + second.new_postings == 3
    assert first.unchanged_postings + second.unchanged_postings == 1
    assert sql1(conn, "SELECT count(*) FROM postings") == 4
    assert sql1(conn, "SELECT count(*) FROM work_items WHERE kind='resolve_identity'") == 4
    assert fake.jobs_remaining == 19995  # duplicate costs a credit but not a new job
    report = ledger(conn)["acquisition_profiles"]
    assert report[P]["unique_postings_first_seen"] == 1
    assert report[D]["unique_postings_first_seen"] == 3
    assert report[D]["confirmed_job_credits"] == 4
    assert report[D]["coverage_scope"] == "saved_query_only_not_whole_market"
    assert report[D]["incomplete_query_windows"] == 0


def test_legacy_cursor_and_profiles_survive_idempotent_migration(conn, clock):
    old = make_fresh_partition(conn, clock)
    with conn.cursor() as cur:
        cur.execute("UPDATE source_partitions SET next_offset=100 WHERE id=%s", (old,))
    conn.commit()
    p = scoped_partition(conn, clock, P)
    d = scoped_partition(conn, clock, D)
    apply_schema(conn)
    apply_schema(conn)
    assert sql1(conn, "SELECT count(*) FROM source_partitions") == 3
    assert sql1(conn, "SELECT next_offset FROM source_partitions WHERE id=%s", (old,)) == 100
    assert sql1(conn, "SELECT query_profile FROM source_partitions WHERE id=%s", (old,)) == "legacy_v1"
    assert {r["id"] for r in sqlall(conn, "SELECT id FROM source_partitions WHERE query_profile <> 'legacy_v1'")} == {p, d}


def test_planning_each_scope_is_idempotent_and_contiguous(conn, clock):
    svc = acquisition(conn, FakeFantastic(), clock)
    for profile in (P, D):
        assert len(svc.plan_fresh_partitions(profile=profile)) == 1
        assert not svc.plan_fresh_partitions(profile=profile)
    clock.advance(hours=2)
    for profile in (P, D):
        assert len(svc.plan_fresh_partitions(profile=profile)) == 2
        assert len(svc.open_partitions("fresh", profile=profile)) == 3
    assert len(svc.open_partitions("fresh")) == 0  # no hidden resume of old scopes


def test_discovery_never_inherits_recent_employer_blacklist(conn, clock):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO employers(canonical_name,name_key,linkedin_slug,employee_count,created_at) "
                    "VALUES ('Employer 0','employer0','employer0',2000,%s)", (clock(),))
    conn.commit()
    fake = FakeFantastic(rows=candidates(clock, n=1))
    svc = acquisition(conn, fake, clock)
    svc.run_partition(scoped_partition(conn, clock, D))
    assert "exclude_organization_slug" not in fake.requests[0]["params"]
    assert sql1(conn, "SELECT count(*) FROM postings") == 1


def new_runner(conn, clock, fake, *, with_budget=True, requests=10, credits=20):
    if with_budget:
        create_budget(conn, "profile-test", BudgetLimits(fantastic_requests=requests, fantastic_credits=credits),
                      expires_at=clock() + timedelta(hours=1))
    s = settings(acquisition_strategy="balanced_v1", fantastic_cycle_page_slots=10,
                 fantastic_page_limit=2, spend_budget_id="profile-test" if with_budget else "")
    return Runner(conn, s, fantastic_transport=fake, apollo_transport=None,
                  airtable_transport=None, instantly_transport=None, now=clock, run_id="profiles-test")


def test_balanced_requires_persistent_spend_budget_before_calls_or_planning(conn, clock):
    fake = FakeFantastic()
    runner = new_runner(conn, clock, fake, with_budget=False)
    with pytest.raises(RuntimeError, match="persistent_spend_budget"):
        runner.acquire(fresh_partitions=1, backfill_partitions=0)
    assert not fake.requests
    assert sql1(conn, "SELECT count(*) FROM source_partitions") == 0


def test_balanced_page_slots_interleave_feeds_and_profiles_and_bound_total_spend(conn, clock):
    fake = FakeFantastic(rows=candidates(clock, 30, hours_ago_start=4),
                         ats_rows=candidates(clock, 30, hours_ago_start=4, prefix="ats"))
    reports = new_runner(conn, clock, fake).acquire(fresh_partitions=1, backfill_partitions=0)
    assert len(fake.requests) == 10
    assert sum(r["pages"] for r in reports) == 10
    assert sum(r["query_profile"] == D for r in reports) == 2
    assert {r["source"] for r in reports if r["query_profile"] == D} == {JB, ATS}
    assert sql1(conn, "SELECT sum(estimated_credits) FROM spend_reservations") == 20
    assert sql1(conn, "SELECT count(*) FROM source_partitions WHERE complete_coverage") == 0
    new_runner_reports = new_runner(conn, clock, fake).acquire(fresh_partitions=1, backfill_partitions=0)
    assert len(fake.requests) == 10  # process restart does not reset budget
    assert any(r["stop_reason"].startswith("spend_budget_exhausted:") for r in new_runner_reports)


def test_timeout_stops_other_profiles_and_keeps_reservation(conn, clock):
    fake = FakeFantastic(fail_offsets={0: "timeout"})
    reports = new_runner(conn, clock, fake).acquire(fresh_partitions=1, backfill_partitions=0)
    assert len(fake.requests) == 1
    assert reports[0]["stop_reason"] == "timeout_uncertain"
    assert sql1(conn, "SELECT count(*) FROM spend_reservations") == 1


def test_failed_historical_scope_remains_a_coverage_gap_not_a_fake_completion(conn, clock):
    svc = acquisition(conn, FakeFantastic(), clock)
    pid = scoped_partition(conn, clock, D)
    with conn.cursor() as cur:
        cur.execute("UPDATE source_partitions SET window_start=window_start-interval '190 days', "
                    "window_end=window_end-interval '190 days' WHERE id=%s", (pid,))
    conn.commit()
    assert svc.run_partition(pid).stop_reason == "partition_outside_provider_time_frame"
    assert sql1(conn, "SELECT state FROM source_partitions WHERE id=%s", (pid,)) == "failed"
    assert sql1(conn, "SELECT complete_coverage FROM source_partitions WHERE id=%s", (pid,)) is False


def test_discovery_resumes_oldest_window_first(conn, clock):
    svc = acquisition(conn, FakeFantastic(), clock)
    first = svc.plan_fresh_partitions(profile=D)[0]
    clock.advance(hours=1)
    last = svc.plan_fresh_partitions(profile=D)[0]
    assert svc.open_partitions("fresh", profile=D, oldest_first=True) == [first, last]


def test_historical_share_uses_persistent_progress(conn, clock):
    svc = acquisition(conn, FakeFantastic(), clock)
    p = scoped_partition(conn, clock, D)
    assert not svc.prefer_backfill(JB, D)
    with conn.cursor() as cur:
        cur.execute("UPDATE source_partitions SET pages_received=4 WHERE id=%s", (p,))
    conn.commit()
    assert acquisition(conn, FakeFantastic(), clock).prefer_backfill(JB, D)
