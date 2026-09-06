"""Work handed back from custody must never be counted as newly captured.

If it were, the week's `jobs_captured` would grow every time a run re-entered
postings it had already bought, and Brett's report would show acquisition volume
that no provider ever billed twice. The rule is an ordering one: capture is
accumulated from the DEDUPED slice, and only afterwards is custody's debt added to
the work list.

Pinned on the source because the branch needs a live Apollo client to reach, and
what matters is the order of the two statements, not what the enrichment engine
then does with the list.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from orchestrator import pending_work, pipeline


class CaptureIsCountedBeforeCustodyIsAdopted(unittest.TestCase):
    def test_the_capture_counter_precedes_the_adoption(self):
        source = inspect.getsource(pipeline)
        captured = source.index('acq_cum["net_new_jobs_captured"] += len(opportunities)')
        adopted = source.index("opportunities = list(opportunities) + resumed")
        self.assertLess(captured, adopted,
                        "resumed work must not inflate net-new capture")

    def test_adoption_happens_once_per_BATCH_and_never_repeats_a_batch(self):
        """It used to be once per RUN, so `PENDING_WORK_RESUME_MAX_PER_RUN` bounded
        the day as well as the batch: a 3,595-posting backlog would have taken two
        days to drain at 2,000 a run, for no reason but a one-shot guard.

        What must not change is that a batch is never handed back to itself. Custody
        deliberately keeps non-terminal work, so the loop needs a run-level record of
        what it has already adopted or it re-loads the same rows for ever."""
        source = inspect.getsource(pipeline)
        self.assertIn("run_adopted_keys: set = set()", source)
        self.assertIn("+ sorted(run_adopted_keys)", source)
        self.assertIn("run_adopted_keys.add(key)", source)

    def test_the_run_own_custody_is_not_counted_as_adoptable_inventory(self):
        """`load` excludes this run's entries by `exclude_run_id` -- they are the work
        in flight, not a debt. Counting them made "inventory exhausted" unreachable."""
        source = inspect.getsource(pipeline)
        self.assertIn('if str(r.get("run_id")) != self.ctx.run_id', source)

    def test_inventory_is_not_exhausted_while_custody_owes_work(self):
        """`kept == 0` means ACQUISITION found nothing -- permanently true on a
        recovery run, where acquisition is deliberately off."""
        source = inspect.getsource(pipeline)
        self.assertIn("last_inventory = (kept == 0 and pending_owed <= 0)", source)

    def test_resumed_rows_are_marked_so_they_are_identifiable(self):
        source = inspect.getsource(pipeline)
        self.assertIn('_resumed_from_pending', source)

    def test_work_already_in_suppression_is_not_re_entered(self):
        source = inspect.getsource(pipeline)
        window = source[source.index("resumed, resume_info = pending_work.load"):]
        self.assertIn("seen_postings()", window,
                      "a posting a previous run finished must not come back")


class CustodyReleaseUsesTheTerminalSetOnly(unittest.TestCase):
    def test_release_on_an_empty_terminal_set_frees_nothing(self):
        """The provider-outage case: no lead finished, so nothing may be forgotten."""
        store = Path(tempfile.mkdtemp()) / "pending_work"
        pending_work.record(store, "run-a", [
            {"job_id": "j1", "posting_id": "j1", "employer_name": "A"},
            {"job_id": "j2", "posting_id": "j2", "employer_name": "B"},
        ])
        info = pending_work.release(store, set(),
                                    outcome=pending_work.OUTCOME_TERMINAL)
        self.assertEqual(info["released"], 0)
        self.assertEqual(pending_work.summary(store)["pending_postings"], 2)

    def test_release_frees_exactly_the_terminal_ids(self):
        store = Path(tempfile.mkdtemp()) / "pending_work"
        pending_work.record(store, "run-a", [
            {"job_id": "j1", "posting_id": "j1", "employer_name": "A"},
            {"job_id": "j2", "posting_id": "j2", "employer_name": "B"},
        ])
        pending_work.release(store, {"j1"}, outcome=pending_work.OUTCOME_TERMINAL)
        self.assertEqual(pending_work.summary(store)["pending_postings"], 1)


if __name__ == "__main__":
    unittest.main()
