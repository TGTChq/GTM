"""Brett's headline semantics, and the plan that must agree with the bottleneck.

Three requirements, all traced to real production evidence from 2026-09-04:

* "Qualified opportunities" must be a commercially qualified opportunity. The
  counter it used to read (``target_role_eligible``, the pre-contact role/source
  gate) passed 5,746 of 6,206 postings -- 92.6% -- on that day's control run. A
  stage that passes almost everything is not a qualification stage, and reporting
  it as one hid the ICP decision that actually rejected 606 opportunities.
* The reason census must survive heavy-artifact pruning, or the action plan
  degrades to generic text for most of any week.
* The action plan must never contradict the bottleneck. The 2026-W36 preview
  printed "lost 5157 of 6205 records (83.1%)" directly above "no boundary lost
  enough records this week to justify a change".
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.run_ledger import backfill_from_artifacts, read_entries
from test_weekly_report import write_run
from test_weekly_report_ledger import write_ledger_run
from weekly_report.evidence import STATUS_UNAVAILABLE
from weekly_report.report import build_report
from weekly_report.timewindow import explicit_window

UTC = timezone.utc


def _september_window():
    """Sep 4 - Sep 10 2026 Pacific: the window today's productive run falls in."""
    return explicit_window(datetime(2026, 9, 4).date(), datetime(2026, 9, 11).date(),
                           boundary_hour=0, tz_name="America/Los_Angeles")


def _bare_run(root: Path, run_id: str, funnel: dict, *, finished: str) -> None:
    """A run carrying only a manifest and a funnel, to isolate one metric."""
    run_dir = root / "run_artifacts" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_manifest.json").write_text(json.dumps({
        "run_id": run_id, "status": "complete",
        "mode": "live_acquisition_and_enrichment",
        "started_at": "2026-09-05T13:00:00Z", "finished_at": finished,
    }), encoding="utf-8")
    (run_dir / "orchestrator_result.json").write_text(
        json.dumps({"enrichment": {"funnel": funnel}}), encoding="utf-8")


# --------------------------------------------------------------------------
# qualified opportunities
# --------------------------------------------------------------------------


def test_qualified_opportunities_is_contact_discovery_entry_not_the_role_gate(tmp_path):
    root = tmp_path / "orchestrator_v2"
    _bare_run(root, "20260905T130000Z-aaaaaaaa", {
        "qualification_input": 2410,
        "target_role_eligible": 2230,        # the loose upstream gate
        "contact_discovery_entered": 1698,   # job/role + company ICP + searchable
    }, finished="2026-09-05T16:00:00Z")

    report = build_report(_september_window(), artifact_roots=[root])

    assert report.metrics["qualified_opportunities"].value == 1698
    assert report.metrics["role_qualified_postings"].value == 2230, (
        "the role gate is still reported, under an honest name"
    )


def test_a_run_without_the_new_counter_reports_qualified_as_unavailable(tmp_path):
    """No silent fallback to the looser counter for pre-2026-09-05 runs."""
    root = tmp_path / "orchestrator_v2"
    _bare_run(root, "20260904T130130Z-13b44a0c", {"target_role_eligible": 5746},
              finished="2026-09-04T16:20:00Z")

    report = build_report(_september_window(), artifact_roots=[root])

    metric = report.metrics["qualified_opportunities"]
    assert metric.status == STATUS_UNAVAILABLE and metric.value is None, (
        "reporting the wrong stage under the right name is worse than reporting nothing"
    )


def test_the_backfill_cannot_reintroduce_the_loose_counter(tmp_path):
    """Regression: the backfill mapped qualified_opportunities to the role gate.

    The metric spec was corrected but the backfill was not, so a pre-ledger run
    came back through the ledger carrying 2,230 "qualified opportunities" that
    were really role-gate passes. Caught by rendering the message, not by a unit
    test, which is why this one exists.
    """
    root = tmp_path / "orchestrator_v2"
    _bare_run(root, "20260904T130130Z-13b44a0c", {"target_role_eligible": 2230},
              finished="2026-09-04T16:20:00Z")
    backfill_from_artifacts(root)

    entry = read_entries(root)[0][0]
    assert "qualified_opportunities" not in entry["metrics"]
    assert entry["metrics"]["role_qualified_postings"] == 2230

    shutil.rmtree(root / "run_artifacts")
    report = build_report(_september_window(), artifact_roots=[root])
    assert report.metrics["qualified_opportunities"].status == STATUS_UNAVAILABLE
    assert report.metrics["role_qualified_postings"].value == 2230


# --------------------------------------------------------------------------
# the reason census must survive pruning
# --------------------------------------------------------------------------


def _productive_run(root: Path, run_id: str = "20260905T130000Z-aaaaaaaa") -> None:
    """One productive run with heavy artifacts, in today's real production shape."""
    write_run(root, run_id, started="2026-09-05T13:00:00Z", finished="2026-09-05T16:00:00Z",
              postings=6205, reviewed=2410, qualified=1698, contacts=1048, created=781,
              reasons={"email_unverified": 740, "not_icp": 606,
                       "hiring_manager_not_found": 247, "company_unresolved": 106})


def test_a_pruned_week_reports_identically_to_an_artifact_backed_one(tmp_path):
    """The whole contract in one test: delete the evidence, keep the report."""
    root = tmp_path / "orchestrator_v2"
    _productive_run(root)
    backfill_from_artifacts(root)
    window = _september_window()

    before = build_report(window, artifact_roots=[root])
    shutil.rmtree(root / "run_artifacts")          # retention, eventually
    after = build_report(window, artifact_roots=[root])

    for key in ("jobs_captured", "jobs_reviewed", "qualified_opportunities",
                "contacts_found", "sent_to_airtable", "review_rate_pct"):
        assert before.metrics[key].value == after.metrics[key].value, key
        assert before.metrics[key].status == after.metrics[key].status, key

    assert after.metrics["qualified_opportunities"].value == 1698
    assert before.reasons == after.reasons, "the reason census survives pruning"
    assert after.reasons["email_unverified"] == 740
    assert before.bottleneck.boundary == after.bottleneck.boundary
    assert before.bottleneck.lost == after.bottleneck.lost
    assert [a.action for a in before.actions] == [a.action for a in after.actions], (
        "next-week actions are materially identical"
    )
    assert after.to_dict()["provenance"]["runs_reported_from_ledger_only"] == [
        "20260905T130000Z-aaaaaaaa"]


def test_the_ledger_census_is_bounded_and_counts_only(tmp_path):
    root = tmp_path / "orchestrator_v2"
    write_run(root, "20260905T130000Z-aaaaaaaa", started="2026-09-05T13:00:00Z",
              finished="2026-09-05T16:00:00Z",
              reasons={f"reason_{i:02d}": 100 - i for i in range(40)})
    backfill_from_artifacts(root)

    entry = read_entries(root)[0][0]
    census = entry["loss_reasons"]
    assert len(census) <= 12, "the census is bounded"
    assert all(isinstance(v, int) for v in census.values()), "counts only, no payloads"
    assert census["reason_00"] == 100, "the largest reasons are the ones kept"
    assert len(json.dumps(entry)) < 4000, "the entry stays tiny"


# --------------------------------------------------------------------------
# the action plan may never contradict the bottleneck
# --------------------------------------------------------------------------


def _loss_without_reasons(root: Path) -> None:
    """A collapsed but COMPARABLE boundary: contact discovery is unmeasured, so
    the adjacent pair becomes qualified_opportunities -> sent_to_airtable. Both
    count company x role-bucket units, so the difference is a real loss."""
    write_ledger_run(root, "20260905T130000Z-aaaaaaaa", started="2026-09-05T13:00:00Z",
                     finished="2026-09-05T16:00:00Z",
                     metrics={"qualified_opportunities": 6205, "sent_to_airtable": 1048})


def test_the_plan_never_claims_no_loss_when_a_loss_was_measured(tmp_path):
    root = tmp_path / "orchestrator_v2"
    _loss_without_reasons(root)

    report = build_report(_september_window(), artifact_roots=[root])

    assert report.bottleneck.lost == 5157
    text = " ".join(a.action for a in report.actions)
    basis = " ".join(a.basis for a in report.actions)
    assert "no boundary lost enough records" not in text
    assert "no measured loss at any funnel boundary" not in basis
    assert report.actions, "a measured loss always produces at least one action"
    assert any("not attributable" in a.action or "spans more than one stage" in a.action
               for a in report.actions)


def test_a_collapsed_boundary_still_produces_a_stage_level_action(tmp_path):
    """Unmeasured intermediate stages make the pair non-adjacent; do not drop it."""
    root = tmp_path / "orchestrator_v2"
    _loss_without_reasons(root)

    report = build_report(_september_window(), artifact_roots=[root])

    assert report.bottleneck.boundary == "qualified_opportunities->sent_to_airtable"
    assert any("spans more than one stage" in a.action for a in report.actions)
    assert str(report.bottleneck.lost) in " ".join(a.basis for a in report.actions)


def test_postings_are_never_subtracted_from_opportunities(tmp_path):
    """The exact subtraction that invented a bottleneck.

    ``jobs_captured`` counts POSTINGS. ``contacts_found`` counts COMPANY x ROLE
    BUCKET units -- ``hiring_manager`` produces one Lead per bucket, not per
    posting. 6,205 - 1,048 is not "5,157 lost"; the second number does not count a
    subset of the first. With no comparable pair left, the report must say the
    bottleneck cannot be measured rather than pick the biggest-looking gap.
    """
    root = tmp_path / "orchestrator_v2"
    write_ledger_run(root, "20260905T130000Z-aaaaaaaa", started="2026-09-05T13:00:00Z",
                     finished="2026-09-05T16:00:00Z",
                     metrics={"jobs_captured": 6205, "contacts_found": 1048})

    report = build_report(_september_window(), artifact_roots=[root])

    assert report.bottleneck.lost is None
    assert report.bottleneck.kind == "no_measured_loss"
    refused = {b["boundary"]: b["reason"]
               for b in report.bottleneck.incomparable_boundaries}
    assert "jobs_captured->contacts_found" in refused
    assert "not a subset" in refused["jobs_captured->contacts_found"]
    assert "5157" not in json.dumps(report.to_dict()), (
        "the invalid difference must not appear anywhere in the document"
    )


def test_instantly_enrollments_are_never_treated_as_a_delivery_loss(tmp_path):
    """Same unit is not enough -- the COHORT differs.

    Enrollment is performed by GTM Approved Sync from the Airtable Approved
    backlog, which accumulates across weeks: the 2026-09-05 sync delivered 770
    leads from 781 rows built up over the preceding fortnight. Subtracting that
    from this window's Airtable rows compares two unrelated populations that
    happen to share a date range.
    """
    root = tmp_path / "orchestrator_v2"
    write_ledger_run(root, "20260905T130000Z-aaaaaaaa", started="2026-09-05T13:00:00Z",
                     finished="2026-09-05T16:00:00Z",
                     metrics={"qualified_opportunities": 900, "contacts_found": 800,
                              "sent_to_airtable": 781})

    from weekly_report.external import CollectorResult

    collector = CollectorResult(name="instantly", enabled=True, ok=True, count=12)
    report = build_report(_september_window(), artifact_roots=[root],
                          instantly=collector)

    assert report.metrics["sent_to_instantly"].value == 12
    refused = {b["boundary"] for b in report.bottleneck.incomparable_boundaries}
    assert "instantly_delivery" in refused
    assert report.bottleneck.boundary != "instantly_delivery"


def test_a_genuinely_clean_week_is_not_described_as_a_loss(tmp_path):
    """The mirror of the contradiction: no loss must not produce loss language."""
    root = tmp_path / "orchestrator_v2"
    write_ledger_run(root, "20260905T130000Z-aaaaaaaa", started="2026-09-05T13:00:00Z",
                     finished="2026-09-05T16:00:00Z",
                     metrics={"jobs_captured": 100, "contacts_found": 100,
                              "sent_to_airtable": 100})

    report = build_report(_september_window(), artifact_roots=[root])

    assert report.bottleneck.kind == "no_measured_loss"
    assert (report.bottleneck.lost or 0) == 0
    text = " ".join(a.action for a in report.actions)
    assert "not attributable" not in text and "spans more than one stage" not in text, (
        "loss language must not appear when nothing was measured to be lost"
    )
    # With stages still unmeasured the plan is evidence-gap chores, which is the
    # correct answer for a clean-but-incompletely-instrumented week.
    assert all(a.basis.startswith("evidence gap on") for a in report.actions)


# --------------------------------------------------------------------------
# captured vs reviewed must be comparable populations
# --------------------------------------------------------------------------


def test_reviewing_every_net_new_posting_is_a_100_percent_review_rate(tmp_path):
    """The populations must be comparable, or the rate is meaningless.

    Production 2026-09-04 returned 6,205 provider rows and reviewed 2,410. The
    report called that a 61% "review boundary" loss. Most of the gap was rows a
    previous run had already processed -- an acquisition cost, not work anyone
    dropped. Counting them as captured invented a bottleneck.
    """
    root = tmp_path / "orchestrator_v2"
    write_ledger_run(root, "20260905T130000Z-aaaaaaaa", started="2026-09-05T13:00:00Z",
                     finished="2026-09-05T16:00:00Z",
                     metrics={"net_new_jobs_captured": 2410,
                              "provider_jobs_returned": 6205,
                              "historical_duplicates": 3795,
                              "jobs_reviewed": 2410})

    report = build_report(_september_window(), artifact_roots=[root])

    assert report.metrics["jobs_captured"].value == 2410, "captured is NET-NEW"
    assert report.metrics["jobs_reviewed"].value == 2410
    # 100%, and structurally so: captured and reviewed are one list measured at two
    # points. The rate is published because the stakeholder format specifies it, not
    # because it measures a conversion -- see tests/test_captured_equals_reviewed.py.
    assert report.metrics["review_rate_pct"].value == 100.0
    # The provider duplication is still visible, just not as a funnel loss.
    assert report.metrics["provider_jobs_returned"].value == 6205
    assert report.metrics["historical_duplicates"].value == 3795


def test_the_review_boundary_is_no_longer_the_invented_bottleneck(tmp_path):
    """With comparable populations, nothing is lost at review.

    SYNTHETIC figures. 2,410 and 1,698 were never emitted as posting or
    qualification counters by any production build -- 2,410 was
    ``unique_opportunities`` (company x bucket lead units) and 1,698 was
    reconstructed from dispositions. They are used here only to exercise the
    renderer and the boundary search.
    """
    root = tmp_path / "orchestrator_v2"
    write_ledger_run(root, "20260905T130000Z-aaaaaaaa", started="2026-09-05T13:00:00Z",
                     finished="2026-09-05T16:00:00Z",
                     metrics={"net_new_jobs_captured": 2410, "jobs_reviewed": 2410,
                              "qualified_opportunities": 1698, "contacts_found": 1048,
                              "sent_to_airtable": 781})

    report = build_report(_september_window(), artifact_roots=[root])

    assert report.bottleneck.boundary != "review", (
        "a fully reviewed batch must not be reported as the biggest loss"
    )
    # ``qualification`` is jobs_reviewed (POSTINGS) -> qualified_opportunities
    # (COMPANY x ROLE BUCKET). 2410 - 1698 = 712 looks like a loss and is not one:
    # the second number does not count a subset of the first. That boundary is now
    # refused, and the largest COMPARABLE loss is contact discovery, which is
    # opportunity -> opportunity on both sides.
    assert report.bottleneck.boundary == "contact_discovery"
    assert report.bottleneck.entered == 1698 and report.bottleneck.advanced == 1048
    assert report.bottleneck.lost == 650
    refused = {b["boundary"] for b in report.bottleneck.incomparable_boundaries}
    assert "qualification" in refused
    assert any("not a loss" in b["reason"]
               for b in report.bottleneck.incomparable_boundaries)


def test_provider_duplication_never_counts_as_captured(tmp_path):
    """A re-bought posting is acquisition cost, and must not inflate throughput."""
    root = tmp_path / "orchestrator_v2"
    write_ledger_run(root, "20260905T130000Z-aaaaaaaa", started="2026-09-05T13:00:00Z",
                     finished="2026-09-05T16:00:00Z",
                     metrics={"net_new_jobs_captured": 0, "provider_jobs_returned": 6000,
                              "historical_duplicates": 6000, "jobs_reviewed": 0})

    report = build_report(_september_window(), artifact_roots=[root])

    assert report.metrics["jobs_captured"].value == 0, (
        "a run that bought 6000 rows it already had captured nothing"
    )
    assert report.metrics["provider_jobs_returned"].value == 6000
