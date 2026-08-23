"""The contact loop must not settle for an e-mail that can never be send-safe.

MEASURED: re-enriching the SAME person recovers nothing (0/10). A DIFFERENT hiring
manager at the same company recovers a verified e-mail at rank 1 33.3%, rank 2 26.1%,
rank 3 16.7%. Historically the loop stopped on the first candidate that merely HAD an
e-mail -- including an Apollo-"extrapolated" one that can never pass send_safe_facts --
which is how a 68-row undeliverable backlog accumulated while
APOLLO_MAX_PERSON_MATCH_ATTEMPTS_PER_BUCKET=3 already allowed two more tries.
"""

from __future__ import annotations

import unittest
from unittest import mock

import config
import hiring_manager as HM


class BudgetTests(unittest.TestCase):
    def setUp(self):
        HM.reset_alternate_contact_budget()
        self.addCleanup(HM.reset_alternate_contact_budget)

    def _cfg(self, enabled=True, cap=100):
        return mock.patch.multiple(config,
                                   ALTERNATE_CONTACT_CASCADE_ENABLED=enabled,
                                   ALTERNATE_CONTACT_MAX_ENRICHMENTS_PER_RUN=cap)

    def test_disabled_flag_never_advances(self):
        with self._cfg(enabled=False):
            self.assertFalse(HM._alternate_budget_available())

    def test_zero_cap_never_advances(self):
        with self._cfg(cap=0):
            self.assertFalse(HM._alternate_budget_available())

    def test_budget_is_exhaustible_and_bounded(self):
        with self._cfg(cap=3):
            for _ in range(3):
                self.assertTrue(HM._alternate_budget_available())
                HM._ALTERNATE_BUDGET["used"] += 1
            self.assertFalse(HM._alternate_budget_available())
        self.assertEqual(HM.alternate_contact_budget_used(), 3)

    def test_reset_clears_the_budget(self):
        HM._ALTERNATE_BUDGET["used"] = 7
        HM.reset_alternate_contact_budget()
        self.assertEqual(HM.alternate_contact_budget_used(), 0)

    def test_run_entry_resets_the_budget(self):
        """A new run must never inherit the previous run's spend."""
        import inspect
        src = inspect.getsource(HM.run_hiring_manager_identification)
        self.assertIn("reset_alternate_contact_budget()", src)


class PersonLevelClassificationTests(unittest.TestCase):
    """Only a PERSON-level failure may advance a rank."""

    def _p(self, status):
        return mock.Mock(email_status=status)

    def test_unverified_statuses_are_person_level(self):
        for status in ("extrapolated", "unavailable", "", None, "guessed"):
            with self.subTest(status=status):
                self.assertTrue(HM._person_level_unverified(self._p(status)))

    def test_verified_is_not_person_level(self):
        self.assertFalse(HM._person_level_unverified(self._p("verified")))
        self.assertFalse(HM._person_level_unverified(self._p("VERIFIED")))


class CascadeWiringTests(unittest.TestCase):
    """The advance must be wired into BOTH contact paths, and fail open."""

    def _src(self):
        import inspect
        return inspect.getsource(HM)

    def test_both_paths_advance_on_person_level_failure(self):
        src = self._src()
        self.assertEqual(src.count("_person_level_unverified(person)"), 3)  # 2 uses + def

    def test_both_paths_fail_open_to_the_original_contact(self):
        src = self._src()
        self.assertIn("fallback_person is not None", src)
        self.assertIn("fallback_bundle is not None", src)
        self.assertEqual(src.count("alternate_cascade_fell_back_to_primary"), 2)

    def test_advance_requires_a_further_candidate(self):
        src = self._src()
        self.assertIn("_has_more_candidates", src)
        self.assertIn("_more2", src)

    def test_advance_is_budget_gated_in_both_paths(self):
        self.assertEqual(self._src().count("_alternate_budget_available()"), 3)  # 2 uses + def

    def test_employer_level_failures_never_advance(self):
        """Domain/identity/company-gate failures must keep the plain `continue`."""
        src = self._src()
        for marker in ("candidate_organization_domain_mismatch",
                       "candidate_email_domain_mismatch"):
            self.assertIn(marker, src)
        # The advance block is guarded by the person-level predicate only.
        self.assertNotIn("_person_level_unverified(person) or", src)

    def test_selected_rank_is_recorded_for_ledger_attribution(self):
        src = self._src()
        self.assertEqual(src.count('"hiring_manager_contact_rank"'), 2)

    def test_max_rank_is_bounded_by_the_existing_attempt_cap(self):
        """Rank 4+ can never run: the slice is the pre-existing per-bucket cap."""
        self.assertGreaterEqual(config.APOLLO_MAX_PERSON_MATCH_ATTEMPTS_PER_BUCKET, 1)
        self.assertLessEqual(config.APOLLO_MAX_PERSON_MATCH_ATTEMPTS_PER_BUCKET, 3)


class DefaultsTests(unittest.TestCase):
    def test_cascade_default_is_declared_off_in_source(self):
        import io
        src = io.open("config.py", encoding="utf-8").read()
        self.assertIn('_env_bool("ALTERNATE_CONTACT_CASCADE_ENABLED", False)', src)

    def test_run_cap_default_is_conservative(self):
        import io
        src = io.open("config.py", encoding="utf-8").read()
        self.assertIn('"ALTERNATE_CONTACT_MAX_ENRICHMENTS_PER_RUN", 100', src)


if __name__ == "__main__":
    unittest.main()
