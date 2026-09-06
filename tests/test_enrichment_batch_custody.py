"""Different company workloads must not reuse each other's outcomes (no HTTP)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import config
import hiring_manager as hm
from orchestrator.adapters_real import RealEnrichmentStage
from orchestrator.enrichment import EnrichmentReport
from tests.test_orchestrator_runtime_budget import _job, _fake_process_company


class BatchCustody(unittest.TestCase):
    def test_recovery_reconciliation_keeps_resumed_inputs_out_of_new_capture(self):
        import run_maintenance
        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root) / "run_artifacts" / "r1"
            (run_dir / "enrichment").mkdir(parents=True)
            (run_dir / "orchestrator_result.json").write_text(json.dumps({"acquisition": {
                "cumulative": {"net_new_jobs_captured": 1,
                               "pending_work_resumed": {"adopted": 1}}}}))
            (run_dir / "enrichment" / "postings.json").write_text(json.dumps({"jobs": [
                _job("one", "Acme"), dict(_job("two", "Beta"), _resumed_from_pending=True)]}))
            result = run_maintenance.reconcile(Path(root), "r1")
            self.assertTrue(result["agrees"])
            self.assertEqual(result["net_new_jobs_captured"], 1)
            self.assertEqual(result["new_postings_retained"], 1)
            self.assertEqual(result["resumed_postings_retained"], 1)
            self.assertEqual(result["opportunities_retained"], 2)
            payload = json.loads((run_dir / "orchestrator_result.json").read_text())
            payload["acquisition"]["cumulative"]["pending_work_resumed"]["adopted"] = 2
            (run_dir / "orchestrator_result.json").write_text(json.dumps(payload))
            self.assertFalse(run_maintenance.reconcile(Path(root), "r1")["agrees"])

    def test_unreadable_checkpoint_stops_before_enrichment_and_preserves_file(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.multiple(
                config, STEP3_OUTPUT_DIR=root), mock.patch.object(
                hm, "validate_preflight"), mock.patch.object(hm, "process_company") as process:
            source = Path(root) / "input.json"
            source.write_text(json.dumps({"jobs": [_job("one", "Acme")]}))
            checkpoint = Path(root) / "enrichment_progress.json"
            checkpoint.write_text("{truncated")
            with self.assertRaises(RuntimeError):
                hm.run_hiring_manager_identification(str(source))
            process.assert_not_called()
            self.assertEqual(checkpoint.read_text(), "{truncated")

    def test_failed_checkpoint_write_stops_before_the_next_company(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.multiple(
                config, STEP3_OUTPUT_DIR=root, ORCHESTRATOR_ENRICHMENT_MAX_RUNTIME_SECONDS=0), mock.patch.object(
                hm, "validate_preflight"), mock.patch.object(
                hm, "process_company", side_effect=_fake_process_company) as process, mock.patch.object(
                hm.os, "replace", side_effect=OSError("disk full")):
            source = Path(root) / "input.json"
            source.write_text(json.dumps({"jobs": [_job("one", "Acme"), _job("two", "Beta")]}))
            with self.assertRaises(RuntimeError):
                hm.run_hiring_manager_identification(str(source))
            self.assertEqual(process.call_count, 1)
            self.assertTrue((Path(root) / "enrichment_progress.json.tmp").is_file())

    def test_same_company_new_posting_is_processed_and_exact_resume_is_reused(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.multiple(
                config, STEP3_OUTPUT_DIR=root, ORCHESTRATOR_ENRICHMENT_MAX_RUNTIME_SECONDS=0), mock.patch.object(
                hm, "validate_preflight"), mock.patch.object(
                hm, "process_company", side_effect=_fake_process_company) as process:
            source = Path(root) / "input.json"
            for posting in ("one", "two", "one"):
                source.write_text(json.dumps({"jobs": [_job(posting, "Acme")]}))
                result = hm.run_hiring_manager_identification(str(source))
                rows = json.loads(Path(result.output_path).read_text())["jobs"]
                self.assertEqual([r["job_id"] for r in rows], [posting])
            self.assertEqual(process.call_count, 2)

    def test_real_stage_keeps_every_batch_and_restart_reuses_its_own_work(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.multiple(
                config, COMPANY_OPPORTUNITY_COLLAPSE_ENABLED=False,
                ORCHESTRATOR_ENRICHMENT_MAX_RUNTIME_SECONDS=0), mock.patch(
                "qualification_pipeline.run_precontact_qualification",
                side_effect=lambda path, **kw: SimpleNamespace(output_path=path)), mock.patch.object(
                hm, "validate_preflight"), mock.patch.object(
                hm, "process_company", side_effect=_fake_process_company) as process, mock.patch.object(
                RealEnrichmentStage, "_to_report", return_value=EnrichmentReport()):
            stage = RealEnrichmentStage(workdir=root)
            stage.run([_job("one", "Acme")])
            first_files = {p: p.read_bytes() for p in Path(root).rglob("jobs_enriched_*.json")}
            stage.run([_job("two", "Acme")])
            self.assertEqual(process.call_count, 2)
            for path, content in first_files.items():
                self.assertEqual(path.read_bytes(), content)
            inputs = json.loads((Path(root) / "postings.json").read_text())["jobs"]
            self.assertEqual({r["job_id"] for r in inputs}, {"one", "two"})
            outputs = list(Path(root).rglob("jobs_enriched_*.json"))
            self.assertEqual(len(outputs), 2)
            RealEnrichmentStage(workdir=root).run([_job("one", "Acme")])
            self.assertEqual(process.call_count, 2)
            inputs = json.loads((Path(root) / "postings.json").read_text())["jobs"]
            self.assertEqual(len(inputs), 2)
