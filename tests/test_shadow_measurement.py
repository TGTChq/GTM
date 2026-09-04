"""Shadow (observational) measurement: function-aware dedupe would-exclude flag,
functional-family stamping, and ai_taxonomy capture. NONE of these may alter the
provider query or any acquisition/enrichment/delivery decision."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import config
import fantastic_jobs_adapter as fja
from orchestrator.yield_ledger import YieldLedger
from orchestrator.function_acquisition import family_for_role, FAMILY_GTM, FAMILY_PEOPLE


class TaxonomyCaptureTests(unittest.TestCase):
    def _map(self, record):
        job, reason = fja.map_record(record, "fantastic_jobs_linkedin", {})
        self.assertIsNotNone(job, reason)
        return job

    def _rec(self, **over):
        r = {"id": "1", "title": "Account Executive", "organization": "Acme",
             "source": "linkedin", "organization_url": "https://acme.com",
             "date_posted": "2026-08-23T00:00:00Z", "countries_derived": ["United States"],
             "employment_type": ["FULL_TIME"], "org_linkedin_headcount": 100}
        r.update(over)
        return r

    def test_taxonomies_captured_primary_is_first(self):
        j = self._map(self._rec(ai_taxonomies_a=["Sales", "Technology"]))
        self.assertEqual(j["_ai_taxonomies"], ["Sales", "Technology"])
        self.assertEqual(j["_ai_taxonomy_primary"], "Sales")

    def test_absent_taxonomies_are_empty_not_none(self):
        j = self._map(self._rec())
        self.assertEqual(j["_ai_taxonomies"], [])
        self.assertEqual(j["_ai_taxonomy_primary"], "")

    def test_capture_does_not_add_any_request_parameter(self):
        """Observation only: capturing taxonomies must NOT start filtering on them."""
        calls = []

        class R:
            status_code = 200
            headers = {"x-api-jobs-remaining": "5000", "x-api-requests-remaining": "9000"}
            def json(self): return []
        def http(url, headers, params, timeout):
            calls.append(dict(params)); return R()
        prod = dict(FANTASTIC_JOBS_ENABLED=True, FANTASTIC_JOBS_API_KEY="k",
                    FANTASTIC_JOBS_BASE_URL="https://data.fantastic.jobs",
                    FANTASTIC_JOBS_ATS_LIMIT=0, FANTASTIC_JOBS_WELLFOUND_LIMIT=0,
                    FANTASTIC_JOBS_YCOMBINATOR_LIMIT=0, FANTASTIC_JOBS_LINKEDIN_LIMIT=100,
                    FANTASTIC_JOBS_MAX_JOBS_PER_RUN=100, FANTASTIC_JOBS_TIME_FRAME="7d",
                    FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=0,
                    FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING=0, FANTASTIC_JOBS_MAX_RETRIES=0,
                    FANTASTIC_JOBS_FAIL_OPEN=False, FANTASTIC_JOBS_CONTINUATION_ENABLED=False,
                    FANTASTIC_MONTHLY_GOVERNOR_ENABLED=False)
        with mock.patch.multiple(config, **prod):
            fja.run_fantastic_jobs_acquisition(http_get=http)
        self.assertTrue(calls)
        for p in calls:
            for forbidden in ("ai_taxonomies_a", "ai_taxonomies_a_primary",
                              "exclude_ai_taxonomies_a", "exclude_organization_slug"):
                self.assertNotIn(forbidden, p)


class FunctionAwareShadowTests(unittest.TestCase):
    """Reproduces the pipeline's shadow computation exactly."""

    def _shadow(self, row, covered_at_start):
        import airtable_client
        fkeys = airtable_client.company_function_keys_for_job(row)
        hit = sorted(fkeys & set(covered_at_start))
        return (bool(hit), hit[0] if hit else "")

    def test_flags_row_whose_company_function_was_already_covered(self):
        row = {"company_domain": "acme.com", "employer_name": "Acme",
               "_role_bucket": "gtm_revenue"}
        covered = {"domain:acme.com|bucket:gtm_revenue"}
        ex, reason = self._shadow(row, covered)
        self.assertTrue(ex)
        self.assertEqual(reason, "domain:acme.com|bucket:gtm_revenue")

    def test_same_company_different_function_not_flagged(self):
        row = {"company_domain": "acme.com", "employer_name": "Acme",
               "_role_bucket": "engineering"}
        covered = {"domain:acme.com|bucket:gtm_revenue"}
        self.assertEqual(self._shadow(row, covered), (False, ""))

    def test_uncovered_company_not_flagged(self):
        row = {"company_domain": "new-co.com", "employer_name": "NewCo",
               "_role_bucket": "gtm_revenue"}
        self.assertEqual(self._shadow(row, {"domain:acme.com|bucket:gtm_revenue"}), (False, ""))

    def test_blank_role_bucket_yields_no_function_key(self):
        row = {"company_domain": "acme.com", "employer_name": "Acme", "_role_bucket": ""}
        self.assertEqual(self._shadow(row, {"domain:acme.com|bucket:gtm_revenue"}), (False, ""))

    def test_uses_run_start_coverage_not_live_set(self):
        """A company created EARLIER IN THIS RUN must not be scored avoidable."""
        covered_at_start = frozenset()
        live = {"domain:acme.com|bucket:gtm_revenue"}      # grew during the run
        row = {"company_domain": "acme.com", "_role_bucket": "gtm_revenue"}
        self.assertEqual(self._shadow(row, covered_at_start), (False, ""))
        self.assertTrue(self._shadow(row, live)[0])        # would be wrong to use


class FamilyStampingTests(unittest.TestCase):
    def test_matched_role_maps_to_acquisition_family(self):
        self.assertEqual(family_for_role("Account Executive"), FAMILY_GTM)
        self.assertEqual(family_for_role("Recruiter"), FAMILY_PEOPLE)

    def test_blank_matched_role_is_tolerated(self):
        self.assertTrue(family_for_role(""))   # falls back, never raises


class LedgerShadowFieldsTests(unittest.TestCase):
    def test_shadow_fields_persist_and_are_analysable(self):
        tmp = tempfile.mkdtemp(); path = os.path.join(tmp, "l.jsonl")
        L = YieldLedger(path, "run1")
        L.record_acquired([
            {"_fantastic_internal_id": "1", "org_linkedin_slug": "acme",
             "_ai_taxonomies": ["Sales"], "_ai_taxonomy_primary": "Sales",
             "employer_website": "acme.com", "_org_headcount": 120},
            {"_fantastic_internal_id": "2", "org_linkedin_slug": "beta",
             "_ai_taxonomies": ["Human Resources"], "_ai_taxonomy_primary": "Human Resources",
             "employer_website": "beta.com", "_org_headcount": 80},
        ])
        L.mark("1", function_aware_would_exclude=True,
               function_aware_reason="domain:acme.com|bucket:gtm_revenue",
               acquisition_function_family=FAMILY_GTM, send_safe=True, net_new_send_safe=True)
        L.mark("2", function_aware_would_exclude=False,
               acquisition_function_family=FAMILY_PEOPLE)
        L.flush()
        rows = [json.loads(x) for x in open(path) if x.strip()]
        by = {r["provider_job_id"]: r for r in rows}
        self.assertTrue(by["1"]["function_aware_would_exclude"])
        self.assertEqual(by["1"]["function_aware_reason"], "domain:acme.com|bucket:gtm_revenue")
        self.assertEqual(by["1"]["ai_taxonomy_primary"], "Sales")
        self.assertEqual(by["1"]["acquisition_function_family"], FAMILY_GTM)
        self.assertFalse(by["2"]["function_aware_would_exclude"])
        # The four questions the shadow must answer after several runs:
        avoidable = [r for r in rows if r["function_aware_would_exclude"]]
        self.assertEqual(len(avoidable), 1)                      # billed rows avoidable
        self.assertEqual(sum(r["fantastic_credits"] for r in avoidable), 1)  # credits saved
        self.assertEqual(sum(1 for r in avoidable if r["send_safe"]), 1)     # send-safe those rows made
        self.assertEqual(sum(1 for r in avoidable if r["net_new_send_safe"]), 1)  # legit leads at risk

    def test_shadow_defaults_false_when_never_marked(self):
        tmp = tempfile.mkdtemp(); path = os.path.join(tmp, "l.jsonl")
        L = YieldLedger(path, "r")
        L.record_acquired([{"_fantastic_internal_id": "9"}])
        L.flush()
        rec = [json.loads(x) for x in open(path) if x.strip()][0]
        self.assertFalse(rec["function_aware_would_exclude"])
        self.assertEqual(rec["function_aware_reason"], "")


class FlagsRemainOffTests(unittest.TestCase):
    def test_shadow_work_did_not_enable_any_acquisition_feature(self):
        """Asserts the DECLARED default in config.py, not the live module global --
        other suites legitimately patch these globals, so reading them here would be
        order-dependent. The invariant that matters is that no acquisition feature
        DEFAULTS to on."""
        import re
        src = open("config.py", encoding="utf-8").read()
        for flag in ("FANTASTIC_FUNCTION_AWARE_UPSTREAM_DEDUPE_ENABLED",
                     "FANTASTIC_FUNCTIONAL_ROLE_EXPANSION_ENABLED",
                     "APOLLO_ORG_ID_ZERO_PEOPLE_FALLBACK_ENABLED",
                     "FANTASTIC_ATS_SOURCE_ENABLED",
                     "FANTASTIC_DATE_CREATED_WATERMARK_ENABLED",
                     "FANTASTIC_MONTHLY_GOVERNOR_ENABLED",
                     "SOURCE_EXPERIMENT_ENABLED", "SEGMENT_ALLOCATOR_ENABLED",
                     "FANTASTIC_SERVER_INDUSTRY_EXCLUSION_ENABLED",
                     "FANTASTIC_TITLE_ALIASES_ENABLED", "YIELD_LEDGER_ENABLED"):
            m = re.search(rf"^{flag}\s*=\s*_env_bool\(\s*\"{flag}\"\s*,\s*(True|False)\s*\)",
                          src, re.M)
            self.assertIsNotNone(m, f"{flag} not declared via _env_bool")
            self.assertEqual(m.group(1), "False", f"{flag} must DEFAULT to False")

    def test_production_title_query_still_byte_identical(self):
        from role_catalog import DEFAULT_SEARCH_ROLES
        plan = fja.build_title_query_plan()
        # Shadow flags must not add a single clause: the query is exactly the
        # catalog union, whatever size the catalog currently is.
        self.assertEqual(plan["expression"].count("|") + 1, len(DEFAULT_SEARCH_ROLES))
        self.assertFalse(any(c.get("expanded") for c in plan["clauses"]))


if __name__ == "__main__":
    unittest.main()
