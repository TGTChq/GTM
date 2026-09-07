"""A clock is not a response, and a reservation is not a response.

Two defects, both reproduced here against the shipped behaviour before being fixed:

1. after a refusal, once the retry interval elapsed, ``paid_acquisition_allowed``
   returned True -- so a run bought a window of postings it still could not enrich,
   on the strength of time passing and nothing else;
2. ``_charge_recovery_budget`` recorded SERVING *before* the request, so zero HTTP
   requests could turn REFUSING into SERVING and clear the retry block entirely.

The separation they force: **an elapsed interval permits one controlled attempt at
Apollo; only a satisfactory response permits buying inventory.** SERVING is written
from a response and from nowhere else -- not at reservation, not on an exception path,
not because a timer expired.

Nothing here reaches a provider: `ci_no_network` blocks sockets and DNS, and the
orchestrator tests below drive the real pipeline with faked boundaries.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import config
from orchestrator import apollo_availability as av
from orchestrator import apollo_budget as budget
from tests.test_throughput_contract import RecoveryProductionLoop


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

    def _refused_hours_ago(self, hours):
        av.record_refusal("BILLING.LIMIT.CREDITS_EXHAUSTED", path=str(self.avail_path))
        state = av.load(str(self.avail_path))
        state["last_attempt_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        av._write(str(self.avail_path), state)


class AnElapsedIntervalIsNotAResponse(_Base):
    def test_a_day_old_refusal_does_not_re_enable_buying(self):
        """Defect 1, exactly as reported: 24 hours, no new answer from Apollo."""
        with self.cfg():
            self._refused_hours_ago(24)
            out = budget.paid_acquisition_allowed(str(self.budget_path))
        self.assertFalse(out["allowed"])
        self.assertEqual(out["reason"], "provider_refusing")

    def test_but_a_controlled_check_IS_due(self):
        """The two questions must diverge, or the wait becomes a permanent stop."""
        with self.cfg():
            self._refused_hours_ago(24)
            out = budget.paid_acquisition_allowed(str(self.budget_path))
            self.assertTrue(out["check_due"], "a retry must be possible")
            self.assertTrue(av.may_attempt(path=str(self.avail_path))["allowed"])
            self.assertFalse(
                av.acquisition_allowed(path=str(self.avail_path))["allowed"])

    def test_within_the_interval_neither_is_permitted(self):
        with self.cfg():
            self._refused_hours_ago(1)
            self.assertFalse(av.may_attempt(path=str(self.avail_path))["allowed"])
            self.assertFalse(
                av.acquisition_allowed(path=str(self.avail_path))["allowed"])

    def test_only_a_served_response_lifts_it(self):
        with self.cfg():
            self._refused_hours_ago(24)
            av.record_served(path=str(self.avail_path))
            out = budget.paid_acquisition_allowed(str(self.budget_path))
        self.assertTrue(out["allowed"])
        self.assertEqual(av.load(str(self.avail_path))["state"], av.SERVING)


class ServingIsWrittenFromAResponse(_Base):
    def test_reserving_budget_does_not_flip_the_provider_state(self):
        """Defect 2: zero HTTP requests must not clear a refusal."""
        import apollo_client

        with self.cfg():
            self._refused_hours_ago(24)          # interval elapsed, so the reservation
            apollo_client._charge_recovery_budget("organization_enrich")
            self.assertEqual(av.load(str(self.avail_path))["state"], av.REFUSING,
                             "a reservation is not evidence the provider answered")

    def test_the_wait_applies_to_enrichment_calls_too(self):
        """Standing acquisition down while enrichment keeps calling governs nothing.

        A refusing provider asked again per company is precisely the retry storm the
        interval exists to prevent, and enrichment is the loudest caller.
        """
        import apollo_client

        with self.cfg():
            self._refused_hours_ago(1)           # inside the interval
            with self.assertRaises(apollo_client.ApolloCreditsExhaustedError) as raised:
                apollo_client._charge_recovery_budget("organization_enrich")
            self.assertIn("Nothing was requested", str(raised.exception))

    def test_an_exception_path_never_records_serving(self):
        import apollo_client

        with self.cfg():
            self._refused_hours_ago(1)
            with self.assertRaises(apollo_client.ApolloCreditsExhaustedError):
                apollo_client._charge_recovery_budget("person_match")
            self.assertEqual(av.load(str(self.avail_path))["state"], av.REFUSING)

    def test_the_response_recorder_is_what_writes_serving(self):
        import apollo_client

        with self.cfg():
            self._refused_hours_ago(24)
            apollo_client._record_provider_served()
            self.assertEqual(av.load(str(self.avail_path))["state"], av.SERVING)


class ThroughTheRealOrchestrator(RecoveryProductionLoop):
    """The pipeline itself, with provider and Airtable boundaries faked."""

    def _run(self, *, refused_hours_ago=None, then_served=False):
        directory = Path(tempfile.mkdtemp())
        budget_path = directory / "budget.json"
        avail_path = directory / "availability.json"
        cfg = dict(APOLLO_CONTINUOUS_MODE=True, APOLLO_RECOVERY_BUDGET_ENABLED=True,
                   APOLLO_RECOVERY_BUDGET_ID="", APOLLO_RECOVERY_BUDGET_CALLS=0,
                   APOLLO_RECOVERY_BUDGET_STATE_PATH=str(budget_path),
                   APOLLO_AVAILABILITY_STATE_PATH=str(avail_path),
                   APOLLO_AVAILABILITY_RETRY_HOURS=6)
        with mock.patch.multiple(config, **cfg):
            if refused_hours_ago is not None:
                av.record_refusal("credits", path=str(avail_path))
                state = av.load(str(avail_path))
                state["last_attempt_at"] = (datetime.now(timezone.utc)
                                            - timedelta(hours=refused_hours_ago)).isoformat()
                av._write(str(avail_path), state)
            if then_served:
                av.record_served(path=str(avail_path))
            return self.exercise(60, 20, 100000, continue_after=True)

    def test_a_day_old_refusal_still_buys_nothing(self):
        """The reported defect, at the level where it would have cost money."""
        result, reached, acquisitions, _ = self._run(refused_hours_ago=24)
        cumulative = result["acquisition"]["cumulative"]
        self.assertFalse(cumulative["paid_acquisition_authorization"]["allowed"])
        self.assertEqual(acquisitions, [], "no paid lane ran on an elapsed clock")
        self.assertIn("fantastic",
                      cumulative["paid_acquisition_stood_down"]["lanes"])
        self.assertEqual(len(set(reached)), 60, "queued work still drained")

    def test_a_fresh_refusal_buys_nothing_either(self):
        result, _, acquisitions, _ = self._run(refused_hours_ago=1)
        self.assertEqual(acquisitions, [])
        self.assertFalse(result["acquisition"]["cumulative"][
            "paid_acquisition_authorization"]["allowed"])

    def test_after_a_top_up_the_next_run_acquires_again(self):
        """Resumption after a reload, end to end and with no human step.

        The refusal stands, then a chargeable call succeeds -- which is what a
        top-up looks like from here -- and the very next run acquires. No new
        authorization id, no variable change.
        """
        result, reached, acquisitions, _ = self._run(refused_hours_ago=24,
                                                     then_served=True)
        cumulative = result["acquisition"]["cumulative"]
        self.assertTrue(cumulative["paid_acquisition_authorization"]["allowed"])
        self.assertNotIn("paid_acquisition_stood_down", cumulative)
        self.assertEqual(len(set(reached)), 60)

    def test_a_provider_never_seen_is_not_treated_as_refusing(self):
        result, _, _, _ = self._run()
        self.assertTrue(result["acquisition"]["cumulative"][
            "paid_acquisition_authorization"]["allowed"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
