"""Reconciliation identities and the dual baseline.

The identities are the harness's whole claim to being a measurement rather than
an estimate: every record a provider returned is accounted for at every stage
boundary, or the run is marked incomplete and exits non-zero.

The subtle one is ``canonical_records_accounted``. Production's classification
loop has five counters but only three of them remove a record
(multi_source_acquisition.py:989-997); ``role_reject`` and ``prefilter_rejected``
annotate records that are still appended at :1009. Subtracting those would make
the funnel appear to lose records it never lost -- so there is an explicit test
that a run dominated by role rejections still reconciles.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from retrieval_measurement.accounting import baselines, dual_uniqueness
from retrieval_measurement.identity import ReadOnlySeenSnapshot
from retrieval_measurement.schema import (
    SourceMetrics,
    UniquenessMetrics,
    reconcile_run,
    reconcile_source,
)


def job(job_id: str, company: str, title: str, *, url: str = "", source: str = "himalayas") -> dict:
    return {
        "job_id": job_id,
        "job_title": title,
        "employer_name": company,
        "employer_website": "",
        "job_apply_link": url or f"https://example.com/{job_id}",
        "canonical_source_url": url or f"https://example.com/{job_id}",
        "job_description": "A sufficiently detailed description of the role and its responsibilities.",
        "job_location": "Remote - United States",
        "job_country": "US",
        "_acquisition_source": source,
    }


def metrics(**overrides) -> SourceMetrics:
    base = dict(
        source="himalayas",
        lane="free_feed",
        provider_rows=10,
        canonical_records=10,
        adapter_dropped_or_capped=0,
        kept_after_removals=10,
        removals={"missing_job_id": 0, "previously_seen": 0, "excluded_by_seniority": 0},
        annotations={"role_accept": 0, "role_review": 0, "role_reject": 0,
                     "prefilter_viable": 0, "prefilter_rejected": 0},
        uniqueness=UniquenessMetrics(
            returned_total=10,
            unique_posting_identity=10,
            duplicates_posting_identity=0,
            unique_production_equivalent=10,
            duplicates_production_equivalent=0,
        ).to_dict(),
    )
    base.update(overrides)
    return SourceMetrics(**base)


class SourceIdentityTests(unittest.TestCase):
    def test_balanced_source_passes_every_identity(self):
        checks = reconcile_source(metrics())
        self.assertTrue(all(check.passed for check in checks), [c.to_dict() for c in checks if not c.passed])

    def test_adapter_drop_must_be_declared(self):
        # Provider returned 10 rows, adapter emitted 8, and the gap is declared.
        good = metrics(provider_rows=10, canonical_records=8, adapter_dropped_or_capped=2,
                       kept_after_removals=8,
                       uniqueness=UniquenessMetrics(
                           returned_total=8, unique_posting_identity=8,
                           unique_production_equivalent=8).to_dict())
        self.assertTrue(all(check.passed for check in reconcile_source(good)))

        # Same gap, undeclared: the identity must catch it.
        bad = metrics(provider_rows=10, canonical_records=8, adapter_dropped_or_capped=0,
                      kept_after_removals=8,
                      uniqueness=UniquenessMetrics(
                          returned_total=8, unique_posting_identity=8,
                          unique_production_equivalent=8).to_dict())
        failed = [check for check in reconcile_source(bad) if not check.passed]
        self.assertEqual([check.name for check in failed], ["provider_rows_accounted"])
        self.assertEqual(failed[0].delta, 2)
        self.assertEqual(failed[0].stage, "adapter")

    def test_removing_discards_are_subtracted(self):
        good = metrics(
            canonical_records=10,
            kept_after_removals=6,
            removals={"missing_job_id": 1, "previously_seen": 2, "excluded_by_seniority": 1},
            uniqueness=UniquenessMetrics(
                returned_total=6, unique_posting_identity=6,
                unique_production_equivalent=6).to_dict(),
        )
        self.assertTrue(all(check.passed for check in reconcile_source(good)))

    def test_annotating_counters_are_never_subtracted(self):
        """A run where almost everything is role-rejected still reconciles,
        because role_reject does not remove a record (see :1000 and :1009)."""
        record = metrics(
            canonical_records=10,
            kept_after_removals=10,
            annotations={"role_accept": 1, "role_review": 0, "role_reject": 9,
                         "prefilter_viable": 1, "prefilter_rejected": 0},
        )
        checks = reconcile_source(record)
        self.assertTrue(all(check.passed for check in checks))

    def test_uniqueness_split_is_checked_for_both_definitions(self):
        record = metrics(
            kept_after_removals=10,
            uniqueness=UniquenessMetrics(
                returned_total=10,
                unique_posting_identity=10,
                duplicates_posting_identity=0,
                unique_production_equivalent=4,   # collapsed
                duplicates_production_equivalent=1,  # deliberately wrong: 4+1 != 10
            ).to_dict(),
        )
        failed = [check.name for check in reconcile_source(record) if not check.passed]
        self.assertEqual(failed, ["production_equivalent_split"])


class RunIdentityTests(unittest.TestCase):
    def test_run_totals_must_equal_the_sum_of_sources(self):
        sources = [metrics(source="himalayas"), metrics(source="jobicy")]
        uniqueness = UniquenessMetrics(
            returned_total=20, unique_posting_identity=20,
            duplicates_posting_identity=0,
            unique_production_equivalent=20, duplicates_production_equivalent=0,
        )
        result = reconcile_run(sources, uniqueness, run_kept=20)
        self.assertTrue(result.passed, result.failed_scopes)

    def test_mismatch_names_the_responsible_scope_and_stage(self):
        sources = [metrics(source="himalayas"), metrics(source="jobicy", kept_after_removals=9)]
        uniqueness = UniquenessMetrics(
            returned_total=19, unique_posting_identity=19,
            unique_production_equivalent=19,
        )
        result = reconcile_run(sources, uniqueness, run_kept=19)
        self.assertFalse(result.passed)
        self.assertIn("jobicy", result.failed_scopes)
        failed = [check for check in result.checks if not check["passed"]]
        self.assertTrue(any(check["stage"] == "discard" for check in failed))


class DualBaselineTests(unittest.TestCase):
    def setUp(self):
        # Three distinct postings; two share company+title.
        self.jobs = [
            job("himalayas:a", "Northwind Systems", "Senior Software Engineer"),
            job("himalayas:b", "Northwind Systems", "Senior Software Engineer"),
            job("himalayas:c", "Contoso Labs", "Data Engineer"),
        ]

    def test_without_a_snapshot_previously_seen_is_unknown_not_zero(self):
        posting, production = baselines(self.jobs, None)
        self.assertEqual(posting.gross_returned, 3)
        self.assertEqual(posting.unique_in_run, 3)
        self.assertIsNone(posting.previously_seen)
        self.assertIsNone(posting.incremental_new)
        self.assertFalse(posting.snapshot_available)
        # The production-equivalent basis collapses the two same-title rows.
        self.assertEqual(production.unique_in_run, 2)

    def test_with_a_snapshot_both_baselines_are_reported(self):
        today = datetime.now().strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            path.write_text(json.dumps({
                "retention_days": 30,
                "job_ids": {"himalayas:a": today},
                "dedup_keys": {"contoso labs|data engineer": today},
            }), encoding="utf-8")
            snapshot = ReadOnlySeenSnapshot.load(path)

        posting, production = baselines(self.jobs, snapshot)
        self.assertEqual(posting.gross_returned, 3)
        self.assertEqual(posting.unique_in_run, 3)
        self.assertEqual(posting.previously_seen, 1)
        self.assertEqual(posting.incremental_new, 2)
        self.assertTrue(posting.snapshot_available)

        self.assertEqual(production.unique_in_run, 2)
        self.assertEqual(production.previously_seen, 1)
        self.assertEqual(production.incremental_new, 1)

    def test_both_bases_are_always_present_and_labelled(self):
        posting, production = baselines(self.jobs, None)
        self.assertEqual(posting.basis, "posting_identity")
        self.assertEqual(production.basis, "production_equivalent")
        self.assertEqual(posting.gross_returned, production.gross_returned)

    def test_uniqueness_split_is_internally_consistent(self):
        uniqueness = dual_uniqueness(self.jobs)
        self.assertEqual(
            uniqueness.returned_total,
            uniqueness.unique_posting_identity + uniqueness.duplicates_posting_identity,
        )
        self.assertEqual(
            uniqueness.returned_total,
            uniqueness.unique_production_equivalent + uniqueness.duplicates_production_equivalent,
        )


if __name__ == "__main__":
    unittest.main()
