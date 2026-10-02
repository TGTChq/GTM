"""Provider-confirmed 24h strategy for the flag-gated canary. SIMULATED Fantastic only (no network, no DB).

Remco + developer.fantastic.jobs "Recommended Strategy": ``time_frame=24h`` daily at the same hour, window on
``date_created`` with a one-hour enrichment delay, ``limit=1000`` from ``offset=0`` stepping by 1,000 until the
first short page; recovery with ``7d`` (offset) or ``6m`` (cursor) plus exact ``date_created_gte/lt``;
``ai_taxonomies_a_primary`` instead of ``ai_taxonomies_a``; separate requests for sources/rows the firmographic
filters drop.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone

import pytest

from tests_core.test_daily_24h_canary import ATS, JB, Feed24h, client, offsets, row
from tgtc_core.domain.acquisition_query import PRIORITY_PROFILE
from tgtc_core.services import daily_24h_canary as canary
from tgtc_core.services.acquisition import SOURCE_JOB_BOARDS

NOW = datetime(2026, 9, 19, 14, 37, 12, tzinfo=timezone.utc)
PRIMARY = "Technology,Sales,Marketing,Customer Service & Support,Creative & Media"


def arm_run(feed, arms, *, max_records=5000, max_requests=25, per_arm=None, registry=None):
    c = canary.Daily24hCanary(client(feed), partitions=arms, limit=canary.STRATEGY_LIMIT, max_records=max_records,
                              max_requests=max_requests, registry=registry or canary.HistoricalRegistry.empty(),
                              now=lambda: NOW, max_records_per_partition=per_arm)
    return c.run()


def arms_by_key(**kw):
    return {a.key: a for a in canary.strategy_arms(**kw)}


# ---------------------------------------------------------------------------- request shape

def test_normal_request_is_24h_limit_1000_offset_0_without_date_filters():
    for arm in canary.strategy_arms():
        _, params = arm.request(offset=0)
        assert params["time_frame"] == "24h" and params["limit"] == 1000 and params["offset"] == 0
        assert not {"date_created_gte", "date_created_lt", "date_posted_gte", "date_posted_lt"} & set(params)


def test_no_title_catalogue_or_exact_title_dependency():
    for arm in canary.strategy_arms():
        assert not set(arm.request(offset=0)[1]) & set(canary.TITLE_PARAMETERS)
    for _, arm in canary.count_probes():
        assert not set(arm.params()) & set(canary.TITLE_PARAMETERS)


def test_every_request_fits_the_10000_character_limit():
    slugs = [f"company-slug-number-{i:03d}" for i in range(100)]
    for arm in canary.strategy_arms(slug_exclusions=slugs):
        assert canary.query_length(*arm.request(offset=123000)) <= canary.MAX_QUERY_CHARS
    for _, arm in canary.count_probes(slug_exclusions=slugs):
        assert canary.query_length(arm.endpoint, arm.params()) <= canary.MAX_QUERY_CHARS


def test_arm_filters_match_the_provider_confirmed_design():
    arms = arms_by_key()
    _, control = canary.daily_request(SOURCE_JOB_BOARDS, PRIORITY_PROFILE, offset=0, limit=1000)
    assert arms["A_broad_control_jb"].params() == {k: v for k, v in control.items() if k not in {"offset", "limit", "time_frame"}}
    b, c, d, e = (arms[k].params() for k in ("B_primary_ats", "C_primary_linkedin", "D_wellfound_yc", "E_ats_missing_profile"))
    assert arms["B_primary_ats"].endpoint == ATS and b["include_basic_organization_details"] == "true"
    assert arms["C_primary_linkedin"].endpoint == JB and c["source"] == "linkedin" and c["exclude_ats_duplicate"] == "true"
    assert arms["D_wellfound_yc"].endpoint == JB and d["source"] == "wellfound,ycombinator"
    assert arms["E_ats_missing_profile"].endpoint == ATS and e["include_basic_organization_details"] == "true"
    for params in (b, c, d, e):
        assert params["ai_taxonomies_a_primary"] == PRIMARY and "ai_taxonomies_a" not in params
        assert params["location"] == "United States"
    for params in (b, c):
        assert params["organization_headcount_gte"] == 25 and params["organization_headcount_lt"] == 1001
        assert params["ai_employment_type"] == "FULL_TIME" and params["organization_agency"] == "exclude"
    for params in (d, e):          # missing-data coverage: no firmographic / employment filter
        assert not {"organization_headcount_gte", "organization_headcount_lt", "ai_employment_type"} & set(params)
    assert "Non-profit Organizations" in b["exclude_organization_industry"]            # corrected labels
    assert "Non-profit Organizations" not in arms["A_broad_control_jb"].params()["exclude_organization_industry"]
    assert "expired" in arms["D_wellfound_yc"].recovers


def test_coverage_filters_can_be_switched_off_when_the_counts_prove_them_unsafe():
    arms = arms_by_key(wf_yc_industry=False, wf_yc_agency=False, ats_missing_profile_full_time=True)
    d, e = arms["D_wellfound_yc"].params(), arms["E_ats_missing_profile"].params()
    assert "exclude_organization_industry" not in d and "organization_agency" not in d
    assert e["ai_employment_type"] == "FULL_TIME" and "organization_headcount_gte" not in e


# ---------------------------------------------------------------------------- pagination

def test_offsets_0_1000_2000_and_stop_at_the_first_short_page():
    feed = Feed24h(ats=[row(i) for i in range(2500)])
    rep = arm_run(feed, [arms_by_key()["B_primary_ats"]])
    assert offsets(feed, ATS) == [0, 1000, 2000]
    assert all(r["params"]["limit"] == 1000 and r["params"]["time_frame"] == "24h" for r in feed.requests)
    part = rep["partitions"][0]
    assert part["stop_reason"] == "short_page" and part["completed"] is True
    assert [p["rows"] for p in part["pages"]] == [1000, 1000, 500]


def test_continues_after_an_exactly_full_page():
    feed = Feed24h(jb=[row(i) for i in range(2000)])
    rep = arm_run(feed, [arms_by_key()["C_primary_linkedin"]])
    assert offsets(feed, JB) == [0, 1000, 2000]
    assert rep["partitions"][0]["pages"][-1]["rows"] == 0 and rep["partitions"][0]["completed"] is True


def test_a_repeated_stable_page_aborts_the_arm_and_adds_nothing():
    feed = Feed24h(ats=[row(i) for i in range(3000)], repeat_at=(ATS, 1000))
    rep = arm_run(feed, [arms_by_key()["B_primary_ats"]])
    part = rep["partitions"][0]
    assert part["stop_reason"] == "repeated_page_signature" and part["completed"] is False
    assert rep["totals"]["net_new_unique_jobs"] == 1000


def test_per_arm_ceiling_stops_only_that_arm():
    arms = arms_by_key()
    feed = Feed24h(jb=[row(i) for i in range(3000)], ats=[row(5000 + i) for i in range(1500)])
    rep = arm_run(feed, [arms["C_primary_linkedin"], arms["B_primary_ats"]], per_arm=1000)
    assert offsets(feed, JB) == [0] and offsets(feed, ATS) == [0]
    assert [p["stop_reason"] for p in rep["partitions"]] == ["partition_record_ceiling", "partition_record_ceiling"]
    assert rep["records_billed"] == 2000


def test_every_billed_row_is_kept_with_its_arm(tmp_path):
    arms = arms_by_key()
    shared = [row(i) for i in range(3)]
    feed = Feed24h(jb=shared, ats=shared)      # stable ids: the same jobs under two arms
    rep = arm_run(feed, [arms["C_primary_linkedin"], arms["B_primary_ats"]])
    assert [b["partition"] for b in rep["_billed_rows"]] == ["C_primary_linkedin"] * 3 + ["B_primary_ats"] * 3
    t = rep["totals"]
    assert t["net_new_unique_jobs"] == 3          # the second arm adds no new job
    assert t["canonical_url_duplicates"] + t["canonical_fingerprint_duplicates"] == 3
    canary.write_artifacts(rep, tmp_path / "s")
    lines = gzip.open(tmp_path / "s" / "billed_rows.jsonl.gz", "rt", encoding="utf-8").read().splitlines()
    assert len(lines) == 6


# ---------------------------------------------------------------------------- windows

def test_daily_count_window_is_24_hours_ending_one_hour_before_the_current_hour():
    lower, upper = canary.daily_count_window(NOW)
    assert upper == datetime(2026, 9, 19, 13, 0, tzinfo=timezone.utc) and upper - lower == timedelta(hours=24)


def test_recovery_within_7d_uses_offset_and_exact_bounds():
    arm = arms_by_key()["B_primary_ats"]
    lower, upper = datetime(2026, 9, 16, 13, tzinfo=timezone.utc), datetime(2026, 9, 17, 13, tzinfo=timezone.utc)
    endpoint, params = canary.recovery_request(arm, lower=lower, upper=upper, now=NOW, offset=2000)
    assert endpoint == ATS and params["time_frame"] == "7d" and params["offset"] == 2000 and params["limit"] == 1000
    assert params["date_created_gte"] == "2026-09-16T13:00:00Z" and params["date_created_lt"] == "2026-09-17T13:00:00Z"
    assert "cursor" not in params
    with pytest.raises(ValueError):
        canary.recovery_request(arm, lower=lower, upper=upper, now=NOW, cursor=5)


def test_recovery_older_than_7d_uses_6m_with_cursor_never_offset():
    arm = arms_by_key()["C_primary_linkedin"]
    lower, upper = datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 8, 2, tzinfo=timezone.utc)
    _, first = canary.recovery_request(arm, lower=lower, upper=upper, now=NOW)
    assert first["time_frame"] == "6m" and "offset" not in first and "cursor" not in first
    assert first["date_created_gte"] == "2026-08-01T00:00:00Z" and first["date_created_lt"] == "2026-08-02T00:00:00Z"
    _, nxt = canary.recovery_request(arm, lower=lower, upper=upper, now=NOW, cursor=987654321)
    assert nxt["cursor"] == 987654321 and "offset" not in nxt
    with pytest.raises(ValueError):
        canary.recovery_request(arm, lower=lower, upper=upper, now=NOW, offset=1000)
    with pytest.raises(ValueError):
        canary.recovery_request(arm, lower=datetime(2025, 1, 1, tzinfo=timezone.utc),
                                upper=datetime(2025, 1, 2, tzinfo=timezone.utc), now=NOW)


# ---------------------------------------------------------------------------- count matrix and isolation

def test_count_matrix_calls_only_count_endpoints_over_the_exact_window():
    feed = Feed24h(count={"count": 7})
    matrix = canary.run_count_matrix(feed, base_url="https://fantastic.test", api_key="k-test", now=NOW,
                                     probes=canary.count_probes())
    assert matrix["job_records_requested"] == 0
    assert all(r["path"].endswith("-count") for r in feed.requests)
    assert all(r["params"]["date_created_gte"] == "2026-09-18T13:00:00Z" and
               r["params"]["date_created_lt"] == "2026-09-19T13:00:00Z" and "time_frame" not in r["params"]
               for r in feed.requests)
    keys = [p["probe"] for p in matrix["probes"]]
    for required in ("A_broad_control_jb", "A_broad_control_ats", "B_primary_ats", "C_primary_linkedin", "D_wellfound_yc",
                     "E_ats_missing_profile", "D_without_industry_exclusion", "E_full_time_only"):
        assert required in keys
    assert "k-test" not in json.dumps(matrix)


def test_strategy_cli_is_flag_gated_and_its_dry_run_uses_no_network(monkeypatch, tmp_path, capsys):
    from tgtc_core.__main__ import main
    import tgtc_core.providers.http as http

    monkeypatch.delenv("FANTASTIC_DAILY_24H_CANARY", raising=False)
    monkeypatch.delenv("TGTC_ACCEPTANCE_MODE", raising=False)
    with pytest.raises(SystemExit):
        main(["canary-24h-strategy", "--state-dir", str(tmp_path / "s"), "--dry-run"])

    def boom(*a, **k):
        raise AssertionError("network used during dry run")

    monkeypatch.setattr(http.RequestsTransport, "request", boom)
    monkeypatch.setenv("FANTASTIC_DAILY_24H_CANARY", "1")
    assert main(["canary-24h-strategy", "--state-dir", str(tmp_path / "s"), "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert [a["key"] for a in plan["arms"]] == ["A_broad_control_jb", "B_primary_ats", "C_primary_linkedin",
                                                 "D_wellfound_yc", "E_ats_missing_profile"]
    assert all(a["params"]["limit"] == 1000 and a["params"]["offset"] == 0 for a in plan["arms"])
    assert "Authorization" not in json.dumps(plan)


def test_records_phase_requires_the_spend_acknowledgement(monkeypatch, tmp_path):
    from tgtc_core.__main__ import main
    monkeypatch.setenv("FANTASTIC_DAILY_24H_CANARY", "1")
    monkeypatch.setenv("FANTASTIC_JOBS_API_KEY", "k-test")
    monkeypatch.delenv("TGTC_ACCEPTANCE_MODE", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["canary-24h-strategy", "--state-dir", str(tmp_path / "s"), "--phase", "records"])
    assert "spend" in str(exc.value)


def test_strategy_code_cannot_reach_production_state_contacts_or_delivery():
    """Same import ban as the canary module test: the strategy lives in that module."""
    import ast
    from pathlib import Path
    tree = ast.parse(Path(canary.__file__).read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    banned = ("db", "work_queue", "psycopg", "delivery", "opportunity", "approval", "apollo", "airtable", "instantly",
              "inference", "runner", "hunter")
    assert not [m for m in imported if any(part in banned for part in m.replace(".", " ").split())]
