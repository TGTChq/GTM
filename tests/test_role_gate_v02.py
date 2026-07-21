import unittest

from decision_types import GateState
from role_gate import RoleGate


class RoleGateV02Tests(unittest.TestCase):
    def test_senior_official_title_rejects(self):
        job = {
            "canonical_job_title": "Senior Staff Accountant",
            "official_job_description": "Own the monthly close.",
            "official_job_url": "https://example.com/job",
            "_matched_role": "Staff Accountant",
        }
        self.assertEqual(RoleGate().evaluate(job).state, GateState.REJECT)

    def test_supporting_senior_accountant_does_not_make_job_senior(self):
        job = {
            "canonical_job_title": "Staff Accountant",
            "official_job_description": "Support the Senior Accountant and assist with monthly close and reconciliations.",
            "official_job_url": "https://example.com/job",
            "_matched_role": "Staff Accountant",
        }
        self.assertEqual(RoleGate().evaluate(job).state, GateState.PASS)

    def test_industrial_automation_rejects(self):
        job = {
            "canonical_job_title": "Automation Specialist",
            "official_job_description": "Program PLC and SCADA controls in a manufacturing plant.",
            "official_job_url": "https://example.com/job",
            "_matched_role": "Automation Specialist",
        }
        self.assertEqual(RoleGate().evaluate(job).state, GateState.REJECT)


if __name__ == "__main__":
    unittest.main()
