"""Delivery: FINAL_PASS-only auto-approval, reconciliation, idempotency,
batch-failure fallback, rollback -- all against fake adapters."""

from __future__ import annotations

import tempfile
import unittest

from orchestrator.enrichment import Lead
from orchestrator.reasons import Disposition, ReasonCode
from orchestrator.modes import ExecutionMode, policy_for
from orchestrator.state import StateManager
from orchestrator.delivery import (
    DeliveryManager,
    FakeAirtableAdapter,
    FakeInstantlyAdapter,
)


def _lead(key, disp=Disposition.FINAL_PASS):
    return Lead(posting_id=key, company={"name": f"C{key}"}, contact={"email": f"{key}@x.com"},
                disposition=disp, primary_reason=ReasonCode.OK, contact_key=f"{key}@x.com")


def _mgr(tmp, *, airtable=None, instantly=None, write=True, approve=True, instantly_on=True):
    sm = StateManager(tmp, policy_for(ExecutionMode.FULL_DRY_RUN), run_id="RID")
    return DeliveryManager(state=sm, airtable=airtable or FakeAirtableAdapter(),
                           instantly=instantly or FakeInstantlyAdapter(),
                           enable_airtable_write=write, auto_approve=approve,
                           enable_instantly=instantly_on)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_dry_run_writes_nothing(self):
        m = _mgr(self.tmp, write=False, approve=False)
        rep = m.deliver([_lead("a"), _lead("b")])
        self.assertEqual(rep.created, 0)
        self.assertEqual(rep.skipped, 2)
        self.assertTrue(rep.reconciles())

    def test_reviewable_never_auto_approves(self):
        m = _mgr(self.tmp)
        rep = m.deliver([_lead("a", Disposition.NEEDS_CHECK),
                         _lead("b", Disposition.UNVERIFIED),
                         _lead("c", Disposition.FINAL_PASS)])
        self.assertEqual(rep.created, 1)                 # only the FINAL_PASS
        self.assertEqual(rep.skipped, 2)                 # both reviewable skipped
        self.assertIn(ReasonCode.NOT_FINAL_PASS.value, rep.skip_reasons)
        self.assertTrue(rep.reconciles())

    def test_created_skipped_failed_reconcile(self):
        m = _mgr(self.tmp)
        rep = m.deliver([_lead("a"), _lead("b"), _lead("c", Disposition.REJECT)])
        self.assertEqual(rep.entered, 3)
        self.assertEqual(rep.created + rep.skipped + rep.failed, 3)
        self.assertTrue(rep.reconciles())

    def test_enrolled_reconciles_to_auto_approved_final_pass(self):
        m = _mgr(self.tmp)
        rep = m.deliver([_lead("a"), _lead("b")])
        self.assertEqual(rep.enrolled, rep.created)
        self.assertEqual(rep.auto_approved_final_pass, 2)
        self.assertTrue(rep.enrollment_reconciles())

    def test_idempotency_across_runs(self):
        m1 = _mgr(self.tmp)
        m1.deliver([_lead("a")])
        m2 = _mgr(self.tmp)                     # new manager, same state dir
        rep = m2.deliver([_lead("a"), _lead("b")])
        self.assertEqual(rep.created, 1)        # 'a' already delivered
        self.assertIn(ReasonCode.ALREADY_DELIVERED.value, rep.skip_reasons)
        self.assertTrue(rep.reconciles())

    def test_batch_failure_falls_back_per_record(self):
        at = FakeAirtableAdapter(fail_batch=True, fail_records=frozenset({"b@x.com"}))
        m = _mgr(self.tmp, airtable=at)
        rep = m.deliver([_lead("a"), _lead("b"), _lead("c")])
        self.assertEqual(rep.created, 2)        # a and c via per-record fallback
        self.assertEqual(rep.failed, 1)         # b failed even per-record
        self.assertEqual(rep.created + rep.skipped + rep.failed, rep.entered)
        self.assertTrue(any(e["event"] == "batch_failed_fallback" for e in rep.audit))

    def test_rollback_clears_idempotency(self):
        m = _mgr(self.tmp)
        rep = m.deliver([_lead("a")])
        self.assertEqual(rep.created, 1)
        m.rollback(rep)
        m2 = _mgr(self.tmp)
        rep2 = m2.deliver([_lead("a")])         # can be delivered again after rollback
        self.assertEqual(rep2.created, 1)


if __name__ == "__main__":
    unittest.main()
