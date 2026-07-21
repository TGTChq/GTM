from __future__ import annotations

import unittest

from run_gold_benchmark import evaluate


class GoldBenchmarkV02Tests(unittest.TestCase):
    def test_empty_pass_set_does_not_prove_precision(self):
        gold = {"lote_2a_leads": [{"job_id": "a", "final_state": "FINAL_PASS", "primary_reason": "FINAL_PASS"}]}
        predictions = {"jobs": [{"job_id": "a", "_final_state": "UNVERIFIED", "_final_primary_reason": "X"}]}
        report = evaluate(gold, predictions)
        self.assertFalse(report["release_ready"])
        self.assertFalse(report["release_checks"]["not_proven_by_empty_auto_pass_set"])
        self.assertEqual(report["metrics"]["false_negative_rate"], 1.0)

    def test_false_positive_breaks_release(self):
        gold = {"lote_2a_leads": [
            {"job_id": "a", "final_state": "FINAL_PASS", "primary_reason": "FINAL_PASS"},
            {"job_id": "b", "final_state": "REJECT", "primary_reason": "REJECT_X"},
        ]}
        predictions = {"jobs": [
            {"job_id": "a", "_final_state": "FINAL_PASS", "_final_primary_reason": "FINAL_PASS"},
            {"job_id": "b", "_final_state": "FINAL_PASS", "_final_primary_reason": "FINAL_PASS"},
        ]}
        report = evaluate(gold, predictions)
        self.assertEqual(report["metrics"]["auto_pass_precision"], 0.5)
        self.assertFalse(report["release_checks"]["zero_fatal_false_positives"])

    def test_exact_predictions_pass_contract_benchmark(self):
        gold = {"lote_2a_leads": [
            {"job_id": "a", "final_state": "FINAL_PASS", "primary_reason": "FINAL_PASS", "publisher": "Official"},
            {"job_id": "b", "final_state": "REJECT", "primary_reason": "REJECT_X", "publisher": "Board"},
        ]}
        predictions = {"jobs": [
            {"job_id": "a", "_final_state": "FINAL_PASS", "_final_primary_reason": "FINAL_PASS"},
            {"job_id": "b", "_final_state": "REJECT", "_final_primary_reason": "REJECT_X"},
        ]}
        report = evaluate(gold, predictions)
        self.assertTrue(report["release_ready"])
        self.assertEqual(report["metrics"]["auto_pass_precision"], 1.0)


if __name__ == "__main__":
    unittest.main()
