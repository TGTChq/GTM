"""A weekly total is only trustworthy if you can name the runs behind it.

"Seven runs, 6,205 captured" is not checkable. A row per run -- each with its own
inclusion decision and its own contribution -- is, and it is the only way to see that
two runs on the same day were both counted rather than one of them quietly dropped.
2026-W36 was reported as four runs when there were seven, and nothing in that report
could have revealed it.

The cron fires daily, but a day can carry several runs: a scheduled one, a manual
re-run after a fix, a run that crashed and was restarted. Every hazard below has a
real shape in this system:

  * two distinct runs finishing on the same Pacific day;
  * one run present in BOTH the ledger and heavy artifacts -- the normal state until
    retention evicts the artifacts;
  * a run that finished but produced nothing;
  * a run that failed part-way;
  * a run whose artifacts carry no timestamp at all;
  * a run that finished just outside the window.

``run_census`` recomputes every headline from its own rows and compares it against
the metric the report renders. The two are computed by different code over the same
runs, so a disagreement means discovery and aggregation saw different run sets.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.run_ledger import RunLedger
from weekly_report.report import build_report
from weekly_report.timewindow import anchored_window

PACIFIC = "America/Los_Angeles"


def _window():
    """Fri 2026-09-04 00:00 PDT -> Fri 2026-09-11 00:00 PDT."""
    return anchored_window(datetime(2026, 9, 4, 7, tzinfo=timezone.utc),
                           datetime(2026, 9, 11, 7, tzinfo=timezone.utc),
                           tz_name=PACIFIC)


def _ledger(root, run_id, *, started, finished, metrics, state="complete"):
    led = RunLedger(root, run_id)
    led.begin(started_at=datetime.fromisoformat(started.replace("Z", "+00:00")),
              mode="live_acquisition_and_enrichment", lanes=("fantastic",))
    if metrics:
        led.record("acquisition", metrics)
    led.finalize(state=state, status=state,
                 finished_at=datetime.fromisoformat(finished.replace("Z", "+00:00")))


def _artifacts(root, run_id, *, started, finished, status="complete",
               waterfall=None, funnel=None, delivery=None, manifest=True):
    d = root / "run_artifacts" / run_id
    d.mkdir(parents=True, exist_ok=True)
    if manifest:
        (d / "run_manifest.json").write_text(json.dumps({
            "run_id": run_id, "started_at": started, "finished_at": finished,
            "status": status, "mode": "live_acquisition_and_enrichment",
            "policy": {"allow_instantly_enrollment": False}}), encoding="utf-8")
    (d / "waterfall.json").write_text(json.dumps(waterfall or {}), encoding="utf-8")
    (d / "orchestrator_result.json").write_text(json.dumps({
        "acquisition": {"cumulative": {}}, "enrichment": {"funnel": funnel or {}},
        "delivery": delivery or {}, "lanes": {}}), encoding="utf-8")


def _build(root):
    return build_report(_window(), artifact_roots=[str(root)],
                        now=datetime(2026, 9, 11, 7, tzinfo=timezone.utc))


def _census_row(report, run_id):
    return next(r for r in report.census["runs"] if r["run_id"] == run_id)


def _populated_week(root):
    """Three runs on Mon 2026-09-07 Pacific, plus the shapes that must not corrupt them."""
    # 03:00 UTC = 20:00 PDT the previous day; use UTC times that land on Sep 7 PDT.
    _ledger(root, "20260907T170000Z-morning1", started="2026-09-07T17:00:00Z",
            finished="2026-09-07T18:00:00Z",
            metrics={"net_new_jobs_captured": 400, "jobs_reviewed": 400,
                     "qualified_opportunities": 120, "contacts_found": 60,
                     "sent_to_airtable": 40})
    # Same Pacific day, a second distinct run.
    _ledger(root, "20260907T230000Z-evening1", started="2026-09-07T23:00:00Z",
            finished="2026-09-08T00:30:00Z",
            metrics={"net_new_jobs_captured": 250, "jobs_reviewed": 250,
                     "qualified_opportunities": 80, "contacts_found": 30,
                     "sent_to_airtable": 25})
    # Third run the same day, which acquired nothing. A measured zero, not silence.
    _ledger(root, "20260907T200000Z-emptyrun", started="2026-09-07T20:00:00Z",
            finished="2026-09-07T20:02:00Z",
            metrics={"net_new_jobs_captured": 0, "jobs_reviewed": 0,
                     "qualified_opportunities": 0, "contacts_found": 0,
                     "sent_to_airtable": 0})
    # A run that failed part-way: it captured, then stopped. Reviewed is SILENT.
    _ledger(root, "20260908T170000Z-partial1", started="2026-09-08T17:00:00Z",
            finished="2026-09-08T17:20:00Z", state="failed",
            metrics={"net_new_jobs_captured": 90})
    return root


class TestEveryRunIsAccountedFor:
    def test_two_runs_on_one_day_are_both_counted(self, tmp_path):
        report = _build(_populated_week(tmp_path / "orchestrator_v2"))

        day = report.census["included_runs_by_local_day"]["2026-09-07"]
        assert day == ["20260907T170000Z-morning1", "20260907T200000Z-emptyrun",
                       "20260907T230000Z-evening1"], day
        assert report.metrics["contacts_found"].value == 90, "60 + 30 + 0"

    def test_the_census_adds_back_up_to_every_rendered_total(self, tmp_path):
        report = _build(_populated_week(tmp_path / "orchestrator_v2"))

        assert report.census["all_reconcile"], report.census["reconciles"]
        for key, check in report.census["reconciles"].items():
            assert check["census_total"] == check["reported_value"], key
            assert check["census_runs"] == check["reported_runs"], key

    def test_completeness_is_reconciled_not_just_the_value(self, tmp_path):
        """A subtotal of zero over the runs that answered is not a measured zero for
        the period while another included run is silent. "Nothing happened" and "one
        run could not say" are different claims, and comparing values alone cannot
        tell them apart -- so the census checks the STATUS the report will render."""
        report = _build(_populated_week(tmp_path / "orchestrator_v2"))

        reviewed = report.census["reconciles"]["jobs_reviewed"]
        assert reviewed["census_silent_runs"] == ["20260908T170000Z-partial1"]
        assert reviewed["census_expected_status"] == "partial"
        assert reviewed["reported_status"] == "partial"
        assert reviewed["values_agree"] and reviewed["completeness_agrees"]

        captured = report.census["reconciles"]["jobs_captured"]
        assert captured["census_silent_runs"] == []
        assert captured["census_expected_status"] == "measured"
        assert captured["reported_status"] == "measured"

    def test_a_silent_run_cannot_certify_a_complete_zero(self, tmp_path):
        """The specific corruption: every run that answered reported 0, one run
        answered nothing, and the period must not read as a measured zero."""
        root = tmp_path / "orchestrator_v2"
        _ledger(root, "20260907T170000Z-zeroonly", started="2026-09-07T17:00:00Z",
                finished="2026-09-07T18:00:00Z",
                metrics={"net_new_jobs_captured": 0, "jobs_reviewed": 0})
        _ledger(root, "20260907T230000Z-silent01", started="2026-09-07T23:00:00Z",
                finished="2026-09-08T00:30:00Z", metrics={})

        report = _build(root)
        check = report.census["reconciles"]["jobs_captured"]

        assert check["census_total"] == 0, "the runs that answered add to zero"
        assert check["census_silent_runs"] == ["20260907T230000Z-silent01"]
        assert check["census_expected_status"] == "partial", "not a complete zero"
        assert check["reported_status"] == "partial"
        assert check["agrees"], "value and completeness both reconcile"

    def test_a_run_in_both_stores_is_counted_once(self, tmp_path):
        """The normal state before retention evicts the artifacts."""
        root = _populated_week(tmp_path / "orchestrator_v2")
        _artifacts(root, "20260907T170000Z-morning1",
                   started="2026-09-07T17:00:00Z", finished="2026-09-07T18:00:00Z",
                   waterfall={"unit_totals": {"contacts": 60}})

        report = _build(root)

        rows = [r for r in report.census["runs"]
                if r["run_id"] == "20260907T170000Z-morning1"]
        assert len(rows) == 1, "one run, one row"
        assert rows[0]["evidence"] == "ledger+artifacts"
        assert report.metrics["contacts_found"].value == 90, "not 150"

    def test_a_failed_run_contributes_what_it_measured_and_no_more(self, tmp_path):
        report = _build(_populated_week(tmp_path / "orchestrator_v2"))
        row = _census_row(report, "20260908T170000Z-partial1")

        assert row["decision"] == "included", "a failed run still did real work"
        assert row["state"] == "failed"
        assert row["contributes"]["jobs_captured"]["value"] == 90
        assert row["contributes"]["jobs_reviewed"]["value"] is None, (
            "it stopped before review; silence is not a zero")
        assert report.metrics["jobs_captured"].value == 740, "400 + 250 + 0 + 90"
        assert report.metrics["jobs_reviewed"].value == 650, "400 + 250 + 0"
        assert report.metrics["jobs_reviewed"].runs_missing_field == [
            "20260908T170000Z-partial1"]

    def test_a_zero_output_run_is_a_measured_zero_not_a_silence(self, tmp_path):
        report = _build(_populated_week(tmp_path / "orchestrator_v2"))
        row = _census_row(report, "20260907T200000Z-emptyrun")

        assert row["contributes"]["jobs_captured"]["value"] == 0
        assert "20260907T200000Z-emptyrun" in report.metrics["jobs_captured"].contributing_run_ids
        assert report.metrics["jobs_captured"].status == "measured"

    def test_a_run_outside_the_window_is_named_and_not_counted(self, tmp_path):
        root = _populated_week(tmp_path / "orchestrator_v2")
        _ledger(root, "20260903T170000Z-tooearly", started="2026-09-03T17:00:00Z",
                finished="2026-09-03T18:00:00Z",
                metrics={"net_new_jobs_captured": 9999, "contacts_found": 9999})

        report = _build(root)
        row = _census_row(report, "20260903T170000Z-tooearly")

        assert row["decision"] == "excluded"
        assert "outside the window" in row["reason"]
        assert report.metrics["jobs_captured"].value == 740, "the early run adds nothing"
        assert report.census["all_reconcile"]

    def test_a_run_with_no_timestamp_is_declared_rather_than_dropped(self, tmp_path):
        root = _populated_week(tmp_path / "orchestrator_v2")
        d = root / "run_artifacts" / "20260907T999999Z-notime01"
        d.mkdir(parents=True, exist_ok=True)
        (d / "orchestrator_result.json").write_text(
            json.dumps({"acquisition": {"cumulative": {}}, "enrichment": {"funnel": {}}}),
            encoding="utf-8")

        report = _build(root)

        assert "20260907T999999Z-notime01" in report.unattributable_run_ids
        row = _census_row(report, "20260907T999999Z-notime01")
        assert row["decision"] == "excluded"
        assert "timestamp" in row["reason"]
        assert report.census["all_reconcile"], "declaring it must not disturb the totals"

    def test_the_units_stay_distinct_across_the_census(self, tmp_path):
        """Postings, company x role-bucket opportunities and Instantly leads are
        three populations. The census must not invite a subtraction across them."""
        report = _build(_populated_week(tmp_path / "orchestrator_v2"))
        units = {k: report.metrics[k].counted_unit
                 for k in ("jobs_captured", "jobs_reviewed", "qualified_opportunities",
                           "contacts_found", "sent_to_airtable")}
        assert units["jobs_captured"] == units["jobs_reviewed"] == "posting"
        assert units["qualified_opportunities"] == units["contacts_found"] == \
            units["sent_to_airtable"] == "company_role_bucket_opportunity"

    def test_the_census_stays_out_of_brett_s_message(self, tmp_path):
        from weekly_report.render import render_stakeholder_summary

        report = _build(_populated_week(tmp_path / "orchestrator_v2"))
        text = render_stakeholder_summary(report)

        assert "run_census" not in text
        for run_id in report.census["included"]:
            assert run_id not in text
        assert "run_census" in report.to_dict(), "but it IS in the document"
