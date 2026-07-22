import unittest
from unittest.mock import Mock, patch

from job_source_resolver import JobSourceResolver


class JobSourceResolutionCostTests(unittest.TestCase):
    @patch("job_source_resolver.JobSourceResolver._fetch")
    @patch("job_source_resolver.JobSourceResolver._discover_company_job_urls")
    def test_direct_official_link_still_runs_bounded_careers_discovery(self, discover, fetch):
        discover.return_value = ([], [])
        fetch.return_value = {
            "status_code": 200,
            "final_url": "https://example.com/careers/jobs/123",
            "text": "<html><body><h1>Data Analyst</h1><button>Apply now</button><p>Responsibilities and qualifications. " + "x" * 600 + "</p></body></html>",
        }
        resolver = JobSourceResolver()
        result = resolver.resolve({
            "employer_name": "Example",
            "employer_website": "https://example.com",
            "job_title": "Data Analyst",
            "job_apply_link": "https://example.com/careers/jobs/123",
        }, fetch=True)
        discover.assert_called_once()
        self.assertEqual(result.state, "ACTIVE_VERIFIED")

    @patch("job_source_resolver.JobSourceResolver._fetch")
    @patch("job_source_resolver.JobSourceResolver._discover_company_job_urls")
    def test_aggregator_only_uses_bounded_company_discovery(self, discover, fetch):
        discover.return_value = ([], [])
        resolver = JobSourceResolver()
        result = resolver.resolve({
            "employer_name": "Example",
            "employer_website": "https://example.com",
            "job_title": "Data Analyst",
            "job_apply_link": "https://www.indeed.com/viewjob?jk=123",
        }, fetch=True)
        discover.assert_called_once()
        fetch.assert_not_called()
        self.assertEqual(result.state, "SOURCE_UNRESOLVED")


if __name__ == "__main__":
    unittest.main()
