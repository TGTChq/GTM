"""Employer/domain resolution: upstream preservation + safe classification.

Covers the source→employer separation and the invariant that a domain is never
guessed: a first-party company host is preserved and used, while ATS/board and
aggregator hosts never become the employer.
"""

import unittest

import free_job_sources as F
import domain_resolution as DR
import hm_observability as O


class FreeFeedPreservationTests(unittest.TestCase):
    def _job(self, company, url):
        return F._canonical_job(source="himalayas", source_name="Himalayas",
                                source_home="https://himalayas.app", source_id="1",
                                title="Sales Manager", company=company, description="d",
                                url=url)

    def test_first_party_apply_host_marked_direct(self):
        # applicationLink is the employer's own name-consistent host -> is_direct True
        job = self._job("Fabrikam", "https://fabrikam.com/careers/9")
        self.assertTrue(job["job_apply_is_direct"])
        self.assertTrue(job["apply_options"][0]["is_direct"])

    def test_ats_apply_host_not_marked_direct(self):
        # a Greenhouse board host is NOT the employer domain -> stays is_direct False
        job = self._job("Fabrikam", "https://boards.greenhouse.io/fabrikam/jobs/9")
        self.assertFalse(job["job_apply_is_direct"])
        self.assertFalse(job["apply_options"][0]["is_direct"])

    def test_name_inconsistent_board_not_marked_direct(self):
        # a job-board host that does not match the employer name -> not direct
        job = self._job("Some Agency", "https://www.governmentjobs.com/x/jobs/1")
        self.assertFalse(job["job_apply_is_direct"])

    def test_apply_url_is_always_preserved(self):
        # even when not direct, the source apply URL survives normalization
        job = self._job("Hidden Co", "https://himalayas.app/jobs/1")
        self.assertEqual(job["job_apply_link"], "https://himalayas.app/jobs/1")
        self.assertEqual(job["canonical_source_url"], "https://himalayas.app/jobs/1")


class EmployerSummaryTests(unittest.TestCase):
    def _lead(self, source, cls=None, reason=None, searched=False, has_domain=False,
              excluded=False):
        return {
            "_step3_status": "excluded" if excluded else ("found" if searched else "unverified"),
            "hiring_manager_name": "X" if searched else None,
            "_role_bucket": "gtm_revenue", "company_domain": "x.com" if has_domain else "",
            "_acquisition_source": source,
            "_row2_diagnostic": {"people_search_call": searched,
                                 "domain_classification": cls, "domain_unresolved_reason": reason},
        }

    def test_summary_reconciles_and_classifies_by_source(self):
        leads = [
            self._lead("ats_greenhouse", searched=True, has_domain=True),        # resolved
            self._lead("himalayas", cls="aggregator_employer_unresolved",
                       reason="aggregator_employer_unresolved"),                 # aggregator
            self._lead("jsearch", cls="intermediary_unknown_client",
                       reason="staffing_or_hidden_client"),                      # staffing
            self._lead("ats_workday", cls="ats_employer_known",
                       reason="ats_employer_known_domain_unresolved"),           # ats known
        ]
        s = O.domain_resolution_summary(leads)
        self.assertEqual(s["postings_evaluated"], 4)
        self.assertEqual(s["employer_resolved"] + s["employer_unresolved"], 4)
        self.assertEqual(s["aggregator"], 1)
        self.assertEqual(s["staffing_or_hidden_client"], 1)
        self.assertEqual(s["ats_employer_known"], 1)
        self.assertIn("himalayas", s["unresolved_by_source"])


class SafetyInvariantTests(unittest.TestCase):
    def test_no_domain_guessing_from_company_name(self):
        # "Acme Corp" must NOT become acme.com / acmecorp.com without evidence
        r = DR.recover_search_domain("", {"employer_name": "Acme Corp"})
        self.assertEqual(r.resolved_domain, "")

    def test_staffing_stays_intermediary_not_employer(self):
        r = DR.recover_search_domain(
            "", {"employer_name": "Insight Global", "_acquisition_source": "jsearch"})
        self.assertEqual(r.resolved_domain, "")
        self.assertEqual(r.classification, DR.INTERMEDIARY_UNKNOWN_CLIENT)


if __name__ == "__main__":
    unittest.main()
