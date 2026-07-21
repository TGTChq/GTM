import unittest

from decision_types import GateState
from job_gate import JobGate
from job_source_resolver import ResolvedJobSource, title_materially_differs


class _Resolver:
    def __init__(self, source):
        self.source = source
    def resolve(self, job, fetch=None):
        return self.source


def source(description, *, title="Staff Accountant", employment_type="FULL_TIME", location="TELECOMMUTE | United States", state="ACTIVE_VERIFIED", active=True):
    return ResolvedJobSource(
        state=state,
        source_url="https://boards.greenhouse.io/example/jobs/1",
        source_type="ats",
        http_status=200,
        active=active,
        canonical_title=title,
        canonical_employer="Example Corp",
        description=description,
        location_text=location,
        employment_type=employment_type,
        official=True,
        corroborated=True,
    )


def job(title="Staff Accountant"):
    return {
        "job_id": "1",
        "job_title": title,
        "employer_name": "Example Corp",
        "employer_website": "https://example.com",
        "job_description": "discovery text",
        "job_location": "United States",
        "job_country": "US",
        "job_employment_type": "Full-time",
        "job_is_remote": True,
        "_matched_role": "Staff Accountant",
    }


class JobGateV02Tests(unittest.TestCase):
    def test_full_time_remote_us_official_source_passes(self):
        gate = JobGate(_Resolver(source(
            "This is a full-time remote role anywhere in the United States. "
            "You will support monthly close and financial reporting."
        )))
        decision = gate.evaluate(job())
        self.assertEqual(decision.state, GateState.PASS)

    def test_fractional_worker_rejects(self):
        gate = JobGate(_Resolver(source(
            "This is a long-term fractional contract position. The role is remote in the United States."
        )))
        decision = gate.evaluate(job())
        self.assertEqual(decision.state, GateState.REJECT)
        self.assertEqual(str(decision.primary_reason.value), "REJECT_FRACTIONAL")

    def test_fractional_service_does_not_reject_full_time_worker(self):
        gate = JobGate(_Resolver(source(
            "Our team provides fractional accounting services to portfolio companies. "
            "This is a full-time remote employee role anywhere in the United States."
        )))
        self.assertEqual(gate.evaluate(job()).state, GateState.PASS)

    def test_unpaid_leave_benefit_is_not_nonpaying_role(self):
        gate = JobGate(_Resolver(source(
            "This is a full-time remote role in the United States. Benefits include paid and unpaid parental leave."
        )))
        self.assertEqual(gate.evaluate(job()).state, GateState.PASS)

    def test_weekly_office_requirement_overrides_remote(self):
        gate = JobGate(_Resolver(source(
            "This is a full-time remote role in the United States. Employees must work in our office three days per week."
        )))
        decision = gate.evaluate(job())
        self.assertEqual(decision.state, GateState.REJECT)
        self.assertEqual(str(decision.primary_reason.value), "REJECT_ONSITE_REQUIRED")

    def test_foreign_only_scope_rejects(self):
        gate = JobGate(_Resolver(source(
            "This is a full-time fully remote position. Open only to candidates based in EMEA."
        , location="TELECOMMUTE | EMEA")))
        decision = gate.evaluate(job())
        self.assertEqual(decision.state, GateState.REJECT)
        self.assertEqual(str(decision.primary_reason.value), "REJECT_NON_US_SCOPE")

    def test_unknown_employment_abstains(self):
        gate = JobGate(_Resolver(source(
            "This role is remote anywhere in the United States.", employment_type=""
        )))
        self.assertEqual(gate.evaluate(job()).state, GateState.UNVERIFIED)

    def test_aggregator_unresolved_cannot_pass(self):
        unresolved = ResolvedJobSource(state="SOURCE_UNRESOLVED", official=False)
        decision = JobGate(_Resolver(unresolved)).evaluate(job())
        self.assertEqual(decision.state, GateState.UNVERIFIED)

    def test_material_title_difference_is_detected(self):
        self.assertTrue(title_materially_differs("Corporate FP&A Analyst", "Corporate FP&A Lead"))
        self.assertFalse(title_materially_differs("Staff Accountant - Remote US", "Staff Accountant"))


if __name__ == "__main__":
    unittest.main()
