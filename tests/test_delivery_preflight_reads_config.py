"""The delivery preflight must report the configuration, not a fixed sentence.

Every run printed, unconditionally:

    delivery             airtable=review-staging(Pending) auto_approve=OFF instantly=OFF

and headed the delivery block "Airtable (review-staging, Status=Pending)". Both were
hardcoded literals. They described nothing, and they were read as evidence: the
2026-09-04 run was reported here as having created 781 *Pending* rows awaiting
manual approval, when `FANTASTIC_AUTO_APPROVE_SEND_SAFE` was on the whole time and a
send-safe Fantastic row is created **Approved** at creation.

Two different things are called auto-approve, which is what made the fixed string
plausible:

  * the ``--auto-approve`` CLI flag -- picks WHICH leads are submitted (FINAL_PASS
    only) and whether they enrol directly;
  * ``FANTASTIC_AUTO_APPROVE_SEND_SAFE`` -- decides the STATUS a created record
    gets, Approved rather than Pending, when the stored facts are send-safe.

The fixed line described the first and read as though it settled the second.
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from unittest import mock

import config
import run_orchestrator


def _args(**over):
    ns = argparse.Namespace(airtable_write=True, auto_approve=False, instantly=False,
                            artifact_root=tempfile.mkdtemp(), resume=False)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def _fantastic_lead(fantastic=True):
    """A lead shaped as the enrichment stage emits it.

    `_final_state` is what puts `_job_to_fields` on the STRICT path, and the
    auto-approval branch lives there -- a bare dict never reaches it.
    """
    job = {
        "_final_state": "FINAL_PASS",
        "job_title": "Staff Accountant",
        "employer_name": "Acme",
        "canonical_employer_name": "Acme",
        "canonical_source_url": "https://acme.com/jobs/1",
        "hiring_manager_email": "hm@acme.com",
        "hiring_manager_name": "HM",
    }
    if fantastic:
        job["_fantastic_internal_id"] = "abc123"
    else:
        job["job_id"] = "ats_1"
    return job


def _delivery_lines(**cfg):
    with mock.patch.multiple(config, **cfg):
        _res, lines = run_orchestrator._preflight_checks(_args())
    return [ln for ln in lines if ln.startswith("delivery") or "record_status" in ln]


class TheDeliveryLineReportsRealSettings(unittest.TestCase):
    def test_send_safe_auto_approval_on_is_reported_as_approved(self):
        lines = "\n".join(_delivery_lines(FANTASTIC_AUTO_APPROVE_SEND_SAFE=True))
        self.assertIn("send_safe_auto_approve=ON", lines)
        self.assertIn("created Approved", lines)

    def test_send_safe_auto_approval_off_is_reported_as_pending(self):
        lines = "\n".join(_delivery_lines(FANTASTIC_AUTO_APPROVE_SEND_SAFE=False))
        self.assertIn("send_safe_auto_approve=OFF", lines)
        self.assertIn("created Pending", lines)

    def test_the_two_auto_approvals_are_reported_separately(self):
        """The CLI flag selects the submit set; it says nothing about record status.
        Reporting them on one line as one fact is what caused the misreading."""
        with mock.patch.object(config, "FANTASTIC_AUTO_APPROVE_SEND_SAFE", True):
            _r, lines = run_orchestrator._preflight_checks(_args(auto_approve=True))
        text = "\n".join(ln for ln in lines if "delivery" in ln or "record_status" in ln)
        self.assertIn("submit_set=final_pass_only", text)
        self.assertIn("send_safe_auto_approve=ON", text)

        with mock.patch.object(config, "FANTASTIC_AUTO_APPROVE_SEND_SAFE", True):
            _r2, lines2 = run_orchestrator._preflight_checks(_args(auto_approve=False))
        text2 = "\n".join(ln for ln in lines2 if "delivery" in ln or "record_status" in ln)
        self.assertIn("submit_set=reviewable", text2)
        self.assertIn("send_safe_auto_approve=ON", text2,
                      "the CLI flag being off must not read as records being Pending")

    def test_no_hardcoded_pending_sentence_survives(self):
        source = open(run_orchestrator.__file__, encoding="utf-8").read()
        self.assertNotIn("airtable=review-staging(Pending) auto_approve=OFF", source)
        self.assertNotIn('"---- Airtable (review-staging, Status=Pending) ----"', source)


class TheAutomaticApprovalPathIsWired(unittest.TestCase):
    """From a send-safe Fantastic candidate straight to Approved, with no manual
    step -- verified on the real builder, not on the flag alone."""

    def _status_for(self, job, *, send_safe, flag=True):
        """Run the REAL field builder and read the Status it assigns."""
        import airtable_client

        reason = "send_safe" if send_safe else "not_send_safe"
        with mock.patch.object(config, "FANTASTIC_AUTO_APPROVE_SEND_SAFE", flag), \
                mock.patch.object(airtable_client, "send_safe_facts",
                                  return_value=(send_safe, reason)):
            return airtable_client._job_to_fields(job).get("Status")

    def test_a_send_safe_fantastic_row_is_created_approved(self):
        status = self._status_for(_fantastic_lead(), send_safe=True)
        self.assertEqual(status, config.AIRTABLE_STATUS_APPROVED,
                         "no manual step stands between send-safe and Approved")

    def test_a_row_that_is_not_send_safe_stays_pending(self):
        status = self._status_for(_fantastic_lead(), send_safe=False)
        self.assertEqual(status, config.AIRTABLE_STATUS_PENDING)

    def test_a_non_fantastic_row_stays_pending_however_safe(self):
        """The auto-approval is scoped to genuine Fantastic Direct API rows."""
        status = self._status_for(_fantastic_lead(fantastic=False), send_safe=True)
        self.assertEqual(status, config.AIRTABLE_STATUS_PENDING)

    def test_turning_the_flag_off_restores_the_manual_gate(self):
        status = self._status_for(_fantastic_lead(), send_safe=True, flag=False)
        self.assertEqual(status, config.AIRTABLE_STATUS_PENDING)

    def test_the_flag_defaults_on(self):
        """It is the default, so "auto-approval was enabled" in earlier closeouts was
        correct and the contradiction was in the log line, not the behaviour."""
        self.assertTrue(config.FANTASTIC_AUTO_APPROVE_SEND_SAFE)


if __name__ == "__main__":
    unittest.main()
