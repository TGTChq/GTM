import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hiring_manager


class DailyThroughputTests(unittest.TestCase):
    @staticmethod
    def _job(index: int) -> dict:
        return {
            "job_id": f"job-{index}",
            "employer_name": f"Company {index}",
            "employer_website": f"https://company{index}.com",
            "job_title": "GTM Engineer",
        }

    @staticmethod
    def _reviewable_lead(index: int) -> dict:
        return {
            "_step3_status": "found",
            "hiring_manager_confidence": "high",
            "hiring_manager_email": f"person{index}@company{index}.com",
            "lead_key": f"company{index}.com|person{index}@company{index}.com|gtm",
            "hiring_manager_name": f"Person {index}",
        }

    @staticmethod
    def _not_found_lead() -> dict:
        return {
            "_step3_status": "not_found",
            "hiring_manager_confidence": "none",
            "hiring_manager_email": None,
            "lead_key": None,
            "hiring_manager_name": None,
        }

    def test_reviewable_target_stops_enrichment_and_preserves_processed_refs(self):
        jobs = [self._job(i) for i in range(1, 6)]
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "filtered.json"
            input_path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")

            side_effect = [
                ([self._reviewable_lead(1)], {}),
                ([self._not_found_lead()], {}),
                ([self._reviewable_lead(3)], {}),
            ]
            with (
                patch.object(hiring_manager, "validate_preflight"),
                patch.object(hiring_manager, "process_company", side_effect=side_effect),
                patch.object(hiring_manager.config, "STEP3_OUTPUT_DIR", tmp),
            ):
                result = hiring_manager.run_hiring_manager_identification(
                    str(input_path),
                    target_reviewable_leads=2,
                    max_eligible_companies=5,
                )

            self.assertEqual(result.reviewable_leads, 2)
            self.assertTrue(result.reviewable_target_reached)
            self.assertEqual(result.companies_considered, 3)
            self.assertEqual(result.stop_reason, "reviewable_lead_target_reached")

            output = json.loads(Path(result.output_path).read_text(encoding="utf-8"))
            self.assertEqual(len(output["processed_job_refs"]), 3)
            self.assertEqual(
                [item["job_id"] for item in output["processed_job_refs"]],
                ["job-1", "job-2", "job-3"],
            )

    def test_eligible_company_cap_stops_low_contactability_run(self):
        jobs = [self._job(i) for i in range(1, 5)]
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "filtered.json"
            input_path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")

            with (
                patch.object(hiring_manager, "validate_preflight"),
                patch.object(
                    hiring_manager,
                    "process_company",
                    side_effect=[
                        ([self._not_found_lead()], {}),
                        ([self._not_found_lead()], {}),
                    ],
                ),
                patch.object(hiring_manager.config, "STEP3_OUTPUT_DIR", tmp),
            ):
                result = hiring_manager.run_hiring_manager_identification(
                    str(input_path),
                    target_reviewable_leads=3,
                    max_eligible_companies=2,
                )

            self.assertEqual(result.reviewable_leads, 0)
            self.assertFalse(result.reviewable_target_reached)
            self.assertTrue(result.eligible_company_limit_reached)
            self.assertEqual(result.eligible_companies, 2)
            self.assertEqual(result.companies_considered, 2)
            self.assertEqual(result.stop_reason, "eligible_company_safety_cap_reached")

    def test_controlled_test_target_keeps_original_semantics(self):
        jobs = [self._job(i) for i in range(1, 4)]
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "filtered.json"
            input_path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")

            with (
                patch.object(hiring_manager, "validate_preflight"),
                patch.object(
                    hiring_manager,
                    "process_company",
                    side_effect=[
                        ([self._not_found_lead()], {}),
                        ([self._not_found_lead()], {}),
                    ],
                ),
                patch.object(hiring_manager.config, "STEP3_OUTPUT_DIR", tmp),
            ):
                result = hiring_manager.run_hiring_manager_identification(
                    str(input_path),
                    target_eligible_companies=2,
                )

            self.assertTrue(result.target_reached)
            self.assertEqual(result.eligible_companies, 2)
            self.assertEqual(result.stop_reason, "eligible_company_target_reached")


if __name__ == "__main__":
    unittest.main()
