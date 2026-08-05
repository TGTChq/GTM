"""Airtable review-staging: write the reviewable set as Status=Pending for manual
review, never auto-approve, never enroll in Instantly. Real airtable_client driven
through the faked request_with_retry seam -- zero network."""

from __future__ import annotations

import unittest

import config
from orchestrator.enrichment import Lead
from orchestrator.reasons import Disposition, ReasonCode
from orchestrator.adapters_real import FakeResponse, RealDelivery, seam_fake


def _lead(key, disp, email="hm@x.com"):
    contact = {"email": email, "name": f"HM {key}"} if email else {}
    return Lead(key, {"name": f"Co-{key}"}, contact,
                disp, ReasonCode.OK if disp is Disposition.FINAL_PASS else ReasonCode.HIRING_MANAGER_NOT_FOUND,
                contact_key=key)


class ReviewStagingTests(unittest.TestCase):
    def setUp(self):
        self._c = {k: getattr(config, k, None) for k in
                   ("AIRTABLE_TOKEN", "AIRTABLE_BASE_ID", "AIRTABLE_TABLE_NAME", "INSTANTLY_API_KEY")}
        config.AIRTABLE_TOKEN = "T"; config.AIRTABLE_BASE_ID = "appT"
        config.AIRTABLE_TABLE_NAME = "Leads"; config.INSTANTLY_API_KEY = "T"

    def tearDown(self):
        for k, v in self._c.items():
            if v is not None:
                setattr(config, k, v)

    def _leads(self):
        return [
            _lead("a", Disposition.FINAL_PASS),
            _lead("b", Disposition.NEEDS_CHECK, email=""),
            _lead("c", Disposition.REJECT),          # must NOT be written
            _lead("d", Disposition.UNVERIFIED, email=""),
        ]

    def _seam(self, existing=(), fail_batch=False):
        urls = []
        def handler(method, url, **k):
            urls.append((str(method).upper(), str(url)))
            u = str(url)
            if "airtable.com" in u:
                if str(method).upper() == "GET":
                    recs = [{"id": f"rec-{key}", "fields": {"Lead Key": key, "Status": "Pending"}}
                            for key in existing]
                    return FakeResponse({"records": recs}, url=u)
                if fail_batch:
                    return FakeResponse({"error": "batch failed"}, status=500, url=u)
                return FakeResponse({"records": [{"id": "recNEW", "fields": {}}]}, url=u)
            if "instantly" in u:
                raise AssertionError("Instantly must never be called in review-staging")
            return FakeResponse({}, url=u)
        return handler, urls

    def test_mode_is_review_staging_and_instantly_never_called(self):
        rd = RealDelivery(enable_airtable_write=True, auto_approve=False, enable_instantly=True)
        handler, urls = self._seam()
        with seam_fake(handler):
            rep = rd.deliver(self._leads(), run_id="RID", source="ats")
        self.assertEqual(rep.mode, "review_staging")
        # reviewable set = FINAL_PASS + NEEDS_CHECK + UNVERIFIED (REJECT excluded)
        self.assertEqual(rep.reviewable_submitted, 3)
        self.assertEqual(rep.final_pass, 1)
        self.assertEqual(rep.needs_check, 1)
        self.assertTrue(rep.instantly_untouched())
        self.assertEqual(rep.enrolled, 0)
        self.assertFalse(any("instantly" in u for _, u in urls))   # never contacted

    def test_reconciliation_holds(self):
        rd = RealDelivery(enable_airtable_write=True, auto_approve=False, enable_instantly=False)
        handler, _ = self._seam()
        with seam_fake(handler):
            rep = rd.deliver(self._leads(), run_id="RID")
        self.assertTrue(rep.reconciles())            # entered == created + skipped + failed
        self.assertTrue(rep.reviewable_reconciles()) # reviewable == created+skipped_existing+failed(+other)

    def test_idempotency_no_duplicate_row_for_existing_lead(self):
        rd = RealDelivery(enable_airtable_write=True, auto_approve=False, enable_instantly=False)
        handler, _ = self._seam(existing=("a",))     # lead 'a' already in Airtable
        with seam_fake(handler):
            rep = rd.deliver(self._leads(), run_id="RID")
        created_keys = rep.detail["airtable"].get("created_lead_keys", [])
        self.assertNotIn("a", created_keys)          # idempotent: no duplicate row for 'a'
        self.assertTrue(rep.reviewable_reconciles())

    def test_batch_failure_preserves_failed_rows_and_reconciles(self):
        rd = RealDelivery(enable_airtable_write=True, auto_approve=False, enable_instantly=False)
        handler, _ = self._seam(fail_batch=True)
        with seam_fake(handler):
            rep = rd.deliver(self._leads(), run_id="RID")
        # an Airtable failure must not crash; entered still reconciles
        self.assertTrue(rep.reconciles())
        self.assertIn("airtable", rep.detail)

    def test_dry_when_airtable_disabled(self):
        rd = RealDelivery(enable_airtable_write=False, auto_approve=False, enable_instantly=False)
        rep = rd.deliver(self._leads(), run_id="RID")
        self.assertEqual(rep.mode, "dry_no_write")
        self.assertEqual(rep.created, 0)
        self.assertTrue(rep.reconciles())
        self.assertTrue(rep.instantly_untouched())


if __name__ == "__main__":
    unittest.main()
