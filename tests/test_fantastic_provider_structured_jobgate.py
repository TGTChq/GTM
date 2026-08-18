"""Commit B: genuine Fantastic Direct API records earn ACTIVE_PROVIDER_STRUCTURED
review eligibility at the JobGate (offline, fetch=False) even when the Direct API
list description is shorter than the aggregator 700-char bar -- because Fantastic
is a trusted structured provider. The relaxation is scoped STRICTLY to genuine
Fantastic Direct API provenance (`_fantastic_internal_id`); every other provider
keeps the full bar, and the outcome is never OFFICIAL_SOURCE.

Root cause it fixes: production Airtable (2026-08-18) showed 648 Apollo-verified
LinkedIn/Fantastic contacts stuck at UNVERIFIED with Decision Reason
`UNVERIFIED_OFFICIAL_SOURCE` -- the JobGate, not Hunter/Apollo.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import config
from decision_types import GateState
from fantastic_jobs_adapter import map_record
from job_filter import annotate_pre_enrichment_assessment
from job_gate import JobGate

_SHORT_DESC = "We are hiring a full-time VP of Information Technology in New York, United States. " * 3  # ~250 chars
_LONG_DESC = "We are hiring a full-time VP of Information Technology in New York, United States. " * 12  # >700 chars


def _fantastic_job(desc=_SHORT_DESC, job_id="123456789", source="linkedin"):
    raw = {
        "id": job_id, "title": "VP of Information Technology",
        "organization": "Northwind Traders",
        "org_linkedin_website": "northwind.com", "domain_derived": "northwind.com",
        "url": f"https://www.linkedin.com/jobs/view/{job_id}",
        "source": source, "source_type": "jobboard",
        "employment_type": ["FULL_TIME"], "date_posted": "2026-08-17T12:00:00Z",
        "countries_derived": ["United States"], "locations_derived": ["New York, NY, US"],
        "location_type": "onsite", "description_text": desc, "org_linkedin_headcount": 240,
    }
    job, reason = map_record(raw, "fantastic_linkedin")
    assert reason == "", reason
    annotate_pre_enrichment_assessment(job)
    return job


class FantasticProviderStructuredJobGateTests(unittest.TestCase):
    def _resolve(self, job):
        return JobGate().evaluate(job, fetch=False)

    def test_short_description_fantastic_record_now_passes_provider_structured(self):
        job = _fantastic_job(_SHORT_DESC)
        self.assertLess(len(job["job_description"]), 700)
        d = self._resolve(job)
        self.assertEqual(d.state, GateState.PASS)
        src = (d.metadata or {}).get("source") or {}
        self.assertEqual(src.get("state"), "ACTIVE_PROVIDER_STRUCTURED")
        self.assertTrue(src.get("corroborated"))
        # Never claims first-party OFFICIAL identity; provenance preserved.
        self.assertFalse(src.get("official"))
        notes = src.get("notes") or []
        self.assertIn("fantastic_direct_api", notes)
        self.assertIn("approved_revalidation_required", notes)
        self.assertTrue(any(str(n).startswith("fantastic_job_id:") for n in notes))
        # Original source URL preserved (the LinkedIn posting), not rewritten.
        self.assertIn("linkedin.com/jobs/view", str(src.get("source_url")))

    def test_long_description_fantastic_record_still_passes(self):
        d = self._resolve(_fantastic_job(_LONG_DESC))
        self.assertEqual(d.state, GateState.PASS)

    def test_relaxation_disabled_reverts_to_strict_bar(self):
        with patch.object(config, "JOB_SOURCE_FANTASTIC_PROVIDER_STRUCTURED_ENABLED", False):
            d = self._resolve(_fantastic_job(_SHORT_DESC))
        self.assertEqual(d.state, GateState.UNVERIFIED)
        self.assertIn("OFFICIAL_SOURCE", str(d.primary_reason))

    def test_below_fantastic_floor_still_blocked(self):
        # An essentially empty description stays blocked even for Fantastic.
        with patch.object(config, "JOB_SOURCE_FANTASTIC_PROVIDER_STRUCTURED_MIN_DESCRIPTION_CHARS", 120):
            d = self._resolve(_fantastic_job("Lead IT."))
        self.assertEqual(d.state, GateState.UNVERIFIED)

    def test_non_fantastic_short_description_record_is_not_relaxed(self):
        # A non-Fantastic structured record (no _fantastic_internal_id) with the
        # same short description must STILL fail -- free feeds/JSearch/adzuna/
        # scraped LinkedIn keep the full 700-char bar.
        job = _fantastic_job(_SHORT_DESC)
        job.pop("_fantastic_internal_id", None)          # strip Fantastic provenance
        job["_acquisition_source"] = "free_feeds"
        d = self._resolve(job)
        self.assertEqual(d.state, GateState.UNVERIFIED)
        self.assertIn("OFFICIAL_SOURCE", str(d.primary_reason))

    def test_fantastic_record_failing_fulltime_prefilter_still_blocked(self):
        # Relaxation touches ONLY description length; the signed full-time/US
        # prefilters still gate admission. A part-time record is not admitted.
        raw = {
            "id": "999", "title": "VP of Information Technology",
            "organization": "Northwind Traders", "domain_derived": "northwind.com",
            "url": "https://www.linkedin.com/jobs/view/999", "source": "linkedin",
            "employment_type": ["PART_TIME"], "date_posted": "2026-08-17T12:00:00Z",
            "countries_derived": ["United States"], "locations_derived": ["New York, NY, US"],
            "location_type": "onsite", "description_text": _SHORT_DESC,
        }
        job, _ = map_record(raw, "fantastic_linkedin")
        annotate_pre_enrichment_assessment(job)
        d = self._resolve(job)
        self.assertNotEqual(d.state, GateState.PASS)


if __name__ == "__main__":
    unittest.main()
