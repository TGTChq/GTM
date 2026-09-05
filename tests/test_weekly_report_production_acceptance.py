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
