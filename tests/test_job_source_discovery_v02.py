from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from job_source_resolver import JobSourceResolver


class _Response:
    def __init__(self, url, status, text):
        self.url = url
        self.status_code = status
        self.text = text
        self.headers = {"content-type": "text/html"}


class _Session:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.mapping.get(url, _Response(url, 404, "not found"))


class JobSourceDiscoveryV02Tests(unittest.TestCase):
    def job(self):
        return {
            "job_id": "123",
            "job_title": "Staff Accountant",
            "employer_name": "Example Corp",
            "employer_website": "https://example.com",
        }

    def test_discovers_exact_company_job_link_from_bounded_careers_pages(self):
        careers = '<a href="/careers/staff-accountant-123">Staff Accountant</a>'
        posting = {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "Staff Accountant",
            "hiringOrganization": {"name": "Example Corp"},
            "description": "Full-time remote role in the United States. Responsibilities include monthly close and reconciliations.",
            "employmentType": "FULL_TIME",
            "jobLocationType": "TELECOMMUTE",
            "applicantLocationRequirements": {"@type": "Country", "name": "United States"},
            "identifier": "123",
        }
        job_html = f'<script type="application/ld+json">{json.dumps(posting)}</script>'
        session = _Session({
            "https://example.com/careers": _Response("https://example.com/careers", 200, careers),
            "https://example.com/jobs": _Response("https://example.com/jobs", 404, ""),
            "https://example.com/careers/jobs": _Response("https://example.com/careers/jobs", 404, ""),
            "https://example.com/careers/staff-accountant-123": _Response("https://example.com/careers/staff-accountant-123", 200, job_html),
        })
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "SOURCE_CACHE_DIR", temp):
            result = JobSourceResolver(session).resolve(self.job(), fetch=True)
        self.assertEqual(result.state, "ACTIVE_VERIFIED")
        self.assertEqual(result.canonical_title, "Staff Accountant")
        self.assertEqual(result.source_url, "https://example.com/careers/staff-accountant-123")
        self.assertTrue(result.official)

    def test_company_homepage_is_not_mistaken_for_job_posting(self):
        homepage = "Example Corp builds accounting software. Careers Apply Responsibilities Qualifications " + ("company text " * 80)
        session = _Session({
            "https://example.com/": _Response("https://example.com/", 200, homepage),
            "https://example.com/careers": _Response("https://example.com/careers", 404, ""),
            "https://example.com/jobs": _Response("https://example.com/jobs", 404, ""),
            "https://example.com/careers/jobs": _Response("https://example.com/careers/jobs", 404, ""),
        })
        job = self.job()
        job["job_apply_link"] = "https://example.com/"
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "SOURCE_CACHE_DIR", temp):
            result = JobSourceResolver(session).resolve(job, fetch=True)
        self.assertEqual(result.state, "SOURCE_UNRESOLVED")


if __name__ == "__main__":
    unittest.main()
