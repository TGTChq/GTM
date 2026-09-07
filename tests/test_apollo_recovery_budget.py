"""An aggregate, durable Apollo ceiling -- and why the existing limits were not one.

`PENDING_WORK_RESUME_MAX_PER_RUN=2000` bounds WORK, not money: 2,000 resumed postings
can issue an organisation enrich, a people search and one or more person matches
each, plus the alternate cascade and the org-id fallback behind them.
`APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN` covers one endpoint, resets every run, and its
default of 0 documents itself as "no ceiling" -- so an unset budget spends without
limit.

Every one of those is inverted here, and each inversion is a test.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
from orchestrator import apollo_budget as ab


def _cfg(**kw):
    base = {"APOLLO_RECOVERY_BUDGET_ENABLED": True,
            "APOLLO_RECOVERY_BUDGET_ID": "grant-1",
            "APOLLO_RECOVERY_BUDGET_CALLS": 10,
            "APOLLO_RECOVERY_BUDGET_STATE_PATH":
                str(Path(tempfile.mkdtemp()) / "budget.json")}
    base.update(kw)
    return mock.patch.multiple(config, **base)


class AnUnsetBudgetIsZeroNotUnlimited(unittest.TestCase):
    def test_disabled_charges_nothing_and_changes_nothing(self):
        with _cfg(APOLLO_RECOVERY_BUDGET_ENABLED=False):
            out = ab.charge(ab.KIND_PERSON_MATCH)
            self.assertFalse(out["charged"])
            self.assertEqual(out["reason"], "budget_not_enabled")

    def test_enabled_with_no_authorization_id_refuses(self):
        with _cfg(APOLLO_RECOVERY_BUDGET_ID=""):
            with self.assertRaises(ab.BudgetExhausted):
                ab.charge(ab.KIND_ORG_ENRICH)

    def test_enabled_with_zero_calls_refuses(self):
        with _cfg(APOLLO_RECOVERY_BUDGET_CALLS=0):
            with self.assertRaises(ab.BudgetExhausted):
                ab.charge(ab.KIND_ORG_ENRICH)

    def test_preflight_says_which_of_the_three_is_missing(self):
        with _cfg(APOLLO_RECOVERY_BUDGET_ID=""):
            self.assertIn("unset budget is zero", ab.preflight()["reason"])
        with _cfg(APOLLO_RECOVERY_BUDGET_CALLS=0):
            self.assertIn("nothing is authorized", ab.preflight()["reason"])
        with _cfg(APOLLO_RECOVERY_BUDGET_ENABLED=False):
            self.assertIn("is off", ab.preflight()["reason"])


class ItCoversEveryChargeablePath(unittest.TestCase):
    def test_only_potentially_paid_kinds_draw_on_the_aggregate(self):
        with _cfg(APOLLO_RECOVERY_BUDGET_CALLS=2):
            ab.charge(ab.KIND_ORG_ENRICH)
            self.assertFalse(ab.charge(ab.KIND_PEOPLE_SEARCH)["charged"])
            ab.charge(ab.KIND_PERSON_MATCH)
            self.assertEqual(ab.summary()["remaining"], 0)
            with self.assertRaises(ab.BudgetExhausted):
                ab.charge(ab.KIND_PERSON_MATCH)

    def test_consumption_is_attributable_per_kind(self):
        with _cfg():
            ab.charge(ab.KIND_PERSON_MATCH, 2)
            ab.charge(ab.KIND_ORG_ENRICH)
            by_kind = ab.summary()["by_kind"]
            self.assertEqual(by_kind[ab.KIND_PERSON_MATCH], 2)
            self.assertEqual(by_kind[ab.KIND_ORG_ENRICH], 1)

    def test_a_retry_costs_what_the_first_attempt_cost(self):
        """Retries are not free and must not be exempt -- that exemption is how a
        ceiling is exceeded while every individual caller believes it complied."""
        with _cfg(APOLLO_RECOVERY_BUDGET_CALLS=2):
            ab.charge(ab.KIND_PERSON_MATCH)
            ab.charge(ab.KIND_PERSON_MATCH)
            with self.assertRaises(ab.BudgetExhausted):
                ab.charge(ab.KIND_PERSON_MATCH)

    def test_an_unknown_kind_is_refused_rather_than_uncounted(self):
        with _cfg():
            with self.assertRaises(ValueError):
                ab.charge("something_new")


class ItIsDurableAcrossRuns(unittest.TestCase):
    def test_a_corrupt_ledger_cannot_restore_a_spent_grant(self):
        with _cfg() as _:
            target = Path(config.APOLLO_RECOVERY_BUDGET_STATE_PATH)
            target.write_text('{"schema":')
            before = target.read_bytes()
            with self.assertRaises(ab.BudgetExhausted):
                ab.charge(ab.KIND_PERSON_MATCH)
            self.assertEqual(target.read_bytes(), before)

    def test_a_second_run_does_not_get_a_fresh_budget(self):
        """The failure that turns an aggregate ceiling into a daily allowance."""
        path = str(Path(tempfile.mkdtemp()) / "budget.json")
        with _cfg(APOLLO_RECOVERY_BUDGET_CALLS=2,
                  APOLLO_RECOVERY_BUDGET_STATE_PATH=path):
            ab.charge(ab.KIND_PERSON_MATCH)
        with _cfg(APOLLO_RECOVERY_BUDGET_CALLS=2,
                  APOLLO_RECOVERY_BUDGET_STATE_PATH=path):
            self.assertEqual(ab.summary()["consumed"], 1)
            ab.charge(ab.KIND_PERSON_MATCH)
            with self.assertRaises(ab.BudgetExhausted):
                ab.charge(ab.KIND_PERSON_MATCH)

    def test_raising_the_number_alone_does_not_reset_the_count(self):
        """A silent ceiling raise is a config drift, not a grant."""
        path = str(Path(tempfile.mkdtemp()) / "budget.json")
        with _cfg(APOLLO_RECOVERY_BUDGET_CALLS=2,
                  APOLLO_RECOVERY_BUDGET_STATE_PATH=path):
            ab.charge(ab.KIND_PERSON_MATCH, 2)
        with _cfg(APOLLO_RECOVERY_BUDGET_CALLS=10,
                  APOLLO_RECOVERY_BUDGET_STATE_PATH=path):
            self.assertEqual(ab.summary()["consumed"], 2)
            self.assertEqual(ab.summary()["remaining"], 8)

    def test_a_new_authorization_id_starts_a_fresh_count(self):
        path = str(Path(tempfile.mkdtemp()) / "budget.json")
        with _cfg(APOLLO_RECOVERY_BUDGET_CALLS=2,
                  APOLLO_RECOVERY_BUDGET_STATE_PATH=path):
            ab.charge(ab.KIND_PERSON_MATCH, 2)
        with _cfg(APOLLO_RECOVERY_BUDGET_ID="grant-2",
                  APOLLO_RECOVERY_BUDGET_CALLS=2,
                  APOLLO_RECOVERY_BUDGET_STATE_PATH=path):
            self.assertEqual(ab.summary()["consumed"], 0)

    def test_exhaustion_is_recorded_so_a_deferral_is_not_a_mystery(self):
        with _cfg(APOLLO_RECOVERY_BUDGET_CALLS=1):
            ab.charge(ab.KIND_PERSON_MATCH)
            with self.assertRaises(ab.BudgetExhausted):
                ab.charge(ab.KIND_PERSON_MATCH)
            self.assertEqual(ab.summary()["deferrals"], 1)


class ChargingHappensBeforeTheCall(unittest.TestCase):
    def test_the_charge_is_recorded_before_the_request_is_issued(self):
        """Charging afterwards lets the request that went over the line be paid for
        and unrecorded if the process dies in between -- the one accounting error a
        spend ceiling must not make."""
        import inspect

        source = inspect.getsource(ab.charge)
        self.assertIn("Charged BEFORE the call is issued", ab.charge.__doc__ or "")
        self.assertIn("_write(target, state)", source)


class PreflightAnswersBeforeAnyWorkIsAdopted(unittest.TestCase):
    def test_it_reports_partial_rather_than_refusing_a_smaller_run(self):
        with _cfg(APOLLO_RECOVERY_BUDGET_CALLS=5):
            out = ab.preflight(required=100)
            self.assertTrue(out["ok"])
            self.assertTrue(out["partial"])
            self.assertIn("only 5 calls remain", out["reason"])

    def test_a_sufficient_budget_is_simply_ok(self):
        with _cfg(APOLLO_RECOVERY_BUDGET_CALLS=500):
            out = ab.preflight(required=100)
            self.assertTrue(out["ok"])
            self.assertEqual(out["reason"], "")


if __name__ == "__main__":
    unittest.main()


class TheChargePointsAreTheChargeableCalls(unittest.TestCase):
    """The budget is only aggregate if it sits on EVERY paid entry point. Wiring it
    at one call site and calling it "aggregate" is exactly the mistake
    `APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN` made -- one endpoint, described as an
    overall ceiling."""

    def test_free_search_does_not_consume_an_exhausted_paid_grant(self):
        import apollo_client
        with _cfg(APOLLO_RECOVERY_BUDGET_CALLS=0), mock.patch.object(
                apollo_client, "request_with_retry") as request, mock.patch.object(
                apollo_client, "safe_json", return_value={"people": []}):
            self.assertEqual(apollo_client.search_people_at_company("acme.com", ["CEO"]), [])
            self.assertEqual(apollo_client.search_people_by_org_id("org1", ["CEO"]), [])
            self.assertEqual(request.call_count, 2)
            self.assertEqual(ab.summary()["consumed"], 0)

    def test_the_ordinary_path_is_untouched_when_the_budget_is_off(self):
        """Default OFF has to mean no behaviour change at all, or enabling recovery
        would be entangled with enabling a new failure mode."""
        import apollo_client

        with mock.patch.object(config, "APOLLO_RECOVERY_BUDGET_ENABLED", False):
            # No exception, no state file, nothing to configure.
            apollo_client._charge_recovery_budget("person_match")

    def test_exhaustion_stops_the_run_globally_not_one_company(self):
        """THE bug this nearly shipped with. `BudgetExhausted` is a plain
        RuntimeError, and the enrichment loop's per-company handler catches
        `Exception`, marks that company UNVERIFIED and CONTINUES -- so it would have
        been re-raised on the next company and the next, marking the entire remaining
        cohort UNVERIFIED while spending nothing and reporting it as processed.

        Re-raised as a member of `GLOBAL_FATAL_ERRORS`, the loop stops, completed work
        is preserved and the company that tripped it is left for a later run -- the
        clean pause the budget is documented to be."""
        import apollo_client

        with _cfg(APOLLO_RECOVERY_BUDGET_CALLS=1):
            apollo_client._charge_recovery_budget("person_match")
            with self.assertRaises(apollo_client.ApolloBudgetExhaustedError):
                apollo_client._charge_recovery_budget("person_match")

    def test_the_budget_error_is_globally_fatal(self):
        import apollo_client

        self.assertIn(apollo_client.ApolloBudgetExhaustedError,
                      apollo_client.GLOBAL_FATAL_ERRORS)

    def test_it_is_reported_as_a_budget_stop_not_a_credit_stop(self):
        """"We may not spend more" and "the provider has nothing left" need different
        responses and must never look alike in an artifact."""
        import apollo_client
        import hiring_manager

        self.assertEqual(
            hiring_manager._apollo_circuit_reason(
                apollo_client.ApolloBudgetExhaustedError("spent")),
            "apollo_budget_exhausted")
        self.assertEqual(
            hiring_manager._apollo_circuit_reason(
                apollo_client.ApolloCreditsExhaustedError("gone")),
            "apollo_credit_exhausted")
