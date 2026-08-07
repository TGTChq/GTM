"""End-to-end lifecycle assertions for the definitive production behaviours.

These lock in the agreed business rules and the defect fixes at the boundaries
that matter, using the REAL code paths (no network):

* Defect A -- real NEEDS_CHECK/UNVERIFIED contacts reach Airtable with genuine
  lead_key/email, never empty placeholders;
* Defect B -- only safe-terminal postings enter cross-run suppression;
* Defect I -- founded-after-cutoff ICP rule (evidence-first, unknown-safe);
* full-time gate is evidence-first (unknown allowed to review, explicit non-FT
  rejected).
"""

from __future__ import annotations

import tempfile
import types
import unittest
from unittest.mock import patch

import config
from account_gate import AccountGate
from apollo_client import OrgEnrichment
from company_source_resolver import CompanySource
from decision_types import GateState
from job_filter import assess_employment_quality

from orchestrator.adapters_real import RealDelivery, RealEnrichmentStage
from orchestrator.enrichment import EnrichmentReport
from orchestrator.reasons import Disposition


class _Resolver:
    def __init__(self, text="A commercial software platform for finance teams."):
        self.text = text

    def resolve(self, domain, fetch=None):
        return CompanySource("RESOLVED", domain, self.text)


def _org(**overrides):
    values = dict(found=True, name="Example Corp", domain="example.com",
                  employee_count=100, industry="Computer Software",
                  raw={"short_description": "A software company for business teams."})
    values.update(overrides)
    return OrgEnrichment(**values)


class FoundedYearNeutralTests(unittest.TestCase):
    """Founding year is intentionally NEUTRAL for qualification: it never rejects
    or changes the qualification state, whether the company is new, old, or the
    year is unknown. Even with the legacy ENFORCE_FOUNDED_BEFORE flag forced on,
    founding year must not affect the outcome."""

    def _decide(self, founded, enforce=False):
        with patch.object(config, "ENFORCE_FOUNDED_BEFORE", enforce), \
             patch.object(config, "FOUNDED_BEFORE_YEAR", 2010):
            return AccountGate(_Resolver()).evaluate(
                org=_org(founded_year=founded), input_company_name="Example Corp",
                input_domain="example.com", jobs=[])

    def test_newer_company_is_not_rejected(self):
        self.assertNotEqual(self._decide(2015).state, GateState.REJECT)
        # Not neutralized only because the flag is off -- forcing it on changes nothing.
        self.assertNotEqual(self._decide(2015, enforce=True).state, GateState.REJECT)

    def test_older_company_is_not_rejected(self):
        self.assertNotEqual(self._decide(2001).state, GateState.REJECT)

    def test_unknown_founded_year_is_not_rejected(self):
        self.assertNotEqual(self._decide(None).state, GateState.REJECT)

    def test_founded_year_does_not_change_state_new_vs_old_vs_unknown(self):
        # New, old, and unknown must all yield the SAME qualification state.
        states = {self._decide(y).state for y in (2001, 2015, None)}
        self.assertEqual(len(states), 1)

    def test_no_founded_after_cutoff_reason_code_exists(self):
        from reason_codes import ReasonCode
        self.assertFalse(hasattr(ReasonCode, "REJECT_FOUNDED_AFTER_CUTOFF"))


class FullTimeEvidenceFirstTests(unittest.TestCase):
    def _job(self, etype):
        return {"job_title": "Data Analyst", "employer_name": "Acme",
                "job_description": "Own analytics.", "job_employment_type": etype}

    def test_explicit_full_time_passes(self):
        self.assertTrue(assess_employment_quality(self._job("Full-time")).eligible)

    def test_unknown_present_type_allowed_to_review(self):
        ev = assess_employment_quality(self._job("Direct Hire"))
        self.assertTrue(ev.eligible)              # allowed, not rejected
        self.assertEqual(ev.classification, "unknown")

    def test_missing_type_allowed(self):
        self.assertTrue(assess_employment_quality(self._job("")).eligible)

    def test_explicit_part_time_rejected(self):
        self.assertFalse(assess_employment_quality(self._job("Part-time")).eligible)

    def test_explicit_contract_rejected(self):
        self.assertFalse(assess_employment_quality(self._job("Contract")).eligible)


def _lead_row(state, key, email, related=None):
    return {
        "_final_state": state, "lead_key": key, "job_id": key.split("|")[0],
        "employer_name": f"Emp {key}", "hiring_manager_name": "Jordan Lee",
        "hiring_manager_email": email, "apollo_email_status": "unverified",
        "_final_primary_reason": "UNVERIFIED_EMAIL_DELIVERABILITY",
        "related_job_ids": related or [],
    }


def _step3(fp, nc, uv, rr, rj):
    return types.SimpleNamespace(
        final_pass_leads=fp, needs_check_leads=nc, unverified_leads=uv,
        reroute_leads=rr, rejected_leads=rj, hiring_manager_not_found=nc)


class ReviewStagingDataPreservationTests(unittest.TestCase):
    """Defect A: NEEDS_CHECK/UNVERIFIED leads carry real data all the way to the
    Airtable payload."""

    def _report(self):
        rows = [
            _lead_row("FINAL_PASS", "d1|b", "fp@example.com"),
            _lead_row("NEEDS_CHECK", "d2|b", "nc@example.com"),
            _lead_row("UNVERIFIED", "d3|b", "uv@example.com"),
        ]
        step3 = _step3(1, 1, 1, 0, 0)
        qual = types.SimpleNamespace(input_jobs=3, contact_eligible_jobs=3,
                                     rejected_jobs=0, needs_check_jobs=1,
                                     unverified_jobs=1, stats={})
        with tempfile.TemporaryDirectory() as tmp:
            stage = RealEnrichmentStage(target_final_pass=1, workdir=tmp)
            return stage._to_report(qual, step3, rows)

    def test_reviewable_leads_keep_real_contact_data(self):
        report = self._report()
        by_state = {l.disposition: l for l in report.leads}
        for disp, email in ((Disposition.NEEDS_CHECK, "nc@example.com"),
                            (Disposition.UNVERIFIED, "uv@example.com")):
            lead = by_state[disp]
            self.assertTrue(lead.contact_key)                    # real lead_key
            self.assertEqual(lead.contact.get("email"), email)   # real email
            self.assertIn("_airtable_row", lead.contact)         # full row carried

    def test_delivery_submits_real_reviewable_rows_to_airtable(self):
        report = self._report()
        captured = {}

        def fake_push(rows):
            captured["rows"] = rows
            return {"created": len(rows), "failed": 0, "skipped_existing": 0,
                    "persisted_lead_keys": [r["lead_key"] for r in rows],
                    "created_lead_keys": [r["lead_key"] for r in rows]}

        deliverer = RealDelivery(enable_airtable_write=True, auto_approve=False,
                                 enable_instantly=False)
        with patch("airtable_client.push_leads", fake_push):
            rep = deliverer.deliver(report.leads, run_id="run-x", source="ats")

        rows_by_key = {r["lead_key"]: r for r in captured["rows"]}
        # All three reviewable states submitted with REAL lead_key + email.
        self.assertEqual(set(rows_by_key), {"d1|b", "d2|b", "d3|b"})
        self.assertEqual(rows_by_key["d2|b"]["hiring_manager_email"], "nc@example.com")
        self.assertEqual(rows_by_key["d3|b"]["hiring_manager_email"], "uv@example.com")
        self.assertEqual(rep.mode, "review_staging")
        self.assertEqual(rep.final_pass, 1)
        self.assertEqual(rep.needs_check, 1)


class SafeTerminalSuppressionTests(unittest.TestCase):
    """Defect B: only FINAL_PASS + genuine REJECT postings are safe-terminal."""

    def _report(self):
        rows = [
            _lead_row("FINAL_PASS", "d1|b", "fp@example.com", related=["d1b", "d1c"]),
            _lead_row("NEEDS_CHECK", "d2|b", "nc@example.com"),
            _lead_row("UNVERIFIED", "d3|b", "uv@example.com"),
            _lead_row("REROUTE", "d4|b", ""),
            _lead_row("REJECT", "d5|b", ""),
        ]
        step3 = _step3(1, 1, 1, 1, 1)
        qual = types.SimpleNamespace(input_jobs=5, contact_eligible_jobs=5,
                                     rejected_jobs=1, needs_check_jobs=1,
                                     unverified_jobs=1, stats={})
        with tempfile.TemporaryDirectory() as tmp:
            return RealEnrichmentStage(target_final_pass=1, workdir=tmp)._to_report(qual, step3, rows)

    def test_only_terminal_postings_are_committed(self):
        terminal = self._report().terminal_posting_ids()
        # FINAL_PASS posting + its related folded postings, and the REJECT posting.
        self.assertIn("d1", terminal)
        self.assertIn("d1b", terminal)
        self.assertIn("d1c", terminal)
        self.assertIn("d5", terminal)
        # Provider-deferred outcomes stay retryable -- never suppressed.
        self.assertNotIn("d2", terminal)   # NEEDS_CHECK
        self.assertNotIn("d3", terminal)   # UNVERIFIED
        self.assertNotIn("d4", terminal)   # REROUTE


if __name__ == "__main__":
    unittest.main()
