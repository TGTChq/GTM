"""Tests for the weekly reporting layer.

The contract under test is not "does it print a number". It is:

* the window is Pacific-local and DST-correct, with no hardcoded UTC offset;
* a run is attributed to a week by when the *run* happened, never by a job's
  ``posted_at``;
* a counter a run did not report is ``partial`` / ``unavailable``, never zero;
* ``sent_to_instantly`` is only claimed when something actually measured it;
* the collectors read and never write.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import run_weekly_report
from weekly_report.evidence import STATUS_MEASURED, STATUS_PARTIAL, STATUS_UNAVAILABLE, as_count
from weekly_report.external import collect_airtable, collect_instantly
from weekly_report.metrics import RUN_METRIC_SPECS, SOURCE_RUN_ARTIFACTS, build_run_metrics
from weekly_report.render import render_summary
from weekly_report.report import HEADLINE_ORDER, build_report
from weekly_report.run_artifacts import discover_runs, load_run, select_window
from weekly_report.timewindow import (
    PACIFIC_TZ_NAME,
    TZ_SOURCE_FALLBACK,
    UsPacificFallback,
    explicit_window,
    iso_z,
    resolve_timezone,
    weekly_window,
)

UTC = timezone.utc


# --------------------------------------------------------------------------
# fixtures: a real-shaped run artifact directory
# --------------------------------------------------------------------------


def write_run(
    root: Path,
    run_id: str,
    *,
    finished: Optional[str],
    started: Optional[str] = None,
    postings: Optional[int] = 100,
    reviewed: Optional[int] = 90,
    qualified: Optional[int] = 40,
    contacts: Optional[int] = 12,
    created: Optional[int] = 10,
    enrolled: Optional[int] = None,
    allow_enrollment: bool = False,
    posted_at: str = "2020-01-01T00:00:00Z",
    reasons: Optional[Dict[str, int]] = None,
    stop_reason: Optional[str] = None,
) -> Path:
    """Write one run directory in the exact shape ``orchestrator/pipeline.py`` writes."""
    run_dir = root / "run_artifacts" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest: Dict[str, Any] = {
        "run_id": run_id,
        "mode": "live_acquisition_and_enrichment",
        "status": "complete",
        "policy": {"allow_instantly_enrollment": allow_enrollment},
        # A job's own publication date lives in the run, and must never drive attribution.
        "notes": [{"posted_at": posted_at}],
    }
    if stop_reason:
        manifest["stop_reason"] = stop_reason
    if started:
        manifest["started_at"] = started
    if finished:
        manifest["finished_at"] = finished
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run_dir / "run_status.json").write_text(
        json.dumps({"run_id": run_id, "status": "complete", "stop_reason": ""}), encoding="utf-8"
    )

    unit_totals: Dict[str, int] = {}
    if postings is not None:
        unit_totals["postings"] = postings
        unit_totals["opportunities"] = postings
    if contacts is not None:
        unit_totals["contacts"] = contacts
    if enrolled is not None:
        unit_totals["enrolled_contacts"] = enrolled
    waterfall = {
        "stages": [
            {
                "stage": "hiring_manager",
                "unit": "lead",
                "primary_reasons": reasons or {"hiring_manager_not_found": 20, "not_icp": 5},
            }
        ],
        "unit_totals": unit_totals,
        "disposition_census": {"FINAL_PASS": 8, "UNVERIFIED": 4},
        "final_pass_count": 8,
    }
    (run_dir / "waterfall.json").write_text(json.dumps(waterfall), encoding="utf-8")

    delivery: Dict[str, Any] = {"mode": "review_staging", "skipped_existing": 3}
    if created is not None:
        delivery["created"] = created
    if enrolled is not None:
        delivery["enrolled"] = enrolled
    (run_dir / "delivery.json").write_text(json.dumps(delivery), encoding="utf-8")

    funnel: Dict[str, Any] = {"companies_considered": 50}
    if reviewed is not None:
        funnel["qualification_input"] = reviewed
    if qualified is not None:
        # "Qualified opportunities" is the contact-discovery entry counter, not
        # the loose upstream role gate. Both are written so a run looks like a
        # real one; only the first is what the metric reads.
        funnel["contact_discovery_entered"] = qualified
        funnel["target_role_eligible"] = qualified
    (run_dir / "orchestrator_result.json").write_text(
        json.dumps(
            {
                "run": {"run_id": run_id, "policy": {"allow_instantly_enrollment": allow_enrollment}},
                "enrichment": {"funnel": funnel, "loss_census": {"hiring_manager_not_found": 20}},
                # Net-new is what the stakeholder funnel counts; the raw provider
                # rows sit beside it as acquisition cost.
                "acquisition": {"cumulative": {
                    "net_new_jobs_captured": postings,
                    "jobs_returned_billed": postings,
                    "historical_duplicates": 0,
                }} if postings is not None else {},
                "delivery": delivery,
                "waterfall": waterfall,
            }
        ),
        encoding="utf-8",
    )
    return run_dir


@pytest.fixture()
def artifact_root(tmp_path: Path) -> Path:
    return tmp_path / "orchestrator_v2"


@pytest.fixture()
def pacific_week() -> Any:
    """The closed week Fri 2026-08-21 -> Fri 2026-08-28, Pacific (PDT)."""
    return explicit_window(date(2026, 8, 21), date(2026, 8, 28), tz_name=PACIFIC_TZ_NAME)


# --------------------------------------------------------------------------
# timewindow: Pacific correctness, no hardcoded offset
# --------------------------------------------------------------------------


def test_summer_week_boundary_is_0700z_and_winter_is_0800z():
    """The same local midnight is a different UTC instant either side of DST."""
    summer = explicit_window(date(2026, 7, 3), date(2026, 7, 10))
    winter = explicit_window(date(2026, 12, 4), date(2026, 12, 11))
    assert iso_z(summer.start_utc) == "2026-07-03T07:00:00Z"
    assert iso_z(winter.start_utc) == "2026-12-04T08:00:00Z"
    # A single hardcoded offset would make these equal; they must not be.
    assert summer.start_utc.hour != winter.start_utc.hour


def test_week_containing_a_dst_transition_is_not_168_hours():
    """Autumn's fall-back week really is 169 hours long; the window must say so."""
    fall_back = explicit_window(date(2026, 10, 30), date(2026, 11, 6))
    spring_forward = explicit_window(date(2026, 3, 6), date(2026, 3, 13))
    assert fall_back.duration_hours == 169.0
    assert spring_forward.duration_hours == 167.0


def test_weekly_window_reports_the_week_that_just_closed():
    """Friday 05:00 Pacific reports Fri->Fri, and never reaches into today."""
    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)  # Fri 05:00 PDT
    window = weekly_window(now, tz_name=PACIFIC_TZ_NAME)
    assert iso_z(window.start_utc) == "2026-08-21T07:00:00Z"
    assert iso_z(window.end_utc) == "2026-08-28T07:00:00Z"
    assert window.end_utc <= now
    assert window.iso_week == "2026-W35"


def test_weekly_window_before_the_boundary_uses_the_previous_week():
    """Thursday night must not report a week that has not closed yet."""
    now = datetime(2026, 8, 28, 6, 0, tzinfo=UTC)  # Thu 23:00 PDT
    window = weekly_window(now, tz_name=PACIFIC_TZ_NAME)
    assert iso_z(window.end_utc) == "2026-08-21T07:00:00Z"


def test_window_is_half_open_so_consecutive_reports_never_double_count():
    first = explicit_window(date(2026, 8, 14), date(2026, 8, 21))
    second = explicit_window(date(2026, 8, 21), date(2026, 8, 28))
    boundary = first.end_utc
    assert not first.contains(boundary)
    assert second.contains(boundary)
    assert first.end_utc == second.start_utc


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 1, 15, 12, tzinfo=UTC),
        datetime(2026, 3, 8, 12, tzinfo=UTC),
        datetime(2026, 7, 4, 12, tzinfo=UTC),
        datetime(2026, 11, 1, 12, tzinfo=UTC),
        datetime(2026, 12, 25, 12, tzinfo=UTC),
    ],
)
def test_builtin_fallback_matches_the_iana_database(moment):
    """The no-tzdata fallback must agree with zoneinfo, or it is not a fallback."""
    iana, source = resolve_timezone(PACIFIC_TZ_NAME)
    assert source != TZ_SOURCE_FALLBACK, "this test needs the real tz database present"
    assert moment.astimezone(iana).utcoffset() == moment.astimezone(UsPacificFallback()).utcoffset()


def test_fallback_window_boundaries_equal_zoneinfo_boundaries():
    real = explicit_window(date(2026, 8, 21), date(2026, 8, 28), tz_name=PACIFIC_TZ_NAME)
    fallback = explicit_window(date(2026, 8, 21), date(2026, 8, 28), tz=UsPacificFallback())
    assert real.start_utc == fallback.start_utc and real.end_utc == fallback.end_utc


def test_naive_now_is_rejected():
    with pytest.raises(ValueError):
        weekly_window(datetime(2026, 8, 28, 12, 0))


# --------------------------------------------------------------------------
# run attribution
# --------------------------------------------------------------------------


def test_runs_are_attributed_by_completion_not_by_job_posted_at(artifact_root, pacific_week):
    """A backlog of 2020 postings processed this week is THIS week's throughput."""
    write_run(artifact_root, "r-inside", finished="2026-08-25T13:00:00Z", posted_at="2020-01-01T00:00:00Z")
    write_run(artifact_root, "r-before", finished="2026-08-20T13:00:00Z")
    runs, problems = discover_runs([artifact_root])
    inside, unattributable = select_window(runs, pacific_week)
    assert problems == []
    assert [r.run_id for r in inside] == ["r-inside"]
    assert unattributable == []
    assert inside[0].attribution_field == "run_manifest.finished_at"


def test_attribution_falls_back_to_started_then_run_id(artifact_root):
    write_run(artifact_root, "r-started", finished=None, started="2026-08-25T13:00:00Z")
    write_run(artifact_root, "20260825T140000Z-abcd1234", finished=None, started=None)
    runs, _ = discover_runs([artifact_root])
    by_id = {r.run_id: r for r in runs}
    assert by_id["r-started"].attribution_field == "run_manifest.started_at"
    assert by_id["20260825T140000Z-abcd1234"].attribution_field == "run_id_prefix"
    assert iso_z(by_id["20260825T140000Z-abcd1234"].attributed_at) == "2026-08-25T14:00:00Z"


def test_an_undateable_run_is_declared_not_silently_dropped(artifact_root, pacific_week):
    write_run(artifact_root, "nodate", finished=None, started=None)
    runs, _ = discover_runs([artifact_root])
    inside, unattributable = select_window(runs, pacific_week)
    assert inside == []
    assert [r.run_id for r in unattributable] == ["nodate"]


def test_a_missing_artifact_root_is_reported_as_a_problem(tmp_path):
    runs, problems = discover_runs([tmp_path / "does_not_exist"])
    assert runs == []
    assert problems and "does not exist" in problems[0]


def test_a_directory_that_is_not_a_run_is_ignored(artifact_root):
    stray = artifact_root / "run_artifacts" / "not_a_run"
    stray.mkdir(parents=True)
    (stray / "notes.txt").write_text("hello", encoding="utf-8")
    assert load_run(stray) is None


def test_unreadable_json_is_recorded_not_fatal(artifact_root):
    run_dir = write_run(artifact_root, "broken", finished="2026-08-25T13:00:00Z")
    (run_dir / "waterfall.json").write_text("{not json", encoding="utf-8")
    record = load_run(run_dir)
    assert record is not None
    assert "waterfall.json" in record.parse_errors


# --------------------------------------------------------------------------
# metrics: silence is never zero
# --------------------------------------------------------------------------


def test_counters_sum_across_the_runs_in_the_window(artifact_root, pacific_week):
    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z", postings=100)
    write_run(artifact_root, "b", finished="2026-08-25T13:00:00Z", postings=40)
    runs, _ = discover_runs([artifact_root])
    inside, _ = select_window(runs, pacific_week)
    metrics = build_run_metrics(inside)
    assert metrics["jobs_captured"].value == 140
    assert metrics["jobs_captured"].status == STATUS_MEASURED
    assert sorted(metrics["jobs_captured"].contributing_run_ids) == ["a", "b"]


def test_a_run_that_does_not_report_a_counter_is_partial_not_zero(artifact_root, pacific_week):
    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z", postings=100)
    write_run(artifact_root, "b", finished="2026-08-25T13:00:00Z", postings=None)
    runs, _ = discover_runs([artifact_root])
    inside, _ = select_window(runs, pacific_week)
    metric = build_run_metrics(inside)["jobs_captured"]
    assert metric.value == 100
    assert metric.status == STATUS_PARTIAL
    assert metric.runs_missing_field == ["b"]
    assert "did not report" in metric.reason


def test_a_counter_no_run_reports_is_unavailable_with_a_reason(artifact_root, pacific_week):
    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z", reviewed=None)
    runs, _ = discover_runs([artifact_root])
    inside, _ = select_window(runs, pacific_week)
    metric = build_run_metrics(inside)["jobs_reviewed"]
    assert metric.value is None
    assert metric.status == STATUS_UNAVAILABLE
    assert "qualification_input" in metric.reason


def test_an_empty_window_reports_unavailable_never_zero():
    metrics = build_run_metrics([])
    for spec in RUN_METRIC_SPECS:
        metric = metrics[spec.key]
        assert metric.value is None
        assert metric.status == STATUS_UNAVAILABLE
        assert "no pipeline run" in metric.reason


def test_review_rate_is_derived_and_refuses_an_empty_denominator(artifact_root, pacific_week):
    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z", postings=200, reviewed=50)
    runs, _ = discover_runs([artifact_root])
    inside, _ = select_window(runs, pacific_week)
    assert build_run_metrics(inside)["review_rate_pct"].value == 25.0

    write_run(artifact_root, "zero", finished="2026-08-23T13:00:00Z", postings=0, reviewed=0)
    metrics = build_run_metrics([load_run(artifact_root / "run_artifacts" / "zero")])
    rate = metrics["review_rate_pct"]
    assert rate.value is None and "undefined" in rate.reason


def test_booleans_are_not_counts():
    assert as_count(True) is None
    assert as_count(-1) is None
    assert as_count(7) == 7
    assert as_count("7") == 7
    assert as_count(7.0) == 7


# --------------------------------------------------------------------------
# sent_to_instantly: measured, or declared unavailable
# --------------------------------------------------------------------------


def test_sent_to_instantly_is_unavailable_when_the_orchestrator_could_not_enroll(
    artifact_root, pacific_week
):
    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z", allow_enrollment=False)
    report = build_report(pacific_week, artifact_roots=[artifact_root])
    metric = report.metrics["sent_to_instantly"]
    assert metric.value is None
    assert metric.status == STATUS_UNAVAILABLE
    assert "Approved Sync" in metric.reason
    assert any(gap.metric == "sent_to_instantly" for gap in report.gaps)


def test_sent_to_instantly_uses_run_artifacts_when_the_run_actually_enrolled(
    artifact_root, pacific_week
):
    write_run(
        artifact_root,
        "a",
        finished="2026-08-22T13:00:00Z",
        allow_enrollment=True,
        enrolled=17,
    )
    report = build_report(pacific_week, artifact_roots=[artifact_root])
    metric = report.metrics["sent_to_instantly"]
    assert metric.value == 17
    # The counter came from the run's own record, not from the Instantly collector.
    # Compared against the constant so the label can evolve without silently
    # breaking the "same source" guard in bottleneck.identify.
    assert metric.source == SOURCE_RUN_ARTIFACTS


def test_sent_to_instantly_prefers_the_instantly_collector(artifact_root, pacific_week):
    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z", allow_enrollment=True, enrolled=17)
    collector = collect_instantly(
        pacific_week,
        cfg=_cfg(),
        campaign_ids=["camp-1"],
        requester=_instantly_requester(
            {
                "camp-1": [
                    {"timestamp_created": "2026-08-22T18:00:00Z"},
                    {"timestamp_created": "2026-08-24T18:00:00Z"},
                    {"timestamp_created": "2026-08-01T18:00:00Z"},  # before the window
                ]
            }
        ),
    )
    report = build_report(pacific_week, artifact_roots=[artifact_root], instantly=collector)
    metric = report.metrics["sent_to_instantly"]
    assert metric.value == 2, "the collector wins over the orchestrator's own counter"
    assert metric.source == "instantly"
    assert metric.attribution == "Instantly lead.timestamp_created"


# --------------------------------------------------------------------------
# collectors: read-only, correctly filtered
# --------------------------------------------------------------------------


class _Cfg:
    INSTANTLY_API_KEY = "test-key"
    INSTANTLY_BASE_URL = "https://api.instantly.ai/api/v2"
    INSTANTLY_CAMPAIGN_ID = "camp-1"
    CAMPAIGN_ENV_BY_BUCKET: Dict[str, str] = {}
    AIRTABLE_TOKEN = "tok"
    AIRTABLE_BASE_ID = "appTest"
    AIRTABLE_TABLE_NAME = "Leads"


def _cfg() -> _Cfg:
    return _Cfg()


def _instantly_requester(pages_by_campaign: Dict[str, List[Dict[str, Any]]], calls: Optional[List] = None):
    def requester(method: str, url: str, *, headers=None, params=None, json_body=None, **_):
        if calls is not None:
            calls.append((method, url, json_body))
        assert method == "POST" and url.endswith("/leads/list")
        assert json_body is not None and "campaign" in json_body, "must use the singular filter"
        assert "campaign_ids" not in json_body, "the plural filter is ignored by the API"
        return {"items": pages_by_campaign.get(json_body["campaign"], []), "next_starting_after": ""}

    return requester


def test_instantly_collector_counts_only_leads_created_in_the_window(pacific_week):
    calls: List = []
    result = collect_instantly(
        pacific_week,
        cfg=_cfg(),
        campaign_ids=["camp-1", "camp-2"],
        requester=_instantly_requester(
            {
                "camp-1": [
                    {"timestamp_created": "2026-08-21T07:00:00Z"},  # exactly the start: inside
                    {"timestamp_created": "2026-08-28T07:00:00Z"},  # exactly the end: outside
                    {"timestamp_created": None},
                ],
                "camp-2": [{"timestamp_created": "2026-08-26T12:00:00Z"}],
            },
            calls,
        ),
    )
    assert result.ok and result.count == 2
    assert result.detail["leads_without_timestamp_created"] == 1
    assert result.detail["per_campaign_in_window"] == {"camp-1": 1, "camp-2": 1}
    assert all(method == "POST" for method, _, _ in calls), "listing only"


def test_instantly_collector_paginates_and_stops():
    window = explicit_window(date(2026, 8, 21), date(2026, 8, 28))
    seen = {"pages": 0}

    def requester(method, url, *, headers=None, params=None, json_body=None, **_):
        seen["pages"] += 1
        if seen["pages"] == 1:
            return {
                "items": [{"timestamp_created": "2026-08-22T10:00:00Z"}],
                "next_starting_after": "cursor-2",
            }
        return {"items": [{"timestamp_created": "2026-08-23T10:00:00Z"}], "next_starting_after": ""}

    result = collect_instantly(window, cfg=_cfg(), campaign_ids=["c"], requester=requester)
    assert seen["pages"] == 2 and result.count == 2


def test_instantly_collector_reports_a_provider_failure_as_a_gap(pacific_week):
    def boom(*args, **kwargs):
        raise RuntimeError("401 unauthorized")

    result = collect_instantly(pacific_week, cfg=_cfg(), campaign_ids=["c"], requester=boom)
    assert not result.ok and result.count is None
    assert "401 unauthorized" in result.errors[0]


def test_instantly_collector_needs_a_key_and_a_campaign(pacific_week):
    class NoKey(_Cfg):
        INSTANTLY_API_KEY = ""

    result = collect_instantly(pacific_week, cfg=NoKey(), requester=_instantly_requester({}))
    assert not result.ok and "INSTANTLY_API_KEY" in result.errors[0]


def test_airtable_collector_counts_created_time_and_labels_status_as_a_snapshot(pacific_week):
    def requester(method, url, *, headers=None, params=None, json_body=None, **_):
        assert method == "GET", "the Airtable collector must never write"
        return {
            "records": [
                {"createdTime": "2026-08-22T10:00:00.000Z", "fields": {"Status": "Pending"}},
                {"createdTime": "2026-08-23T10:00:00.000Z", "fields": {"Status": "Approved"}},
                {"createdTime": "2026-07-01T10:00:00.000Z", "fields": {"Status": "Enrolled"}},
            ],
            "offset": "",
        }

    result = collect_airtable(pacific_week, cfg=_cfg(), requester=requester)
    assert result.ok and result.count == 2
    assert result.detail["by_current_status"] == {"Approved": 1, "Pending": 1}
    assert "CURRENT value" in result.detail["status_caveat"]


# --------------------------------------------------------------------------
# the assembled document
# --------------------------------------------------------------------------


def test_report_document_carries_the_window_stamps_and_run_ids(artifact_root, pacific_week):
    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z")
    write_run(artifact_root, "b", finished="2026-08-26T13:00:00Z")
    document = build_report(
        pacific_week,
        artifact_roots=[artifact_root],
        now=datetime(2026, 8, 28, 13, 0, tzinfo=UTC),
    ).to_dict()

    assert document["reporting_window_start"] == "2026-08-21T07:00:00Z"
    assert document["reporting_window_end"] == "2026-08-28T07:00:00Z"
    assert document["generated_at"] == "2026-08-28T13:00:00Z"
    assert document["included_run_ids"] == ["a", "b"]
    assert document["timezone"] == PACIFIC_TZ_NAME
    assert document["provenance"]["writes_performed"] == "none (read-only report)"
    assert json.loads(json.dumps(document)) == document, "the document must be JSON-serialisable"


def test_report_exposes_every_requested_headline_metric(artifact_root, pacific_week):
    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z")
    document = build_report(pacific_week, artifact_roots=[artifact_root]).to_dict()
    keys = [entry["key"] for entry in document["headline"]]
    assert keys == list(HEADLINE_ORDER)
    for entry in document["headline"]:
        assert entry["definition"], f"{entry['key']} must define itself"
        assert entry["status"] in {"measured", "partial", "unavailable", "not_applicable"}
        if entry["status"] != "unavailable":
            assert entry["evidence"], f"{entry['key']} must name its evidence"
            assert entry["attribution"], f"{entry['key']} must name its timestamp"


def test_report_buckets_runs_by_pacific_day_for_the_dashboard(artifact_root, pacific_week):
    # 2026-08-22T06:00Z is still Friday 23:00 Pacific, not Saturday.
    write_run(artifact_root, "fri", finished="2026-08-22T06:00:00Z", postings=10)
    write_run(artifact_root, "sat", finished="2026-08-22T08:00:00Z", postings=5)
    document = build_report(pacific_week, artifact_roots=[artifact_root]).to_dict()
    days = {bucket["local_day"]: bucket for bucket in document["daily"]}
    assert days["2026-08-21"]["metrics"]["jobs_captured"] == 10
    assert days["2026-08-22"]["metrics"]["jobs_captured"] == 5


def test_bottleneck_is_the_largest_measured_loss(artifact_root, pacific_week):
    write_run(
        artifact_root,
        "a",
        finished="2026-08-22T13:00:00Z",
        postings=100,
        reviewed=95,
        qualified=90,
        contacts=10,
        created=9,
    )
    report = build_report(pacific_week, artifact_roots=[artifact_root])
    assert report.bottleneck.boundary == "contact_discovery"
    assert report.bottleneck.lost == 80
    assert report.actions and report.actions[0].basis.startswith("largest measured loss")


def test_a_week_with_no_runs_names_execution_not_a_funnel_stage(artifact_root, pacific_week):
    artifact_root.mkdir(parents=True, exist_ok=True)
    report = build_report(pacific_week, artifact_roots=[artifact_root])
    assert report.bottleneck.kind == "no_pipeline_activity"
    assert "scheduling" in report.actions[0].action
    assert any(gap.metric == "pipeline_activity" for gap in report.gaps)


def test_zero_capture_names_acquisition_not_clean_throughput(artifact_root, pacific_week):
    """The defect this replaces: with every counter 0 no boundary shows a loss, so
    the report said "no funnel boundary could be shown to lose records" -- a total
    acquisition outage read as clean throughput. Shape copied from the real week of
    2026-08-28: four runs blocked by the credit governor, one that reached the
    provider and still captured nothing."""
    for i in range(4):
        write_run(artifact_root, f"zero{i}", finished=f"2026-08-2{2+i%1}T13:00:00Z",
                  postings=0, reviewed=0, qualified=0, contacts=0, created=0,
                  stop_reason="governor_zero_budget")
    write_run(artifact_root, "reached", finished="2026-08-22T14:00:00Z",
              postings=0, reviewed=0, qualified=0, contacts=0, created=0,
              stop_reason="max_iterations_guard")
    report = build_report(pacific_week, artifact_roots=[artifact_root])
    assert report.bottleneck.kind == "acquisition_entry"
    assert report.bottleneck.boundary == "acquisition"
    assert "captured 0 jobs across 5 runs" in report.bottleneck.statement
    assert "granted zero budget on 4" in report.bottleneck.statement
    assert "1 run had budget and reached the provider" in report.bottleneck.statement
    # The old wording must never appear for a zero-capture week.
    assert "clean end to end" not in report.bottleneck.statement


def test_zero_capture_plan_leads_with_acquisition_not_instrumentation_chores(
        artifact_root, pacific_week):
    for i in range(3):
        write_run(artifact_root, f"z{i}", finished="2026-08-22T13:00:00Z",
                  postings=0, reviewed=0, qualified=0, contacts=0, created=0,
                  stop_reason="governor_zero_budget")
    report = build_report(pacific_week, artifact_roots=[artifact_root])
    assert report.actions[0].basis.startswith("jobs_captured = 0")
    assert "acquisition entry" in report.actions[0].action
    assert any("quota snapshot" in a.action for a in report.actions)
    # Evidence-gap chores must not bury a concrete production problem.
    assert not any("evidence gap" in a.basis for a in report.actions)


def test_positive_acquisition_still_selects_the_largest_funnel_boundary(
        artifact_root, pacific_week):
    """The zero-capture guard must not shadow the normal boundary search."""
    write_run(artifact_root, "ok", finished="2026-08-22T13:00:00Z",
              postings=100, reviewed=95, qualified=90, contacts=10, created=9)
    report = build_report(pacific_week, artifact_roots=[artifact_root])
    assert report.bottleneck.kind == "funnel_boundary"
    assert report.bottleneck.boundary == "contact_discovery"


def test_zero_capture_with_no_recorded_stop_reason_says_so(artifact_root, pacific_week):
    write_run(artifact_root, "u", finished="2026-08-22T13:00:00Z",
              postings=0, reviewed=0, qualified=0, contacts=0, created=0)
    report = build_report(pacific_week, artifact_roots=[artifact_root])
    assert report.bottleneck.kind == "acquisition_entry"
    # "unrecorded" is counted as a run that reached the provider only if it is not a
    # governor stop; either way the wording must never claim a cause it cannot show.
    assert "governor" not in report.bottleneck.statement


def test_a_failed_acquisition_lane_outranks_any_funnel_boundary(artifact_root, pacific_week):
    run_dir = write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z")
    (run_dir / "lanes.json").write_text(
        json.dumps({"fantastic": {"status": "failed", "jobs": 0, "errors": ["422 quota exhausted"]}}),
        encoding="utf-8",
    )
    report = build_report(pacific_week, artifact_roots=[artifact_root])
    assert report.bottleneck.kind == "acquisition_failure"
    assert "fantastic" in report.bottleneck.statement


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def test_summary_never_prints_an_unmeasured_metric_as_zero(artifact_root, pacific_week):
    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z", allow_enrollment=False)
    summary = render_summary(build_report(pacific_week, artifact_roots=[artifact_root]))
    line = next(l for l in summary.splitlines() if l.strip().startswith("Sent to Instantly"))
    assert "not measured" in line and "0" not in line.split("Instantly")[1]
    assert "NOT MEASURED" in summary


def test_summary_states_the_window_and_that_nothing_was_written(artifact_root, pacific_week):
    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z")
    summary = render_summary(build_report(pacific_week, artifact_roots=[artifact_root]))
    assert "2026-08-21T07:00:00Z -> 2026-08-28T07:00:00Z" in summary
    assert "America/Los_Angeles" in summary
    assert "writes performed: none" in summary
    assert max(len(line) for line in summary.splitlines()) <= 110


# --------------------------------------------------------------------------
# the CLI
# --------------------------------------------------------------------------


def test_cli_writes_both_outputs_and_exits_zero(artifact_root, tmp_path, capsys):
    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z")
    out = tmp_path / "out"
    code = run_weekly_report.main(
        [
            "--artifact-root", str(artifact_root),
            "--start", "2026-08-21",
            "--end", "2026-08-28",
            "--out-dir", str(out),
        ]
    )
    assert code == 0
    document = json.loads((out / "weekly_report_2026-W35.json").read_text(encoding="utf-8"))
    assert document["included_run_ids"] == ["a"]
    assert (out / "weekly_report_2026-W35.txt").read_text(encoding="utf-8").startswith("=")
    assert "TGTC WEEKLY PIPELINE REPORT" in capsys.readouterr().out


def test_cli_if_due_skips_on_the_wrong_local_day(artifact_root, tmp_path, capsys):
    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z")
    out = tmp_path / "out"
    code = run_weekly_report.main(
        [
            "--artifact-root", str(artifact_root),
            "--if-due", "friday",
            "--now", "2026-08-26T18:00:00Z",  # Wednesday in Pacific
            "--out-dir", str(out),
        ]
    )
    assert code == 0
    assert not out.exists(), "a skipped report must write nothing"
    assert "not due today" in capsys.readouterr().out


def test_cli_if_due_runs_on_the_right_local_day(artifact_root, tmp_path):
    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z")
    out = tmp_path / "out"
    code = run_weekly_report.main(
        [
            "--artifact-root", str(artifact_root),
            "--if-due", "friday",
            "--now", "2026-08-28T13:00:00Z",  # Friday 06:00 PDT
            "--out-dir", str(out),
            "--quiet",
        ]
    )
    assert code == 0
    document = json.loads((out / "weekly_report_2026-W35.json").read_text(encoding="utf-8"))
    assert document["reporting_window_end"] == "2026-08-28T07:00:00Z"
    assert document["included_run_ids"] == ["a"]


def test_cli_no_write_writes_nothing(artifact_root, tmp_path):
    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z")
    out = tmp_path / "out"
    code = run_weekly_report.main(
        ["--artifact-root", str(artifact_root), "--out-dir", str(out), "--no-write", "--quiet"]
    )
    assert code == 0 and not out.exists()


def test_cli_strict_fails_when_a_headline_metric_is_missing(artifact_root, tmp_path):
    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z", allow_enrollment=False)
    code = run_weekly_report.main(
        [
            "--artifact-root", str(artifact_root),
            "--start", "2026-08-21",
            "--end", "2026-08-28",
            "--out-dir", str(tmp_path / "out"),
            "--quiet",
            "--strict",
        ]
    )
    assert code == 2


def test_cli_defaults_to_the_last_closed_pacific_week(artifact_root, tmp_path):
    write_run(artifact_root, "a", finished="2026-08-25T13:00:00Z")
    out = tmp_path / "out"
    run_weekly_report.main(
        [
            "--artifact-root", str(artifact_root),
            "--now", "2026-08-28T15:00:00Z",
            "--out-dir", str(out),
            "--quiet",
        ]
    )
    document = json.loads((out / "weekly_report_2026-W35.json").read_text(encoding="utf-8"))
    assert document["reporting_window_start"] == "2026-08-21T07:00:00Z"
    assert document["reporting_window_end"] == "2026-08-28T07:00:00Z"
    assert document["reporting_window_duration_hours"] == 168.0


def test_cli_rejects_a_half_specified_window(artifact_root):
    with pytest.raises(SystemExit):
        run_weekly_report.main(["--artifact-root", str(artifact_root), "--start", "2026-08-21"])


# --------------------------------------------------------------------------
# dry runs must never be reported as business throughput
# --------------------------------------------------------------------------


def _make_simulated(run_dir: Path, *, mode: str = "full_dry_run", allow_network: bool = False,
                    lanes: Optional[Dict[str, Any]] = None) -> None:
    """Rewrite a run directory as the dry run it would really be."""
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    manifest["mode"] = mode
    manifest["policy"] = {"allow_network": allow_network, "allow_instantly_enrollment": True}
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if lanes is not None:
        (run_dir / "lanes.json").write_text(json.dumps(lanes), encoding="utf-8")


def test_a_full_dry_run_is_classified_simulated(artifact_root):
    run_dir = write_run(artifact_root, "dry", finished="2026-08-22T13:00:00Z")
    _make_simulated(run_dir)
    record = load_run(run_dir)
    assert record.realism == "simulated"
    assert "full_dry_run" in record.realism_reason


def test_a_networkless_run_is_simulated_even_with_a_live_sounding_mode(artifact_root):
    run_dir = write_run(artifact_root, "offline", finished="2026-08-22T13:00:00Z")
    _make_simulated(run_dir, mode="live_acquisition_and_enrichment", allow_network=False)
    record = load_run(run_dir)
    assert record.realism == "simulated"
    assert "allow_network=false" in record.realism_reason


def test_a_run_whose_only_lane_is_synthetic_is_simulated(artifact_root):
    run_dir = write_run(artifact_root, "synth", finished="2026-08-22T13:00:00Z")
    _make_simulated(
        run_dir,
        mode="live_acquisition_and_enrichment",
        allow_network=True,
        lanes={"synthetic": {"status": "complete", "jobs": 40, "errors": []}},
    )
    assert load_run(run_dir).realism == "simulated"


def test_a_real_run_stays_production(artifact_root):
    run_dir = write_run(artifact_root, "real", finished="2026-08-22T13:00:00Z")
    (run_dir / "lanes.json").write_text(
        json.dumps({"fantastic": {"status": "complete", "jobs": 100, "errors": []}}), encoding="utf-8"
    )
    record = load_run(run_dir)
    assert record.realism == "production" and record.realism_reason == ""


def test_dry_run_counters_are_excluded_from_every_metric(artifact_root, pacific_week):
    """The 2026-08-05 corpus: a full_dry_run reported enrolled=20 against a synthetic lane."""
    dry = write_run(
        artifact_root, "dry", finished="2026-08-22T13:00:00Z", postings=40, contacts=40, enrolled=20
    )
    _make_simulated(dry, lanes={"synthetic": {"status": "complete", "jobs": 40, "errors": []}})
    write_run(artifact_root, "real", finished="2026-08-23T13:00:00Z", postings=7, contacts=3)

    report = build_report(pacific_week, artifact_roots=[artifact_root])
    assert report.run_ids == ["real"]
    assert report.metrics["jobs_captured"].value == 7, "the dry run's 40 postings must not be summed"
    assert report.metrics["sent_to_instantly"].value is None, "a dry run never delivered anything"
    assert [entry["run_id"] for entry in report.excluded_simulated] == ["dry"]
    assert any(gap.metric == "simulated_runs" for gap in report.gaps)


def test_include_simulated_is_available_but_declares_itself(artifact_root, pacific_week):
    dry = write_run(artifact_root, "dry", finished="2026-08-22T13:00:00Z", postings=40)
    _make_simulated(dry)
    report = build_report(pacific_week, artifact_roots=[artifact_root], include_simulated=True)
    assert report.run_ids == ["dry"]
    assert report.metrics["jobs_captured"].value == 40
    assert report.excluded_simulated == []


def test_summary_names_the_excluded_dry_runs(artifact_root, pacific_week):
    dry = write_run(artifact_root, "dry", finished="2026-08-22T13:00:00Z")
    _make_simulated(dry)
    write_run(artifact_root, "real", finished="2026-08-23T13:00:00Z")
    summary = render_summary(build_report(pacific_week, artifact_roots=[artifact_root]))
    assert "Dry runs excluded" in summary and "dry" in summary


def test_a_failed_instantly_read_asks_for_the_credential_not_for_the_flag_again(
    artifact_root, pacific_week
):
    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z")

    class NoKey(_Cfg):
        INSTANTLY_API_KEY = ""

    collector = collect_instantly(pacific_week, cfg=NoKey(), requester=_instantly_requester({}))
    report = build_report(pacific_week, artifact_roots=[artifact_root], instantly=collector)
    gap = next(g for g in report.gaps if g.metric == "sent_to_instantly")
    assert "INSTANTLY_API_KEY" in gap.remedy
    assert "--instantly" not in gap.remedy, "the flag was already passed; asking again helps nobody"


# --------------------------------------------------------------------------
# pre-deployment review fixes
# --------------------------------------------------------------------------


def test_a_campaign_that_fails_mid_pagination_contributes_nothing(pacific_week):
    """The headline must always equal the sum of its own per-campaign breakdown."""
    state = {"pages": 0}

    def requester(method, url, *, headers=None, params=None, json_body=None, **_):
        campaign = json_body["campaign"]
        if campaign == "ok":
            return {"items": [{"timestamp_created": "2026-08-22T10:00:00Z"}], "next_starting_after": ""}
        # "flaky" returns one good page of in-window leads, then blows up.
        state["pages"] += 1
        if state["pages"] == 1:
            return {
                "items": [
                    {"timestamp_created": "2026-08-23T10:00:00Z"},
                    {"timestamp_created": "2026-08-24T10:00:00Z"},
                ],
                "next_starting_after": "cursor-2",
            }
        raise RuntimeError("503 upstream")

    result = collect_instantly(
        pacific_week, cfg=_cfg(), campaign_ids=["ok", "flaky"], requester=requester
    )
    assert result.ok
    assert result.count == sum(result.detail["per_campaign_in_window"].values())
    assert result.count == 1, "the 2 hits from the failed campaign must not leak into the total"
    assert result.detail["campaigns_failed"] == ["flaky"]
    assert result.detail["campaigns_read"] == ["ok"]
    assert any("could not be read" in e for e in result.errors)


def test_a_truncated_campaign_is_flagged_and_makes_the_metric_partial(artifact_root, pacific_week):
    seen = {"n": 0}

    def requester(method, url, *, headers=None, params=None, json_body=None, **_):
        seen["n"] += 1
        return {
            "items": [{"timestamp_created": "2026-08-22T10:00:00Z"}],
            "next_starting_after": f"cursor-{seen['n']}",  # a cursor that never ends
        }

    result = collect_instantly(pacific_week, cfg=_cfg(), campaign_ids=["big"], requester=requester)
    assert result.ok
    assert result.detail["campaigns_truncated"] == ["big"]
    assert any("floor, not a total" in e for e in result.errors)

    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z")
    report = build_report(pacific_week, artifact_roots=[artifact_root], instantly=result)
    assert report.metrics["sent_to_instantly"].status == STATUS_PARTIAL


def test_boundaries_summed_over_different_run_sets_are_not_subtracted(artifact_root, pacific_week):
    """A partial metric makes the neighbouring boundary arithmetic meaningless."""
    write_run(
        artifact_root, "a", finished="2026-08-22T13:00:00Z",
        postings=100, reviewed=100, qualified=100, contacts=90, created=90,
    )
    # 'b' reports postings but is silent about everything downstream.
    write_run(
        artifact_root, "b", finished="2026-08-23T13:00:00Z",
        postings=500, reviewed=None, qualified=None, contacts=None, created=None,
    )
    report = build_report(pacific_week, artifact_roots=[artifact_root])
    assert report.metrics["jobs_captured"].value == 600
    assert report.metrics["jobs_reviewed"].value == 100

    boundaries = {entry["boundary"] for entry in report.bottleneck.incomparable_boundaries}
    assert "review" in boundaries, "600 - 100 is not a loss; the run sets differ"
    assert report.bottleneck.boundary != "review"


def test_incomplete_runs_are_declared_as_a_gap(artifact_root, pacific_week):
    run_dir = write_run(artifact_root, "stopped", finished="2026-08-22T13:00:00Z")
    (run_dir / "run_status.json").write_text(
        json.dumps({"run_id": "stopped", "status": "incomplete", "stop_reason": "budget_exhausted"}),
        encoding="utf-8",
    )
    report = build_report(pacific_week, artifact_roots=[artifact_root])
    assert report.run_status_census == {"incomplete": 1}
    gap = next(g for g in report.gaps if g.metric == "run_completeness")
    assert "incomplete=1" in gap.reason
    assert "Runs not complete: incomplete=1" in render_summary(report)


def test_reports_are_written_atomically_leaving_no_partial_file(artifact_root, tmp_path):
    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z")
    out = tmp_path / "out"
    run_weekly_report.main(
        [
            "--artifact-root", str(artifact_root),
            "--start", "2026-08-21", "--end", "2026-08-28",
            "--out-dir", str(out), "--quiet",
        ]
    )
    assert list(out.glob("*.tmp")) == [], "no temp file may survive a completed write"
    json.loads((out / "weekly_report_2026-W35.json").read_text(encoding="utf-8"))


def test_once_per_window_makes_a_second_run_a_no_op(artifact_root, tmp_path, capsys):
    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z")
    out = tmp_path / "out"
    argv = [
        "--artifact-root", str(artifact_root),
        "--start", "2026-08-21", "--end", "2026-08-28",
        "--out-dir", str(out), "--once-per-window", "--quiet",
    ]
    assert run_weekly_report.main(argv) == 0
    target = out / "weekly_report_2026-W35.json"
    first = target.read_text(encoding="utf-8")

    target.write_text('{"sentinel": true}', encoding="utf-8")
    assert run_weekly_report.main(argv) == 0
    assert target.read_text(encoding="utf-8") == '{"sentinel": true}', "second run must not rewrite"

    assert run_weekly_report.main(argv + ["--force"]) == 0
    assert json.loads(target.read_text(encoding="utf-8"))["included_run_ids"] == ["a"]
    assert first != '{"sentinel": true}'


def test_once_per_window_skips_before_touching_any_provider(artifact_root, tmp_path):
    """A restart must not re-scan Instantly for a window already reported."""
    write_run(artifact_root, "a", finished="2026-08-22T13:00:00Z")
    out = tmp_path / "out"
    out.mkdir(parents=True)
    (out / "weekly_report_2026-W35.json").write_text("{}", encoding="utf-8")

    def explode(*args, **kwargs):
        raise AssertionError("no provider call may happen when the window is already reported")

    original = run_weekly_report.collect_instantly
    run_weekly_report.collect_instantly = explode
    try:
        code = run_weekly_report.main(
            [
                "--artifact-root", str(artifact_root),
                "--start", "2026-08-21", "--end", "2026-08-28",
                "--out-dir", str(out), "--once-per-window", "--instantly", "--quiet",
            ]
        )
    finally:
        run_weekly_report.collect_instantly = original
    assert code == 0


def test_the_time_budget_stops_the_instantly_read_and_declares_a_floor(pacific_week):
    """A hung provider must not outlast the morning the report is due."""
    calls = {"n": 0}

    def clock():
        # In budget for the first campaign only; every later check is past the deadline.
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 100.0

    result = collect_instantly(
        pacific_week,
        cfg=_cfg(),
        campaign_ids=["first", "second", "third"],
        requester=_instantly_requester(
            {"first": [{"timestamp_created": "2026-08-22T10:00:00Z"}]}
        ),
        deadline=50.0,
        clock=clock,
    )
    assert result.ok and result.count == 1, "what was read still counts"
    assert result.detail["campaigns_skipped_out_of_time"] == ["second", "third"]
    assert any("time budget exhausted" in e for e in result.errors)


def test_no_deadline_means_no_early_stop(pacific_week):
    result = collect_instantly(
        pacific_week,
        cfg=_cfg(),
        campaign_ids=["a", "b"],
        requester=_instantly_requester(
            {
                "a": [{"timestamp_created": "2026-08-22T10:00:00Z"}],
                "b": [{"timestamp_created": "2026-08-23T10:00:00Z"}],
            }
        ),
    )
    assert result.count == 2
    assert result.detail["campaigns_skipped_out_of_time"] == []
