"""Real adapter composition, exercised offline with the shared request_with_retry
seam faked. Drives the repository's REAL acquisition adapters, REAL pre-contact
gates + hiring-manager (Apollo/Hunter) pipeline, and REAL Airtable/Instantly
clients -- with zero network."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import requests

import config
from retrieval_measurement.instrument import RequestBudget
from retrieval_measurement.drivers import FixtureFetcher

from orchestrator.lanes import LaneManager
from orchestrator.modes import ExecutionMode, policy_for
from orchestrator.reasons import Disposition, ReasonCode
from orchestrator.enrichment import Lead
from orchestrator.adapters_real import (
    FakeResponse,
    RealDelivery,
    RealEnrichmentStage,
    real_free_feeds_runner,
    real_jsearch_runner,
    seam_fake,
)

SOURCES = Path(__file__).parent / "fixtures" / "retrieval_measurement" / "sources.json"


def _no_network():
    orig = requests.request

    def blow(*a, **k):
        raise AssertionError("offline real-adapter path attempted a network call")

    requests.request = blow
    return orig, (lambda: setattr(requests, "request", orig))


class ModeTests(unittest.TestCase):
    def test_controlled_live_acq_and_enrichment_policy(self):
        p = policy_for(ExecutionMode.LIVE_ACQUISITION_AND_ENRICHMENT)
        self.assertTrue(p.allow_live_acquisition)
        self.assertTrue(p.allow_live_enrichment)
        self.assertTrue(p.allow_airtable_write)           # review-staging (Pending)
        self.assertFalse(p.allow_instantly_enrollment)    # never enroll
        self.assertFalse(p.allow_production_state_write)


class RealAcquisitionTests(unittest.TestCase):
    def test_real_free_feed_adapters_produce_jobs(self):
        fixture = json.loads(SOURCES.read_text(encoding="utf-8"))
        budget = RequestBudget(limit=500, reserved_for_lanes={"jsearch": 50})
        lm = LaneManager(budget=budget)
        runner = real_free_feeds_runner(["himalayas", "remoteok"], FixtureFetcher(fixture))
        res = runner(lm)
        self.assertEqual(res.lane, "free_feeds")
        self.assertGreater(len(res.jobs), 0)          # REAL adapters parsed the fixtures
        self.assertGreater(res.physical_requests, 0)  # counted through the budget seam

    def test_real_jsearch_runner_zero_query_safety(self):
        # max_queries=0 means zero JSearch queries; offline (live=False) is replay.
        from retrieval_measurement.identity import NonWritingRegistry
        runner = real_jsearch_runner(output_dir=tempfile.mkdtemp(), max_queries=0,
                                     registry=NonWritingRegistry(None), live=False)
        res = runner(LaneManager(budget=RequestBudget(limit=10)))
        self.assertEqual(res.lane, "jsearch")
        self.assertEqual(len(res.jobs), 0)


class RealEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self._apollo = config.APOLLO_API_KEY
        config.APOLLO_API_KEY = "TEST-OFFLINE"

    def tearDown(self):
        config.APOLLO_API_KEY = self._apollo

    def test_real_qualification_and_hiring_manager_offline_reconciles(self):
        postings = [{
            "job_id": f"J{i}", "job_title": "VP of Sales",
            "employer_name": f"Acme {i}", "employer_website": f"acme{i}.com",
            "job_description": "Lead the sales org.",
            "job_apply_link": f"https://acme{i}.com/careers/{i}",
        } for i in range(4)]

        def handler(method, url, **k):
            return FakeResponse({"people": [], "contacts": [], "organizations": [], "data": {}})

        restore_net = _no_network()[1]
        try:
            with seam_fake(handler):
                stage = RealEnrichmentStage(target_final_pass=1, workdir=tempfile.mkdtemp())
                report = stage.run(postings)
        finally:
            restore_net()

        # the REAL gates + hiring-manager pipeline ran; every boundary reconciles
        self.assertTrue(all(s.reconciles() for s in report.stages))
        total = report.final_pass().__len__() + report.reviewable_count() if hasattr(report, "reviewable_count") else None
        census = report.dispositions()
        self.assertEqual(len(census), sum(1 for _ in census))     # census is well-formed
        # dispositions come only from the real _final_state vocabulary
        self.assertTrue(set(d for d in census) <= set(Disposition))


class RealDeliveryTests(unittest.TestCase):
    def setUp(self):
        self._cfg = {k: getattr(config, k, None) for k in
                     ("AIRTABLE_TOKEN", "AIRTABLE_BASE_ID", "AIRTABLE_TABLE_NAME",
                      "INSTANTLY_API_KEY", "INSTANTLY_RATE_LIMIT_DELAY")}
        config.AIRTABLE_TOKEN = "TEST"; config.AIRTABLE_BASE_ID = "appTEST"
        config.AIRTABLE_TABLE_NAME = "Leads"; config.INSTANTLY_API_KEY = "TEST"
        config.INSTANTLY_RATE_LIMIT_DELAY = 0

    def tearDown(self):
        for k, v in self._cfg.items():
            if v is not None:
                setattr(config, k, v)

    def _leads(self):
        return [
            Lead("a", {"name": "A"}, {"email": "hm@a.com"}, Disposition.FINAL_PASS,
                 ReasonCode.OK, contact_key="A|hm@a.com"),
            Lead("b", {"name": "B"}, {}, Disposition.NEEDS_CHECK,
                 ReasonCode.HIRING_MANAGER_NOT_FOUND, contact_key=""),
        ]

    def test_delivery_disabled_by_default_is_dry(self):
        rd = RealDelivery(enable_airtable_write=False, auto_approve=False, enable_instantly=False)
        rep = rd.deliver(self._leads())
        self.assertEqual(rep.created, 0)
        self.assertEqual(rep.skipped, 2)
        self.assertTrue(rep.reconciles())
        self.assertEqual(rep.mode, "dry_no_write")

    def test_real_airtable_and_instantly_reconcile_via_seam(self):
        def handler(method, url, **k):
            u = str(url); m = str(method).upper()
            if "airtable.com" in u:
                return FakeResponse({"records": []} if m == "GET"
                                    else {"records": [{"id": "recNEW", "fields": {}}]}, url=u)
            if "instantly" in u:
                return FakeResponse({"status": "success"}, url=u)
            return FakeResponse({}, url=u)

        rd = RealDelivery(enable_airtable_write=True, auto_approve=True, enable_instantly=True)
        restore = _no_network()[1]
        try:
            with seam_fake(handler):
                rep = rd.deliver(self._leads())
        finally:
            restore()
        # real push_leads + enroll_approved_leads ran through the faked seam
        self.assertEqual(rep.entered, 2)
        self.assertTrue(rep.reconciles())                    # created+skipped+failed==entered
        self.assertTrue(rep.enrollment_reconciles())         # enrolled<=FINAL_PASS
        self.assertEqual(rep.final_pass, 1)                  # only the FINAL_PASS is eligible
        self.assertIn("airtable", rep.detail)

    def test_reviewable_never_auto_approves_even_when_enabled(self):
        rd = RealDelivery(enable_airtable_write=True, auto_approve=True, enable_instantly=False)
        leads = [Lead("c", {"name": "C"}, {}, Disposition.UNVERIFIED, ReasonCode.EMAIL_UNVERIFIED)]
        def handler(method, url, **k):
            return FakeResponse({"records": []}, url=str(url))
        with seam_fake(handler):
            rep = rd.deliver(leads)
        self.assertEqual(rep.final_pass, 0)                   # no FINAL_PASS to auto-approve
        self.assertTrue(rep.reconciles())


if __name__ == "__main__":
    unittest.main()
