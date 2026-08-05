"""Pre-flight remediation R1-R5: lane selection, registry, containment, budget.

These tests exist because a read-only pre-flight review found that the harness,
as originally written, would have spent real requests on five public feeds and
then died on an absent ``RAPIDAPI_KEY`` before writing a single artifact -- with
no way to deselect the JSearch lane, and with ``run_daily_scrape`` implicitly
constructing the production ``SeenJobsRegistry`` on the way.

Every test below pins one of those failure modes shut. None of them makes a
network call: the live path is reached only through injected transports that
record the attempt and refuse to perform it.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import run_retrieval_measurement
from retrieval_measurement.artifacts import ARTIFACT_DIRNAME
from retrieval_measurement.drivers import run_jsearch_lane
from retrieval_measurement.identity import (
    NonWritingRegistry,
    ReadOnlySeenSnapshot,
    RegistryWriteRefused,
    credential_state,
    redact_text,
)
from retrieval_measurement.schema import LANES, CredentialStatus

FIXTURE = Path(__file__).parent / "fixtures" / "retrieval_measurement" / "sources.json"


def parse(argv):
    return run_retrieval_measurement.build_parser().parse_args(argv)


class NetworkTripwire(Exception):
    """Raised if anything tries to leave the process."""


def tripwire(*_args, **_kwargs):
    raise NetworkTripwire("a test attempted a real outbound request")


# --------------------------------------------------------------------------
# R1 -- explicit lane selection
# --------------------------------------------------------------------------


class LaneSelectionTests(unittest.TestCase):
    def test_every_lane_is_independently_selectable(self):
        for lane in LANES:
            lanes, source, error = run_retrieval_measurement.resolve_lanes(
                parse(["--mode", "live_acquisition", "--lanes", lane])
            )
            self.assertEqual(error, "")
            self.assertEqual(lanes, [lane])
            self.assertEqual(source, "explicit")

    def test_lanes_may_be_combined_and_are_returned_in_canonical_order(self):
        lanes, _source, error = run_retrieval_measurement.resolve_lanes(
            parse(["--mode", "live_acquisition", "--lanes", "ats,jsearch,free_feeds"])
        )
        self.assertEqual(error, "")
        self.assertEqual(lanes, ["free_feeds", "jsearch", "ats"])

    def test_live_acquisition_refuses_to_run_without_an_explicit_selection(self):
        lanes, source, error = run_retrieval_measurement.resolve_lanes(
            parse(["--mode", "live_acquisition"])
        )
        self.assertEqual(lanes, [])
        self.assertEqual(source, "none")
        self.assertIn("explicit --lanes", error)

    def test_an_unknown_lane_is_refused_rather_than_ignored(self):
        _lanes, _source, error = run_retrieval_measurement.resolve_lanes(
            parse(["--mode", "live_acquisition", "--lanes", "free_feeds,delivery"])
        )
        self.assertIn("unknown lane", error)
        self.assertIn("delivery", error)

    def test_an_empty_lanes_value_is_refused_not_treated_as_a_default(self):
        _lanes, _source, error = run_retrieval_measurement.resolve_lanes(
            parse(["--mode", "live_acquisition", "--lanes", " , "])
        )
        self.assertIn("selected nothing", error)

    def test_live_mode_exits_two_and_makes_no_request_without_lanes(self):
        with mock.patch("retrieval_measurement.instrument.MeasuringFetcher") as fetcher:
            code = run_retrieval_measurement.main(["--mode", "live_acquisition"])
        self.assertEqual(code, 2)
        fetcher.assert_not_called()

    def test_fixture_mode_default_is_unchanged_and_still_recorded(self):
        # Backward compatibility: no --lanes still means the historical
        # fixture-mode behaviour, but the choice is written to the artifact.
        lanes, source, error = run_retrieval_measurement.resolve_lanes(
            parse(["--mode", "fixture", "--fixture", str(FIXTURE)])
        )
        self.assertEqual(error, "")
        self.assertEqual(source, "mode_default")
        self.assertEqual(lanes, ["free_feeds"])

    def test_free_feeds_only_never_enters_jsearch_adzuna_or_ats_code(self):
        """The whole point of R1: prove the other lanes are not merely empty."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(run_retrieval_measurement, "run_jsearch_lane") as jsearch, \
                 mock.patch.object(run_retrieval_measurement, "run_adzuna_lane") as adzuna, \
                 mock.patch.object(run_retrieval_measurement, "run_ats_lane") as ats, \
                 mock.patch.object(run_retrieval_measurement, "JSearchTransport") as transport:
                code = run_retrieval_measurement.main([
                    "--mode", "fixture", "--fixture", str(FIXTURE),
                    "--lanes", "free_feeds",
                    "--artifact-root", tmp, "--sources", "himalayas",
                ])
            self.assertEqual(code, 0)
            jsearch.assert_not_called()
            adzuna.assert_not_called()
            ats.assert_not_called()
            transport.assert_not_called()

    def test_live_mode_with_free_feeds_only_never_builds_the_jsearch_transport(self):
        """Same proof as above, but on the live branch, where it matters.

        Every lane function is replaced, so nothing leaves the process: the
        assertion is about which branches the dispatcher takes.
        """
        from free_job_sources import SourceResult

        from retrieval_measurement.drivers import LaneOutput

        def one_free_lane(_fetcher, _sources):
            return [LaneOutput(
                source="himalayas", lane="free_feed",
                result=SourceResult(source="himalayas", success=True),
            )]

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(run_retrieval_measurement, "run_free_source_lane", one_free_lane), \
                 mock.patch.object(run_retrieval_measurement, "run_jsearch_lane") as jsearch, \
                 mock.patch.object(run_retrieval_measurement, "run_adzuna_lane") as adzuna, \
                 mock.patch.object(run_retrieval_measurement, "run_ats_lane") as ats, \
                 mock.patch.object(run_retrieval_measurement, "JSearchTransport") as transport, \
                 mock.patch.object(run_retrieval_measurement, "_live_request", tripwire):
                code = run_retrieval_measurement.main([
                    "--mode", "live_acquisition", "--lanes", "free_feeds",
                    "--artifact-root", tmp, "--sources", "himalayas",
                ])
            self.assertEqual(code, 0)
            jsearch.assert_not_called()
            adzuna.assert_not_called()
            ats.assert_not_called()
            transport.assert_not_called()
            manifest = json.loads(
                (next((Path(tmp) / ARTIFACT_DIRNAME).iterdir()) / "run_manifest.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["lanes_selected"], ["free_feeds"])
            self.assertEqual(manifest["mode"], "live_acquisition")

    def test_the_manifest_names_the_lanes_that_ran_and_the_ones_that_did_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_retrieval_measurement.main([
                "--mode", "fixture", "--fixture", str(FIXTURE),
                "--lanes", "free_feeds",
                "--artifact-root", tmp, "--sources", "himalayas",
            ])
            run = next((Path(tmp) / ARTIFACT_DIRNAME).iterdir())
            manifest = json.loads((run / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["lanes_selected"], ["free_feeds"])
            self.assertEqual(manifest["lane_selection_source"], "explicit")
            selected = {check["lane"]: check["selected"] for check in manifest["preflight"]}
            self.assertEqual(selected, {
                "free_feeds": True, "jsearch": False, "adzuna": False, "ats": False,
            })


# --------------------------------------------------------------------------
# R2 -- no implicit SeenJobsRegistry
# --------------------------------------------------------------------------


class RegistryIsolationTests(unittest.TestCase):
    def test_the_jsearch_lane_passes_an_explicit_non_writing_registry(self):
        captured = {}

        def fake_scrape(registry=None, **kwargs):
            captured["registry"] = registry
            captured["kwargs"] = kwargs
            raise NetworkTripwire("stop before any request")

        module = mock.MagicMock()
        module.run_daily_scrape = fake_scrape
        with mock.patch.dict(sys.modules, {"jsearch_scraper": module}):
            with self.assertRaises(NetworkTripwire):
                run_jsearch_lane(tripwire, output_dir="unused", max_queries=1)

        self.assertIsInstance(captured["registry"], NonWritingRegistry)
        self.assertNotIn("registry", captured["kwargs"])

    def test_no_production_registry_is_ever_constructed(self):
        import pipeline_state

        with mock.patch.object(
            pipeline_state, "SeenJobsRegistry", side_effect=AssertionError(
                "the harness constructed the production SeenJobsRegistry")
        ):
            registry = NonWritingRegistry()
            self.assertFalse(registry.has_job_id("anything"))
            self.assertFalse(registry.has_dedup_key(("acme", "engineer")))

    def test_every_write_method_refuses(self):
        registry = NonWritingRegistry()
        for method in ("save", "mark_jobs", "mark_dedup_keys", "_load", "_prune"):
            with self.subTest(method=method):
                with self.assertRaises(RegistryWriteRefused):
                    getattr(registry, method)()

    def test_it_opens_no_file_and_holds_no_path(self):
        registry = NonWritingRegistry()
        self.assertEqual(registry.path, "")
        self.assertFalse(registry.describe()["write_capable"])
        self.assertFalse(registry.describe()["backed_by_snapshot"])

    def test_a_supplied_snapshot_backs_the_lookups_read_only(self):
        snapshot = ReadOnlySeenSnapshot({"jsearch:1": "2026-08-01"}, {"acme|engineer": "2026-08-01"})
        registry = NonWritingRegistry(snapshot)
        self.assertTrue(registry.has_job_id("jsearch:1"))
        self.assertTrue(registry.has_dedup_key(("acme", "engineer")))
        self.assertFalse(registry.has_job_id("jsearch:2"))
        self.assertEqual(registry.lookups, 3)
        with self.assertRaises(RegistryWriteRefused):
            registry.save()

    def test_a_run_touches_no_seen_jobs_file_anywhere(self):
        opened = []
        real_open = open

        def watching_open(file, *args, **kwargs):
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("builtins.open", watching_open):
                code = run_retrieval_measurement.main([
                    "--mode", "fixture", "--fixture", str(FIXTURE),
                    "--lanes", "free_feeds",
                    "--artifact-root", tmp, "--sources", "himalayas",
                ])
        self.assertEqual(code, 0)
        self.assertFalse(
            [path for path in opened if "seen_jobs" in path.lower()],
            "the run touched a seen-jobs file",
        )

    def test_without_a_snapshot_the_deltas_stay_null_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_retrieval_measurement.main([
                "--mode", "fixture", "--fixture", str(FIXTURE),
                "--lanes", "free_feeds",
                "--artifact-root", tmp, "--sources", "himalayas",
            ])
            run = next((Path(tmp) / ARTIFACT_DIRNAME).iterdir())
            summary = json.loads((run / "coverage_summary.json").read_text(encoding="utf-8"))
            for baseline in ("run_baseline_posting_identity",
                             "run_baseline_production_equivalent"):
                self.assertIsNone(summary[baseline]["previously_seen"], baseline)
                self.assertIsNone(summary[baseline]["incremental_new"], baseline)


# --------------------------------------------------------------------------
# R3 -- per-lane exception containment
# --------------------------------------------------------------------------


class LaneContainmentTests(unittest.TestCase):
    def _run_with_failing_adzuna(self, tmp):
        with mock.patch.object(config, "ADZUNA_ENABLED", True), \
             mock.patch.object(
                 run_retrieval_measurement, "run_adzuna_lane",
                 side_effect=RuntimeError("adzuna exploded"),
             ):
            return run_retrieval_measurement.main([
                "--mode", "fixture", "--fixture", str(FIXTURE),
                "--lanes", "free_feeds,adzuna",
                "--artifact-root", tmp, "--sources", "himalayas",
            ])

    def test_a_failed_lane_does_not_discard_a_completed_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = self._run_with_failing_adzuna(tmp)
            self.assertEqual(code, 1)
            run = next((Path(tmp) / ARTIFACT_DIRNAME).iterdir())
            summary = json.loads((run / "coverage_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["lanes_completed"], ["free_feeds"])
            self.assertGreater(summary["run_baseline_posting_identity"]["gross_returned"], 0)
            self.assertTrue(summary["reconciliation"]["passed"])

    def test_partial_artifacts_are_still_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run_with_failing_adzuna(tmp)
            run = next((Path(tmp) / ARTIFACT_DIRNAME).iterdir())
            for name in ("run_manifest.json", "coverage_summary.json", "coverage_summary.md",
                         "source_metrics.json", "lane_failures.json", "preflight.json",
                         "run_status.json"):
                self.assertTrue((run / name).is_file(), name)

    def test_the_failure_record_carries_everything_needed_to_diagnose_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run_with_failing_adzuna(tmp)
            run = next((Path(tmp) / ARTIFACT_DIRNAME).iterdir())
            failures = json.loads((run / "lane_failures.json").read_text(encoding="utf-8"))
            self.assertEqual(len(failures), 1)
            failure = failures[0]
            self.assertEqual(failure["lane"], "adzuna")
            self.assertEqual(failure["exception_type"], "RuntimeError")
            self.assertIn("adzuna exploded", failure["error"])
            self.assertIsInstance(failure["requests_attempted_before_failure"], int)

    def test_run_status_names_the_artifacts_that_actually_reached_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run_with_failing_adzuna(tmp)
            run = next((Path(tmp) / ARTIFACT_DIRNAME).iterdir())
            status = json.loads((run / "run_status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "incomplete")
            self.assertIn("coverage_summary.json", status["artifacts_persisted"])
            self.assertIn("lane_failures.json", status["artifacts_persisted"])
            self.assertEqual(status["artifacts_unwritable"], {})
            for name in status["artifacts_persisted"]:
                self.assertTrue((run / name).is_file(), name)

    def test_a_lane_failure_is_never_recorded_as_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run_with_failing_adzuna(tmp)
            run = next((Path(tmp) / ARTIFACT_DIRNAME).iterdir())
            metrics = json.loads((run / "source_metrics.json").read_text(encoding="utf-8"))
            for metric in metrics:
                for record in metric["truncation"]:
                    self.assertNotEqual(record["kind"], "provider_exhaustion")
            report = (run / "coverage_summary.md").read_text(encoding="utf-8")
            self.assertIn("Lane failures", report)
            self.assertIn("PARTIAL", report)

    def test_the_status_is_incomplete_and_the_exit_code_is_non_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = self._run_with_failing_adzuna(tmp)
            self.assertNotEqual(code, 0)
            run = next((Path(tmp) / ARTIFACT_DIRNAME).iterdir())
            manifest = json.loads((run / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "incomplete")
            self.assertIn("lane failure", manifest["exit_reason"])
            self.assertIn("adzuna/RuntimeError", manifest["exit_reason"])

    def test_the_recorded_error_is_redacted(self):
        with mock.patch.object(config, "RAPIDAPI_KEY", "super-secret-key-value"):
            cleaned = redact_text("failed calling https://x/?api_key=super-secret-key-value now")
        self.assertNotIn("super-secret-key-value", cleaned)
        self.assertIn("<redacted>", cleaned)

    def test_redaction_also_catches_a_key_the_config_never_held(self):
        cleaned = redact_text("GET /search?token=abcdef123456&page=2 failed")
        self.assertNotIn("abcdef123456", cleaned)
        self.assertIn("page=2", cleaned)


# --------------------------------------------------------------------------
# R4 -- --max-queries semantics
# --------------------------------------------------------------------------


class MaxQueriesTests(unittest.TestCase):
    def test_zero_means_zero_queries_and_imports_nothing(self):
        # Not "zero" as in "we asked for zero and production read it as 50".
        # jsearch_scraper.py:640 treats 0 as falsy and returns ALL roles, so the
        # harness must never hand a 0 down; it skips the lane outright.
        with mock.patch.dict(sys.modules, {"jsearch_scraper": None}):
            outputs = run_jsearch_lane(tripwire, output_dir="unused", max_queries=0)
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].result.requests_attempted, 0)
        self.assertEqual(outputs[0].result.raw_records, 0)
        self.assertTrue(outputs[0].result.success)
        self.assertIn("zero JSearch queries", " ".join(outputs[0].notes))

    def test_one_is_passed_through_as_one(self):
        self.assertEqual(self._forwarded(max_queries=1), 1)

    def test_a_positive_n_is_passed_through_as_n(self):
        self.assertEqual(self._forwarded(max_queries=7), 7)

    def test_omission_preserves_the_configured_default(self):
        self.assertIsNone(self._forwarded(max_queries=None))

    def test_a_negative_value_is_refused_before_any_request(self):
        checks = run_retrieval_measurement.lane_preflight(
            ["jsearch"],
            parse(["--mode", "live_acquisition", "--lanes", "jsearch", "--max-queries", "-1"]),
            require_credentials=False,
        )
        jsearch = next(check for check in checks if check.lane == "jsearch")
        self.assertFalse(jsearch.ready)
        self.assertIn("--max-queries cannot be negative", jsearch.blocking_reasons)

    def _forwarded(self, *, max_queries):
        captured = {}

        def fake_scrape(registry=None, **kwargs):
            captured.update(kwargs)
            raise NetworkTripwire("stop before any request")

        module = mock.MagicMock()
        module.run_daily_scrape = fake_scrape
        with mock.patch.dict(sys.modules, {"jsearch_scraper": module}):
            with self.assertRaises(NetworkTripwire):
                run_jsearch_lane(tripwire, output_dir="unused", max_queries=max_queries)
        return captured["max_queries"]


# --------------------------------------------------------------------------
# R5 -- pre-network validation
# --------------------------------------------------------------------------


class PreflightTests(unittest.TestCase):
    def test_missing_jsearch_credentials_cause_zero_network_calls(self):
        calls = []

        def counting_request(*args, **kwargs):
            calls.append(args)
            raise NetworkTripwire("a request escaped preflight")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "RAPIDAPI_KEY", ""), \
                 mock.patch.object(run_retrieval_measurement, "_live_request", counting_request), \
                 mock.patch.object(run_retrieval_measurement, "run_free_source_lane") as free:
                code = run_retrieval_measurement.main([
                    "--mode", "live_acquisition",
                    "--lanes", "free_feeds,jsearch",
                    "--artifact-root", tmp,
                ])
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])
        # Crucially: the free-feed lane never ran either. Under the old code it
        # would have made 29 real requests before JSearch raised.
        free.assert_not_called()

    def test_an_unselected_jsearch_lane_does_not_require_a_key(self):
        with mock.patch.object(config, "RAPIDAPI_KEY", ""):
            checks = run_retrieval_measurement.lane_preflight(
                ["free_feeds"],
                parse(["--mode", "live_acquisition", "--lanes", "free_feeds"]),
                require_credentials=True,
            )
        free = next(check for check in checks if check.lane == "free_feeds")
        jsearch = next(check for check in checks if check.lane == "jsearch")
        self.assertTrue(free.ready)
        self.assertFalse(jsearch.selected)
        self.assertEqual(jsearch.blocking_reasons, [])
        self.assertEqual(
            [entry["status"] for entry in jsearch.credentials], ["NOT_REQUIRED"]
        )

    def test_a_selected_jsearch_lane_with_a_key_present_passes(self):
        with mock.patch.object(config, "RAPIDAPI_KEY", "present-value"):
            checks = run_retrieval_measurement.lane_preflight(
                ["jsearch"],
                parse(["--mode", "live_acquisition", "--lanes", "jsearch"]),
                require_credentials=True,
            )
        jsearch = next(check for check in checks if check.lane == "jsearch")
        self.assertTrue(jsearch.ready)
        self.assertEqual([entry["status"] for entry in jsearch.credentials], ["PRESENT"])

    def test_adzuna_requires_both_its_credentials_and_its_enable_flag(self):
        with mock.patch.object(config, "ADZUNA_ENABLED", False), \
             mock.patch.object(config, "ADZUNA_APP_ID", ""), \
             mock.patch.object(config, "ADZUNA_APP_KEY", ""):
            checks = run_retrieval_measurement.lane_preflight(
                ["adzuna"],
                parse(["--mode", "live_acquisition", "--lanes", "adzuna"]),
                require_credentials=True,
            )
        adzuna = next(check for check in checks if check.lane == "adzuna")
        self.assertFalse(adzuna.ready)
        self.assertIn("config.ADZUNA_ENABLED is False", adzuna.blocking_reasons)
        self.assertIn("ADZUNA_APP_ID is ABSENT", adzuna.blocking_reasons)
        self.assertIn("ADZUNA_APP_KEY is ABSENT", adzuna.blocking_reasons)

    def test_the_ats_lane_requires_a_readable_board_registry(self):
        checks = run_retrieval_measurement.lane_preflight(
            ["ats"],
            parse(["--mode", "live_acquisition", "--lanes", "ats"]),
            require_credentials=True,
        )
        ats = next(check for check in checks if check.lane == "ats")
        self.assertFalse(ats.ready)
        self.assertIn("--boards is required for the ats lane", ats.blocking_reasons)

    def test_an_unreadable_board_registry_is_refused_before_any_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "boards.json"
            broken.write_text("{not json", encoding="utf-8")
            checks = run_retrieval_measurement.lane_preflight(
                ["ats"],
                parse(["--mode", "live_acquisition", "--lanes", "ats", "--boards", str(broken)]),
                require_credentials=True,
            )
        ats = next(check for check in checks if check.lane == "ats")
        self.assertFalse(ats.ready)
        self.assertTrue(any("unusable" in reason for reason in ats.blocking_reasons))

    def test_an_unknown_free_source_is_refused_before_any_request(self):
        checks = run_retrieval_measurement.lane_preflight(
            ["free_feeds"],
            parse(["--mode", "live_acquisition", "--lanes", "free_feeds",
                   "--sources", "himalayas,linkedin"]),
            require_credentials=True,
        )
        free = next(check for check in checks if check.lane == "free_feeds")
        self.assertFalse(free.ready)
        self.assertTrue(any("linkedin" in reason for reason in free.blocking_reasons))

    def test_credential_state_reports_presence_only(self):
        with mock.patch.object(config, "RAPIDAPI_KEY", "a-real-looking-secret"):
            self.assertEqual(credential_state("RAPIDAPI_KEY"), "PRESENT")
            self.assertEqual(credential_state("RAPIDAPI_KEY", required=False), "NOT_REQUIRED")
        with mock.patch.object(config, "RAPIDAPI_KEY", "   "):
            self.assertEqual(credential_state("RAPIDAPI_KEY"), "ABSENT")

    def test_no_credential_value_ever_reaches_an_artifact(self):
        secret = "zz-unmistakable-secret-value-zz"
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "RAPIDAPI_KEY", secret):
                run_retrieval_measurement.main([
                    "--mode", "fixture", "--fixture", str(FIXTURE),
                    "--lanes", "free_feeds",
                    "--artifact-root", tmp, "--sources", "himalayas",
                ])
            run = next((Path(tmp) / ARTIFACT_DIRNAME).iterdir())
            for path in run.rglob("*"):
                if path.is_file() and path.suffix in {".json", ".md"}:
                    self.assertNotIn(secret, path.read_text(encoding="utf-8"), str(path))

    def test_a_credential_status_cannot_carry_an_unknown_state(self):
        with self.assertRaises(ValueError):
            CredentialStatus(name="RAPIDAPI_KEY", lane="jsearch", status="present")


# --------------------------------------------------------------------------
# Write confinement
# --------------------------------------------------------------------------


class WriteConfinementTests(unittest.TestCase):
    def test_every_write_lands_inside_the_harness_artifact_root(self):
        """Files created during a run must all be under <root>/retrieval_measurement.

        ``config`` creates data/ subdirectories at import time -- that happens
        before this test runs and is documented, not fixed here. What must hold
        is that the RUN itself adds nothing outside its own root.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = {str(path) for path in root.rglob("*")}
            code = run_retrieval_measurement.main([
                "--mode", "fixture", "--fixture", str(FIXTURE),
                "--lanes", "free_feeds",
                "--artifact-root", str(root), "--sources", "himalayas",
            ])
            self.assertEqual(code, 0)
            created = {str(path) for path in root.rglob("*")} - before
            self.assertTrue(created)
            confined = root / ARTIFACT_DIRNAME
            for path in created:
                self.assertTrue(
                    Path(path) == confined or confined in Path(path).parents,
                    f"{path} escaped the harness artifact root",
                )

    def test_retention_remains_report_only_by_default(self):
        parser = run_retrieval_measurement.build_parser()
        self.assertFalse(parser.parse_args([]).retention_apply)
        self.assertFalse(
            parser.parse_args(["--retention-report"]).retention_apply,
            "--retention-report must never imply deletion",
        )


if __name__ == "__main__":
    unittest.main()
