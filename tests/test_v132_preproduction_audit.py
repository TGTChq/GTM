from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import multi_source_acquisition
from ats_board_registry import AtsBoardRegistry
from decision_types import GateState
from job_fact_extractor import _provider_signal_fact
from job_filter import is_stale_job
from qualification_pipeline import run_precontact_qualification


class PreproductionIdentityTests(unittest.TestCase):
    def test_company_website_candidate_rejects_substring_collision(self):
        self.assertFalse(
            multi_source_acquisition._company_website_candidate(
                "https://metabase.com/careers", "Meta", "himalayas.app"
            )
        )
        self.assertTrue(
            multi_source_acquisition._company_website_candidate(
                "https://acme.com/careers", "Acme Corporation", "himalayas.app"
            )
        )

    def test_domain_propagation_handles_legal_suffix_but_not_conflicts(self):
        jobs = [
            {"employer_name": "Acme", "employer_website": "https://acme.com"},
            {"employer_name": "Acme Corporation", "employer_website": ""},
        ]
        self.assertEqual(multi_source_acquisition._propagate_company_websites(jobs), 1)
        self.assertEqual(jobs[1]["employer_website"], "https://acme.com")

        conflicts = [
            {"employer_name": "Acme", "employer_website": "https://acme.com"},
            {"employer_name": "Acme Corp", "employer_website": "https://other.com"},
            {"employer_name": "Acme Corporation", "employer_website": ""},
        ]
        self.assertEqual(multi_source_acquisition._propagate_company_websites(conflicts), 0)
        self.assertEqual(conflicts[2]["employer_website"], "")

    def test_registry_weak_conflict_cannot_overwrite_existing_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = AtsBoardRegistry(path=str(Path(temp) / "boards.json"))
            url = "https://jobs.ashbyhq.com/acme/123"
            registry.upsert_from_job({
                "employer_name": "Acme",
                "employer_website": "https://acme.com",
                "job_apply_link": url,
                "_acquisition_source": "himalayas",
            })
            key = next(iter(registry.entries))
            registry.upsert_from_job({
                "employer_name": "Unrelated Company",
                "employer_website": "https://unrelated.com",
                "job_apply_link": url,
                "_acquisition_source": "jobicy",
            })
            self.assertEqual(registry.entries[key]["company_name"], "Acme")
            self.assertEqual(registry.entries[key]["company_domain"], "acme.com")
            self.assertEqual(registry.entries[key]["identity_conflicts"], 1)

            registry.upsert_from_job({
                "employer_name": "Acme Corporation",
                "employer_website": "https://acme.com",
                "job_apply_link": url,
                "job_apply_is_direct": True,
                "_acquisition_source": "ats_ashby",
                "_ats_board_identity_verified": True,
            })
            self.assertEqual(registry.entries[key]["company_name"], "Acme Corporation")
            self.assertEqual(registry.entries[key]["identity_confidence"], 3)

    def test_forced_board_refresh_respects_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = AtsBoardRegistry(path=str(Path(temp) / "boards.json"))
            for index in range(5):
                registry.upsert_from_job({
                    "employer_name": f"Company {index}",
                    "job_apply_link": f"https://jobs.ashbyhq.com/company-{index}/1",
                    "_acquisition_source": "himalayas",
                })
            self.assertEqual(len(registry.due_entries(limit=2, force=True)), 2)


class PreproductionFreshnessTests(unittest.TestCase):
    def test_greenhouse_without_first_published_is_rejected_before_enrichment(self):
        rejected, reason = is_stale_job({
            "_acquisition_source": "ats_greenhouse",
            "_ats_provider": "greenhouse",
            "_greenhouse_detail_request_made": True,
            "job_posted_at_datetime_utc": "",
        })
        self.assertTrue(rejected)
        self.assertEqual(reason, "greenhouse_first_published_unavailable")

    def test_greenhouse_not_checked_due_to_budget_is_rejected(self):
        rejected, reason = is_stale_job({
            "_acquisition_source": "ats_greenhouse",
            "_ats_provider": "greenhouse",
            "_greenhouse_detail_request_made": False,
            "job_posted_at_datetime_utc": "",
        })
        self.assertTrue(rejected)
        self.assertEqual(reason, "greenhouse_first_published_not_checked")


class PreproductionQualificationTests(unittest.TestCase):
    def test_needs_check_count_reads_gate_state(self):
        class NeedsCheckJobGate:
            def annotate(self, job, fetch=None):
                return {
                    **job,
                    "_job_gate_state": GateState.NEEDS_CHECK.value,
                    "_job_gate_reason": "manual_review",
                    "_job_gate_decision": {"metadata": {"source": {"state": "NEEDS_CHECK"}}},
                }

        class NeverCalledRoleGate:
            def annotate(self, job):
                raise AssertionError("role gate should not run")

        with tempfile.TemporaryDirectory() as temp:
            input_path = Path(temp) / "input.json"
            input_path.write_text(json.dumps({"jobs": [{
                "job_title": "Customer Success Manager",
                "employer_name": "Acme",
                "job_description": "Own onboarding and renewals.",
            }]}), encoding="utf-8")
            result = run_precontact_qualification(
                str(input_path),
                output_dir=temp,
                job_gate=NeedsCheckJobGate(),
                role_gate=NeverCalledRoleGate(),
            )
        self.assertEqual(result.needs_check_jobs, 1)
        self.assertEqual(result.contact_eligible_jobs, 0)

    def test_provider_evidence_labels_are_source_aware(self):
        fact = _provider_signal_fact(
            "intent_market",
            "us_market",
            {"_acquisition_source": "himalayas", "job_apply_link": "https://example.test/job"},
            ["Remote within the United States"],
        )
        self.assertEqual(fact.evidence[0].source_type, "provider_record:himalayas")


class PreproductionConfigTests(unittest.TestCase):
    def test_shadow_board_cap_has_safe_default(self):
        self.assertGreater(config.ATS_SHADOW_FORCE_REFRESH_MAX_BOARDS, 0)
        self.assertLessEqual(config.ATS_SHADOW_FORCE_REFRESH_MAX_BOARDS, config.ATS_MAX_BOARDS_PER_RUN)

    def test_shadow_company_metrics_collapse_legal_suffix_variants(self):
        from run_free_source_shadow import _shadow_company_metrics

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "jobs.json"
            path.write_text(json.dumps({"jobs": [
                {"employer_name": "Acme", "employer_website": ""},
                {"employer_name": "Acme Corporation", "employer_website": ""},
            ]}), encoding="utf-8")
            metrics = _shadow_company_metrics(str(path))
        self.assertEqual(metrics["unique_companies"], 1)
        self.assertEqual(metrics["extra_jobs_above_one_per_company"], 1)


if __name__ == "__main__":
    unittest.main()
