from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
import final_pass_topup
from business_model_classifier import classify_business_model
from decision_types import GateState
from evidence_types import EvidenceStatus
from hiring_manager import Step3Result
from job_gate import JobGate
from job_signal import select_job_url
from job_source_resolver import (
    JobSourceResolver,
    ResolvedJobSource,
    SourceAttempt,
    _job_page_has_title,
)
from jsearch_scraper import ScrapeResult
from pipeline_state import SeenJobsRegistry


class _Resolver:
    def __init__(self, resolved: ResolvedJobSource):
        self.resolved = resolved

    def resolve(self, job, fetch=None):
        return self.resolved


def _source(
    description: str = "",
    *,
    employment_type: str = "",
    location: str = "",
    source_url: str = "https://example.com/jobs/staff-accountant",
    source_type: str = "company",
) -> ResolvedJobSource:
    return ResolvedJobSource(
        state="ACTIVE_VERIFIED",
        source_url=source_url,
        source_type=source_type,
        http_status=200,
        active=True,
        canonical_title="Staff Accountant",
        canonical_employer="Example Corp",
        description=description,
        location_text=location,
        employment_type=employment_type,
        official=True,
        corroborated=True,
    )


def _provider_job() -> dict:
    return {
        "job_id": "j1",
        "job_title": "Staff Accountant",
        "employer_name": "Example Corp",
        "employer_website": "https://example.com",
        "job_apply_link": "https://provider.example/jobs/j1",
        "job_description": "Join the accounting team and support monthly close and reporting.",
        "job_employment_type": "Full-time",
        "job_is_remote": True,
        "job_location": "Anywhere",
        "job_country": None,
        "_employment_quality": "full_time",
        "_employment_quality_reason": "provider_full_time",
        "_work_arrangement": "remote",
        "_work_arrangement_reason": "provider_remote_true",
        "_remote_scope": "us_provider_confirmed",
        "_us_eligibility_reason": "provider_confirmed_us_remote",
        "_matched_role": "Staff Accountant",
    }


class CrossSourceJobFactTests(unittest.TestCase):
    def test_active_official_posting_can_use_structured_provider_facts_when_silent(self):
        decision = JobGate(_Resolver(_source(
            "You will support monthly close, reconciliations, and financial reporting."
        ))).evaluate(_provider_job())
        self.assertEqual(decision.state, GateState.PASS)
        facts = decision.evidence.facts
        self.assertEqual(facts["employment_type"].status, EvidenceStatus.VERIFIED_CROSS_SOURCE)
        self.assertEqual(facts["work_arrangement"].status, EvidenceStatus.VERIFIED_CROSS_SOURCE)
        self.assertEqual(facts["intent_market"].status, EvidenceStatus.VERIFIED_CROSS_SOURCE)

    def test_official_onsite_contradiction_overrides_provider_remote(self):
        decision = JobGate(_Resolver(_source(
            "Employees must work in our office three days per week."
        ))).evaluate(_provider_job())
        self.assertEqual(decision.state, GateState.REJECT)
        self.assertEqual(str(decision.primary_reason.value), "REJECT_ONSITE_REQUIRED")

    def test_official_contract_contradiction_overrides_provider_full_time(self):
        decision = JobGate(_Resolver(_source(
            "This is an independent contractor position supporting monthly close."
        ))).evaluate(_provider_job())
        self.assertEqual(decision.state, GateState.REJECT)
        self.assertEqual(str(decision.primary_reason.value), "REJECT_CONTRACT")

    def test_provider_fact_requires_prefilter_and_structured_signal(self):
        job = _provider_job()
        job.pop("_employment_quality")
        decision = JobGate(_Resolver(_source(
            "This role supports monthly close. It is remote in the United States."
        ))).evaluate(job)
        self.assertEqual(decision.state, GateState.UNVERIFIED)
        self.assertEqual(str(decision.primary_reason.value), "UNVERIFIED_EMPLOYMENT_TYPE")


class SourceResolverRecoveryTests(unittest.TestCase):
    def test_noisy_provider_title_matches_clean_official_job_page(self):
        self.assertTrue(_job_page_has_title(
            "Remote Full Stack Developer Jobs in Kansas City",
            "Remote Full-Stack Developer. Responsibilities, qualifications, and requirements.",
        ))

    def test_job_signal_preserves_job_gate_canonical_url_without_reprobing(self):
        selected, status, source_type, reason = select_job_url({
            "job_apply_link": "https://remote-rocketship.com/job/123",
            "official_job_url": "https://example.com/jobs/staff-accountant",
            "official_job_status": "ACTIVE_VERIFIED",
            "official_job_source_type": "company",
        }, probe=False)
        self.assertEqual(selected, "https://example.com/jobs/staff-accountant")
        self.assertEqual(status, "verified")
        self.assertEqual(source_type, "company")
        self.assertEqual(reason, "verified_by_job_gate")

    def test_discovery_403_is_retryable_temporary_unavailability(self):
        resolver = JobSourceResolver()
        job = {
            **_provider_job(),
            "job_apply_link": "https://remote-rocketship.com/job/123",
        }
        with (
            patch.object(resolver, "_discover_company_job_urls", return_value=(
                [],
                [SourceAttempt(
                    "https://example.com/careers",
                    "company",
                    "discovery_unavailable",
                    403,
                    "https://example.com/careers",
                    "forbidden",
                )],
            )),
            patch.object(resolver, "_fetch", return_value={
                "status_code": 404,
                "final_url": "https://remote-rocketship.com/job/123",
                "text": "",
                "error": "",
            }),
        ):
            resolved = resolver.resolve(job, fetch=True)

        self.assertEqual(resolved.state, "SOURCE_TEMPORARILY_UNAVAILABLE")
        self.assertTrue(resolved.retryable)
        self.assertTrue(resolved.temporarily_unavailable)

    def test_discovery_runs_even_when_provider_supplies_direct_company_url(self):
        resolver = JobSourceResolver()
        direct = "https://example.com/jobs/old-board"
        discovered = "https://example.com/jobs/staff-accountant"
        job = {
            **_provider_job(),
            "job_apply_link": direct,
        }
        body = json.dumps({
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "Staff Accountant",
            "hiringOrganization": {"name": "Example Corp"},
            "description": "Full-time remote role in the United States. " + "Responsibilities and qualifications. " * 30,
            "employmentType": "FULL_TIME",
            "jobLocationType": "TELECOMMUTE",
            "validThrough": "2099-12-31T23:59:59Z",
        })
        html = f'<script type="application/ld+json">{body}</script>'

        def fetch(url):
            if url == discovered:
                return {"status_code": 200, "final_url": discovered, "text": html, "error": ""}
            return {"status_code": 404, "final_url": url, "text": "", "error": ""}

        with (
            patch.object(resolver, "_discover_company_job_urls", return_value=(
                [discovered],
                [SourceAttempt("https://example.com/careers", "company", "discovery_page", 200)],
            )) as discover_mock,
            patch.object(resolver, "_fetch", side_effect=fetch),
        ):
            resolved = resolver.resolve(job, fetch=True)

        discover_mock.assert_called_once()
        self.assertEqual(resolved.state, "ACTIVE_VERIFIED")
        self.assertEqual(resolved.source_url, discovered)


class BusinessModelSafetyTests(unittest.TestCase):
    def test_absence_of_excluded_model_does_not_require_positive_product_clause(self):
        result = classify_business_model(
            company_text="Welcome. Read our team biographies, company history, awards, and careers.",
            apollo_industry="Professional Services",
            apollo_description=("Experienced people serving customers across many markets. " * 10),
        )
        self.assertEqual(result.state, "ALLOWED")

    def test_apollo_own_offering_clause_can_corroborate_allowed_model(self):
        result = classify_business_model(
            company_text="",
            apollo_industry="Computer Software",
            apollo_description="We build workflow software and analytics tools for finance teams.",
        )
        self.assertEqual(result.state, "ALLOWED")
        self.assertEqual(result.evidence[0].status, EvidenceStatus.VERIFIED_CROSS_SOURCE)

    def test_financial_media_description_is_excluded(self):
        result = classify_business_model(
            company_text="Benzinga is a leading financial media outlet for investors.",
            apollo_industry="Online Media",
            apollo_description="A financial media company publishing market news.",
        )
        self.assertEqual(result.state, "EXCLUDED")
        self.assertEqual(result.reason_code, "REJECT_EXCLUDED_INDUSTRY")


class TopupExistingCompanyTests(unittest.TestCase):
    @staticmethod
    def _result(path: Path, *, final_pass: int, company_keys: list[str]) -> Step3Result:
        return Step3Result(
            output_path=str(path), total_input_jobs=1, total_output_leads=final_pass,
            company_criteria_excluded=0, hiring_manager_found=final_pass,
            hiring_manager_not_found=0, match_rate=1.0 if final_pass else 0.0,
            contactable_hiring_managers=final_pass, uncontactable_hiring_managers=0,
            contactable_rate=1.0 if final_pass else 0.0, companies_considered=final_pass,
            eligible_companies=final_pass, company_criteria_excluded_companies=0,
            final_pass_target=1, final_pass_leads=final_pass,
            final_pass_target_reached=bool(final_pass), reviewable_leads=final_pass,
            reviewable_target_reached=bool(final_pass), max_eligible_companies=None,
            stop_reason="candidate_pool_exhausted", processed_company_keys=company_keys,
            stats={},
        )

    def test_topup_preserves_airtable_company_exclusions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initial_raw = root / "raw.json"
            initial_raw.write_text(json.dumps({"jobs": [{"job_id": "j0"}]}))
            initial_payload = root / "initial.json"
            initial_payload.write_text(json.dumps({
                "jobs": [], "processed_job_refs": [], "processed_company_keys": []
            }))
            initial = self._result(initial_payload, final_pass=0, company_keys=[])
            initial_scrape = ScrapeResult(
                output_path=str(initial_raw), total_jobs=1, roles_with_results=1,
                stats={"estimated_request_units": 1, "query_metrics": {}},
            )
            topup_raw = root / "topup.json"
            topup_raw.write_text(json.dumps({"jobs": [{"job_id": "j1"}]}))
            topup_scrape = ScrapeResult(
                output_path=str(topup_raw), total_jobs=1, roles_with_results=1,
                stats={
                    "estimated_request_units": 1,
                    "queries_attempted": 1,
                    "queried_search_roles": ["Staff Accountant"],
                    "topup_new_prefilter_viable": 1,
                    "topup_stop_reason": "topup_unit_budget_exhausted",
                    "query_metrics": {},
                },
            )
            filtered_path = root / "filtered.json"
            filtered_path.write_text(json.dumps({"jobs": [{"job_id": "j1"}]}))
            qualified_path = root / "qualified.json"
            qualified_path.write_text(json.dumps({"jobs": [{"job_id": "j1", "_job_gate_state": "PASS"}]}))
            enriched_path = root / "enriched.json"
            enriched_path.write_text(json.dumps({
                "jobs": [{"job_id": "j1", "lead_key": "new", "_final_state": "FINAL_PASS"}],
                "processed_job_refs": [{"job_id": "j1"}],
                "processed_company_keys": ["new.com"],
            }))
            enriched = self._result(enriched_path, final_pass=1, company_keys=["new.com"])

            with (
                patch.object(config, "STEP3_OUTPUT_DIR", str(root)),
                patch.object(config, "FILTERED_OUTPUT_DIR", str(root)),
                patch.object(config, "FINAL_PASS_MAX_TOPUP_ITERATIONS", 2),
                patch.object(config, "FINAL_PASS_MAX_RUNTIME_SECONDS", 300),
                patch.object(config, "FINAL_PASS_MICROBATCH_QUERY_UNITS", 6),
                patch.object(config, "JSEARCH_MAX_ESTIMATED_UNITS_PER_RUN", 100),
                patch.object(final_pass_topup, "run_targeted_topup_scrape", return_value=topup_scrape),
                patch.object(final_pass_topup, "run_filter", return_value=SimpleNamespace(
                    output_path=str(filtered_path), kept_count=1, rejected_count=0,
                    success=True, errors=[]
                )),
                patch.object(final_pass_topup, "run_precontact_qualification", return_value=SimpleNamespace(
                    output_path=str(qualified_path), contact_eligible_jobs=1,
                    rejected_jobs=0, unverified_jobs=0
                )),
                patch.object(final_pass_topup, "run_hiring_manager_identification", return_value=enriched) as hm_mock,
            ):
                combined, _details = final_pass_topup.run_final_pass_topup(
                    initial_scrape=initial_scrape,
                    initial_enriched=initial,
                    registry=SeenJobsRegistry(path=str(root / "seen.json")),
                    target_final_pass_leads=1,
                    max_eligible_companies=0,
                    exclude_company_keys={"old.com"},
                )

        self.assertEqual(combined.final_pass_leads, 1)
        self.assertEqual(hm_mock.call_args.kwargs["exclude_company_keys"], {"old.com"})


    def test_secret_clearance_obtain_and_maintain_is_rejected(self):
        job = _provider_job()
        source = _source(
            description=(
                "This is a full-time, fully remote role in the United States. "
                "Candidates must have the ability to pass a background check, obtain, "
                "and maintain a Department of War Secret Clearance."
            )
        )
        decision = JobGate(_Resolver(source)).evaluate(job, fetch=False)
        self.assertEqual(decision.state, GateState.REJECT)
        self.assertEqual(str(decision.primary_reason.value), "REJECT_SECURITY_CLEARANCE_REQUIRED")

    def test_builtinsf_publisher_cannot_be_treated_as_official_company_source(self):
        job = _provider_job()
        job["employer_website"] = ""
        source = _source(
            source_url="https://www.builtinsf.com/job/google-cloud-engineer/9888948",
            source_type="company",
        )
        decision = JobGate(_Resolver(source)).evaluate(job, fetch=False)
        self.assertEqual(decision.state, GateState.UNVERIFIED)
        self.assertEqual(str(decision.primary_reason.value), "UNVERIFIED_OFFICIAL_SOURCE")



if __name__ == "__main__":
    unittest.main()
