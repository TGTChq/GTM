"""Production acceptance for the weekly report, as executable requirements.

Three separate things are pinned here, each of which was a live defect on
2026-09-04:

1. **The ``--if-due`` gate must be on the scheduler's calendar.** Covered in
   ``test_weekly_report_anchored_window.py``; the ordering requirement it implies
   is covered below.

2. **A pruned week must render identically.** The whole reason the compact ledger
   exists is that heavy run artifacts are deleted by retention long before the
   8-week reporting horizon. If the two stores do not produce the same
   stakeholder message, the ledger is a second opinion rather than a survivor --
   so the acceptance test is byte equality of the literal Slack text, not "the
   numbers look close".

3. **Instantly campaign coverage must include the Wave 1 challenger arm.**
   ``configured_campaign_ids`` enumerated ``INSTANTLY_CAMPAIGN_*`` only. Wave 1 is
   enabled in production at a 50% account split with ten challenger campaigns
   that are not in ``CAMPAIGN_ENV_BY_BUCKET``, so "sent to Instantly" was set to
   under-report by roughly half -- as a clean number, with no gap declared.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import run_weekly_report
from orchestrator.run_ledger import LEDGER_STORE
from weekly_report.external import configured_campaign_ids
from test_weekly_report import write_run
from test_weekly_report_ledger import _instant


# --------------------------------------------------------------------------
# 2. heavy artifacts vs ledger-only: the same message, byte for byte
# --------------------------------------------------------------------------

WINDOW = ("--start", "2026-09-05", "--end", "2026-09-12")

#: One productive run, expressed once. Both stores are populated from it, so the
#: test cannot pass by accident of two different fixtures agreeing loosely.
RUN = {
    "run_id": "20260908T030131Z-acceptance",
    "finished": "2026-09-08T03:41:00Z",
    "postings": 2411,
    "reviewed": 2380,
    "qualified": 1048,
    "contacts": 781,
    "created": 604,
}


def _render(root: Path, out: Path) -> str:
    """Generate into ``out`` and return the literal Slack text."""
    rc = run_weekly_report.main([
        "--artifact-root", str(root), *WINDOW,
        "--out-dir", str(out), "--quiet",
    ])
    assert rc == 0
    slack = sorted(out.glob("*.slack.txt"))
    assert len(slack) == 1, f"expected one Slack message, got {[p.name for p in slack]}"
    return slack[0].read_text(encoding="utf-8")


#: The loss census the pipeline records at STAGE_FINAL. It is part of the ledger
#: contract, not decoration: without it the report can still state the size of a
#: drop but not its cause, and says so -- which is a DIFFERENT message from the
#: heavy render. Byte equality is therefore also a check that the ledger carries
#: everything the stakeholder page is built from.
LOSS_REASONS = {"hiring_manager_not_found": 247, "email_unverified": 740, "not_icp": 606}


@pytest.fixture
def heavy_root(tmp_path):
    """A run with BOTH stores populated, in the order production writes them."""
    from orchestrator.run_ledger import RunLedger

    root = tmp_path / "artifacts"
    write_run(
        root, RUN["run_id"], finished=RUN["finished"],
        postings=RUN["postings"], reviewed=RUN["reviewed"], qualified=RUN["qualified"],
        contacts=RUN["contacts"], created=RUN["created"],
        reasons=LOSS_REASONS,
    )
    ledger = RunLedger(root, RUN["run_id"])
    ledger.begin(
        started_at=_instant("2026-09-08T03:01:31Z"),
        mode="live_acquisition_and_enrichment", allow_network=True,
        allow_enrichment=True, allow_instantly_enrollment=False, lanes=("fantastic",),
    )
    ledger.record("acquisition", {
        "jobs_captured": RUN["postings"],
        "net_new_jobs_captured": RUN["postings"],
    })
    ledger.record("enrichment", {
        "jobs_reviewed": RUN["reviewed"],
        "qualified_opportunities": RUN["qualified"],
        "contacts_found": RUN["contacts"],
    })
    ledger.record("delivery", {"sent_to_airtable": RUN["created"]})
    ledger.record("final", loss_reasons=dict(LOSS_REASONS))
    ledger.finalize(state="complete", status="complete",
                    finished_at=_instant(RUN["finished"]))
    return root


def test_a_week_renders_identically_from_the_ledger_alone(heavy_root, tmp_path):
    """Retention deletes the heavy evidence; the message must not change.

    The ledger-only root is built by COPYING only the ledger store and nothing
    else, which is exactly the state a pruned volume is in eight weeks later.
    """
    with_heavy = _render(heavy_root, tmp_path / "out_heavy")

    ledger_only = tmp_path / "ledger_only"
    shutil.copytree(heavy_root / LEDGER_STORE, ledger_only / LEDGER_STORE)
    assert not (ledger_only / "run_artifacts").exists(), "the heavy evidence is gone"

    without_heavy = _render(ledger_only, tmp_path / "out_ledger")

    assert without_heavy == with_heavy, (
        "the stakeholder message must not depend on whether the heavy artifacts "
        "still exist"
    )


def test_the_ledger_only_render_still_names_the_run_it_counted(heavy_root, tmp_path):
    ledger_only = tmp_path / "ledger_only"
    shutil.copytree(heavy_root / LEDGER_STORE, ledger_only / LEDGER_STORE)
    _render(ledger_only, tmp_path / "out")
    document = json.loads(
        next((tmp_path / "out").glob("*.json")).read_text(encoding="utf-8"))
    assert document["included_run_ids"] == [RUN["run_id"]]
    assert document["metrics"]["jobs_captured"]["value"] == RUN["postings"]


def test_acceptance_renders_without_sending_slack_or_writing_a_receipt(
        heavy_root, tmp_path):
    """Acceptance must be observable without delivering anything."""
    out = tmp_path / "out"
    _render(heavy_root, out)
    assert not list(out.glob("*.slack_sent.json")), "no delivery receipt is written"
    assert not (heavy_root / "weekly_reports").exists(), (
        "--out-dir keeps acceptance out of the production report directory"
    )


def test_an_explicit_window_never_touches_the_production_anchor(heavy_root, tmp_path):
    """``--start/--end`` is the acceptance path precisely because it cannot move
    the boundary the next real report will read."""
    _render(heavy_root, tmp_path / "out")
    anchors = list(heavy_root.rglob("weekly_report_anchor.json"))
    assert anchors == [], "acceptance must leave the anchor untouched"


# --------------------------------------------------------------------------
# 3. Instantly campaign coverage
# --------------------------------------------------------------------------


class _Cfg:
    CAMPAIGN_ENV_BY_BUCKET = {
        "finance": "INSTANTLY_CAMPAIGN_FINANCE",
        "operations": "INSTANTLY_CAMPAIGN_OPERATIONS",
    }
    INSTANTLY_CAMPAIGN_ID = ""
    OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS: dict = {}


def test_wave1_challenger_campaigns_are_counted(monkeypatch):
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_FINANCE", "control-finance")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_OPERATIONS", "control-ops")
    cfg = _Cfg()
    cfg.OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS = {
        "finance": "challenger-finance", "operations": "challenger-ops"}

    ids = configured_campaign_ids(cfg)

    assert ids == ["control-finance", "control-ops",
                   "challenger-finance", "challenger-ops"], (
        "with Wave 1 at a 50% split, omitting the challenger arm halves the "
        "reported delivery count and declares no gap"
    )


def test_a_campaign_shared_between_arms_is_counted_once(monkeypatch):
    """customer_success and customer_support share one campaign in production, and
    a bucket may map its challenger to a control id during a partial rollout."""
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_FINANCE", "shared")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_OPERATIONS", "shared")
    cfg = _Cfg()
    cfg.OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS = {"finance": "shared"}

    assert configured_campaign_ids(cfg) == ["shared"]


def test_no_wave1_configuration_leaves_the_control_set_unchanged(monkeypatch):
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_FINANCE", "control-finance")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_OPERATIONS", "control-ops")
    cfg = _Cfg()

    assert configured_campaign_ids(cfg) == ["control-finance", "control-ops"]


def test_a_malformed_wave1_mapping_is_ignored_rather_than_crashing(monkeypatch):
    """``_env_json`` yields ``{}`` on a bad value, but a hand-set attribute could
    be anything. Reporting must degrade to the control set, never raise."""
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_FINANCE", "control-finance")
    cfg = _Cfg()
    for bad in ("not-a-dict", None, ["a", "b"], 7):
        cfg.OUTBOUND_WAVE1_CHALLENGER_CAMPAIGNS = bad
        assert configured_campaign_ids(cfg) == ["control-finance"]


# --------------------------------------------------------------------------
# 4. Brett's format is FIXED
# --------------------------------------------------------------------------
#
# The message is four numbers, a cause and a plan. Everything else -- the metric
# dashboard, the acquisition table, the FINAL_PASS census, provider duplication,
# gate jargon, the gap register -- is already written to the JSON document and
# the internal summary on every run. Adding any of it here is how a stakeholder
# message stops being read, so the structure is asserted rather than sampled.

from weekly_report.render import render_stakeholder_summary  # noqa: E402
from weekly_report.report import build_report  # noqa: E402
from weekly_report.timewindow import explicit_window  # noqa: E402


def _stakeholder(root: Path) -> str:
    window = explicit_window(
        __import__("datetime").date(2026, 9, 5), __import__("datetime").date(2026, 9, 12),
        boundary_hour=0, tz_name="America/Los_Angeles")
    return render_stakeholder_summary(build_report(window, artifact_roots=[str(root)]))


def test_the_message_is_exactly_the_agreed_shape(heavy_root):
    text = _stakeholder(heavy_root)
    lines = text.splitlines()

    assert lines[0].startswith("Week of "), "one short period label, then the numbers"
    assert lines[1].startswith("Jobs: ") and " captured / " in lines[1]
    assert lines[2].startswith("Qualified opportunities: ")
    assert lines[3].startswith("Contacts found: ")
    assert lines[4].startswith("sent to Instantly: ")
    assert lines[5] == ""
    assert lines[6] == "Biggest bottleneck from past week"
    blank = lines.index("", 7)
    assert lines[blank + 1] == "Action plan for the following week"


def test_the_message_carries_no_dashboard_debug_or_gate_jargon(heavy_root):
    text = _stakeholder(heavy_root)
    for banned in (
        "FINAL_PASS", "NEEDS_CHECK", "UNVERIFIED", "JobGate", "RoleGate",
        "provider_jobs_returned", "historical_duplicates", "cross_source",
        "per_source", "run_ledger", "reporting_ledger:", "waterfall.json",
        "orchestrator_result", "NOT MEASURED", "CONVERSION", "FUNNEL",
        "Headline metrics measured", "writes performed", "basis:",
        "tz source", "Dry runs excluded", "Pipeline runs",
    ):
        assert banned not in text, f"{banned!r} belongs in the internal view, not Brett's"


def test_at_most_three_actions_reach_the_message(heavy_root):
    text = _stakeholder(heavy_root)
    plan = text.split("Action plan for the following week", 1)[1]
    numbered = [l for l in plan.splitlines() if l[:2] in ("1.", "2.", "3.", "4.", "5.")]
    assert 0 <= len(numbered) <= 3


def test_equal_populations_render_100_percent_and_zero_renders_na(tmp_path):
    """Two specific renderings the format calls out by name."""
    from orchestrator.run_ledger import RunLedger

    def render(metrics):
        root = tmp_path / f"r{abs(hash(tuple(sorted(metrics.items()))))}"
        ledger = RunLedger(root, "20260908T030131Z-rate")
        ledger.begin(started_at=_instant("2026-09-08T03:01:31Z"),
                     mode="live_acquisition_and_enrichment", lanes=("fantastic",))
        ledger.record("acquisition", metrics)
        ledger.finalize(state="complete", status="complete",
                        finished_at=_instant("2026-09-08T03:41:00Z"))
        return _stakeholder(root)

    equal = render({"net_new_jobs_captured": 412, "jobs_reviewed": 412})
    assert "Jobs: 412 captured / 412 reviewed (100%)" in equal, (
        "equal non-zero populations are 100%, not 100.0% and not a rounded 99%"
    )

    empty = render({"net_new_jobs_captured": 0, "jobs_reviewed": 0})
    assert "Jobs: 0 captured / 0 reviewed (N/A)" in empty, (
        "a rate over an empty population is undefined; 0% would assert that "
        "nothing captured was reviewed"
    )


# --------------------------------------------------------------------------
# 5. a PARTIAL number must not read as a total
# --------------------------------------------------------------------------
#
# Found by rendering the real 2026-09-04/05 week. `qualified_opportunities` was
# emitted by the 03:00 run (which acquired nothing: 0) and NOT by the 13:00 run,
# whose build predated the counter and which found 1,048 contacts. Brett's message
# said:
#
#     Qualified opportunities: 0
#     Contacts found: 1,048
#
# a thousand contacts from nothing. The number was right; the silence beside it
# was what needed saying.

def _two_runs(root: Path, *, second_reports_qualified: bool):
    from orchestrator.run_ledger import RunLedger

    for run_id, started, finished, metrics in (
        ("run_silent", "2026-09-08T03:00:00Z", "2026-09-08T06:00:00Z",
         {"acquisition": {"net_new_jobs_captured": 6205},
          "enrichment": {"jobs_reviewed": 6205, "contacts_found": 1048}}),
        ("run_reports", "2026-09-09T03:00:00Z", "2026-09-09T03:05:00Z",
         {"acquisition": {"net_new_jobs_captured": 0},
          "enrichment": ({"jobs_reviewed": 0, "contacts_found": 0,
                          "qualified_opportunities": 0}
                         if second_reports_qualified
                         else {"jobs_reviewed": 0, "contacts_found": 0})}),
    ):
        led = RunLedger(root, run_id)
        led.begin(started_at=_instant(started),
                  mode="live_acquisition_and_enrichment", lanes=("fantastic",))
        for stage, values in metrics.items():
            led.record(stage, values)
        led.finalize(state="complete", status="complete",
                     finished_at=_instant(finished))
    return root


def test_a_partial_metric_says_so_in_bretts_message(tmp_path):
    root = _two_runs(tmp_path / "artifacts", second_reports_qualified=True)
    text = _stakeholder(root)

    line = next(l for l in text.splitlines() if l.startswith("Qualified opportunities:"))
    assert line.startswith("Qualified opportunities: 0 ("), line
    assert "partial" in line and "1 of 2 runs" in line, (
        "an unqualified 0 beside 1,048 contacts reads as a total it is not"
    )
    # A metric EVERY run reported stays a plain number.
    assert "Contacts found: 1,048" in text


def test_a_fully_reported_metric_is_never_annotated(tmp_path):
    root = _two_runs(tmp_path / "artifacts", second_reports_qualified=False)
    text = _stakeholder(root)

    assert "Contacts found: 1,048" in text
    assert "Jobs: 6,205 captured / 6,205 reviewed (100%)" in text
    # No run reported it at all -> not measured, which is the existing rule.
    assert "Qualified opportunities: not measured" in text


def test_a_partial_review_rate_is_labelled_too(tmp_path):
    """A rate over an incomplete population is an incomplete rate."""
    from orchestrator.run_ledger import RunLedger

    root = tmp_path / "artifacts"
    for run_id, started, finished, metrics in (
        ("a", "2026-09-08T03:00:00Z", "2026-09-08T06:00:00Z",
         {"net_new_jobs_captured": 100, "jobs_reviewed": 100}),
        ("b", "2026-09-09T03:00:00Z", "2026-09-09T03:05:00Z",
         {"net_new_jobs_captured": 50}),
    ):
        led = RunLedger(root, run_id)
        led.begin(started_at=_instant(started),
                  mode="live_acquisition_and_enrichment", lanes=("fantastic",))
        led.record("acquisition", metrics)
        led.finalize(state="complete", status="complete",
                     finished_at=_instant(finished))

    line = next(l for l in _stakeholder(root).splitlines() if l.startswith("Jobs:"))
    assert "partial" in line, line
    assert "150 captured" in line and "100 reviewed" in line
