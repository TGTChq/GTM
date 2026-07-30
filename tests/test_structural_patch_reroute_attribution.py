"""Regression tests for D9 (per-candidate reroute reason/TTL attribution).

Traces to ROOT_CAUSE_TABLE_STRUCTURAL.md row 8: reroute_state.py previously
applied one shared reason (from whichever candidate was attempted *last*) to
every candidate attempted in a bucket, under- or over-blocking specific
individuals.
"""
from __future__ import annotations

import tempfile
import unittest

from reroute_state import RerouteRegistry


class RecordManyTests(unittest.TestCase):
    def test_each_candidate_gets_its_own_reason_and_ttl(self):
        with tempfile.TemporaryDirectory() as temp:
            path = f"{temp}/reroute.json"
            registry = RerouteRegistry(path)
            registry.record_many(
                "acme.com|finance",
                {
                    "wrong-org-1": "REROUTE_WRONG_ORGANIZATION",  # permanent marker
                    "transient-1": "UNVERIFIED_NO_VALID_CONTACT",  # not a permanent marker
                },
            )
            record = registry.payload["accounts"]["acme.com|finance"]["people"]
            self.assertEqual(record["wrong-org-1"]["reason"], "REROUTE_WRONG_ORGANIZATION")
            self.assertEqual(record["transient-1"]["reason"], "UNVERIFIED_NO_VALID_CONTACT")
            # Permanent-marker reason expires much further out than the transient one.
            self.assertNotEqual(record["wrong-org-1"]["expires_at"], record["transient-1"]["expires_at"])

    def test_record_still_applies_one_shared_reason_to_all_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = RerouteRegistry(f"{temp}/reroute.json")
            registry.record("acme.com|finance", ["a", "b"], "UNVERIFIED_NO_VALID_CONTACT")
            record = registry.payload["accounts"]["acme.com|finance"]["people"]
            self.assertEqual(record["a"]["reason"], "UNVERIFIED_NO_VALID_CONTACT")
            self.assertEqual(record["b"]["reason"], "UNVERIFIED_NO_VALID_CONTACT")

    def test_a_transiently_failed_candidate_is_not_permanently_blocked_by_a_later_permanent_failure(self):
        """The concrete failure mode from the audit: candidate #1 fails for a
        transient reason, candidate #2 (attempted later, same bucket) fails
        for a genuine permanent mismatch. #1 must not inherit #2's long TTL."""
        with tempfile.TemporaryDirectory() as temp:
            registry = RerouteRegistry(f"{temp}/reroute.json")
            registry.record_many(
                "acme.com|finance",
                {
                    "candidate-1": "UNVERIFIED_NO_VALID_CONTACT",
                    "candidate-2": "REROUTE_SENIORITY_MISMATCH",
                },
            )
            record = registry.payload["accounts"]["acme.com|finance"]["people"]
            self.assertNotEqual(record["candidate-1"]["expires_at"], record["candidate-2"]["expires_at"])


if __name__ == "__main__":
    unittest.main()
