"""Regression tests for D1 (top-up disabled-reason visibility), D11
(_priority_score state-awareness), and D12 (NEEDS_CHECK reason visibility).

Traces to ROOT_CAUSE_TABLE_STRUCTURAL.md rows 1, 10, 11.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import config
import run_daily
from recovery_inventory import FinalPassInventory
import tempfile


class TopupDisabledReasonTests(unittest.TestCase):
    def test_names_every_failed_gate(self):
        with (
            patch.object(config, "MULTI_SOURCE_JSEARCH_TOPUP_ENABLED", False),
            patch.object(config, "JSEARCH_TOPUP_MAX_ROUNDS", 0),
        ):
            reason = run_daily._jsearch_topup_disabled_reason(
                "multi_source", jsearch_available=True, target_final_pass=30,
            )
        self.assertIn("topup_switch", reason)
        # multi_source mode always satisfies legacy_rounds_enabled regardless
        # of JSEARCH_TOPUP_MAX_ROUNDS -- only topup_switch should be named.
        self.assertNotIn("legacy_rounds_enabled", reason)

    def test_empty_reason_when_enabled(self):
        with (
            patch.object(config, "MULTI_SOURCE_JSEARCH_TOPUP_ENABLED", True),
        ):
            enabled = run_daily._jsearch_topup_enabled(
                "multi_source", jsearch_available=True, target_final_pass=30,
            )
            reason = run_daily._jsearch_topup_disabled_reason(
                "multi_source", jsearch_available=True, target_final_pass=30,
            )
        self.assertTrue(enabled)
        self.assertEqual(reason, "")

    def test_jsearch_unavailable_is_named(self):
        reason = run_daily._jsearch_topup_disabled_reason(
            "multi_source", jsearch_available=False, target_final_pass=30,
        )
        self.assertIn("jsearch_available", reason)


class PriorityScoreStateAwarenessTests(unittest.TestCase):
    def test_final_pass_outranks_needs_check_even_with_lower_score(self):
        with tempfile.TemporaryDirectory() as temp:
            inv = FinalPassInventory(f"{temp}/inv.json")
            weak_final_pass = {
                "_final_state": "FINAL_PASS", "employer_website": "https://a.com",
                "bucket": "finance", "lead_key": "a-1", "job_signal_confidence": "",
                "hiring_manager_email": "a@a.com", "hiring_manager_name": "A Person",
            }
            strong_needs_check = {
                "_final_state": "NEEDS_CHECK", "employer_website": "https://b.com",
                "bucket": "finance", "lead_key": "b-1", "job_signal_confidence": "official",
                "hiring_manager_selection_tier": "direct", "apollo_email_status": "verified",
                "hunter_email_status": "valid", "company_employee_count": 100,
                "hiring_manager_email": "b@b.com", "hiring_manager_name": "B Person",
            }
            inv.stage([weak_final_pass, strong_needs_check])
            available = inv.available()
        self.assertEqual(available[0]["lead_key"], "a-1")
        self.assertEqual(available[1]["lead_key"], "b-1")


if __name__ == "__main__":
    unittest.main()
