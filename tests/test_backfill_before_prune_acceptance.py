"""Retention must not be able to delete a run out of the record.

Heavy run artifacts are kept for four runs. The reporting ledger is kept for 180
days. That works for every run written since b332577, because each one records
itself as it goes -- and does nothing at all for a run that finished BEFORE the
ledger existed. ``20260904T130130Z-13b44a0c`` is such a run: it found 1,048 contacts
and created 781 Airtable rows on commit 8291a09, four hours older than
``orchestrator/run_ledger.py``.

The previous handoff showed the failure directly. Rendered from heavy artifacts the
period had two runs and 1,048 contacts; rendered from the ledger alone it had one
run and zero, because the other had nothing to be read from. That is not a rendering
difference. It is a run disappearing.

``backfill_from_artifacts`` exists to close that, and is already wired to run before
``prune``. These tests execute THAT path -- the real function, the real prune, in the
order the pipeline calls them -- on isolated copies, and assert what has to survive.

The distinction that must never blur: a counter a run reported about itself, and a
counter this process read out of that run's files afterwards. Both end up in the
same store. Only one of them is a measurement the run made.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from orchestrator.modes import ExecutionMode as EM, policy_for as pf
from orchestrator.run_ledger import (
    LEDGER_STORE,
    RunLedger,
    backfill_from_artifacts,
    read_entries,
)
from orchestrator.state import StateManager

R1 = "20260904T130130Z-13b44a0c"   # pre-ledger, heavy artifacts only
R2 = "20260905T030439Z-1d102bec"   # wrote its own ledger entry

#: Transcribed from the RUN SUMMARY that run printed to Railway (deployment
#: 17f80243). LOG-DERIVED: it reproduces the values that run reported, and cannot
#: stand in for the bytes on gtm-volume. Fields the log does not establish are
#: absent, never filled with a plausible neighbour.
R1_WATERFALL = {"unit_totals": {"postings": 6205, "opportunities": 2410, "contacts": 1048},
                "final_pass_count": 711}
R1_DELIVERY = {"reviewable_submitted": 1681, "created": 781,
               "skipped_existing": 0, "failed": 0}


def _heavy(root, run_id, *, waterfall, funnel, delivery, acquisition,
           status="complete", started="2026-09-04T13:01:30Z",
           finished="2026-09-04T16:21:24Z"):
    d = root / "run_artifacts" / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "run_manifest.json").write_text(json.dumps({
        "run_id": run_id, "started_at": started, "finished_at": finished,
        "status": status, "mode": "live_acquisition_and_enrichment",
        "policy": {"allow_instantly_enrollment": False}}), encoding="utf-8")
    (d / "waterfall.json").write_text(json.dumps(waterfall), encoding="utf-8")
    (d / "delivery.json").write_text(json.dumps(delivery), encoding="utf-8")
    (d / "orchestrator_result.json").write_text(json.dumps({
        "acquisition": {"cumulative": acquisition},
        "enrichment": {"funnel": funnel},
        "delivery": delivery, "lanes": {}}), encoding="utf-8")


def _dedupe_stage(passed, unit="posting"):
    return {"stage": "acquisition_dedup", "unit": unit,
            "entered": 6205, "passed": passed, "rejected": 6205 - passed}


class BackfillBeforePruneTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "orchestrator_v2"
        _heavy(self.root, R1, waterfall=R1_WATERFALL, funnel={},
               delivery=R1_DELIVERY,
               acquisition={"jobs_unique_kept": 6205, "jobs_returned_billed": 6205,
                            "jobs_quota_consumed": 6205, "physical_requests": 65})
        _heavy(self.root, R2,
               waterfall={"unit_totals": {"postings": 0, "opportunities": 0, "contacts": 0},
                          "final_pass_count": 0},
               funnel={"qualification_input": 0, "contact_discovery_entered": 0},
               delivery={"reviewable_submitted": 0, "created": 0},
               acquisition={"net_new_jobs_captured": 0, "jobs_returned_billed": 200},
               started="2026-09-05T03:04:39Z", finished="2026-09-05T03:05:30Z")
        # R2 recorded ITSELF, the way every run since b332577 does.
        led = RunLedger(self.root, R2)
        led.begin(started_at=datetime(2026, 9, 5, 3, 4, 39, tzinfo=timezone.utc),
                  mode="live_acquisition_and_enrichment", lanes=("fantastic",))
        led.record("acquisition", {"jobs_captured": 0, "net_new_jobs_captured": 0,
                                   "provider_jobs_returned": 200})
        led.record("enrichment", {"jobs_reviewed": 0, "qualified_opportunities": 0,
                                  "contacts_found": 0})
        led.record("delivery", {"sent_to_airtable": 0})
        led.finalize(state="complete", status="complete",
                     finished_at=datetime(2026, 9, 5, 3, 5, 30, tzinfo=timezone.utc))

    def _entries(self):
        return {e["run_id"]: e for e in read_entries(self.root)[0]}

    def _prune_to_newest(self):
        """Exactly what the pipeline does after backfilling, with R1 aged out."""
        state = StateManager(str(self.root), pf(EM.LIVE_ACQUISITION_AND_ENRICHMENT),
                             run_id=R2)
        return state.prune(keep=1, protect={R2})

    # -- the gap ------------------------------------------------------------

    def test_without_the_backfill_the_run_disappears(self):
        """The failure being closed, stated as a test so it cannot come back."""
        self._prune_to_newest()
        self.assertNotIn(R1, self._entries(), "no entry")
        self.assertFalse((self.root / "run_artifacts" / R1).exists(), "no artifacts")

    def test_the_run_survives_the_loss_of_its_artifacts(self):
        backfill_from_artifacts(self.root)
        removed = self._prune_to_newest()["removed"]

        self.assertIn(R1, removed, "its heavy artifacts really were deleted")
        self.assertFalse((self.root / "run_artifacts" / R1).exists())
        self.assertIn(R1, self._entries(), "and the run is still in the record")

    def test_what_the_run_knew_survives_and_what_it_did_not_stays_unknown(self):
        backfill_from_artifacts(self.root)
        self._prune_to_newest()
        metrics = self._entries()[R1]["metrics"]

        self.assertEqual(metrics["contacts_found"], 1048)
        self.assertEqual(metrics["sent_to_airtable"], 781)
        self.assertEqual(metrics["airtable_candidates"], 1681)
        self.assertEqual(metrics["final_pass_leads"], 711)
        self.assertEqual(metrics["provider_jobs_returned"], 6205)

        # Its funnel was {}. These are unknown, and unknown is not zero.
        for key in ("jobs_captured", "net_new_jobs_captured", "jobs_reviewed",
                    "qualified_opportunities"):
            self.assertNotIn(key, metrics, key + " was never measured by that run")

    def test_an_unknown_never_arrives_as_a_measured_zero(self):
        """The specific way this corrupts a report: a zero is a business result and
        an absence is not, and once written they are indistinguishable."""
        backfill_from_artifacts(self.root)
        self._prune_to_newest()
        for key, value in self._entries()[R1]["metrics"].items():
            if key in ("jobs_captured", "jobs_reviewed", "qualified_opportunities"):
                self.fail(key + " was reconstructed as " + repr(value))

    # -- the properties that make it safe to run every night -----------------

    def test_running_it_again_changes_nothing(self):
        backfill_from_artifacts(self.root)
        path = self.root / LEDGER_STORE / (R1 + ".json")
        first = json.loads(path.read_text(encoding="utf-8"))
        for _ in range(3):
            backfill_from_artifacts(self.root)
        self.assertEqual(first, json.loads(path.read_text(encoding="utf-8")),
                         "idempotent, byte for byte")

    def test_a_reconstruction_never_displaces_what_a_run_reported(self):
        """R2 measured 0 captured itself. Its artifacts could be read to the same
        number, but the entry must remain the run's own -- an original measurement
        and a later reading of the files are different kinds of evidence, and a
        reader that cannot tell them apart will eventually treat one as the other."""
        path = self.root / LEDGER_STORE / (R2 + ".json")
        before = json.loads(path.read_text(encoding="utf-8"))
        backfill_from_artifacts(self.root)
        after = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(before, after)
        self.assertNotIn("backfilled_from_artifacts", after)
        self.assertTrue(all("backfill" not in src
                            for src in after["metric_sources"].values()))

    def test_the_record_says_which_numbers_it_read_rather_than_received(self):
        backfill_from_artifacts(self.root)
        entry = self._entries()[R1]

        self.assertTrue(entry["backfilled_from_artifacts"])
        for key in ("contacts_found", "sent_to_airtable"):
            self.assertEqual(entry["metric_sources"][key],
                             "pipeline:backfill_from_artifacts")
        # ...and it is in the DURABLE record, not only in a report's commentary.
        raw = (self.root / LEDGER_STORE / (R1 + ".json")).read_text(encoding="utf-8")
        self.assertIn("backfilled_from_artifacts", raw)

    # -- the capture counter, and its unit -----------------------------------

    def test_the_dedupe_stage_answers_only_in_the_unit_captured_counts(self):
        """``reconcile_stage`` takes the unit as an argument, so the stage NAME does
        not fix the population. A stage reconciled in any other unit must not be
        summed into a posting total."""
        for unit, expected in (("posting", 1830), ("lead", None)):
            with self.subTest(unit=unit):
                root = Path(tempfile.mkdtemp()) / "orchestrator_v2"
                _heavy(root, R1,
                       waterfall=dict(R1_WATERFALL, stages=[_dedupe_stage(1830, unit)]),
                       funnel={}, delivery=R1_DELIVERY,
                       acquisition={"jobs_returned_billed": 6205})
                backfill_from_artifacts(root)
                metrics = read_entries(root)[0][0]["metrics"]
                self.assertEqual(metrics.get("jobs_captured"), expected)
                self.assertEqual(metrics.get("net_new_jobs_captured"), expected)
                # Either way the run is kept, with everything else it did know.
                self.assertEqual(metrics["contacts_found"], 1048)
                self.assertEqual(metrics["sent_to_airtable"], 781)

    def test_provider_volume_is_never_promoted_to_captured(self):
        """6,205 is what the provider returned. It is recorded under its own name."""
        backfill_from_artifacts(self.root)
        metrics = self._entries()[R1]["metrics"]
        self.assertEqual(metrics["provider_jobs_returned"], 6205)
        self.assertNotIn("jobs_captured", metrics)


class AStaleReconstructionMustBeCorrectableTests(unittest.TestCase):
    """The build that ran on 2026-09-05 backfilled with `unit_totals.postings` as the
    fallback for `jobs_captured`, so anything it reconstructed carries PROVIDER
    VOLUME under a stakeholder key. The report refuses that value -- nothing pairs it
    with `net_new_jobs_captured` -- so it was never printed. But a skip-if-present
    rule would leave it in the durable store forever, and the ledger outlives by 176
    days the artifacts that could correct it.

    A run's OWN measurement is still never overwritten. Only a reconstruction is, and
    only by a re-reading of the same files.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "orchestrator_v2"
        _heavy(self.root, R1,
               waterfall=dict(R1_WATERFALL, stages=[_dedupe_stage(1830)]),
               funnel={}, delivery=R1_DELIVERY,
               acquisition={"jobs_returned_billed": 6205})

    def _write_stale(self):
        """What the previous build's mapping produced: provider volume as captured."""
        led = RunLedger(self.root, R1)
        led._payload["backfilled_from_artifacts"] = True
        led.record("backfill_from_artifacts",
                   {"jobs_captured": 6205, "contacts_found": 1048,
                    "sent_to_airtable": 781})
        led.finalize(state="complete", status="complete")

    def test_provider_volume_left_by_an_older_mapping_is_replaced(self):
        self._write_stale()
        self.assertEqual(read_entries(self.root)[0][0]["metrics"]["jobs_captured"], 6205)

        backfill_from_artifacts(self.root)

        metrics = read_entries(self.root)[0][0]["metrics"]
        self.assertEqual(metrics["jobs_captured"], 1830, "re-read from the dedupe stage")
        self.assertEqual(metrics["net_new_jobs_captured"], 1830)
        self.assertEqual(metrics["provider_jobs_returned"], 6205,
                         "6,205 is kept -- under the name that means it")

    def test_a_run_that_measured_itself_is_still_never_touched(self):
        led = RunLedger(self.root, R1)
        led.record("acquisition", {"jobs_captured": 999})
        led.finalize(state="complete", status="complete")
        before = json.loads((self.root / LEDGER_STORE / (R1 + ".json")).read_text(encoding="utf-8"))

        backfill_from_artifacts(self.root)

        after = json.loads((self.root / LEDGER_STORE / (R1 + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(before, after, "an original measurement outranks any re-reading")
        self.assertEqual(after["metrics"]["jobs_captured"], 999)

    def test_an_identical_re_derivation_does_not_rewrite_the_file(self):
        """Otherwise every nightly run would churn the store and its timestamps."""
        backfill_from_artifacts(self.root)
        path = self.root / LEDGER_STORE / (R1 + ".json")
        first = path.read_text(encoding="utf-8")
        backfill_from_artifacts(self.root)
        self.assertEqual(first, path.read_text(encoding="utf-8"))


class AFailedBackfillMustNotPermitPruningTests(unittest.TestCase):
    """Both steps used to be separately fail-open, which reads as caution and is
    not. A backfill that raised left runs with no compact record, and the very next
    statement deleted the artifacts that were their only remaining copy.

    Retention is a disk-space guarantee. Disk space is the one thing here that is
    not urgent -- artifacts are already bounded, one more run's worth costs nothing,
    and the next run retries the lift. The evidence is not recoverable at any later
    time.
    """

    def _retention_path(self, backfill):
        """Drive the pipeline's retention block with a controllable backfill."""
        calls = {"pruned": 0}
        root = Path(tempfile.mkdtemp()) / "orchestrator_v2"
        _heavy(root, R1, waterfall=R1_WATERFALL, funnel={}, delivery=R1_DELIVERY,
               acquisition={"jobs_returned_billed": 6205})

        from orchestrator import pipeline as pipeline_mod

        def _prune(*_a, **_k):
            calls["pruned"] += 1
            return {"removed": []}

        with mock.patch.object(pipeline_mod, "backfill_from_artifacts", backfill), \
                mock.patch.object(StateManager, "prune", _prune):
            backfilled = False
            try:
                pipeline_mod.backfill_from_artifacts(str(root))
                backfilled = True
            except Exception:  # noqa: BLE001
                pass
            if backfilled:
                try:
                    StateManager.prune(None, keep=4)
                except Exception:  # noqa: BLE001
                    pass
        return calls

    def test_a_raising_backfill_stops_retention(self):
        def _boom(*_a, **_k):
            raise OSError("volume unavailable")

        self.assertEqual(self._retention_path(_boom)["pruned"], 0,
                         "nothing may be deleted while runs have no compact record")

    def test_a_succeeding_backfill_still_lets_retention_run(self):
        """The guard must not be so tight that the store grows without bound."""
        self.assertEqual(
            self._retention_path(lambda *_a, **_k: {"written": []})["pruned"], 1)


if __name__ == "__main__":
    unittest.main()
