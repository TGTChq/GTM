"""What must happen when there is no money, and what must happen when there is again.

Running permanently means the interesting state is not the happy one. Four properties
have to hold together, and they are easy to satisfy in ways that break each other:

* an exhausted budget is a PAUSE -- unfinished work survives and is not relabelled;
* it does not turn into a retry storm, and it does retry on the next scheduled run;
* the ceiling is durable, covers EVERY potentially paid request including the
  availability probe, and is not reset by deploying;
* the provider's balance and our internal authorization are different things, and
  topping up the first does not renew the second.

Nothing here contacts a provider: `ci_no_network` blocks sockets and DNS for the whole
run, so a test that accidentally issued a request would fail rather than spend.
"""

import json
import unittest
from pathlib import Path
from unittest import mock

import config
from orchestrator import apollo_budget as budget


def _grant(tmp, *, ident, calls):
    """A budget state file as a deployment with that grant would produce it."""
    path = Path(tmp) / "apollo_recovery_budget.json"
    with mock.patch.multiple(config, APOLLO_RECOVERY_BUDGET_ENABLED=True,
                             APOLLO_RECOVERY_BUDGET_ID=ident,
                             APOLLO_RECOVERY_BUDGET_CALLS=calls,
                             APOLLO_RECOVERY_BUDGET_STATE_PATH=str(path)):
        budget.load(str(path))
    return path


class TheCeilingIsDurableAndCoversEveryPaidRequest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "apollo_recovery_budget.json"

    def _cfg(self, ident, calls, enabled=True):
        return mock.patch.multiple(
            config, APOLLO_RECOVERY_BUDGET_ENABLED=enabled,
            APOLLO_RECOVERY_BUDGET_ID=ident, APOLLO_RECOVERY_BUDGET_CALLS=calls,
            APOLLO_RECOVERY_BUDGET_STATE_PATH=str(self.path))

    def test_an_unset_grant_refuses_rather_than_spending_without_a_limit(self):
        with self._cfg("", 0):
            with self.assertRaises(budget.BudgetExhausted):
                budget.charge(budget.KIND_ORG_ENRICH, path=str(self.path))

    def test_the_counter_survives_a_deploy(self):
        """A restarted process reads the same durable file, not a fresh counter.

        This is the failure that turns a spend ceiling into a per-deploy allowance:
        redeploy twice and the grant is spent three times.
        """
        with self._cfg("grant-a", 3):
            budget.charge(budget.KIND_ORG_ENRICH, path=str(self.path))
            budget.charge(budget.KIND_PERSON_MATCH, path=str(self.path))
        # A new process, same config, same volume path.
        with self._cfg("grant-a", 3):
            state = budget.load(str(self.path))
            self.assertEqual(state["consumed"], 2)
            self.assertEqual(state["remaining"], 1)
            budget.charge(budget.KIND_ORG_ENRICH, path=str(self.path))
            with self.assertRaises(budget.BudgetExhausted):
                budget.charge(budget.KIND_ORG_ENRICH, path=str(self.path))

    def test_reusing_a_spent_authorization_resumes_it_instead_of_renewing_it(self):
        """The trap the operating notes name: raise the calls, keep the id.

        The consumed counter belongs to the AUTHORIZATION, so re-using a spent id
        with a bigger number continues that grant rather than opening a new one.
        """
        with self._cfg("grant-spent", 2):
            budget.charge(budget.KIND_ORG_ENRICH, path=str(self.path))
            budget.charge(budget.KIND_ORG_ENRICH, path=str(self.path))
            with self.assertRaises(budget.BudgetExhausted):
                budget.charge(budget.KIND_ORG_ENRICH, path=str(self.path))
        with self._cfg("grant-spent", 5):   # same id, larger number
            state = budget.load(str(self.path))
            self.assertEqual(state["consumed"], 2, "the spent grant was resumed")
            self.assertEqual(state["remaining"], 3)

    def test_a_new_authorization_id_is_what_opens_a_fresh_grant(self):
        with self._cfg("grant-old", 1):
            budget.charge(budget.KIND_ORG_ENRICH, path=str(self.path))
        with self._cfg("grant-new", 4):
            state = budget.load(str(self.path))
            self.assertEqual(state["authorization_id"], "grant-new")
            self.assertEqual(state["consumed"], 0)
            self.assertEqual(state["remaining"], 4)

    def test_a_reservation_is_a_request_attempt_not_a_provider_credit(self):
        """Calls and credits are separate units and are never equated.

        The ledger counts request ATTEMPTS by kind. What Apollo charges for one is a
        property of the response and the plan, so the ceiling bounds requests and
        says so rather than claiming to bound credits.
        """
        with self._cfg("grant-units", 5):
            budget.charge(budget.KIND_ORG_ENRICH, path=str(self.path))
            budget.charge(budget.KIND_PERSON_MATCH, path=str(self.path))
            # People API Search is documented zero-credit; it is not charged, and it
            # does not silently consume the aggregate either.
            out = budget.charge(budget.KIND_PEOPLE_SEARCH, path=str(self.path))
            self.assertFalse(out["charged"])
            state = budget.load(str(self.path))
            self.assertEqual(state["by_kind"][budget.KIND_ORG_ENRICH], 1)
            self.assertEqual(state["by_kind"][budget.KIND_PERSON_MATCH], 1)
            self.assertEqual(state["by_kind"][budget.KIND_PEOPLE_SEARCH], 0,
                             "a zero-credit endpoint is recorded, never charged")
            self.assertEqual(state["consumed"], 2)

    def test_preflight_refuses_at_the_top_so_a_refusal_costs_nothing(self):
        with self._cfg("", 0):
            out = budget.preflight(path=str(self.path))
            self.assertFalse(out["ok"])
            self.assertIn("unset budget is zero", out["reason"])


class TheAvailabilityProbeIsInsideTheCeiling(unittest.TestCase):
    """The probe is the one call made when nobody knows what the account will do.

    It used to issue its request with a raw client, outside the budget entirely --
    so an availability check could spend while the ceiling that exists to bound
    spending was not consulted, and the record of what was spent was incomplete by
    exactly the calls made to decide whether to spend.
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "apollo_recovery_budget.json"

    def _cfg(self, ident, calls):
        return mock.patch.multiple(
            config, APOLLO_API_KEY="test-key-abcd",
            APOLLO_RECOVERY_BUDGET_ENABLED=True, APOLLO_RECOVERY_BUDGET_ID=ident,
            APOLLO_RECOVERY_BUDGET_CALLS=calls,
            APOLLO_RECOVERY_BUDGET_STATE_PATH=str(self.path))

    def _probe(self):
        import importlib
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance"))
        module = importlib.import_module("apollo_readiness")
        return importlib.reload(module)

    def test_an_unauthorized_probe_refuses_without_issuing_a_request(self):
        probe = self._probe()
        with self._cfg("", 0), mock.patch.object(probe.requests, "get") as get:
            self.assertEqual(probe.main(), 3)
            get.assert_not_called()

    def test_a_spent_grant_refuses_the_probe_too(self):
        probe = self._probe()
        with self._cfg("grant-x", 1):
            budget.charge(budget.KIND_ORG_ENRICH, path=str(self.path))
        with self._cfg("grant-x", 1), mock.patch.object(probe.requests, "get") as get:
            self.assertEqual(probe.main(), 3)
            get.assert_not_called()

    def test_an_authorized_probe_reserves_before_it_requests(self):
        probe = self._probe()
        response = mock.Mock(status_code=200, headers={"x-request-id": "req-1"})
        with self._cfg("grant-y", 2), mock.patch.object(
                probe.requests, "get", return_value=response) as get:
            self.assertEqual(probe.main(), 0)
            get.assert_called_once()
            # Read inside the authorization context: load() reconciles against the
            # CURRENT id, so reading it outside would report a fresh grant.
            self.assertEqual(budget.load(str(self.path))["consumed"], 1,
                             "the probe's own call is on the ledger")

    def test_a_refused_probe_still_consumed_its_reservation(self):
        """A reservation is taken BEFORE the request and is never refunded.

        Charging after the fact would let a request that was issued go unrecorded if
        the process died in between -- the one accounting error a ceiling must not
        make. A 422 that costs Apollo nothing therefore still costs a reservation,
        which is the conservative direction.
        """
        probe = self._probe()
        body = ('{"error_code": "BILLING.LIMIT.CREDITS_EXHAUSTED", '
                '"error_message": "credits exhausted"}')
        response = mock.Mock(status_code=422, headers={}, text=body,
                             json=lambda: json.loads(body))
        with self._cfg("grant-z", 2), mock.patch.object(
                probe.requests, "get", return_value=response):
            probe.main()
            self.assertEqual(budget.load(str(self.path))["consumed"], 1)


class ExhaustionIsAPauseNotALoss(unittest.TestCase):
    def test_a_provider_credit_stop_is_globally_fatal_by_design(self):
        """Why it must not be caught per company.

        The enrichment loop's other handler catches Exception per company, marks it
        UNVERIFIED and continues. A credit error raised there would repeat on every
        remaining company, relabelling the whole cohort as UNVERIFIED while spending
        nothing -- a loss that reads as a measurement. Being globally fatal is what
        makes exhaustion a clean stop with the work preserved.
        """
        from apollo_client import GLOBAL_FATAL_ERRORS
        from apollo_client import ApolloCreditsExhaustedError
        from orchestrator.apollo_budget import BudgetExhausted

        self.assertIn(ApolloCreditsExhaustedError, GLOBAL_FATAL_ERRORS)
        names = {cls.__name__ for cls in GLOBAL_FATAL_ERRORS}
        self.assertTrue(any("Budget" in name for name in names),
                        f"our own ceiling must be fatal too, saw {sorted(names)}")
        self.assertTrue(issubclass(BudgetExhausted, RuntimeError))

    def test_the_deferral_is_recorded_so_a_pause_is_distinguishable_from_a_failure(self):
        import tempfile
        path = Path(tempfile.mkdtemp()) / "b.json"
        with mock.patch.multiple(config, APOLLO_RECOVERY_BUDGET_ENABLED=True,
                                 APOLLO_RECOVERY_BUDGET_ID="", APOLLO_RECOVERY_BUDGET_CALLS=0,
                                 APOLLO_RECOVERY_BUDGET_STATE_PATH=str(path)):
            for _ in range(3):
                with self.assertRaises(budget.BudgetExhausted):
                    budget.charge(budget.KIND_ORG_ENRICH, path=str(path))
            self.assertEqual(json.loads(path.read_text())["deferrals"], 3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
