"""Apollo failure-containment hotfix (2026-08-06 production incident).

A single organization-level HTTP 422 for ``vaco.com`` was misclassified as
``ApolloCreditsExhaustedError`` and aborted a full ~35-minute run. These tests
pin the corrected behavior:

* a generic 422 is a record-level VALIDATION error, never credit exhaustion;
* only an explicit credit body -> CREDIT_EXHAUSTED; 401/403 -> AUTHORIZATION;
  429 / long retry window -> RATE_LIMIT -- all kept strictly distinct;
* a sanitized, secret-free evidence artifact is captured for each failure;
* one company's failure cannot terminate the batch, and the affected company
  cannot become FINAL_PASS without evidence;
* a whole-account outage opens an Apollo-only circuit that preserves completed
  work instead of crashing;
* a resumed run repeats neither completed acquisition nor enriched companies,
  while a failed company stays recoverable.

Everything here is zero-network: ``requests.request`` is patched to raise, and the
provider seam ``apollo_client.request_with_retry`` / ``process_company`` is faked.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

import apollo_client
import apollo_errors
import config
import hiring_manager
import http_utils
from apollo_errors import ApolloErrorCategory, classify_apollo_error
from orchestrator.modes import ExecutionMode, policy_for


def _http_error(status: int, body: str = "", *, retry_after: str | None = None,
                url: str = "https://api.apollo.io/api/v1/organizations/enrich") -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    response.url = url
    response._content = body.encode("utf-8")
    response.request = requests.Request("GET", url).prepare()
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    return requests.HTTPError(f"HTTP {status}", response=response)


# --------------------------------------------------------------------------
# (1)(2)(9) Classification
# --------------------------------------------------------------------------
class ClassificationTests(unittest.TestCase):
    def test_generic_422_is_validation_not_credit(self):
        c = classify_apollo_error(_http_error(
            422, '{"error": "domain is not a valid company identifier"}'))
        self.assertEqual(c.category, ApolloErrorCategory.VALIDATION)
        self.assertFalse(c.global_fatal)
        self.assertNotEqual(c.category, ApolloErrorCategory.CREDIT_EXHAUSTED)

    def test_explicit_credit_body_is_credit_exhausted(self):
        for status in (422, 403, 200):
            c = classify_apollo_error(_http_error(
                status, "Your team's shared credits are used up. Buy more credits."))
            self.assertEqual(c.category, ApolloErrorCategory.CREDIT_EXHAUSTED, status)
            self.assertTrue(c.global_fatal)

    def test_401_403_are_authorization_not_credit(self):
        for status in (401, 403):
            c = classify_apollo_error(_http_error(status, '{"error":"unauthorized"}'))
            self.assertEqual(c.category, ApolloErrorCategory.AUTHORIZATION, status)
            self.assertTrue(c.global_fatal)
            self.assertNotEqual(c.category, ApolloErrorCategory.CREDIT_EXHAUSTED)

    def test_429_and_long_window_are_rate_limit(self):
        c429 = classify_apollo_error(_http_error(429, "too many requests"))
        self.assertEqual(c429.category, ApolloErrorCategory.RATE_LIMIT)
        self.assertTrue(c429.global_fatal)
        window = http_utils.RetryWindowTooLong(
            "window too long",
            response=_http_error(429, "slow down", retry_after="1900").response,
            retry_after=1900.0,
        )
        cwin = classify_apollo_error(window)
        self.assertEqual(cwin.category, ApolloErrorCategory.RATE_LIMIT)
        self.assertEqual(cwin.retry_after, 1900.0)
        self.assertNotEqual(cwin.category, ApolloErrorCategory.CREDIT_EXHAUSTED)

    def test_5xx_is_server_and_record_level(self):
        c = classify_apollo_error(_http_error(503, "upstream unavailable"))
        self.assertEqual(c.category, ApolloErrorCategory.SERVER)
        self.assertFalse(c.global_fatal)


# --------------------------------------------------------------------------
# (3) Sanitized evidence artifact
# --------------------------------------------------------------------------
class EvidenceArtifactTests(unittest.TestCase):
    def test_422_body_is_captured_and_sanitized(self):
        body = ('{"error_code": "VALIDATION_ERROR", "error": "bad domain", '
                '"api_key": "sk-live-SHOULD-NOT-PERSIST"}')
        classification = classify_apollo_error(_http_error(422, body))
        record = apollo_errors.build_error_record(
            classification, company_key="vaco.com", domain="vaco.com",
            retry_decision="retry_domain_only", final_outcome="unresolved_organization")
        with tempfile.TemporaryDirectory() as d:
            path = apollo_errors.write_error_artifact(d, record)
            self.assertIsNotNone(path)
            written = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual(written["http_status"], 422)
        self.assertEqual(written["classification"], "validation")
        self.assertEqual(written["apollo_error_code"], "VALIDATION_ERROR")
        self.assertEqual(written["domain"], "vaco.com")
        # The secret Apollo echoed back must not survive into the artifact.
        self.assertNotIn("sk-live-SHOULD-NOT-PERSIST", json.dumps(written))


# --------------------------------------------------------------------------
# (4) Bounded domain-only retry (real enrich_organization)
# --------------------------------------------------------------------------
class BoundedRetryTests(unittest.TestCase):
    def setUp(self):
        self._log = config.LOG_DIR
        self._tmp = tempfile.mkdtemp()
        config.LOG_DIR = self._tmp

    def tearDown(self):
        config.LOG_DIR = self._log

    @mock.patch("apollo_client.request_with_retry")
    def test_valid_domain_retries_at_most_once_then_continues(self, mocked):
        mocked.side_effect = [_http_error(422), _http_error(422)]
        result = apollo_client.enrich_organization(
            domain="vaco.com", name="Vaco by Highspring", website="https://vaco.com")
        self.assertFalse(result.found)
        self.assertEqual(result.domain, "vaco.com")   # input identity preserved
        self.assertEqual(mocked.call_count, 2)         # at most one retry

    @mock.patch("apollo_client.request_with_retry")
    def test_explicit_credit_body_stops_without_retry(self, mocked):
        mocked.side_effect = _http_error(422, "shared credits are used up")
        with self.assertRaises(apollo_client.ApolloCreditsExhaustedError):
            apollo_client.enrich_organization(
                domain="vaco.com", name="Vaco", website="https://vaco.com")
        self.assertEqual(mocked.call_count, 1)


# --------------------------------------------------------------------------
# Batch containment / circuit breaker / resume (drives the enrichment loop)
# --------------------------------------------------------------------------
def _job(company: str, domain: str) -> dict:
    return {
        "job_id": f"job-{company}",
        "employer_name": company,
        "employer_website": f"https://{domain}",
        "job_title": "VP Marketing",
        "_job_gate_state": "PASS",
    }


def _success_leads(company: str, domain: str, *, final_pass: bool):
    state = "FINAL_PASS" if final_pass else "UNVERIFIED"
    lead = {
        "employer_name": company,
        "_role_bucket": "marketing",
        "_account_gate_state": "PASS",
        "_final_state": state,
        "_step3_status": "found" if final_pass else "unverified",
        "hiring_manager_name": "Jane Doe" if final_pass else None,
        "hiring_manager_email": f"jane@{domain}" if final_pass else None,
        "lead_key": f"{domain}|jane@{domain}|marketing" if final_pass else None,
    }
    return [lead], {f"final_{state.lower()}": 1}


class _BatchHarness(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(config, k, None) for k in
                       ("APOLLO_API_KEY", "FINAL_PASS_PIPELINE_ENABLED",
                        "STEP3_OUTPUT_DIR", "LOG_DIR", "ENFORCE_HM_MATCH_RATE",
                        "CONTINUE_AFTER_FINAL_PASS_TARGET")}
        config.APOLLO_API_KEY = "test-key"
        config.FINAL_PASS_PIPELINE_ENABLED = True
        config.ENFORCE_HM_MATCH_RATE = False
        config.CONTINUE_AFTER_FINAL_PASS_TARGET = True
        self.tmp = tempfile.mkdtemp()
        config.STEP3_OUTPUT_DIR = self.tmp
        config.LOG_DIR = self.tmp

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)

    def _input(self, jobs) -> str:
        p = Path(self.tmp) / "input.json"
        p.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
        return str(p)

    def _leads(self, result):
        return json.loads(Path(result.output_path).read_text(encoding="utf-8"))["jobs"]


# (5)(7) One company's failure does not terminate the batch; others continue.
class BatchContainmentTests(_BatchHarness):
    def test_one_company_record_failure_does_not_terminate_batch(self):
        jobs = [_job("Alpha", "alpha.com"), _job("Bravo", "bravo.com")]

        def dispatch(company_jobs):
            name = company_jobs[0]["employer_name"]
            if name == "Alpha":
                raise _http_error(500, "upstream boom")  # record-level, not global
            return _success_leads("Bravo", "bravo.com", final_pass=True)

        with mock.patch.object(hiring_manager, "process_company", side_effect=dispatch):
            result = hiring_manager.run_hiring_manager_identification(self._input(jobs))

        leads = self._leads(result)
        names = {l.get("employer_name") for l in leads}
        self.assertIn("Alpha", names)   # degraded, still present
        self.assertIn("Bravo", names)   # continued and processed
        alpha = next(l for l in leads if l.get("employer_name") == "Alpha")
        self.assertEqual(alpha["_final_state"], "UNVERIFIED")
        self.assertTrue(alpha.get("_apollo_enrichment_failed"))
        self.assertEqual(result.total_output_leads, len(leads))

    # (6) The failed company can never be FINAL_PASS without evidence.
    def test_degraded_company_cannot_become_final_pass(self):
        leads, _ = hiring_manager._degraded_company_leads(
            [_job("Alpha", "alpha.com")], reason="apollo_record_error")
        self.assertEqual(leads[0]["_final_state"], "UNVERIFIED")
        self.assertNotEqual(leads[0]["_final_state"], "FINAL_PASS")
        self.assertIsNone(leads[0]["hiring_manager_email"])
        self.assertFalse(hiring_manager._is_final_pass_lead(leads[0]))


# (10)(13) Circuit breaker preserves completed work; failed company recoverable.
class CircuitBreakerTests(_BatchHarness):
    def test_global_fatal_opens_circuit_preserves_completed_no_crash(self):
        jobs = [_job("Alpha", "alpha.com"), _job("Bravo", "bravo.com"),
                _job("Charlie", "charlie.com")]

        def dispatch(company_jobs):
            name = company_jobs[0]["employer_name"]
            if name == "Alpha":
                return _success_leads("Alpha", "alpha.com", final_pass=True)
            if name == "Bravo":
                raise apollo_client.ApolloCreditsExhaustedError("credits gone")
            raise AssertionError("Charlie must NOT be processed after the circuit opens")

        with mock.patch.object(hiring_manager, "process_company", side_effect=dispatch):
            result = hiring_manager.run_hiring_manager_identification(self._input(jobs))

        # No crash: a reconciled Step3Result is returned.
        payload = json.loads(Path(result.output_path).read_text(encoding="utf-8"))
        self.assertEqual(payload["stop_reason"], "apollo_circuit_open")
        names = {l.get("employer_name") for l in payload["jobs"]}
        self.assertIn("Alpha", names)          # completed work preserved
        self.assertNotIn("Charlie", names)     # no further Apollo calls

        # Only the successfully-enriched company is checkpointed; the company that
        # tripped the circuit is NOT, so it stays recoverable on the next run.
        ckpt = json.loads((Path(self.tmp) / "enrichment_progress.json").read_text("utf-8"))
        self.assertIn("alpha.com", ckpt["companies"])
        self.assertNotIn("bravo.com", ckpt["companies"])
        self.assertNotIn("charlie.com", ckpt["companies"])


# (12) Resume reuses enriched companies without calling Apollo again.
class ResumeReuseTests(_BatchHarness):
    def test_resume_does_not_re_enrich_completed_company(self):
        jobs = [_job("Alpha", "alpha.com")]

        with mock.patch.object(hiring_manager, "process_company",
                               side_effect=lambda cj: _success_leads("Alpha", "alpha.com", final_pass=True)) as first:
            hiring_manager.run_hiring_manager_identification(self._input(jobs))
        self.assertEqual(first.call_count, 1)

        # Second run over the SAME checkpoint dir: process_company must not be
        # called again for the already-enriched company.
        def explode(company_jobs):
            raise AssertionError("completed company must not be re-enriched")

        with mock.patch.object(hiring_manager, "process_company", side_effect=explode):
            result = hiring_manager.run_hiring_manager_identification(self._input(jobs))
        stats = json.loads(Path(result.output_path).read_text("utf-8"))["stats"]
        self.assertGreaterEqual(stats.get("enrichment_resume_reused_companies", 0), 1)


# (11) Resume does not repeat completed acquisition (orchestrator checkpoint).
class AcquisitionResumeTests(unittest.TestCase):
    def test_resume_reuses_acquisition_checkpoint_without_refetch(self):
        from orchestrator.modes import ExecutionMode as EM, policy_for as pf
        from orchestrator.runcontrol import RunContext
        from orchestrator.state import StateManager
        from orchestrator.lanes import LaneResult
        from orchestrator.pipeline import Orchestrator, OrchestratorPlan
        from orchestrator.enrichment import EnrichmentReport

        tmp = tempfile.mkdtemp()
        policy = pf(EM.FULL_DRY_RUN)
        ctx = RunContext.create(EM.FULL_DRY_RUN, {"mode": "full_dry_run"}, run_id="20260806T000000Z-resume")
        state = StateManager(tmp, policy, run_id=ctx.run_id)

        class _Budget:
            lane = source = None
            def reserve(self, *a, **k): return True
            def to_dict(self): return {}

        calls = {"n": 0}
        def runner(_manager):
            calls["n"] += 1
            return LaneResult(lane="acq", status="complete",
                              jobs=[{"job_id": "j1", "posting_id": "j1"}])

        class _Engine:
            def run(self, opportunities, **k):
                return EnrichmentReport(leads=[], stages=[], loss_census={})

        class _Delivery:
            def deliver(self, leads, **k):
                from orchestrator.adapters_real import RealDeliveryReport
                return RealDeliveryReport(entered=0)

        plan = OrchestratorPlan(lanes=["acq"], lane_runners={"acq": runner},
                                enrichment_engine=_Engine(), delivery_manager=_Delivery())

        Orchestrator(ctx, state, _Budget()).run(plan, resume=False)
        self.assertEqual(calls["n"], 1)
        # Resume: the acquisition runner must not be invoked again.
        ctx2 = RunContext.create(EM.FULL_DRY_RUN, {"mode": "full_dry_run"}, run_id=ctx.run_id)
        Orchestrator(ctx2, state, _Budget()).run(plan, resume=True)
        self.assertEqual(calls["n"], 1)


# --------------------------------------------------------------------------
# (8) 429 bounded Retry-After handling in the shared HTTP layer
# --------------------------------------------------------------------------
class RateLimitBoundedRetryTests(unittest.TestCase):
    @mock.patch("http_utils.time.sleep")
    @mock.patch("http_utils.requests.request")
    def test_long_retry_after_fails_fast_as_rate_limit(self, mocked_request, mocked_sleep):
        resp = requests.Response()
        resp.status_code = 429
        resp.url = "https://api.apollo.io/api/v1/organizations/enrich"
        resp._content = b"rate limited"
        resp.request = requests.Request("GET", resp.url).prepare()
        resp.headers["Retry-After"] = "1900"
        mocked_request.return_value = resp
        with self.assertRaises(http_utils.RetryWindowTooLong):
            http_utils.request_with_retry("GET", resp.url, max_retries=3)
        mocked_sleep.assert_not_called()
        self.assertEqual(mocked_request.call_count, 1)


# --------------------------------------------------------------------------
# (14)(15)(16)(17) Preserved production guarantees + zero-network
# --------------------------------------------------------------------------
class PreservedBehaviorTests(unittest.TestCase):
    def test_airtable_review_staging_and_instantly_off(self):
        p = policy_for(ExecutionMode.LIVE_ACQUISITION_AND_ENRICHMENT)
        self.assertTrue(p.allow_airtable_write)          # review-staging enabled
        self.assertFalse(p.allow_instantly_enrollment)   # Instantly disabled
        self.assertFalse(p.allow_production_state_write)

    def test_review_staging_never_enrolls_instantly(self):
        from orchestrator.adapters_real import RealDelivery, _REVIEWABLE
        from orchestrator.reasons import Disposition
        self.assertNotIn(Disposition.REJECT, _REVIEWABLE)
        self.assertNotIn(Disposition.REROUTE, _REVIEWABLE)
        d = RealDelivery(enable_airtable_write=True, auto_approve=False, enable_instantly=False)
        self.assertFalse(d.auto_approve)
        self.assertFalse(d.enable_instantly)

    def test_package_integrity_passes(self):
        import run_orchestrator as R
        if not Path("orchestrator.MANIFEST.sha256").is_file():
            self.skipTest("manifest not present in CWD")
        res, _ = R._preflight_checks(
            R.build_parser().parse_args(["--artifact-root", tempfile.mkdtemp()]))
        self.assertTrue(res["integrity_ok"])

    def test_classification_and_containment_touch_no_network(self):
        orig = requests.request
        requests.request = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network!"))
        try:
            c = classify_apollo_error(_http_error(422, '{"error":"bad"}'))
            self.assertEqual(c.category, ApolloErrorCategory.VALIDATION)
            leads, _ = hiring_manager._degraded_company_leads(
                [{"employer_name": "X", "employer_website": "https://x.com"}],
                reason="apollo_record_error")
            self.assertEqual(leads[0]["_final_state"], "UNVERIFIED")
        finally:
            requests.request = orig


if __name__ == "__main__":
    unittest.main()
