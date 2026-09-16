"""Real PostgreSQL regressions; simulated providers only. Never a portable skip."""
from datetime import timedelta

from tgtc_core.db.connection import jsonb
from tgtc_core.services.acquisition import SOURCE_JOB_BOARDS
from tgtc_core.services.identity_service import resolve_posting_identity
from tgtc_core.testing.fakes import FakeFantastic, make_posting_row
from tests_core.helpers import acquisition, make_fresh_partition, rows_in_window, sql1, sqlall
from tests_core.seed import seed_posting
from tests_core.test_yield_cohort_regressions import VEC_TEXT, PUBLISHER


def test_publisher_conflict_stops_before_creating_employer_or_classification(conn, clock):
    row = make_posting_row(id="publisher-job", title="Junior Accountant", organization=PUBLISHER,
                          domain="tvppa.com", description=VEC_TEXT, date_created=clock() - timedelta(hours=4))
    pid = seed_posting(conn, clock, row)
    result = resolve_posting_identity(conn, pid, now=clock())
    assert result.reason == "employer_attribution_conflict" and result.employer_id is None
    assert sql1(conn, "SELECT close_reason FROM postings WHERE id = %s", (pid,)) == result.reason
    assert sql1(conn, "SELECT count(*) FROM employers") == 0
    assert sql1(conn, "SELECT count(*) FROM work_items WHERE kind = 'classify'") == 0


def test_resumed_old_query_does_not_take_new_filters_at_old_offset(conn, clock):
    rows = rows_in_window(clock, 4)
    for row in rows:
        row["org_linkedin_industry"] = "Hospitals and Health Care"
    fake = FakeFantastic(rows=rows)
    svc = acquisition(conn, fake, clock, page_limit=2)
    pid = make_fresh_partition(conn, clock)
    part = svc._load_partition(pid)
    endpoint, params = svc.request_params(SOURCE_JOB_BOARDS, lower=part["window_start"], upper=part["window_end"], offset=0)
    params.pop("organization_agency")
    params.pop("exclude_organization_industry")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO request_attempts (provider, operation, partition_id, params_json, status) "
                    "VALUES ('fantastic','page',%s,%s,'served')", (pid, jsonb({"endpoint": endpoint, **params})))
        cur.execute("UPDATE source_partitions SET next_offset = 2 WHERE id = %s", (pid,))
    conn.commit()
    run = svc.run_partition(pid)
    assert run.stop_reason == "complete" and run.rows == 2
    assert all("exclude_organization_industry" not in r["params"] for r in fake.requests)
    assert fake.requests[0]["params"]["offset"] == "2"


def test_new_partition_filters_disqualified_industry_without_removing_unknowns(conn, clock):
    rows = rows_in_window(clock, 4)
    rows[0]["org_linkedin_industry"] = "Hospitals and Health Care"
    rows[1]["org_linkedin_recruitment_agency_derived"] = True
    rows[2]["org_linkedin_industry"] = None
    rows[2]["org_linkedin_headcount"] = None
    fake = FakeFantastic(rows=rows)
    svc = acquisition(conn, fake, clock, page_limit=2)
    pid = make_fresh_partition(conn, clock)
    run = svc.run_partition(pid)
    assert run.stop_reason == "complete" and run.rows == 2
    assert {r["provider_job_id"] for r in sqlall(conn, "SELECT provider_job_id FROM postings")} == {rows[2]["id"], rows[3]["id"]}
    assert fake.jobs_remaining == 19998


def test_same_rejected_job_is_not_reopened_or_reclassified_on_identical_observation(conn, clock):
    from tgtc_core.services.acquisition import upsert_posting
    row = make_posting_row(id="rejected", title="Analyst", organization="Acme", domain="acme.com",
                          description="This is a part-time position.", date_created=clock() - timedelta(hours=4))
    pid = seed_posting(conn, clock, row)
    with conn.cursor() as cur:
        cur.execute("UPDATE postings SET state = 'closed', close_reason = 'employment:part_time' WHERE id = %s", (pid,))
    conn.commit()
    result = upsert_posting(conn, source=SOURCE_JOB_BOARDS, row=row, lane="fresh", now=clock())
    conn.commit()
    assert result.state == "unchanged"
    assert sql1(conn, "SELECT state FROM postings WHERE id = %s", (pid,)) == "closed"
    assert sql1(conn, "SELECT count(*) FROM postings") == 1


def test_known_outside_size_is_excluded_upstream_on_a_new_partition(conn, clock):
    row = make_posting_row(id="known-large", title="Analyst", organization="Large Co", domain="large.example",
                          org_linkedin_slug="large-co", org_linkedin_headcount=2000,
                          date_created=clock() - timedelta(hours=4, minutes=30))
    pid = seed_posting(conn, clock, row)
    resolved = resolve_posting_identity(conn, pid, now=clock())
    with conn.cursor() as cur:
        cur.execute("UPDATE employers SET created_at = %s WHERE id = %s", (clock(), resolved.employer_id))
    conn.commit()
    fake = FakeFantastic(rows=[dict(row, id="next-large-job")])
    svc = acquisition(conn, fake, clock)
    partition = make_fresh_partition(conn, clock)
    result = svc.run_partition(partition)
    assert result.stop_reason == "complete" and result.rows == 0
    assert fake.requests[0]["params"]["exclude_organization_slug"] == "large-co"
    assert fake.jobs_remaining == 20000
