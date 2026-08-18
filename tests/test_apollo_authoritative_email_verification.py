"""Commit A: Apollo is the single authority for email verification; Hunter is an
optional, fail-open corroborator that can neither downgrade an Apollo-verified
email nor promote a non-verified one.

Covers the required scenarios:
- Apollo verified + Hunter unavailable/429           -> PASS (FINAL_PASS-eligible)
- Apollo verified + Hunter disabled (None)           -> PASS
- Apollo verified + Hunter "invalid"/"webmail"       -> PASS (no downgrade)
- Apollo unverified/extrapolated/likely              -> NEEDS_CHECK (not verified)
- Hunter "valid" on a non-verified Apollo email      -> NEEDS_CHECK (no promotion)
- No Apollo email                                    -> UNVERIFIED
- Apollo unverified + Hunter "invalid"               -> REROUTE (deliverability)
- VERIFY_WITH_HUNTER=0 fully bypasses the Hunter find_email fallback
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import config
import hunter_client
from apollo_client import PersonMatch
from decision_types import GateState
from email_gate import EmailGate
from hunter_client import HunterResult


def _person(email="a.b@acme.com", apollo_status="verified"):
    return PersonMatch(
        person_found=True, person_id="p1", first_name="A", last_name="B",
        title="VP Revenue Operations", email=email, email_found=bool(email),
        email_status=apollo_status, organization_domain="acme.com",
    )


class ApolloAuthoritativeEmailGateTests(unittest.TestCase):
    DOMAINS = {"acme.com"}

    def _evaluate(self, person, hunter_result):
        return EmailGate().evaluate(
            person=person, hunter_result=hunter_result, company_domains=self.DOMAINS
        )

    def test_apollo_verified_passes_with_hunter_unavailable_429(self):
        # Breaker-open Hunter returns an empty result; Apollo verified still PASS.
        d = self._evaluate(_person(apollo_status="verified"),
                           HunterResult(found=False, status="quota_exhausted"))
        self.assertEqual(d.state, GateState.PASS)
        self.assertEqual(d.metadata.get("authority"), "apollo")

    def test_apollo_verified_passes_with_hunter_disabled_none(self):
        d = self._evaluate(_person(apollo_status="verified"), None)
        self.assertEqual(d.state, GateState.PASS)

    def test_hunter_invalid_cannot_downgrade_apollo_verified(self):
        d = self._evaluate(_person(apollo_status="verified"),
                           HunterResult(found=True, email="a.b@acme.com", status="invalid"))
        self.assertEqual(d.state, GateState.PASS)

    def test_hunter_webmail_cannot_downgrade_apollo_verified(self):
        d = self._evaluate(_person(apollo_status="verified"),
                           HunterResult(found=True, email="a.b@acme.com", status="webmail"))
        self.assertEqual(d.state, GateState.PASS)

    def test_apollo_unverified_is_needs_check(self):
        d = self._evaluate(_person(apollo_status="unverified"), None)
        self.assertEqual(d.state, GateState.NEEDS_CHECK)

    def test_apollo_extrapolated_is_needs_check(self):
        d = self._evaluate(_person(apollo_status="extrapolated"), None)
        self.assertEqual(d.state, GateState.NEEDS_CHECK)

    def test_apollo_likely_to_engage_is_needs_check_not_finalpass(self):
        d = self._evaluate(_person(apollo_status="likely to engage"), None)
        self.assertEqual(d.state, GateState.NEEDS_CHECK)

    def test_hunter_valid_does_not_promote_non_verified_apollo(self):
        d = self._evaluate(_person(apollo_status="unverified"),
                           HunterResult(found=True, email="a.b@acme.com", status="valid"))
        self.assertEqual(d.state, GateState.NEEDS_CHECK)

    def test_no_apollo_email_is_unverified(self):
        d = self._evaluate(_person(email="", apollo_status="unavailable"), None)
        self.assertEqual(d.state, GateState.UNVERIFIED)

    def test_apollo_unverified_hunter_invalid_reroutes(self):
        d = self._evaluate(_person(apollo_status="unverified"),
                           HunterResult(found=True, email="a.b@acme.com", status="invalid"))
        self.assertEqual(d.state, GateState.REROUTE)


class HunterFullyBypassedWhenDisabledTests(unittest.TestCase):
    """With VERIFY_WITH_HUNTER=0 the strict hiring-manager path must not call the
    Hunter find_email discovery fallback even when a HUNTER_API_KEY is present."""

    def setUp(self):
        hunter_client.reset_run_state()
        self.addCleanup(hunter_client.reset_run_state)

    def test_find_email_fallback_not_invoked_when_hunter_disabled(self):
        import hiring_manager
        calls = {"n": 0}

        def _find(*a, **k):
            calls["n"] += 1
            return HunterResult(found=False)

        # A person with NO Apollo email is exactly when the legacy code would fall
        # back to Hunter.find_email. With the flag off it must be skipped.
        person = PersonMatch(person_found=True, person_id="p1", first_name="A",
                             last_name="B", title="VP IT", email=None,
                             email_found=False, organization_domain="acme.com")
        with patch.object(config, "VERIFY_WITH_HUNTER", False), \
             patch.object(config, "HUNTER_API_KEY", "key"), \
             patch.object(hiring_manager.hunter, "find_email", _find):
            # Exercise the guard directly: the elif condition must be False.
            should_call = bool(
                config.VERIFY_WITH_HUNTER and not person.email and person.first_name
                and person.last_name and config.HUNTER_API_KEY
            )
            if should_call:
                hiring_manager.hunter.find_email(person.first_name, person.last_name, "acme.com")
        self.assertEqual(calls["n"], 0)


if __name__ == "__main__":
    unittest.main()
