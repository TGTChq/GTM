import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from final_pass_topup import _combine
from hiring_manager import Step3Result


class FinalPassCombinationTests(unittest.TestCase):
    def _result(self, *, success=True, errors=None):
        return Step3Result(
            output_path="unused.json",
            total_input_jobs=0,
            total_output_leads=0,
            company_criteria_excluded=0,
            hiring_manager_found=0,
            hiring_manager_not_found=0,
            match_rate=0.0,
            contactable_hiring_managers=0,
            uncontactable_hiring_managers=0,
            contactable_rate=0.0,
            companies_considered=0,
            eligible_companies=0,
            company_criteria_excluded_companies=0,
            target_reviewable_leads=1,
            reviewable_leads=0,
            reviewable_target_reached=False,
            final_pass_target=1,
            final_pass_leads=0,
            needs_check_leads=0,
            reroute_leads=0,
            unverified_leads=0,
            rejected_leads=0,
            final_pass_target_reached=False,
            max_eligible_companies=10,
            eligible_company_limit_reached=False,
            target_reached=False,
            stop_reason="",
            processed_company_keys=[],
            stats={},
            success=success,
            errors=list(errors or []),
        )

    def test_combined_result_propagates_any_later_failure_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp, patch("config.STEP3_OUTPUT_DIR", tmp):
            result = _combine(
                results=[self._result(), self._result(success=False, errors=["round failed"])],
                payloads=[{"jobs": []}, {"jobs": []}],
                target=1,
                max_eligible_companies=10,
                stop_reason="topup_error",
                topup_stats={},
            )
        self.assertFalse(result.success)
        self.assertEqual(result.errors, ["round failed"])


if __name__ == "__main__":
    unittest.main()
