"""Per-source discard attribution, dual uniqueness, and title coverage.

Production tallies discards globally, so "we lost 1,400 rows" can never be
traced to a source (multi_source_acquisition.py:987-1009 writes into one flat
``stats`` dict). These tests pin the harness's per-source replay of that loop:
same order, same semantics, same removal set, but attributable.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from retrieval_measurement.accounting import (
    attribute_discards,
    dual_uniqueness,
    normalize_apply_url,
    posting_identity,
    posting_identity_uniqueness,
    posted_at_bounds,
    production_equivalent_uniqueness,
    split_counters,
    title_coverage,
)
from retrieval_measurement.identity import ReadOnlySeenSnapshot
from retrieval_measurement.schema import ANNOTATING_COUNTERS, REMOVING_DISCARD_REASONS

DESCRIPTION = (
    "We are hiring for this role to support continued growth. You will own delivery "
    "end to end, partner with product, and help us scale the team."
)


def job(job_id, company, title, *, source="himalayas", url=None, posted="2026-07-28T09:00:00Z"):
    link = url if url is not None else (f"https://example.com/{job_id}" if job_id else "")
    return {
        "job_id": job_id,
        "job_title": title,
        "employer_name": company,
        "employer_website": "",
        "job_apply_link": link,
        "canonical_source_url": link,
        "job_description": DESCRIPTION,
        "job_location": "Remote - United States",
        "job_country": "US",
        "job_posted_at_datetime_utc": posted,
        "_acquisition_source": source,
    }


class DiscardAttributionTests(unittest.TestCase):
    def test_only_three_reasons_remove_a_record(self):
        jobs = [
            job("", "Acme", "Software Engineer"),                       # missing_job_id
            job("h:2", "Acme", "VP of Engineering"),             # excluded_by_seniority
            job("h:3", "Acme", "Software Engineer"),                    # kept
        ]
        kept, records, counters = attribute_discards(jobs, None)
        self.assertEqual(len(kept), 1)
        bucket = counters["himalayas"]
        self.assertEqual(bucket["missing_job_id"], 1)
        self.assertEqual(bucket["excluded_by_seniority"], 1)
        self.assertEqual(bucket["previously_seen"], 0)

        removing = {record.reason for record in records if record.removes_record}
        self.assertEqual(removing, set(REMOVING_DISCARD_REASONS))
        annotating = {record.reason for record in records if not record.removes_record}
        self.assertEqual(annotating, set(ANNOTATING_COUNTERS))

    def test_records_are_attributed_to_their_own_source(self):
        jobs = [
            job("h:1", "Acme", "Software Engineer", source="himalayas"),
            job("", "Acme", "Software Engineer", source="jobicy"),
            job("r:1", "Beta", "Data Engineer", source="remotive"),
        ]
        kept, _records, counters = attribute_discards(
            jobs, None, source_of=lambda item: str(item.get("_acquisition_source"))
        )
        self.assertEqual(len(kept), 2)
        self.assertEqual(counters["jobicy"]["missing_job_id"], 1)
        self.assertEqual(counters["himalayas"]["missing_job_id"], 0)
        self.assertNotIn("adzuna", counters)

    def test_previously_seen_is_checked_before_the_seniority_exclusion(self):
        """Order matters: a record that is both already-seen and excluded lands
        in previously_seen, exactly as production does at :992 before :995.
        Changing the order would make these counts non-comparable."""
        today = datetime.now().strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            path.write_text(json.dumps({
                "retention_days": 30, "job_ids": {"h:9": today}, "dedup_keys": {},
            }), encoding="utf-8")
            snapshot = ReadOnlySeenSnapshot.load(path)

        _kept, _records, counters = attribute_discards(
            [job("h:9", "Acme", "VP of Engineering")], snapshot
        )
        self.assertEqual(counters["himalayas"]["previously_seen"], 1)
        self.assertEqual(counters["himalayas"]["excluded_by_seniority"], 0)

    def test_role_rejected_records_are_counted_but_kept(self):
        """multi_source_acquisition.py:1000 increments role_reject and :1009
        appends the record anyway."""
        jobs = [job("h:1", "Acme", "Underwater Basket Weaver")]
        kept, _records, counters = attribute_discards(jobs, None)
        self.assertEqual(len(kept), 1)
        bucket = counters["himalayas"]
        self.assertEqual(
            bucket["role_accept"] + bucket["role_review"] + bucket["role_reject"], 1
        )
        removals, annotations = split_counters(bucket)
        self.assertEqual(sum(removals.values()), 0)
        self.assertEqual(sum(annotations[key] for key in ("role_accept", "role_review", "role_reject")), 1)

    def test_classification_does_not_mutate_the_caller_input(self):
        original = job("h:1", "Acme", "Software Engineer")
        snapshot_of_input = dict(original)
        attribute_discards([original], None)
        self.assertEqual(original, snapshot_of_input)


class PostingIdentityTests(unittest.TestCase):
    def test_identity_ladder_falls_back_in_order(self):
        strength, key = posting_identity(job("h:1", "Acme", "Engineer"))
        self.assertEqual(strength, "provider_job_id")
        self.assertEqual(key, "h:1")

        strength, key = posting_identity(job("", "Acme", "Engineer", url="https://Acme.com/Jobs/7/"))
        self.assertEqual(strength, "apply_url")
        self.assertEqual(key, "https://acme.com/Jobs/7")

        strength, key = posting_identity(job("", "Acme", "Engineer", url=""))
        self.assertEqual(strength, "content_digest")
        self.assertEqual(len(key), 32)

    def test_url_normalization_keeps_the_query_string(self):
        """ATS posting ids live in the query (?gh_jid=...). Stripping it would
        merge distinct postings and understate inventory."""
        first = normalize_apply_url("https://boards.greenhouse.io/acme?gh_jid=1")
        second = normalize_apply_url("https://boards.greenhouse.io/acme?gh_jid=2")
        self.assertNotEqual(first, second)
        self.assertEqual(
            normalize_apply_url("HTTPS://Boards.Greenhouse.IO/acme/"),
            "https://boards.greenhouse.io/acme",
        )

    def test_identity_strength_histogram_is_reported(self):
        jobs = [
            job("h:1", "Acme", "Engineer"),
            job("", "Acme", "Engineer", url="https://acme.com/2"),
            job("", "Beta", "Engineer", url=""),
        ]
        _unique, _dups, histogram = posting_identity_uniqueness(jobs)
        self.assertEqual(histogram, {"provider_job_id": 1, "apply_url": 1, "content_digest": 1})


class DualUniquenessTests(unittest.TestCase):
    def test_distinct_postings_collapse_under_production_dedupe(self):
        """Three real openings, one company, same title. Posting identity says
        3; production's (company, title) dedupe says 1. Both are correct for
        their purpose, which is exactly why both are reported."""
        jobs = [
            job("h:1", "Northwind Systems", "Senior Software Engineer"),
            job("h:2", "Northwind Systems", "Senior Software Engineer"),
            job("h:3", "Northwind Systems", "Senior Software Engineer"),
        ]
        uniqueness = dual_uniqueness(jobs)
        self.assertEqual(uniqueness.returned_total, 3)
        self.assertEqual(uniqueness.unique_posting_identity, 3)
        self.assertEqual(uniqueness.unique_production_equivalent, 1)
        self.assertEqual(uniqueness.collapse_delta, 2)

    def test_repeated_provider_ids_are_duplicates_under_both_definitions(self):
        jobs = [job("h:1", "Acme", "Engineer"), job("h:1", "Acme", "Engineer")]
        uniqueness = dual_uniqueness(jobs)
        self.assertEqual(uniqueness.unique_posting_identity, 1)
        self.assertEqual(uniqueness.duplicates_posting_identity, 1)
        self.assertEqual(uniqueness.unique_production_equivalent, 1)

    def test_production_dedupe_receives_copies_only(self):
        """_dedupe writes _discovery_sources into the dicts it is given. The
        harness measures production; it must not alter the records it measured."""
        jobs = [job("h:1", "Acme", "Engineer"), job("h:2", "Acme", "Engineer")]
        before = [dict(item) for item in jobs]
        production_equivalent_uniqueness(jobs)
        self.assertEqual(jobs, before)
        self.assertNotIn("_discovery_sources", jobs[0])


class CoverageTests(unittest.TestCase):
    def test_titles_with_zero_results_are_retained(self):
        jobs = [dict(job("h:1", "Acme", "Software Engineer"), _matched_role="Software Engineer")]
        coverage = title_coverage(jobs, ["Software Engineer", "Data Engineer"])
        by_title = {entry.title: entry for entry in coverage}
        self.assertEqual(by_title["Software Engineer"].matched_records, 1)
        self.assertEqual(by_title["Software Engineer"].sources, ["himalayas"])
        self.assertEqual(by_title["Data Engineer"].matched_records, 0)

    def test_posted_at_bounds_are_captured_for_the_weekly_protocol(self):
        jobs = [
            job("h:1", "Acme", "Engineer", posted="2026-07-20T00:00:00Z"),
            job("h:2", "Acme", "Engineer", posted="2026-07-28T00:00:00Z"),
        ]
        first, last = posted_at_bounds(jobs)
        self.assertEqual(first, "2026-07-20T00:00:00Z")
        self.assertEqual(last, "2026-07-28T00:00:00Z")
        self.assertEqual(posted_at_bounds([]), ("", ""))


if __name__ == "__main__":
    unittest.main()
