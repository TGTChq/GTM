"""Custody must be durable BEFORE the cursor is, and must never lie about outcomes.

"Before enrichment" does not establish the ordering guarantee. Per-source offsets
become durable inside ``DateCreatedWatermarkEngine.checkpoint()`` -- at the END of
acquisition, before the pipeline has seen a single posting -- and they are replayed
FORWARD. Once saved, rows between the old and new offset are never requested again.
So custody has to succeed first, and a custody failure must leave the cursor where
it was: re-billing a page costs credits, advancing past it costs the work.

The other half is honesty about departures. Work leaves custody for three different
reasons and only one of them is success:

    terminal            finished (FINAL_PASS / REJECT) -- the only completion
    deduped             dedupe proved it was never new work
    expired_unresolved  custody aged out with the work STILL UNDONE

An expiry that deleted the payloads, or that a reader silently skipped, would turn a
retention policy into invisible data loss -- the exact failure this store exists to
prevent.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import fantastic_jobs_adapter as fja
from orchestrator import pending_work

from tests.test_pending_work_custody import (  # noqa: E402
    _ApolloRefuses, _posting, _run,
)


class CustodyPrecedesTheCursor(unittest.TestCase):
    def _engine(self, acquired):
        eng = fja.DateCreatedWatermarkEngine(
            result=None, quota=None, http_get=None, seen_ids=set(),
            metrics={"watermark": {}, "segments": {}}, run_cap=0)
        eng.opened = True
        eng.acquired = acquired
        eng.state = {"window_offsets": {"linkedin": 2822}, "window_acquired_ids": []}
        eng.window_drained = lambda labels: False
        eng.drained_sources = lambda: {}
        return eng

    def tearDown(self):
        fja.set_custody_hook(None)

    def test_custody_runs_before_the_offsets_are_persisted(self):
        order = []
        eng = self._engine([{"job_id": "j1", "posting_id": "j1"}])
        eng._save = lambda: order.append("save_offsets")

        def _hold(_rows):
            order.append("custody")
            return True

        fja.set_custody_hook(_hold)
        eng.checkpoint(("linkedin",))
        self.assertEqual(order, ["custody", "save_offsets"])

    def test_a_failed_custody_does_not_advance_the_cursor(self):
        saved = []
        eng = self._engine([{"job_id": "j1", "posting_id": "j1"}])
        eng._save = lambda: saved.append(1)

        fja.set_custody_hook(lambda _rows: False)
        eng.checkpoint(("linkedin",))

        self.assertEqual(saved, [], "offsets must NOT become durable")
        self.assertTrue(eng.metrics["watermark"]["custody_failed"])
        self.assertTrue(eng.metrics["watermark"]["offsets_not_advanced"])

    def test_a_raising_custody_is_a_failure_not_a_pass(self):
        saved = []
        eng = self._engine([{"job_id": "j1", "posting_id": "j1"}])
        eng._save = lambda: saved.append(1)

        def _boom(_rows):
            raise OSError("disk full")

        fja.set_custody_hook(_boom)
        eng.checkpoint(("linkedin",))

        self.assertEqual(saved, [])
        self.assertIn("OSError", eng.metrics["watermark"]["custody_error"])

    def test_with_no_hook_the_checkpoint_is_unchanged(self):
        saved = []
        eng = self._engine([{"job_id": "j1", "posting_id": "j1"}])
        eng._save = lambda: saved.append(1)
        fja.set_custody_hook(None)
        eng.checkpoint(("linkedin",))
        self.assertEqual(saved, [1])


class ExpiryIsNotCompletion(unittest.TestCase):
    def setUp(self):
        self.store = Path(tempfile.mkdtemp()) / "pending_work"

    def _age(self):
        path = next(p for p in self.store.glob("*.json") if not p.name.startswith("_"))
        held = pending_work._read(path)
        held["recorded_at"] = "2020-01-01T00:00:00+00:00"
        path.write_text(json.dumps(held), encoding="utf-8")

    def test_expired_work_is_archived_not_deleted(self):
        pending_work.record(self.store, "run-a", [_posting(1), _posting(2)])
        self._age()
        info = pending_work.expire(self.store, max_age_days=14, run_id="run-z")
        self.assertEqual(info["expired_postings"], 2)
        archived = self.store / "expired" / "run-a.json"
        self.assertTrue(archived.is_file(), "payloads must remain recoverable")
        self.assertEqual(len(pending_work._read(archived)["jobs"]), 2)

    def test_the_outcome_says_unresolved_and_never_terminal(self):
        pending_work.record(self.store, "run-a", [_posting(1)])
        self._age()
        pending_work.expire(self.store, max_age_days=14, run_id="run-z")
        audit = (self.store / "_audit.jsonl").read_text(encoding="utf-8")
        self.assertIn(pending_work.OUTCOME_EXPIRED, audit)
        self.assertNotIn(pending_work.OUTCOME_TERMINAL, audit)
        archived = pending_work._read(self.store / "expired" / "run-a.json")
        self.assertEqual(archived["outcome"], pending_work.OUTCOME_EXPIRED)

    def test_expired_work_still_shows_in_the_summary(self):
        pending_work.record(self.store, "run-a", [_posting(1)])
        self._age()
        pending_work.expire(self.store, max_age_days=14, run_id="run-z")
        state = pending_work.summary(self.store)
        self.assertEqual(state["pending_postings"], 0)
        self.assertEqual(state["expired_unresolved_postings"], 1,
                         "aged-out debt stays visible; it did not get done")

    def test_a_read_never_applies_retention(self):
        pending_work.record(self.store, "run-a", [_posting(1)])
        self._age()
        jobs, _info = pending_work.load(self.store, exclude_run_id="now")
        self.assertEqual(len(jobs), 1, "load must not quietly drop aged work")

    def test_every_departure_is_audited_with_its_reason(self):
        pending_work.record(self.store, "run-a", [_posting(1), _posting(2)])
        pending_work.release(self.store, {"j1"},
                             outcome=pending_work.OUTCOME_DEDUPED, run_id="run-z")
        pending_work.release(self.store, {"j2"},
                             outcome=pending_work.OUTCOME_TERMINAL, run_id="run-z")
        rows = [json.loads(l) for l in
                (self.store / "_audit.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sorted(r["outcome"] for r in rows),
                         [pending_work.OUTCOME_DEDUPED, pending_work.OUTCOME_TERMINAL])


class RecoveryFromExistingArtifacts(unittest.TestCase):
    """A run that finished before custody existed left its opportunity list in
    ``run_artifacts/<run_id>/enrichment/postings.json``, which retention deletes."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.store = self.root / "pending_work"

    def _artifact(self, run_id, n):
        d = self.root / "run_artifacts" / run_id / "enrichment"
        d.mkdir(parents=True, exist_ok=True)
        (d / "postings.json").write_text(
            json.dumps({"jobs": [_posting(i) for i in range(n)]}), encoding="utf-8")

    def test_it_lifts_an_opportunity_list_into_custody(self):
        self._artifact("20260906T030230Z-2f74ac7c", 3)
        info = pending_work.adopt_from_artifacts(self.root, self.store)
        self.assertEqual(info["runs_imported"], 1)
        self.assertEqual(info["postings_imported"], 3)
        self.assertEqual(pending_work.summary(self.store)["pending_postings"], 3)

    def test_it_is_idempotent(self):
        self._artifact("20260906T030230Z-2f74ac7c", 3)
        pending_work.adopt_from_artifacts(self.root, self.store)
        again = pending_work.adopt_from_artifacts(self.root, self.store)
        self.assertEqual(again["postings_imported"], 0)
        self.assertEqual(again["already_imported"], 1)
        self.assertEqual(pending_work.summary(self.store)["pending_postings"], 3)

    def test_it_is_bounded_by_runs_and_rows(self):
        for i in range(5):
            self._artifact(f"2026090{i}T030000Z-aaaaaaaa", 10)
        info = pending_work.adopt_from_artifacts(self.root, self.store,
                                                 limit=12, max_runs=2)
        self.assertLessEqual(info["runs_imported"], 2)
        self.assertLessEqual(info["postings_imported"], 12)

    def test_work_already_finished_is_not_resurrected(self):
        self._artifact("20260906T030230Z-2f74ac7c", 3)
        info = pending_work.adopt_from_artifacts(self.root, self.store,
                                                 exclude_keys={"j0", "j1"})
        self.assertEqual(info["postings_imported"], 1)

    def test_it_reports_the_counting_unit_and_the_artifact_total(self):
        """``postings.json`` holds NORMALIZED OPPORTUNITIES, not raw provider
        payloads, and is not guaranteed to hold every row the run billed. The counts
        are reported per run so that difference is never assumed away."""
        self._artifact("20260906T030230Z-2f74ac7c", 3)
        info = pending_work.adopt_from_artifacts(self.root, self.store)
        row = info["runs"][0]
        self.assertEqual(row["unit"], "normalized_opportunity")
        self.assertEqual(row["opportunities_in_artifact"], 3)
        self.assertEqual(pending_work.summary(self.store)["unit"],
                         "normalized_opportunity")

    def test_a_run_with_no_such_file_is_simply_skipped(self):
        (self.root / "run_artifacts" / "empty-run").mkdir(parents=True)
        info = pending_work.adopt_from_artifacts(self.root, self.store)
        self.assertEqual(info["runs_imported"], 0)


class RunsThatOweWorkKeepTheirEvidence(unittest.TestCase):
    def test_pending_run_ids_drive_prune_protection(self):
        store = Path(tempfile.mkdtemp()) / "pending_work"
        pending_work.record(store, "20260906T030230Z-2f74ac7c", [_posting(1)])
        self.assertEqual(pending_work.pending_run_ids(store),
                         {"20260906T030230Z-2f74ac7c"})
        pending_work.release(store, {"j1"}, outcome=pending_work.OUTCOME_TERMINAL)
        self.assertEqual(pending_work.pending_run_ids(store), set())

    def test_a_run_owed_work_survives_retention_end_to_end(self):
        root = tempfile.mkdtemp()
        _run(root, "20260906T030230Z-aaaaaaaa",
             [_posting(i) for i in range(3)], _ApolloRefuses())
        for i in range(4):
            _run(root, f"2026091{i}T030000Z-bbbbbbbb", [], _ApolloRefuses())
        kept = {d.name for d in (Path(root) / "run_artifacts").iterdir() if d.is_dir()}
        self.assertIn("20260906T030230Z-aaaaaaaa", kept,
                      "a run that still owes work keeps the evidence of that work")


if __name__ == "__main__":
    unittest.main()


class AdoptionMustNotStrandTheRemainder(unittest.TestCase):
    """A budget-truncated import used to be marked FINISHED.

    `adopt_from_artifacts` checks its marker before it even opens the file, so a run
    marked imported is never revisited. Marking a truncated import complete
    therefore stranded the remainder permanently -- on production it left 4,431 of
    the 2026-09-04 run's 6,205 retained opportunities outside custody, with the run
    recorded as done.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.store = self.root / "pending_work"

    def _artifact(self, run_id, n):
        d = self.root / "run_artifacts" / run_id / "enrichment"
        d.mkdir(parents=True, exist_ok=True)
        (d / "postings.json").write_text(
            json.dumps({"jobs": [_posting(i) for i in range(n)]}), encoding="utf-8")

    def test_a_truncated_import_is_not_marked_finished(self):
        self._artifact("20260904T130130Z-13b44a0c", 50)
        first = pending_work.adopt_from_artifacts(self.root, self.store, limit=20)
        self.assertEqual(first["postings_imported"], 20)

        second = pending_work.adopt_from_artifacts(self.root, self.store, limit=20)
        self.assertGreater(second["postings_imported"], 0,
                           "the remainder must still be reachable")
        self.assertEqual(second["already_imported"], 0)

    def test_repeated_passes_eventually_take_everything(self):
        self._artifact("20260904T130130Z-13b44a0c", 50)
        for _ in range(5):
            pending_work.adopt_from_artifacts(self.root, self.store, limit=20)
        self.assertEqual(pending_work.summary(self.store)["pending_postings"], 50)

    def test_a_complete_import_is_marked_finished(self):
        self._artifact("20260904T130130Z-13b44a0c", 10)
        pending_work.adopt_from_artifacts(self.root, self.store, limit=1000)
        again = pending_work.adopt_from_artifacts(self.root, self.store, limit=1000)
        self.assertEqual(again["already_imported"], 1)


class TerminalWorkIsReadFromTheRealSuppressionStore(unittest.TestCase):
    """The maintenance pass guessed `seen_suppression/seen.json` and a top-level
    `postings` list. The real file is `seen_suppression/postings.json` with the ids
    under `keys`, so the guess silently excluded nothing and finished work was taken
    into custody."""

    def test_it_reads_the_path_and_shape_the_store_actually_writes(self):
        import run_maintenance
        from orchestrator.suppression import SuppressionStore

        root = Path(tempfile.mkdtemp())
        d = root / "seen_suppression"
        d.mkdir(parents=True)
        (d / SuppressionStore.POSTINGS).write_text(
            json.dumps({"schema_version": 1, "count": 2, "keys": ["k1", "k2"]}),
            encoding="utf-8")
        self.assertEqual(run_maintenance.terminal_posting_keys(root), {"k1", "k2"})

    def test_a_missing_store_is_empty_not_an_error(self):
        import run_maintenance
        self.assertEqual(run_maintenance.terminal_posting_keys(Path(tempfile.mkdtemp())),
                         set())
