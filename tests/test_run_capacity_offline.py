"""Can the CODE deliver a thousand distinct approvals in one run, and what stops it?

Every provider and Airtable boundary here is an offline fake, so nothing in this file
is evidence about live contact yield, provider coverage or commercial performance. It
establishes something narrower and still worth establishing: that when the inputs
satisfy the requirements, the machinery approves a thousand distinct NEW leads in a
single run rather than stopping at a batch boundary, a stale counter or a one-shot
guard -- and that when quality fails or the budget ends, it approves correspondingly
fewer and says why.

The counted unit is a DISTINCT NEW approved lead key, read back from the durable
store the run wrote. Postings, contacts found, Pending rows, duplicates and leads a
previous run already approved are all excluded by construction: ``record_approved``
keeps a lifetime set and counts only first sightings.
"""

import unittest

from orchestrator import daily_target, pending_work
from tests.test_throughput_contract import RecoveryProductionLoop


class OfflineRunCapacity(RecoveryProductionLoop):
    """Reuses the production-loop harness: the real Orchestrator, fake boundaries."""

    RUN_ID = "20260906T220000Z-regression"

    def _store(self, root):
        """Where the run recorded its approvals, or None if it recorded none.

        A run that approves nothing writes no store at all, which is the correct
        outcome and must read as zero rather than as a missing file.
        """
        found = list(root.rglob("daily_approved.json"))
        return found[0].parent if found else None

    def _approved(self, root):
        """Distinct NEW approved keys attributed to this run, from its own store."""
        store = self._store(root)
        return daily_target.approved_for_run(store, self.RUN_ID) if store else 0

    # -- capacity ----------------------------------------------------------
    def test_one_run_approves_more_than_a_thousand_distinct_new_leads(self):
        """1,500 eligible opportunities, a 250-row batch, and a 1,000 target.

        The target is a MINIMUM: with ``CONTINUE_AFTER_TARGET`` the run keeps going
        while eligible work remains, so a good day is not truncated at exactly the
        goal. What this rules out is the failure that looks identical from outside --
        stopping at 250 because that is the batch, or at 1,000 because the counter
        stopped being refreshed.
        """
        result, reached, _, root = self.exercise(1500, 250, 1000, continue_after=True)
        approved = self._approved(root)
        self.assertGreater(approved, 1000, "a run must be able to exceed the target")
        self.assertEqual(approved, 1500)
        self.assertEqual(len(reached), len(set(reached)), "no lead worked twice")
        goal = result["acquisition"]["cumulative"]["run_approved_goal"]
        self.assertEqual(goal["approved_this_run"], 1500)
        self.assertTrue(goal["met"])
        self.assertEqual(pending_work.load(root / pending_work.STORE)[0], [],
                         "eligible work must be drained, not left owed")

    def test_a_batch_limit_is_not_a_daily_ceiling(self):
        """3,524 owed against a 2,000-row batch -- production's actual numbers.

        The batch bounds MEMORY and keeps a failure small. It once bounded the day
        as well, because adoption ran once per run: a backlog larger than one batch
        needed a second day for no reason but a one-shot guard. Adoption is now once
        per ITERATION, so the whole backlog drains inside one run.
        """
        _, reached, _, root = self.exercise(3524, 2000, 100000, continue_after=True)
        self.assertEqual(len(set(reached)), 3524)
        self.assertGreater(len(set(reached)), 2000, "the batch is not the ceiling")
        self.assertEqual(self._approved(root), 3524)
        self.assertEqual(pending_work.load(root / pending_work.STORE)[0], [])

    def test_a_previous_runs_approvals_never_count_toward_this_run(self):
        """1,000 approved yesterday does not make today's run finished."""
        result, reached, _, root = self.exercise(1200, 200, 1000, prior=1000)
        self.assertEqual(self._approved(root), 1000)
        self.assertEqual(len(reached), 1000)
        self.assertEqual(result["topup"]["final_stop_reason"], "run_approved_target_met")

    # -- controls: the run must NOT reach a thousand when it should not -----
    def test_created_rows_that_are_not_approved_count_for_nothing(self):
        """Quality control. Airtable creates the rows and returns no Approved key.

        This is the failure mode a created-row counter cannot see: 1,500 rows
        written, every one of them Pending, and the honest answer is zero.
        """
        result, reached, _, root = self.exercise(1500, 250, 1000, approved=False,
                                                 continue_after=True)
        self.assertEqual(self._approved(root), 0)
        self.assertGreater(len(reached), 1000, "the work was done; the approvals were not")
        goal = result["acquisition"]["cumulative"]["run_approved_goal"]
        self.assertEqual(goal["approved_this_run"], 0)
        self.assertFalse(goal["met"])

    def test_a_failed_write_is_not_an_approval(self):
        result, _, _, root = self.exercise(1200, 200, 1000, delivery_failed=True,
                                           continue_after=True)
        self.assertEqual(self._approved(root), 0)
        self.assertEqual(result["delivery"]["created"], 0)

    def test_a_budget_interruption_keeps_the_approvals_it_already_earned(self):
        """The run ends on the provider budget AFTER a batch was delivered.

        Two things have to hold together, and they used to be in tension: the
        approvals from the final batch are counted (the goal snapshot is refreshed
        after delivery, not only at the next loop header), and the work that never
        got processed stays in custody for a later run rather than being lost or
        re-bought.
        """
        result, reached, _, root = self.exercise(1500, 250, 100000, interrupted=True,
                                                 continue_after=True)
        approved = self._approved(root)
        self.assertEqual(approved, 250, "the delivered batch counts")
        self.assertEqual(result["delivery"]["created"], 250)
        goal = result["acquisition"]["cumulative"]["run_approved_goal"]
        self.assertEqual(goal["approved_this_run"], 250)
        self.assertEqual(result["topup"]["final_stop_reason"], "apollo_budget_exhausted")
        owed = pending_work.load(root / pending_work.STORE)[0]
        self.assertGreaterEqual(len(owed), 1500 - len(set(reached)),
                                "unprocessed work must survive the interruption")

    def test_the_same_lead_delivered_twice_is_counted_once(self):
        """Distinctness is the point of the unit.

        The store keeps a lifetime set, so a lead re-delivered by a retry, a later
        batch touching the same company, or a duplicate provider record adds nothing.
        """
        _, reached, _, root = self.exercise(600, 200, 100000, continue_after=True)
        store = self._store(root)
        before = daily_target.approved_for_run(store, self.RUN_ID)
        self.assertEqual(before, 600)
        # Replay the run's OWN keys -- whatever the delivery fake used as the
        # approved key -- rather than a guess at their shape.
        repeat = daily_target.record_approved(store, sorted(set(reached)),
                                              run_id=self.RUN_ID)
        self.assertEqual(repeat["added"], 0, "a re-delivery adds no new approvals")
        self.assertEqual(daily_target.approved_for_run(store, self.RUN_ID), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
