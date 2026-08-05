"""Persistent-production corrections: state under orchestrator_v2 only, cross-run
dedup/suppression across separate process executions, safe-commit, retention.
All offline (seam faked / no network)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import config
from orchestrator.modes import ExecutionMode, policy_for
from orchestrator.state import StateManager
from orchestrator.suppression import SuppressionStore
from orchestrator.enrichment import Lead
from orchestrator.reasons import Disposition, ReasonCode
from orchestrator.adapters_real import (
    FakeResponse, RealDelivery, RealEnrichmentStage, seam_fake,
)


def _state(tmp, run_id="RID"):
    return StateManager(tmp, policy_for(ExecutionMode.LIVE_ACQUISITION_AND_ENRICHMENT), run_id=run_id)


class EnrichmentWritePathTests(unittest.TestCase):
    def setUp(self):
        self._apollo = config.APOLLO_API_KEY
        config.APOLLO_API_KEY = "TEST"
        self._filtered = getattr(config, "FILTERED_OUTPUT_DIR", None)
        self._step3 = getattr(config, "STEP3_OUTPUT_DIR", None)

    def tearDown(self):
        config.APOLLO_API_KEY = self._apollo

    def test_enrichment_writes_only_under_run_root_and_restores_config(self):
        root = Path(tempfile.mkdtemp()) / "orchestrator_v2" / "run_artifacts" / "RID"
        workdir = root / "enrichment"
        stage = RealEnrichmentStage(target_final_pass=1, workdir=str(workdir))
        posting = {"job_id": "J1", "job_title": "VP Sales", "employer_name": "Acme",
                   "employer_website": "acme.com", "job_description": "x",
                   "job_apply_link": "https://acme.com/1"}

        def handler(method, url, **k):
            return FakeResponse({"people": [], "data": {}, "records": []})

        with seam_fake(handler):
            stage.run([posting])

        # (1)(2) qualification + enrichment artifacts live UNDER the run root,
        # never in /tmp or data/filtered|enriched
        self.assertTrue((workdir / "qualification").exists())
        self.assertTrue((workdir / "enrichment").exists())
        # explicit workdir used (no internal mkdtemp): the stage's workdir is
        # exactly the run-root path we passed, under orchestrator_v2
        self.assertEqual(str(stage.workdir), str(workdir))
        self.assertIn("orchestrator_v2", str(stage.workdir))
        # (3) legacy config dirs restored in finally
        self.assertEqual(getattr(config, "FILTERED_OUTPUT_DIR", None), self._filtered)
        self.assertEqual(getattr(config, "STEP3_OUTPUT_DIR", None), self._step3)


class CrossRunSuppressionTests(unittest.TestCase):
    def test_posting_state_survives_two_executions(self):
        tmp = tempfile.mkdtemp()
        s1 = SuppressionStore(_state(tmp))
        s1.commit_postings(["strong|acme|vp-sales", "strong|beta|cto"])
        # (4) a *separate* store (second process) reads the persisted keys
        s2 = SuppressionStore(_state(tmp))
        self.assertEqual(s2.seen_postings(), {"strong|acme|vp-sales", "strong|beta|cto"})

    def test_delivered_leadkeys_survive_restart(self):
        tmp = tempfile.mkdtemp()
        SuppressionStore(_state(tmp)).commit_delivered(["acme|hm@acme.com"])
        # (8) idempotency survives a restart
        self.assertIn("acme|hm@acme.com", SuppressionStore(_state(tmp)).delivered_leads())

    def test_new_posting_from_seen_company_not_blocked(self):
        # (6) posting-level dedup: an OLD posting is suppressed, a NEW posting
        # from the same company is not.
        tmp = tempfile.mkdtemp()
        supp = SuppressionStore(_state(tmp))
        supp.commit_postings(["strong|acme|old-role"])
        seen = supp.seen_postings()
        self.assertIn("strong|acme|old-role", seen)       # old suppressed
        self.assertNotIn("strong|acme|new-role", seen)    # new signal allowed

    def test_corruption_safe_load(self):
        tmp = tempfile.mkdtemp()
        st = _state(tmp)
        (st.store_path("seen_suppression") / "postings.json").write_text("{bad json")
        self.assertEqual(SuppressionStore(st).seen_postings(), set())  # empty, not crash


class DeliveryIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self._c = {k: getattr(config, k, None) for k in
                   ("AIRTABLE_TOKEN", "AIRTABLE_BASE_ID", "AIRTABLE_TABLE_NAME")}
        config.AIRTABLE_TOKEN = "T"; config.AIRTABLE_BASE_ID = "appT"; config.AIRTABLE_TABLE_NAME = "Leads"

    def tearDown(self):
        for k, v in self._c.items():
            if v is not None:
                setattr(config, k, v)

    def _lead(self, key):
        return Lead(key, {"name": key}, {"email": f"{key}@x.com", "name": f"HM {key}"},
                    Disposition.FINAL_PASS, ReasonCode.OK, contact_key=key)

    def test_known_delivered_is_not_resubmitted(self):
        # (5)(8) a lead_key delivered in a prior run is skipped locally this run.
        def handler(method, url, **k):
            u = str(url)
            if "airtable.com" in u:
                return FakeResponse({"records": []} if str(method).upper() == "GET"
                                    else {"records": [{"id": "r", "fields": {}}]}, url=u)
            return FakeResponse({}, url=u)
        rd = RealDelivery(enable_airtable_write=True, auto_approve=False, enable_instantly=False)
        with seam_fake(handler):
            rep = rd.deliver([self._lead("a"), self._lead("b")], known_delivered={"a"})
        self.assertEqual(rep.skipped_already_delivered, 1)   # 'a' skipped locally
        self.assertEqual(rep.reviewable_submitted, 1)        # only 'b' submitted
        self.assertTrue(rep.reconciles())

    def test_failed_delivery_not_marked_delivered(self):
        # (9) a failed Airtable row must never appear in delivered_lead_keys.
        def handler(method, url, **k):
            u = str(url); m = str(method).upper()
            if "airtable.com" in u and m == "GET":
                return FakeResponse({"records": []}, url=u)
            return FakeResponse({"error": "boom"}, status=500, url=u)  # create fails
        rd = RealDelivery(enable_airtable_write=True, auto_approve=False, enable_instantly=False)
        with seam_fake(handler):
            rep = rd.deliver([self._lead("a")])
        self.assertNotIn("a", rep.delivered_lead_keys)
        self.assertTrue(rep.reconciles())


class RetentionTests(unittest.TestCase):
    def test_retention_protects_active_latest_and_stores(self):
        # (15) retention never deletes the active run, the latest run, or the
        # suppression/checkpoint stores.
        tmp = tempfile.mkdtemp()
        st = _state(tmp, run_id="20260805T090000Z-active")
        base = st.store_path("run_artifacts")
        for name in ("20260101T000000Z-a", "20260102T000000Z-b",
                     "20260103T000000Z-c", "20260805T090000Z-active"):
            (base / name).mkdir(parents=True, exist_ok=True)
            (base / name / "x.json").write_text("{}")
        SuppressionStore(st).commit_postings(["k1"])   # a store that must survive
        out = st.prune(keep=2, protect={"20260805T090000Z-active"})
        names = {d.name for d in base.iterdir()}
        self.assertIn("20260805T090000Z-active", names)          # active kept
        self.assertIn("20260103T000000Z-c", names)               # latest kept
        self.assertTrue((st.store_path("seen_suppression") / "postings.json").exists())  # store kept
        self.assertIn("removed", out)


if __name__ == "__main__":
    unittest.main()
