"""COMMIT 3 -- adaptive net-new top-up.

Two layers:
* TopUpController (pure): every hard-stop boundary, the target unit, the safety-cap
  clamp (billing can never exceed the cap), and the max-iteration guarantee that no
  loop can ever be infinite.
* _run_body_topup (pipeline loop, stubbed I/O): the loop advances continuation
  slices, stops on target / cap / inventory, never bills past the safety cap, counts
  only net-new send-safe, and emits a topup result block.
"""

import tempfile
import unittest
from unittest.mock import patch

import config
from orchestrator.topup import TopUpController


class ControllerBoundaryTests(unittest.TestCase):
    def _c(self, **over):
        kw = dict(target_net_new=1000, safety_cap_jobs=100, slice_jobs=30,
                  min_quota_remaining=50, runtime_budget_seconds=None,
                  max_iterations=40)
        kw.update(over)
        return TopUpController(**kw)

    def test_disabled_when_target_zero(self):
        self.assertFalse(self._c(target_net_new=0).enabled)
        self.assertTrue(self._c(target_net_new=1).enabled)

    def test_stops_on_target_reached(self):
        c = self._c(target_net_new=5)
        c.record(billed=10, net_new_send_safe=5)
        self.assertEqual(c.decide().stop_reason, "target_reached")
        self.assertFalse(c.decide().should_continue)

    def test_next_slice_never_exceeds_remaining_cap(self):
        c = self._c(safety_cap_jobs=100, slice_jobs=30)
        c.record(billed=90, net_new_send_safe=0)      # 10 left under the cap
        d = c.decide()
        self.assertTrue(d.should_continue)
        self.assertEqual(d.next_slice, 10)            # clamped, not 30

    def test_stops_on_safety_cap(self):
        c = self._c(safety_cap_jobs=100)
        c.record(billed=100, net_new_send_safe=0)
        self.assertEqual(c.decide().stop_reason, "acquisition_safety_cap")

    def test_stops_on_quota_floor(self):
        c = self._c(min_quota_remaining=50)
        self.assertEqual(c.decide(quota_remaining=50).stop_reason, "fantastic_quota_floor")
        self.assertEqual(c.decide(quota_remaining=49).stop_reason, "fantastic_quota_floor")
        self.assertTrue(c.decide(quota_remaining=51).should_continue)

    def test_stops_on_apollo_circuit_open(self):
        self.assertEqual(self._c().decide(apollo_circuit_open=True).stop_reason,
                         "apollo_circuit_open")

    def test_stops_on_inventory_exhausted(self):
        self.assertEqual(self._c().decide(inventory_exhausted=True).stop_reason,
                         "inventory_exhausted")

    def test_stops_on_runtime_budget(self):
        clock = [0.0]
        c = self._c(runtime_budget_seconds=10, clock=lambda: clock[0])
        self.assertTrue(c.decide().should_continue)
        clock[0] = 10.0
        self.assertEqual(c.decide().stop_reason, "runtime_budget")

    def test_max_iterations_guard_prevents_infinite_loop(self):
        # Even with an unreachable target, a huge cap and endless inventory, the
        # controller MUST stop after max_iterations.
        c = self._c(target_net_new=10_000, safety_cap_jobs=10_000_000,
                    slice_jobs=1, max_iterations=5)
        iters = 0
        while c.decide().should_continue:
            c.record(billed=1, net_new_send_safe=0)
            iters += 1
            if iters > 1000:
                self.fail("controller allowed an unbounded loop")
        self.assertEqual(iters, 5)
        self.assertEqual(c.last_stop_reason, "max_iterations_guard")

    def test_billing_never_exceeds_cap_across_a_full_drain(self):
        c = self._c(target_net_new=10_000, safety_cap_jobs=100, slice_jobs=30,
                    min_quota_remaining=0)
        while True:
            d = c.decide(quota_remaining=999)
            if not d.should_continue:
                break
            self.assertLessEqual(c.billed + d.next_slice, 100)   # invariant
            c.record(billed=d.next_slice, net_new_send_safe=0)
        self.assertEqual(c.billed, 100)
        self.assertEqual(c.last_stop_reason, "acquisition_safety_cap")


# --------------------------------------------------------------------------
# Pipeline loop integration (stubbed acquisition / enrichment / delivery).
# --------------------------------------------------------------------------
from retrieval_measurement.instrument import RequestBudget           # noqa: E402
from orchestrator.enrichment import Disposition, EnrichmentReport, Lead  # noqa: E402
from orchestrator.adapters_real import RealDeliveryReport            # noqa: E402
from orchestrator.lanes import LaneResult                            # noqa: E402
from orchestrator.modes import ExecutionMode, policy_for             # noqa: E402
from orchestrator.reasons import ReasonCode                          # noqa: E402
from orchestrator.runcontrol import RunContext                       # noqa: E402
from orchestrator.state import StateManager                          # noqa: E402
from orchestrator.pipeline import Orchestrator, OrchestratorPlan     # noqa: E402


def _lead(i):
    return Lead(posting_id=f"P{i}", company={"name": f"Co{i}"},
                contact={"email": f"e{i}@x.com", "_airtable_row": {}},
                disposition=Disposition.FINAL_PASS, primary_reason=ReasonCode.OK,
                contact_key=f"k{i}")


class _StubEnrichment:
    def __init__(self, leads_per_slice):
        self.leads_per_slice = leads_per_slice
        self._n = 0

    def run(self, opportunities, **kw):
        leads = [_lead(f"{self._n}_{j}") for j in range(min(self.leads_per_slice, len(opportunities)))]
        self._n += 1
        return EnrichmentReport(leads=leads, stages=[])


class _StubDelivery:
    def __init__(self, created_per_slice):
        self.created_per_slice = created_per_slice
        self._n = 0

    def deliver(self, leads, *, run_id="", known_delivered=None, **kw):
        created = min(self.created_per_slice, len(leads))
        self._n += 1
        keys = [l.contact_key for l in leads[:created]]
        return RealDeliveryReport(
            mode="review_staging", entered=len(leads), reviewable_submitted=len(leads),
            created=created, skipped=len(leads) - created, failed=0, final_pass=len(leads),
            delivered_lead_keys=keys,
            # Every lead entered is submitted here, so nothing is withheld -- but
            # the count must be RECORDED: `reconciles()` no longer treats an
            # absent one as a pass, because an unverifiable identity is not a
            # satisfied one.
            detail={"airtable": {"created_lead_keys": keys},
                    "withheld_before_submit": 0})


def _fantastic_runner(supply_per_slice, quota=999999):
    """Returns up to the loop-imposed per-call cap of fresh postings each call, from
    a schedule of per-slice supply (0 => inventory exhausted)."""
    state = {"i": 0, "uid": 0}

    def runner(manager):
        import fantastic_jobs_adapter as _fja
        n = supply_per_slice[state["i"]] if state["i"] < len(supply_per_slice) else 0
        state["i"] += 1
        # Mirror the real adapter: clamp this iteration's billing to the RUNTIME slice
        # budget (decoupled from the config-validated FANTASTIC_JOBS_MAX_JOBS_PER_RUN).
        n = min(n, _fja._effective_run_cap())
        jobs = []
        for _ in range(n):
            state["uid"] += 1
            jobs.append({"job_id": f"J{state['uid']}", "employer_name": "Co",
                         "job_title": "Engineer"})
        return LaneResult(lane="fantastic", status="complete", jobs=jobs,
                          physical_requests=1,
                          attribution={"source": "fantastic_jobs", "records": n,
                                       "jobs_quota_remaining": quota, "stop_reason": ""})
    return runner


class TopUpLoopTests(unittest.TestCase):
    def _run(self, *, target, cap, slice_jobs, supply, created_per_slice,
             leads_per_slice=10, quota=999999, max_iter=40):
        tmp = tempfile.mkdtemp()
        mode = ExecutionMode.FULL_DRY_RUN
        ctx = RunContext.create(mode, {"t": 1}, run_id="TOPUP")
        state = StateManager(tmp, policy_for(mode), run_id="TOPUP")
        budget = RequestBudget(limit=10_000)
        plan = OrchestratorPlan(
            lanes=["fantastic"],
            lane_runners={"fantastic": _fantastic_runner(supply, quota=quota)},
            enrichment_engine=_StubEnrichment(leads_per_slice),
            delivery_manager=_StubDelivery(created_per_slice),
            target=5)
        with (
            patch.object(config, "NET_NEW_SEND_SAFE_TARGET", target),
            patch.object(config, "FANTASTIC_JOBS_MAX_JOBS_PER_RUN", cap),
            patch.object(config, "FANTASTIC_TOPUP_SLICE_JOBS", slice_jobs),
            patch.object(config, "FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING", 500),
            patch.object(config, "TOPUP_MAX_ITERATIONS", max_iter),
            patch.object(config, "PRE_APOLLO_EXISTING_DEDUPE", False),
            # Simulate send-safe-only semantics: every created row is net-new send-safe.
            patch.object(Orchestrator, "_count_net_new_send_safe",
                         staticmethod(lambda leads, delivery: delivery.created)),
        ):
            return Orchestrator(ctx, state, budget).run(plan)

    def test_stops_when_target_reached_without_exceeding_cap(self):
        res = self._run(target=3, cap=1000, slice_jobs=2, supply=[5, 5, 5, 5],
                        created_per_slice=2)
        tu = res["topup"]
        self.assertTrue(tu["target_reached"])
        self.assertEqual(tu["stop_reason"], "target_reached")
        self.assertGreaterEqual(tu["net_new_send_safe"], 3)
        self.assertLessEqual(tu["jobs_billed"], 1000)
        self.assertTrue(res["all_reconcile"])

    def test_never_bills_beyond_safety_cap(self):
        # Target unreachable, endless inventory, zero yield -> must stop at the cap
        # with billing exactly == cap (slices of 30 into a 100 cap).
        res = self._run(target=10_000, cap=100, slice_jobs=30,
                        supply=[30] * 50, created_per_slice=0)
        tu = res["topup"]
        self.assertEqual(tu["stop_reason"], "acquisition_safety_cap")
        self.assertEqual(tu["jobs_billed"], 100)
        self.assertLessEqual(tu["jobs_billed"], 100)

    def test_stops_on_inventory_exhaustion(self):
        res = self._run(target=10_000, cap=10_000, slice_jobs=50,
                        supply=[50, 50, 0], created_per_slice=1)
        tu = res["topup"]
        self.assertEqual(tu["stop_reason"], "inventory_exhausted")
        self.assertEqual(tu["jobs_billed"], 100)   # two slices billed, third empty

    def test_no_infinite_loop_when_yield_is_zero(self):
        res = self._run(target=10_000, cap=10_000_000, slice_jobs=1,
                        supply=[1] * 100000, created_per_slice=0, max_iter=7)
        tu = res["topup"]
        self.assertEqual(tu["stop_reason"], "max_iterations_guard")
        self.assertEqual(tu["iterations"], 7)


if __name__ == "__main__":
    unittest.main()
