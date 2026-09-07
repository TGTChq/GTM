"""Exhaustion, restart and automatic recovery -- verified before the mode is armed.

Continuous mode removes the manual grant after each top-up. That is only safe if
three things hold without anyone watching:

* **exhaustion** stops paid acquisition and preserves work, rather than buying
  postings nothing can enrich;
* **restart** does not lose the fact that the provider is refusing, because a
  forgotten refusal means the next run attempts as if nothing happened;
* **recovery** happens on its own the moment Apollo serves again -- no new grant, no
  variable edit, no human.

None of this contacts a provider: `ci_no_network` blocks sockets and DNS for the whole
run. The refusal is simulated at the boundary the real one crosses.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import config
from orchestrator import apollo_availability as av
from orchestrator import apollo_budget as budget


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.budget_path = self.dir / "budget.json"
        self.avail_path = self.dir / "availability.json"

    def cfg(self, **over):
        base = dict(APOLLO_CONTINUOUS_MODE=True,
                    APOLLO_RECOVERY_BUDGET_ENABLED=True,
                    APOLLO_RECOVERY_BUDGET_ID="", APOLLO_RECOVERY_BUDGET_CALLS=0,
                    APOLLO_RECOVERY_BUDGET_STATE_PATH=str(self.budget_path),
                    APOLLO_AVAILABILITY_STATE_PATH=str(self.avail_path),
                    APOLLO_AVAILABILITY_RETRY_HOURS=6)
        base.update(over)
        return mock.patch.multiple(config, **base)

    def _age_last_attempt(self, hours):
        state = av.load(str(self.avail_path))
        state["last_attempt_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        av._write(str(self.avail_path), state)


class Exhaustion(_Base):
    def test_a_refusal_stands_paid_acquisition_down(self):
        """The point of the whole gate: never buy what cannot be enriched."""
        with self.cfg():
            self.assertTrue(budget.paid_acquisition_allowed(str(self.budget_path))["allowed"])
            av.record_refusal("BILLING.LIMIT.CREDITS_EXHAUSTED", path=str(self.avail_path))
            out = budget.paid_acquisition_allowed(str(self.budget_path))
        self.assertFalse(out["allowed"])
        self.assertEqual(out["reason"], "provider_refusing")
        self.assertIn("not bought until a call actually succeeds", out["detail"])

    def test_the_refusal_is_recorded_with_when_and_why(self):
        with self.cfg():
            av.record_refusal("BILLING.LIMIT.CREDITS_EXHAUSTED", path=str(self.avail_path))
            state = av.summary(str(self.avail_path))
        self.assertEqual(state["state"], av.REFUSING)
        self.assertTrue(state["refusing_since"])
        self.assertEqual(state["last_error_code"], "BILLING.LIMIT.CREDITS_EXHAUSTED")
        self.assertEqual(state["consecutive_refusals"], 1)

    def test_repeated_refusals_do_not_move_the_since_timestamp(self):
        """`refusing_since` answers "how long", so it must not reset on each retry."""
        with self.cfg():
            first = av.record_refusal("x", path=str(self.avail_path))["refusing_since"]
            av.record_refusal("x", path=str(self.avail_path))
            state = av.load(str(self.avail_path))
        self.assertEqual(state["refusing_since"], first)
        self.assertEqual(state["consecutive_refusals"], 2)

    def test_an_uncapped_continuous_call_is_still_counted(self):
        """Running without a cap must not mean running without a count."""
        with self.cfg():
            for _ in range(4):
                budget.charge(budget.KIND_ORG_ENRICH, path=str(self.budget_path))
            state = budget.load(str(self.budget_path))
        self.assertEqual(state["consumed"], 4)
        self.assertEqual(state["uncapped_continuous_calls"], 4)

    def test_a_configured_aggregate_still_refuses_in_continuous_mode(self):
        """Continuous does not mean uncontrolled: a set ceiling is still a ceiling."""
        with self.cfg(APOLLO_RECOVERY_BUDGET_ID="g", APOLLO_RECOVERY_BUDGET_CALLS=2):
            budget.charge(budget.KIND_ORG_ENRICH, path=str(self.budget_path))
            budget.charge(budget.KIND_ORG_ENRICH, path=str(self.budget_path))
            with self.assertRaises(budget.BudgetExhausted):
                budget.charge(budget.KIND_ORG_ENRICH, path=str(self.budget_path))


class Restart(_Base):
    def test_a_refusal_survives_the_process_that_saw_it(self):
        """A forgotten refusal means the next run attempts as if nothing happened."""
        with self.cfg():
            av.record_refusal("credits", path=str(self.avail_path))
        # A brand-new process reading the same volume path.
        with self.cfg():
            out = budget.paid_acquisition_allowed(str(self.budget_path))
        self.assertFalse(out["allowed"])
        self.assertEqual(av.load(str(self.avail_path))["state"], av.REFUSING)

    def test_a_missing_or_corrupt_record_reads_as_unknown_and_attempts(self):
        """Unknown must mean TRY.

        A failed attempt is free; a wrongly withheld attempt costs a whole day. So
        the conservative direction for a lost file is to go and find out.
        """
        self.avail_path.write_text("{not json", encoding="utf-8")
        with self.cfg():
            self.assertEqual(av.load(str(self.avail_path))["state"], av.UNKNOWN)
            self.assertTrue(budget.paid_acquisition_allowed(str(self.budget_path))["allowed"])

    def test_a_deploy_does_not_clear_a_refusal(self):
        """The record lives on the mounted volume, not in the image."""
        with self.cfg():
            av.record_refusal("credits", path=str(self.avail_path))
            before = av.load(str(self.avail_path))
        with self.cfg():                       # new process, same path
            after = av.load(str(self.avail_path))
        self.assertEqual(after["refusing_since"], before["refusing_since"])


class AutomaticRecovery(_Base):
    def test_an_elapsed_interval_permits_a_CHECK_and_not_a_PURCHASE(self):
        """Corrected: this test used to assert the defect.

        It read "interval elapsed, therefore acquisition allowed" -- which is how a
        refusing provider silently re-enabled buying after seven hours, with no new
        answer from Apollo. The interval governs whether a controlled attempt may be
        made; only a served response governs whether inventory may be bought.
        """
        with self.cfg():
            av.record_refusal("credits", path=str(self.avail_path))
            blocked = budget.paid_acquisition_allowed(str(self.budget_path))
            self.assertFalse(blocked["allowed"])
            self.assertFalse(av.may_attempt(path=str(self.avail_path))["allowed"])

            self._age_last_attempt(7)
            after = budget.paid_acquisition_allowed(str(self.budget_path))
            self.assertFalse(after["allowed"], "a clock is not a response")
            self.assertTrue(after["check_due"], "but a controlled retry is now due")
            self.assertTrue(av.may_attempt(path=str(self.avail_path))["allowed"])

            av.record_served(path=str(self.avail_path))
            self.assertTrue(
                budget.paid_acquisition_allowed(str(self.budget_path))["allowed"])

    def test_a_served_call_restores_operation_with_no_human_step(self):
        """The whole point: no new grant, no variable edit, no person.

        Recovery is a side effect of the next scheduled run's own first chargeable
        call succeeding -- which is also why learning that credits are back costs
        nothing when they are not.
        """
        with self.cfg():
            av.record_refusal("credits", path=str(self.avail_path))
            self.assertFalse(budget.paid_acquisition_allowed(str(self.budget_path))["allowed"])
            av.record_served(path=str(self.avail_path))          # Apollo served again
            out = budget.paid_acquisition_allowed(str(self.budget_path))
        self.assertTrue(out["allowed"])
        self.assertEqual(av.summary(str(self.avail_path))["state"], av.SERVING)
        self.assertEqual(av.summary(str(self.avail_path))["consecutive_refusals"], 0)
        self.assertEqual(av.summary(str(self.avail_path))["refusing_since"], "")

    def test_recovery_needs_no_new_authorization_id(self):
        """Contrast with the grant model, which this mode replaces."""
        with self.cfg():
            av.record_refusal("credits", path=str(self.avail_path))
            av.record_served(path=str(self.avail_path))
            out = budget.paid_acquisition_allowed(str(self.budget_path))
        self.assertTrue(out["allowed"])
        self.assertEqual(out["reason"], "continuous_mode")
        self.assertEqual(config.APOLLO_RECOVERY_BUDGET_ID, "",
                         "no id was issued and none was needed")

    def test_serving_is_a_memory_of_the_last_answer_not_a_prediction(self):
        """A stored SERVING never asserts that credits exist now.

        The next refusal flips it straight back, which is what keeps the mode honest
        when a balance runs out between runs.
        """
        with self.cfg():
            av.record_served(path=str(self.avail_path))
            self.assertTrue(budget.paid_acquisition_allowed(str(self.budget_path))["allowed"])
            av.record_refusal("credits", path=str(self.avail_path))
            self.assertFalse(budget.paid_acquisition_allowed(str(self.budget_path))["allowed"])


class TheGrantModelIsUntouchedWhenTheModeIsOff(_Base):
    def test_without_continuous_mode_an_unset_grant_still_refuses(self):
        with self.cfg(APOLLO_CONTINUOUS_MODE=False):
            with self.assertRaises(budget.BudgetExhausted):
                budget.charge(budget.KIND_ORG_ENRICH, path=str(self.budget_path))
            out = budget.paid_acquisition_allowed(str(self.budget_path))
        self.assertFalse(out["allowed"])
        self.assertEqual(out["reason"], "no_enrichment_authorization")

    def test_without_continuous_mode_a_refusing_provider_is_not_consulted(self):
        """The two gates are independent; only one is in force at a time."""
        with self.cfg(APOLLO_CONTINUOUS_MODE=False, APOLLO_RECOVERY_BUDGET_ID="g",
                      APOLLO_RECOVERY_BUDGET_CALLS=5):
            av.record_refusal("credits", path=str(self.avail_path))
            out = budget.paid_acquisition_allowed(str(self.budget_path))
        self.assertTrue(out["allowed"], "the grant model does not read availability")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
