import unittest
from job_filter import assess_employment_quality


class NonActiveContextV02Tests(unittest.TestCase):
    def test_incidental_talent_pool_phrase_does_not_reject_active_job(self):
        job = {
            "job_title": "Recruiter",
            "job_description": "This is a full-time role. You will build a strong talent pool for upcoming hiring needs.",
            "job_employment_type": "Full-time",
        }
        result = assess_employment_quality(job)
        self.assertTrue(result.eligible)
        self.assertNotEqual(result.classification, "non_active")

    def test_explicit_future_application_is_nonactive(self):
        job = {
            "job_title": "General Application",
            "job_description": "This application is for future opportunities and is not an active opening.",
            "job_employment_type": "Full-time",
        }
        result = assess_employment_quality(job)
        self.assertEqual(result.classification, "non_active")


if __name__ == "__main__":
    unittest.main()
