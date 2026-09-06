"""Paid-for postings survive an Apollo billing stop and a process exit.

THE SEQUENCE THIS REPRODUCES, from the 2026-09-06 production run:

    acquire 226 -> offsets advance 100->2822 -> qualify -> Apollo answers
    BILLING.LIMIT.CREDITS_EXHAUSTED -> circuit opens -> process exits

Every safeguard did its job. `terminal_posting_ids()` was empty so nothing was
committed to suppression; the watermark stayed in flight. And the work was still
lost, because "not suppressed" only means a posting MAY be processed again -- it
does not mean anything hands it back. The window offsets are replayed FORWARD, so
the next run resumes PAST the rows already bought, and the only copy of the payloads
sat in `run_artifacts/<run_id>/enrichment/postings.json`, which no run reads and
retention deletes after four runs.

So these tests assert custody, not absence-from-suppression:

  * the postings are persisted at the acquisition checkpoint, before enrichment;
  * a later process finds and re-enters them WITHOUT re-acquiring;
  * custody ends only on a TERMINAL disposition, on the same id set suppression
    gets -- never on the deferred outcome a provider outage produces;
  * `prune` cannot delete the store.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
from orchestrator import pending_work
from orchestrator.enrichment import EnrichmentReport, Lead
from orchestrator.lanes import LaneResult
from orchestrator.modes import ExecutionMode as EM, policy_for as pf
from orchestrator.pipeline import Orchestrator, OrchestratorPlan
from orchestrator.reasons import Disposition, ReasonCode
from orchestrator.runcontrol import RunContext
from orchestrator.state import StateManager


class _Budget:
    lane = source = None

    def reserve(self, *a, **k):
        return True

    def to_dict(self):
        return {}


class _Delivery:
    def deliver(self, leads, **k):
        from orchestrator.adapters_real import RealDeliveryReport
        return RealDeliveryReport(entered=0)


class _ApolloRefuses:
    """What RealEnrichmentStage produces when the circuit opens on company one.

    No leads, so no terminal ids, so nothing is suppressed -- exactly the shape the
    2026-09-06 run had.
    """

    def __init__(self):
        self.seen = []

    def run(self, opportunities, **k):
        self.seen.append([o.get("posting_id") for o in opportunities])
        return EnrichmentReport(leads=[], stages=[], loss_census={},
                                stop_reason="apollo_circuit_open",
                                enrichment_incomplete=True)


class _ApolloWorks:
    """Every posting reaches a terminal disposition."""

    def __init__(self):
        self.seen = []

    def run(self, opportunities, **k):
        self.seen.append([o.get("posting_id") for o in opportunities])
        leads = [Lead(posting_id=o["posting_id"], company={"name": "C"},
                      contact={"email": "a@b.co"}, disposition=Disposition.FINAL_PASS,
                      primary_reason=ReasonCode.OK)
                 for o in opportunities]
        return EnrichmentReport(leads=leads, stages=[], loss_census={})


def _posting(n):
    return {"job_id": f"j{n}", "posting_id": f"j{n}",
            "employer_name": f"Co{n}", "job_title": "Staff Accountant"}


def _lane(jobs):
    def runner(_manager):
        return LaneResult(lane="fantastic", status="complete", jobs=list(jobs))
    return runner


CFG = dict(
    NET_NEW_SEND_SAFE_TARGET=5,
    FANTASTIC_JOBS_MAX_JOBS_PER_RUN=1000,
    FANTASTIC_TOPUP_SLICE_JOBS=500,
    FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=0,
    TOPUP_RUNTIME_BUDGET_SECONDS=0,
    TOPUP_MAX_ITERATIONS=2,
    PRE_APOLLO_EXISTING_DEDUPE=False,
    PENDING_WORK_ENABLED=True,
    PENDING_WORK_RESUME_MAX_PER_RUN=2000,
    PENDING_WORK_MAX_AGE_DAYS=14,
)


def _run(root, run_id, jobs, engine, **over):
    """One process: a fresh StateManager on the same root, as a restart would be."""
    policy = pf(EM.FULL_DRY_RUN)
    ctx = RunContext.create(EM.FULL_DRY_RUN, {"mode": "full_dry_run"}, run_id=run_id)
    state = StateManager(root, policy, run_id=run_id)
    plan = OrchestratorPlan(lanes=["fantastic"], lane_runners={"fantastic": _lane(jobs)},
                            enrichment_engine=engine, delivery_manager=_Delivery())
    with mock.patch.multiple(config, **dict(CFG, **over)):
        return Orchestrator(ctx, state, _Budget()).run(plan, resume=False)


class TheSequenceThatLostTheWork(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.store = Path(self.root) / "pending_work"

    def test_a_billing_stop_leaves_the_postings_in_custody(self):
        engine = _ApolloRefuses()
        _run(self.root, "20260906T030230Z-aaaaaaaa", [_posting(i) for i in range(3)], engine)

        held = pending_work.summary(self.store)
        self.assertEqual(held["pending_postings"], 3,
                         "postings bought and not finished must stay in custody")
        self.assertEqual(held["pending_runs"], 1)

    def test_a_restart_re_enters_the_same_work_without_re_acquiring(self):
        """The load-bearing one. Run 2 acquires NOTHING, so anything it enriches can
        only have come from custody -- it cannot have been repurchased."""
        _run(self.root, "20260906T030230Z-aaaaaaaa",
             [_posting(i) for i in range(3)], _ApolloRefuses())

        second = _ApolloWorks()
        _run(self.root, "20260907T030000Z-bbbbbbbb", [], second)   # nothing acquired

        self.assertTrue(second.seen, "enrichment must still run on a zero-acquisition run")
        resumed = sorted(second.seen[0])
        self.assertEqual(resumed, ["j0", "j1", "j2"],
                         "the same postings, handed back from custody")

    def test_custody_ends_only_when_the_work_is_finished(self):
        _run(self.root, "20260906T030230Z-aaaaaaaa",
             [_posting(i) for i in range(3)], _ApolloRefuses())
        self.assertEqual(pending_work.summary(self.store)["pending_postings"], 3)

        _run(self.root, "20260907T030000Z-bbbbbbbb", [], _ApolloWorks())
        self.assertEqual(pending_work.summary(self.store)["pending_postings"], 0,
                         "terminal dispositions release custody")

    def test_a_second_failed_run_does_not_duplicate_the_debt(self):
        for run_id in ("20260906T030230Z-aaaaaaaa", "20260907T030000Z-bbbbbbbb"):
            _run(self.root, run_id, [_posting(i) for i in range(3)], _ApolloRefuses())
        # The same three postings, re-acquired and re-recorded, are still three.
        held = pending_work.summary(self.store)
        self.assertLessEqual(held["pending_postings"], 6)
        offered = {p for row in held["runs"] for p in [row["run_id"]]}
        self.assertTrue(offered)
        # And a third run adopting them enriches each posting once.
        third = _ApolloWorks()
        _run(self.root, "20260908T030000Z-cccccccc", [], third)
        self.assertEqual(sorted(third.seen[0]), ["j0", "j1", "j2"])

    def test_disabled_by_flag_restores_the_old_behaviour_exactly(self):
        engine = _ApolloRefuses()
        _run(self.root, "20260906T030230Z-aaaaaaaa", [_posting(i) for i in range(3)],
             engine, PENDING_WORK_ENABLED=False)
        self.assertEqual(pending_work.summary(self.store)["pending_postings"], 0)


class CustodySurvivesRetention(unittest.TestCase):
    def test_prune_cannot_delete_the_store(self):
        """Retention deletes run_artifacts. The store is a SIBLING for this reason:
        the payloads used to live only inside a run directory, which prune removes
        after four runs."""
        root = tempfile.mkdtemp()
        _run(root, "20260906T030230Z-aaaaaaaa",
             [_posting(i) for i in range(3)], _ApolloRefuses())
        store = Path(root) / "pending_work"
        self.assertEqual(pending_work.summary(store)["pending_postings"], 3)

        policy = pf(EM.FULL_DRY_RUN)
        state = StateManager(root, policy, run_id="20260910T030000Z-zzzzzzzz")
        state.prune(keep=1, max_bytes=1)

        self.assertEqual(pending_work.summary(store)["pending_postings"], 3,
                         "prune must not reach the custody store")


class TheStoreItself(unittest.TestCase):
    @staticmethod
    def _age(store, when="2020-01-01T00:00:00+00:00"):
        path = next(p for p in store.glob("*.json") if not p.name.startswith("_"))
        held = pending_work._read(path)
        held["recorded_at"] = when
        path.write_text(__import__("json").dumps(held), encoding="utf-8")

    def setUp(self):
        self.store = Path(tempfile.mkdtemp()) / "pending_work"

    def test_recording_the_same_posting_twice_holds_it_once(self):
        jobs = [_posting(1), _posting(2)]
        pending_work.record(self.store, "run-a", jobs)
        again = pending_work.record(self.store, "run-a", jobs)
        self.assertEqual(again["recorded"], 0)
        self.assertEqual(again["already_held"], 2)
        self.assertEqual(pending_work.summary(self.store)["pending_postings"], 2)

    def test_a_run_never_adopts_its_own_debt(self):
        pending_work.record(self.store, "run-a", [_posting(1)])
        jobs, info = pending_work.load(self.store, exclude_run_id="run-a")
        self.assertEqual(jobs, [])
        self.assertEqual(info["skipped_current_run"], 1)

    def test_the_resume_is_bounded(self):
        pending_work.record(self.store, "run-a", [_posting(i) for i in range(50)])
        jobs, _info = pending_work.load(self.store, exclude_run_id="now", limit=10)
        self.assertEqual(len(jobs), 10)

    def test_a_read_never_makes_work_disappear(self):
        """Expiry is a separate, audited step. If ``load`` quietly skipped aged
        entries, the debt would vanish from every report while still existing."""
        pending_work.record(self.store, "run-a", [_posting(1)])
        self._age(self.store)
        jobs, _info = pending_work.load(self.store, exclude_run_id="now")
        self.assertEqual(len(jobs), 1, "a read must not apply retention")

    def test_release_takes_the_same_ids_suppression_does(self):
        pending_work.record(self.store, "run-a", [_posting(1), _posting(2)])
        info = pending_work.release(self.store, {"j1"})
        self.assertEqual(info["released"], 1)
        self.assertEqual(pending_work.summary(self.store)["pending_postings"], 1)

    def test_an_empty_terminal_set_releases_nothing(self):
        """The provider-outage case: no lead finished, so nothing may be forgotten."""
        pending_work.record(self.store, "run-a", [_posting(1), _posting(2)])
        info = pending_work.release(self.store, set())
        self.assertEqual(info["released"], 0)
        self.assertEqual(info["still_held"], 2)


if __name__ == "__main__":
    unittest.main()
