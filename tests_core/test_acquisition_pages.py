"""Pagination and persistence: overlap, repeated page, limit, late response, crash around the
checkpoint, source-local failure, quota reserve, contiguous windows. SIMULATED Fantastic."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tgtc_core.providers.http import TransportTimeout
from tgtc_core.services import acquisition as acq
from tgtc_core.testing.fakes import FakeFantastic
from tests_core.helpers import acquisition, make_fresh_partition, rows_in_window, sql1, sqlall


def test_overlapping_windows_never_duplicate_a_posting(conn, clock):
    fake = FakeFantastic(rows=rows_in_window(clock, 12))
    svc = acquisition(conn, fake, clock, page_limit=10)
    p1 = make_fresh_partition(conn, clock, hours_ago_start=5.0, hours=1.0)
    p2 = make_fresh_partition(conn, clock, hours_ago_start=5.5, hours=1.0)  # overlaps p1 by 30 min
    r1 = svc.run_partition(p1)
    r2 = svc.run_partition(p2)
    assert r1.stop_reason == "complete" and r2.stop_reason == "complete"
    assert r1.new_postings == 12 and r2.new_postings == 0
    assert sql1(conn, "SELECT count(*) FROM postings") == 12
    assert sql1(conn, "SELECT count(*) FROM page_receipts") == r1.pages + r2.pages
    # re-observation refreshed last_confirmed_active_at but never moved the age anchor
    assert sql1(conn, "SELECT count(*) FROM postings WHERE commercial_age_anchor <= first_seen_at") == 12
    assert sql1(conn, "SELECT count(*) FROM work_items WHERE kind = 'resolve_identity'") == 12


def test_repeated_page_is_recorded_and_does_not_complete_the_partition(conn, clock):
    fake = FakeFantastic(rows=rows_in_window(clock, 9), repeat_page_at_offset=3)
    svc = acquisition(conn, fake, clock, page_limit=3)
    pid = make_fresh_partition(conn, clock)
    run = svc.run_partition(pid)
    assert run.stop_reason == "complete"
    assert run.duplicate_pages == 1 and run.new_postings == 9
    receipts = sqlall(conn, "SELECT page_offset, row_count, duplicate_of_receipt_id FROM page_receipts WHERE partition_id = %s ORDER BY id", (pid,))
    # the duplicate at offset 3 did NOT move the cursor: offset 3 was requested again
    assert [r["page_offset"] for r in receipts] == [0, 3, 3, 6, 9]
    assert receipts[1]["duplicate_of_receipt_id"] is not None      # the repeated page points at the original
    assert receipts[2]["duplicate_of_receipt_id"] is None
    assert sql1(conn, "SELECT state FROM source_partitions WHERE id = %s", (pid,)) == "complete"
    assert sql1(conn, "SELECT count(*) FROM postings") == 9


def test_a_duplicate_page_loop_stalls_instead_of_completing(conn, clock):
    """The provider keeps serving the same page: never 'complete', never a skipped offset."""
    fake = FakeFantastic(rows=rows_in_window(clock, 6))
    orig = fake.request

    def stuck(method, url, **kw):
        params = dict(kw.get("params") or {})
        if params.get("offset") not in (None, "0", 0):
            params["offset"] = "0"
            kw["params"] = params
        return orig(method, url, **kw)

    fake.request = stuck  # type: ignore[assignment]
    svc = acquisition(conn, fake, clock, page_limit=3)
    pid = make_fresh_partition(conn, clock)
    run = svc.run_partition(pid)
    assert run.stop_reason == "duplicate_page_loop" and run.duplicate_pages == 3
    assert sql1(conn, "SELECT state || ':' || next_offset FROM source_partitions WHERE id = %s", (pid,)) == "stalled:3"
    assert sql1(conn, "SELECT complete_coverage FROM source_partitions WHERE id = %s", (pid,)) is False


def test_limit_pages_and_short_page_completes(conn, clock):
    fake = FakeFantastic(rows=rows_in_window(clock, 7))
    svc = acquisition(conn, fake, clock, page_limit=3)
    pid = make_fresh_partition(conn, clock)
    run = svc.run_partition(pid)
    assert run.pages == 3 and run.rows == 7 and run.stop_reason == "complete"
    part = sqlall(conn, "SELECT next_offset, complete_coverage, rows_received FROM source_partitions WHERE id = %s", (pid,))[0]
    assert part == {"next_offset": 7, "complete_coverage": True, "rows_received": 7}
    assert [r["params"]["offset"] for r in fake.requests] == ["0", "3", "6"]
    assert "title_advanced" not in fake.requests[0]["params"] and "description_advanced" not in fake.requests[0]["params"]
    assert fake.requests[0]["params"]["description_format"] == "text"


def test_failed_later_page_keeps_earlier_pages_and_resumes_from_its_own_cursor(conn, clock):
    fake = FakeFantastic(rows=rows_in_window(clock, 7), fail_offsets={3: 404})
    svc = acquisition(conn, fake, clock, page_limit=3)
    pid = make_fresh_partition(conn, clock)
    run = svc.run_partition(pid)
    assert run.stop_reason == "request_error:http_404" and run.pages == 1
    assert sql1(conn, "SELECT next_offset || ':' || state FROM source_partitions WHERE id = %s", (pid,)) == "3:open"
    assert sql1(conn, "SELECT count(*) FROM postings") == 3
    assert sql1(conn, "SELECT status FROM request_attempts ORDER BY id DESC LIMIT 1") == "failed"
    run2 = svc.run_partition(pid)
    assert run2.stop_reason == "complete" and sql1(conn, "SELECT count(*) FROM postings") == 7
    assert [r["params"]["offset"] for r in fake.requests] == ["0", "3", "3", "6"]


def test_timeout_is_uncertain_not_free_and_the_cursor_does_not_move(conn, clock):
    fake = FakeFantastic(rows=rows_in_window(clock, 7), fail_offsets={3: "timeout"})
    svc = acquisition(conn, fake, clock, page_limit=3)
    svc.client._retries = 0
    pid = make_fresh_partition(conn, clock)
    run = svc.run_partition(pid)
    assert run.stop_reason == "timeout_uncertain"
    attempt = sqlall(conn, "SELECT status, estimated_credits, confirmed_credits FROM request_attempts ORDER BY id DESC LIMIT 1")[0]
    assert attempt["status"] == "uncertain" and attempt["estimated_credits"] == 3 and attempt["confirmed_credits"] is None
    assert sql1(conn, "SELECT next_offset FROM source_partitions WHERE id = %s", (pid,)) == 3
    ce = sqlall(conn, "SELECT basis, estimated_credits, confirmed_credits FROM credit_events ORDER BY id DESC LIMIT 1")[0]
    assert ce["basis"] == "estimate" and ce["confirmed_credits"] is None
    run2 = svc.run_partition(pid)
    assert run2.stop_reason == "complete" and sql1(conn, "SELECT count(*) FROM postings") == 7


def test_crash_between_call_and_commit_loses_nothing_and_re_requests_the_page(conn, clock, monkeypatch):
    fake = FakeFantastic(rows=rows_in_window(clock, 5))
    svc = acquisition(conn, fake, clock, page_limit=3)
    pid = make_fresh_partition(conn, clock)
    real = acq.zlib.compress
    calls = {"n": 0}

    def boom(data):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash after the call, before commit")
        return real(data)

    monkeypatch.setattr(acq.zlib, "compress", boom)
    with pytest.raises(RuntimeError):
        svc.run_partition(pid)
    # page 1 committed; page 2 rolled back entirely
    assert sql1(conn, "SELECT next_offset FROM source_partitions WHERE id = %s", (pid,)) == 3
    assert sql1(conn, "SELECT count(*) FROM page_receipts") == 1
    assert sql1(conn, "SELECT count(*) FROM postings") == 3
    assert sql1(conn, "SELECT status FROM request_attempts ORDER BY id DESC LIMIT 1") == "intended"  # the intent survives
    monkeypatch.setattr(acq.zlib, "compress", real)
    run = svc.run_partition(pid)
    assert run.stop_reason == "complete"
    assert sql1(conn, "SELECT count(*) FROM postings") == 5
    assert sql1(conn, "SELECT count(*) FROM postings p JOIN postings q ON q.provider_job_id = p.provider_job_id AND q.id <> p.id") == 0


def test_source_failure_is_local(conn, clock):
    """Auth refusal on one source stalls THAT partition, keeps its rows, and the other source proceeds."""
    bad = FakeFantastic(rows=rows_in_window(clock, 6, prefix="a", domain_prefix="a"), fail_offsets={3: 401})
    good = FakeFantastic(ats_rows=rows_in_window(clock, 4, prefix="b", domain_prefix="b"))   # the ATS feed is its own endpoint (R02)
    svc_bad = acquisition(conn, bad, clock, page_limit=3)
    svc_good = acquisition(conn, good, clock, page_limit=3)
    p_bad = make_fresh_partition(conn, clock, source="fantastic:active-jb")
    p_good = make_fresh_partition(conn, clock, source="fantastic:active-ats")
    r_bad = svc_bad.run_partition(p_bad)
    assert r_bad.stop_reason == "auth_refused" and r_bad.rows == 3
    assert sql1(conn, "SELECT state FROM source_partitions WHERE id = %s", (p_bad,)) == "stalled"
    assert sql1(conn, "SELECT count(*) FROM postings WHERE source = 'fantastic:active-jb'") == 3
    assert sql1(conn, "SELECT state FROM provider_state WHERE provider = 'fantastic'") == "unauthorized"
    r_good = svc_good.run_partition(p_good)
    assert r_good.stop_reason == "complete" and r_good.new_postings == 4
    # a served response flips the provider back and stalled partitions can be reopened at their own cursor
    assert sql1(conn, "SELECT state FROM provider_state WHERE provider = 'fantastic'") == "serving"
    assert good.requests[0]["path"] == "/v1/active-ats" and bad.requests[0]["path"] == "/v1/active-jb"
    assert svc_bad.reopen_stalled("fantastic:active-jb") == 1
    assert sql1(conn, "SELECT next_offset FROM source_partitions WHERE id = %s", (p_bad,)) == 3


def test_quota_reserve_stops_before_breaching(conn, clock):
    fake = FakeFantastic(rows=rows_in_window(clock, 9), jobs_remaining=95)
    svc = acquisition(conn, fake, clock, page_limit=3, min_jobs_quota_remaining=90)
    pid = make_fresh_partition(conn, clock)
    run = svc.run_partition(pid)
    # first page served (92 left), second page would leave 89 < 90 -> stop, partition stays open
    assert run.pages == 1 and run.stop_reason == "quota_reserve:jobs"
    assert sql1(conn, "SELECT state FROM source_partitions WHERE id = %s", (pid,)) == "open"


def test_fresh_windows_are_contiguous_and_backfill_walks_backwards(conn, clock):
    fake = FakeFantastic()
    svc = acquisition(conn, fake, clock)
    first = svc.plan_fresh_partitions(max_new=3)
    assert len(first) == 1                       # from (now-lag-1h) to (now-lag), hour-aligned
    assert svc.plan_fresh_partitions(max_new=3) == []
    clock.advance(hours=2)
    second = svc.plan_fresh_partitions(max_new=3)
    assert len(second) == 2
    wins = sqlall(conn, "SELECT window_start, window_end FROM source_partitions WHERE lane = 'fresh' ORDER BY window_start")
    for a, b in zip(wins, wins[1:]):
        assert a["window_end"] == b["window_start"]   # no gap, no overlap
    b1 = svc.plan_backfill_partition()
    b2 = svc.plan_backfill_partition()
    bf = sqlall(conn, "SELECT window_start, window_end FROM source_partitions WHERE lane = 'backfill' ORDER BY window_start DESC")
    assert bf[0]["window_end"] == wins[0]["window_start"] and bf[1]["window_end"] == bf[0]["window_start"]
    assert bf[0]["window_end"] - bf[0]["window_start"] == timedelta(hours=24)


def test_modified_posting_gets_a_version_and_is_reclassified(conn, clock):
    rows = rows_in_window(clock, 1)
    fake = FakeFantastic(rows=[dict(r) for r in rows])
    svc = acquisition(conn, fake, clock)
    pid = make_fresh_partition(conn, clock)
    svc.run_partition(pid)
    # provider modifies the description; a second window observation sees the change
    fake.rows[0]["description_text"] = rows[0]["description_text"] + " Now also owns renewals reporting."
    p2 = make_fresh_partition(conn, clock, hours_ago_start=5.25, hours=1.0)
    run = svc.run_partition(p2)
    assert run.modified_postings == 1 and run.new_postings == 0
    assert sql1(conn, "SELECT max(version) FROM posting_versions") == 2
    assert sql1(conn, "SELECT count(*) FROM work_items WHERE kind = 'classify'") == 1
