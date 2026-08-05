"""Artifacts, retention safety, import isolation, and the CLI end to end.

Retention is the part worth being paranoid about. The production pipeline has
no eviction anywhere -- ``source_cache.py:38`` treats an expired entry as a miss
and never unlinks it, and the only ``unlink`` calls in the codebase belong to
the checkpoint and the lock -- and the volume filled up. The response to that is
not a measurement harness that deletes things by default. So: report-only
unless asked, confined to the harness artifact root, and refusing symlinks and
anything resolving into a production directory.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import run_retrieval_measurement
from retrieval_measurement import DeliveryImportBlocked, DeliveryImportGuard
from retrieval_measurement.artifacts import (
    ARTIFACT_DIRNAME,
    RetentionRefused,
    assert_deletable,
    assert_no_typical_week_claim,
    atomic_write_json,
    evaluate_retention,
    render_report,
    run_dir,
    write_run_artifacts,
)
from retrieval_measurement.schema import CLAIM_BOUNDARY

FIXTURE = Path(__file__).parent / "fixtures" / "retrieval_measurement" / "sources.json"


def make_run(root: Path, run_id: str, *, size: int = 512, age_days: float = 0.0) -> Path:
    directory = root / ARTIFACT_DIRNAME / run_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "coverage_summary.json").write_text("x" * size, encoding="utf-8")
    if age_days:
        stamp = (datetime.now(timezone.utc) - timedelta(days=age_days)).timestamp()
        os.utime(directory, (stamp, stamp))
    return directory


class AtomicWriteTests(unittest.TestCase):
    def test_write_is_atomic_and_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "artifact.json"
            atomic_write_json(target, {"a": 1})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"a": 1})
            self.assertEqual([item.name for item in target.parent.iterdir()], ["artifact.json"])


class RetentionTests(unittest.TestCase):
    def test_reports_without_deleting_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(25):
                make_run(root, f"run-{index:03d}")
            report = evaluate_retention(root)
            self.assertFalse(report.applied)
            self.assertEqual(report.total_runs, 25)
            self.assertEqual(len(report.candidates), 5)
            self.assertEqual(report.deleted, [])
            self.assertEqual(len(list((root / ARTIFACT_DIRNAME).iterdir())), 25)

    def test_age_bound_is_reported_with_its_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_run(root, "fresh")
            make_run(root, "ancient", age_days=45)
            report = evaluate_retention(root)
            self.assertEqual([item["run_id"] for item in report.candidates], ["ancient"])
            self.assertIn("older than 30 days", report.candidates[0]["reasons"][0])

    def test_size_bound_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_run(root, "run-a", size=4096)
            make_run(root, "run-b", size=4096)
            report = evaluate_retention(root, max_total_bytes=5000)
            self.assertTrue(report.candidates)
            self.assertIn("exceed", report.candidates[0]["reasons"][0])

    def test_apply_deletes_only_the_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(22):
                make_run(root, f"run-{index:03d}")
            report = evaluate_retention(root, apply=True)
            self.assertTrue(report.applied)
            self.assertEqual(len(report.deleted), 2)
            remaining = sorted(item.name for item in (root / ARTIFACT_DIRNAME).iterdir())
            self.assertEqual(len(remaining), 20)
            self.assertNotIn("run-000", remaining)
            self.assertIn("run-021", remaining)

    def test_nothing_is_deleted_when_within_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_run(root, "only-run")
            report = evaluate_retention(root, apply=True)
            self.assertEqual(report.deleted, [])
            self.assertTrue((root / ARTIFACT_DIRNAME / "only-run").is_dir())

    def test_missing_artifact_root_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_retention(Path(tmp) / "never-created")
            self.assertEqual(report.total_runs, 0)
            self.assertEqual(report.candidates, [])


class RetentionSafetyTests(unittest.TestCase):
    def test_refuses_a_path_outside_the_artifact_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "somewhere-else"
            outside.mkdir()
            with self.assertRaises(RetentionRefused):
                assert_deletable(outside, root / ARTIFACT_DIRNAME)

    def test_refuses_production_directories_under_every_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ARTIFACT_DIRNAME
            for fragment in ("raw", "filtered", "enriched", "state", "evidence"):
                candidate = root / "data" / fragment
                candidate.mkdir(parents=True)
                with self.assertRaises(RetentionRefused, msg=fragment):
                    assert_deletable(candidate, root)

    def test_refuses_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ARTIFACT_DIRNAME
            real = root / "real-run"
            real.mkdir(parents=True)
            link = root / "link-run"
            try:
                link.symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation requires privileges on this platform")
            with self.assertRaises(RetentionRefused):
                assert_deletable(link, root)

    def test_a_refused_candidate_is_reported_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_run(root, "run-000", age_days=45)
            report = evaluate_retention(root, apply=True, max_age_days=30)
            self.assertEqual(len(report.deleted) + len(report.refused), 1)


class DeliveryIsolationTests(unittest.TestCase):
    def test_importing_a_delivery_module_is_blocked_during_a_run(self):
        names = ("apollo_client", "hunter_client", "airtable_client", "instantly_client")
        saved = {name: sys.modules.pop(name, None) for name in names}
        try:
            with DeliveryImportGuard():
                with self.assertRaises(DeliveryImportBlocked):
                    __import__("apollo_client")
                with self.assertRaises(DeliveryImportBlocked):
                    __import__("airtable_client")
        finally:
            # Restore exactly what was there. Leaving a module unimported would
            # make unrelated tests depend on execution order.
            for name, module in saved.items():
                if module is not None:
                    sys.modules[name] = module
                else:
                    sys.modules.pop(name, None)

    def test_the_guard_is_removed_on_exit(self):
        before = len(sys.meta_path)
        with DeliveryImportGuard():
            self.assertEqual(len(sys.meta_path), before + 1)
        self.assertEqual(len(sys.meta_path), before)

    def test_acquisition_modules_remain_importable_under_the_guard(self):
        with DeliveryImportGuard():
            import free_job_sources  # noqa: F401
            import multi_source_acquisition  # noqa: F401
            import ats_board_registry  # noqa: F401
            import adzuna_client  # noqa: F401


class ReportTests(unittest.TestCase):
    def _summary(self):
        return {
            "run_id": "20260803T120000Z-abcdef01",
            "mode": "fixture",
            "run_baseline_posting_identity": {
                "gross_returned": 100, "unique_in_run": 90,
                "previously_seen": 10, "incremental_new": 80,
            },
            "run_baseline_production_equivalent": {
                "gross_returned": 100, "unique_in_run": 55,
                "previously_seen": 5, "incremental_new": 50,
            },
            "per_source": [{
                "source": "himalayas", "canonical_records": 100, "kept_after_removals": 95,
                "uniqueness": {"unique_posting_identity": 90, "unique_production_equivalent": 55},
                "denominator": {"value": 480}, "capture_rate": 0.208,
                "truncation": [{"kind": "configured_cap", "detected": True}],
            }],
            "sources_with_denominator": ["himalayas"],
            "sources_without_denominator": ["jobicy"],
            "total_market_estimate": None,
            "total_market_estimate_reason": "Not derivable.",
            "reconciliation": {"passed": True, "checks": [{"passed": True}]},
            "title_coverage": [{"title": "Data Engineer", "matched_records": 0}],
            "notes": [],
        }

    def test_report_leads_with_the_claim_boundary(self):
        report = render_report(self._summary(), {"git_commit": "abc", "python_version": "3.12.10"})
        self.assertIn(CLAIM_BOUNDARY, report)
        self.assertIn("Milestone 1 candidate", report)
        self.assertLess(report.index("Claim boundary"), report.index("Run totals"))

    def test_report_shows_both_baselines_and_refuses_a_market_total(self):
        report = render_report(self._summary(), {})
        self.assertIn("posting identity", report)
        self.assertIn("production equivalent", report)
        self.assertIn("No total-US-market capture rate is reported", report)

    def test_report_includes_the_typical_week_protocol(self):
        report = render_report(self._summary(), {})
        self.assertIn("Measuring a typical week (not yet done)", report)
        self.assertIn("Cross-day deduplication", report)
        self.assertIn("right-censoring", report)

    def test_assertive_typical_week_claims_are_refused(self):
        assert_no_typical_week_claim(render_report(self._summary(), {}))
        with self.assertRaises(RuntimeError):
            assert_no_typical_week_claim("In a typical week we retrieve 1,200 postings.")

    def test_titles_with_zero_results_are_surfaced(self):
        report = render_report(self._summary(), {})
        self.assertIn("titles with zero retrieved postings: 1", report)


class ArtifactWritingTests(unittest.TestCase):
    def test_every_artifact_is_written_and_byte_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = write_run_artifacts(
                tmp, "run-1",
                manifest={"run_id": "run-1", "git_commit": "abc"},
                summary=ReportTests()._summary(),
                source_metrics=[{"source": "himalayas"}],
                request_ledger=[{"sequence": 1}],
                posting_lineage=[{"run_id": "run-1"}],
            )
            directory = run_dir(tmp, "run-1")
            for name in ("run_manifest.json", "coverage_summary.json", "source_metrics.json",
                         "request_ledger.jsonl.gz", "posting_lineage.jsonl.gz",
                         "coverage_summary.md"):
                self.assertTrue((directory / name).is_file(), name)
            self.assertGreater(written["_total_bytes"], 0)


class CommandLineTests(unittest.TestCase):
    def test_fixture_mode_runs_end_to_end_and_reconciles(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = run_retrieval_measurement.main([
                "--mode", "fixture",
                "--fixture", str(FIXTURE),
                "--artifact-root", tmp,
                "--sources", "himalayas,jobicy,remotive,remoteok,weworkremotely",
            ])
            self.assertEqual(code, 0, "reconciliation or parity failed in fixture mode")
            runs = list((Path(tmp) / ARTIFACT_DIRNAME).iterdir())
            self.assertEqual(len(runs), 1)
            summary = json.loads((runs[0] / "coverage_summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["reconciliation"]["passed"])
            self.assertIsNone(summary["total_market_estimate"])
            self.assertGreater(summary["run_baseline_posting_identity"]["gross_returned"], 0)
            # Both baselines present; no snapshot supplied, so previously_seen is unknown.
            self.assertIsNone(summary["run_baseline_posting_identity"]["previously_seen"])
            self.assertTrue(all(check["passed"] for check in summary["parity"]))
            manifest = json.loads((runs[0] / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "complete")
            self.assertFalse(manifest["seen_snapshot"]["write_capable"])

    def test_run_manifest_never_contains_a_secret_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_retrieval_measurement.main([
                "--mode", "fixture", "--fixture", str(FIXTURE),
                "--artifact-root", tmp, "--sources", "himalayas",
            ])
            runs = list((Path(tmp) / ARTIFACT_DIRNAME).iterdir())
            manifest = json.loads((runs[0] / "run_manifest.json").read_text(encoding="utf-8"))
            for entry in manifest["effective_config"]:
                if entry["redacted"]:
                    self.assertEqual(set(entry["value"].keys()), {"configured"})

    def test_fixture_mode_with_a_snapshot_reports_both_baselines(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "snapshot.json"
            today = datetime.now().strftime("%Y-%m-%d")
            snapshot.write_text(json.dumps({
                "retention_days": 30,
                "job_ids": {"himalayas:hm-1001": today},
                "dedup_keys": {},
            }), encoding="utf-8")
            code = run_retrieval_measurement.main([
                "--mode", "fixture", "--fixture", str(FIXTURE),
                "--artifact-root", tmp, "--sources", "himalayas",
                "--seen-snapshot", str(snapshot),
            ])
            self.assertEqual(code, 0)
            runs = list((Path(tmp) / ARTIFACT_DIRNAME).iterdir())
            summary = json.loads((runs[0] / "coverage_summary.json").read_text(encoding="utf-8"))
            posting = summary["run_baseline_posting_identity"]
            self.assertEqual(posting["previously_seen"], 1)
            self.assertEqual(posting["incremental_new"], posting["unique_in_run"] - 1)
            self.assertTrue(posting["snapshot_available"])
            # The snapshot file is left exactly as it was.
            self.assertIn("himalayas:hm-1001", json.loads(snapshot.read_text(encoding="utf-8"))["job_ids"])

    def test_retention_report_flag_deletes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(23):
                make_run(Path(tmp), f"run-{index:03d}")
            code = run_retrieval_measurement.main(["--retention-report", "--artifact-root", tmp])
            self.assertEqual(code, 0)
            self.assertEqual(len(list((Path(tmp) / ARTIFACT_DIRNAME).iterdir())), 23)

    def test_denominators_are_captured_from_the_fixture_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_retrieval_measurement.main([
                "--mode", "fixture", "--fixture", str(FIXTURE),
                "--artifact-root", tmp, "--sources", "himalayas",
            ])
            runs = list((Path(tmp) / ARTIFACT_DIRNAME).iterdir())
            metrics = json.loads((runs[0] / "source_metrics.json").read_text(encoding="utf-8"))
            himalayas = next(item for item in metrics if item["source"] == "himalayas")
            self.assertEqual(himalayas["denominator"]["value"], 480)
            self.assertEqual(himalayas["denominator"]["field_name"], "totalCount")
            self.assertIsNotNone(himalayas["capture_rate"])
            self.assertIn("totalCount", himalayas["capture_rate_basis"])


if __name__ == "__main__":
    unittest.main()
