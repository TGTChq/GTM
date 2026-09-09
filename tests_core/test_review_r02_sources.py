"""R02 -- both Fantastic feeds are scheduled, each on its own endpoint with its own
parameters; ``exclude_ats_duplicate`` never removes inventory whose ATS counterpart is
not acquired; a genuine cross-source repeat keeps its first occurrence."""

from __future__ import annotations

from datetime import timedelta

from tgtc_core.services.acquisition import DEFAULT_SOURCES, SOURCE_ATS, SOURCE_JOB_BOARDS, SOURCE_SPECS
from tgtc_core.testing.fakes import FakeFantastic, make_posting_row
from tgtc_core.testing.scenario import build_nine_route_scenario
from tests_core.helpers import acquisition, make_fresh_partition, rows_in_window, runner, sql1, sqlall
from tests_core.seed import example_for


def test_source_registry_maps_each_source_to_its_endpoint():
    assert SOURCE_SPECS[SOURCE_JOB_BOARDS].endpoint == "/v1/active-jb"
    assert SOURCE_SPECS[SOURCE_ATS].endpoint == "/v1/active-ats"
    assert DEFAULT_SOURCES == (SOURCE_JOB_BOARDS, SOURCE_ATS)


def test_ats_partition_requests_the_ats_endpoint(conn, clock):
    fake = FakeFantastic(ats_rows=rows_in_window(clock, 3, prefix="ats"))
    svc = acquisition(conn, fake, clock)
    pid = make_fresh_partition(conn, clock, source=SOURCE_ATS)
    run = svc.run_partition(pid)
    assert run.stop_reason == "complete" and run.new_postings == 3
    assert [r["path"] for r in fake.requests] == ["/v1/active-ats"]
    assert "exclude_ats_duplicate" not in fake.requests[0]["params"]
    assert sql1(conn, "SELECT count(*) FROM postings WHERE source = %s", (SOURCE_ATS,)) == 3


def test_exclude_ats_duplicate_is_sent_only_when_the_ats_feed_is_scheduled(conn, clock):
    rows = rows_in_window(clock, 4)
    rows[0]["ats_duplicate"] = True                       # the provider knows this row also exists in the ATS feed
    fake_both = FakeFantastic(rows=[dict(r) for r in rows])
    both = acquisition(conn, fake_both, clock)            # default: both sources scheduled
    p1 = make_fresh_partition(conn, clock, source=SOURCE_JOB_BOARDS)
    run1 = both.run_partition(p1)
    assert fake_both.requests[0]["params"].get("exclude_ats_duplicate") == "true"
    assert run1.new_postings == 3                          # the ATS counterpart will arrive via the ATS feed
    # job boards ONLY: the flag must not be sent, otherwise the row would be lost for good
    fake_jb = FakeFantastic(rows=[dict(r) for r in rows])
    jb_only = acquisition(conn, fake_jb, clock, sources=(SOURCE_JOB_BOARDS,))
    p2 = make_fresh_partition(conn, clock, source=SOURCE_JOB_BOARDS, hours_ago_start=5.5)
    run2 = jb_only.run_partition(p2)
    assert "exclude_ats_duplicate" not in fake_jb.requests[0]["params"]
    assert run2.rows == 4 and sql1(conn, "SELECT count(*) FROM postings") == 4


def test_runner_plans_and_acquires_both_sources(conn, clock):
    sc = build_nine_route_scenario(clock())
    ex = example_for("finance")
    sc.fantastic.ats_rows = [make_posting_row(id="ats-1", title=ex.title, organization="Finance Co", domain="financeco.com",
                                              description=ex.description, date_created=clock() - timedelta(hours=4),
                                              source="greenhouse", source_type="ats")]
    r = runner(conn, sc, clock)
    reports = r.acquire()
    sources = {x["source"] for x in reports}
    assert sources == {SOURCE_JOB_BOARDS, SOURCE_ATS}
    paths = {req["path"] for req in sc.fantastic.requests}
    assert paths == {"/v1/active-jb", "/v1/active-ats"}
    parts = sqlall(conn, "SELECT source, lane, state FROM source_partitions ORDER BY source, lane")
    assert {(p["source"], p["lane"]) for p in parts} >= {(SOURCE_JOB_BOARDS, "fresh"), (SOURCE_ATS, "fresh")}


def test_cross_source_repeat_keeps_the_first_occurrence_and_links_the_second(conn, clock):
    """The same job seen on a job board and in the ATS feed: one opportunity, first posting
    kept as the original, the later one linked as its duplicate."""
    ex = example_for("finance")
    created = clock() - timedelta(hours=4)
    jb = make_posting_row(id="jb-9", title=ex.title, organization="Finance Co", domain="financeco.com", description=ex.description,
                          date_created=created, ats_duplicate=True)
    ats = make_posting_row(id="ats-9", title=ex.title, organization="Finance Co", domain="financeco.com", description=ex.description,
                           date_created=created + timedelta(minutes=5), source="lever", source_type="ats")
    fake = FakeFantastic(rows=[jb], ats_rows=[ats])
    svc = acquisition(conn, fake, clock, sources=(SOURCE_JOB_BOARDS,))   # job boards first, alone
    p_jb = make_fresh_partition(conn, clock, source=SOURCE_JOB_BOARDS)
    assert svc.run_partition(p_jb).new_postings == 1
    from tgtc_core.services.identity_service import resolve_posting_identity
    from tgtc_core.services.classification_service import classify_one
    first_id = sql1(conn, "SELECT id FROM postings WHERE provider_job_id = 'jb-9'")
    resolve_posting_identity(conn, first_id, now=clock())
    classify_one(conn, first_id, inference=None, now=clock())
    svc2 = acquisition(conn, fake, clock)                                # then the ATS feed
    p_ats = make_fresh_partition(conn, clock, source=SOURCE_ATS)
    assert svc2.run_partition(p_ats).new_postings == 1
    second_id = sql1(conn, "SELECT id FROM postings WHERE provider_job_id = 'ats-9'")
    resolve_posting_identity(conn, second_id, now=clock())
    classify_one(conn, second_id, inference=None, now=clock())
    assert sql1(conn, "SELECT duplicate_of_posting_id FROM postings WHERE id = %s", (second_id,)) == first_id
    assert sql1(conn, "SELECT duplicate_of_posting_id FROM postings WHERE id = %s", (first_id,)) is None
    assert sql1(conn, "SELECT count(*) FROM opportunities") == 1
    assert sql1(conn, "SELECT count(*) FROM opportunity_postings") == 2
