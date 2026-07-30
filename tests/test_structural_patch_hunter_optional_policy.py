"""Phase 13 section 1: Hunter is an OPTIONAL corroboration provider, never a
throughput dependency.

- On a 429/quota exhaustion, the existing run-level circuit breaker opens and
  every subsequent Hunter call is skipped with no retry loop.
- An Apollo email_status="verified" + company-domain match reaches EmailGate
  PASS on its own, with Hunter unavailable.
- A non-verified Apollo email must NOT pass merely because Hunter is
  unavailable (it goes to NEEDS_CHECK).
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import config
import hunter_client
from apollo_client import PersonMatch
from email_gate import EmailGate
from decision_types import GateState
from http_utils import QuotaExhaustedError


class _Resp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class HunterCircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        hunter_client.reset_run_state()
        self.addCleanup(hunter_client.reset_run_state)

    def test_429_opens_breaker_and_further_calls_skip_without_retry(self):
        err = Exception("boom")
        err.response = _Resp(429, "monthly quota exceeded")
        call_count = {"n": 0}

        def raising(*a, **k):
            call_count["n"] += 1
            raise err

        with patch.object(config, "HUNTER_API_KEY", "key"), patch.object(
            hunter_client, "request_with_retry", raising
        ):
            first = hunter_client.verify_email("a@acme.com")   # trips the breaker
            self.assertFalse(first.found)
            self.assertTrue(hunter_client.run_status()["quota_unavailable"])
            # Subsequent calls must NOT hit the network again (no retry loop).
            second = hunter_client.verify_email("b@acme.com")
            third = hunter_client.verify_email("c@acme.com")
            self.assertFalse(second.found)
            self.assertFalse(third.found)
        self.assertEqual(call_count["n"], 1)  # only the first call reached the client
        status = hunter_client.run_status()
        self.assertEqual(status["skipped_reason"], "hunter_skipped_quota_unavailable")
        self.assertEqual(status["calls_skipped"], 2)

    def test_quota_exhausted_error_also_opens_breaker(self):
        def raising(*a, **k):
            raise QuotaExhaustedError("credits exhausted")

        with patch.object(config, "HUNTER_API_KEY", "key"), patch.object(
            hunter_client, "request_with_retry", raising
        ):
            hunter_client.verify_email("a@acme.com")
        self.assertTrue(hunter_client.run_status()["quota_unavailable"])

    def test_reset_run_state_clears_breaker_for_next_run(self):
        hunter_client._hunter_quota_exhausted_for_run = True
        hunter_client.reset_run_state()
        self.assertFalse(hunter_client.run_status()["quota_unavailable"])
        self.assertEqual(hunter_client.run_status()["calls_skipped"], 0)

    def test_non_quota_error_is_reraised_not_silently_swallowed(self):
        def raising(*a, **k):
            raise RuntimeError("transient network")

        with patch.object(config, "HUNTER_API_KEY", "key"), patch.object(
            hunter_client, "request_with_retry", raising
        ):
            with self.assertRaises(RuntimeError):
                hunter_client.verify_email("a@acme.com")
        # A transient (non-quota) error must not open the breaker.
        self.assertFalse(hunter_client.run_status()["quota_unavailable"])


class EmailGateHunterOptionalTests(unittest.TestCase):
    def _person(self, email, apollo_status):
        return PersonMatch(
            person_found=True, person_id="p1", first_name="A", last_name="B",
            title="VP Revenue Operations", email=email, email_found=True,
            email_status=apollo_status, organization_domain="acme.com",
        )

    def test_apollo_verified_email_passes_with_hunter_unavailable(self):
        person = self._person("a.b@acme.com", "verified")
        # Hunter unavailable -> None result (breaker open / skipped).
        decision = EmailGate().evaluate(person=person, hunter_result=None, company_domains={"acme.com"})
        self.assertEqual(decision.state_value, GateState.PASS.value)

    def test_apollo_verified_email_passes_with_empty_hunter_result(self):
        person = self._person("a.b@acme.com", "verified")
        empty = hunter_client.HunterResult(found=False)
        decision = EmailGate().evaluate(person=person, hunter_result=empty, company_domains={"acme.com"})
        self.assertEqual(decision.state_value, GateState.PASS.value)

    def test_unverified_apollo_email_does_not_pass_just_because_hunter_absent(self):
        person = self._person("a.b@acme.com", "unavailable")
        decision = EmailGate().evaluate(person=person, hunter_result=None, company_domains={"acme.com"})
        self.assertEqual(decision.state_value, GateState.NEEDS_CHECK.value)

    def test_wrong_domain_apollo_verified_email_still_rejected(self):
        person = self._person("a.b@other.com", "verified")
        decision = EmailGate().evaluate(person=person, hunter_result=None, company_domains={"acme.com"})
        self.assertNotEqual(decision.state_value, GateState.PASS.value)


if __name__ == "__main__":
    unittest.main()
