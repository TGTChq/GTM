"""Tests for deterministic, evidence-based employer-domain resolution.

Guarantees: never accept an intermediary/staffing host as the employer; additive
recovery only fills an EMPTY domain; explicit classification distinguishes
staffing/aggregator posters, known-employer acronym cases, and no-evidence cases
from a genuine resolver success.
"""

import unittest
from unittest import mock

import config
import domain_resolution as DR


class ResolutionTests(unittest.TestCase):
    def test_existing_domain_is_classified_direct_and_never_overridden(self):
        r = DR.recover_search_domain("acme.com", {"employer_name": "Acme"})
        self.assertEqual(r.resolved_domain, "acme.com")
        self.assertEqual(r.classification, DR.DIRECT_EMPLOYER)
        self.assertEqual(r.resolution_method, "enrichment_resolved")

    def test_apply_options_direct_host_recovers_domain(self):
        job = {"employer_name": "Acme", "apply_options": [
            {"is_direct": True, "apply_link": "https://careers.acme.com/job/1"}]}
        r = DR.recover_search_domain("", job)
        self.assertEqual(r.resolved_domain, "acme.com")
        self.assertEqual(r.resolution_method, "apply_options_direct_host")
        self.assertEqual(r.classification, DR.DIRECT_EMPLOYER)

    def test_intermediary_apply_host_is_never_accepted(self):
        # A staffing/board host in apply_options must NOT become the employer.
        job = {"employer_name": "Acme", "apply_options": [
            {"is_direct": True, "apply_link": "https://www.linkedin.com/jobs/1"}]}
        r = DR.recover_search_domain("", job)
        self.assertEqual(r.resolved_domain, "")

    def test_alias_map_recovers_known_acronym_employer(self):
        with mock.patch.object(config, "COMPANY_DOMAIN_ALIASES",
                               {"johnson & johnson": "jnj.com"}):
            r = DR.recover_search_domain("", {"employer_name": "Johnson & Johnson"})
        self.assertEqual(r.resolved_domain, "jnj.com")
        self.assertEqual(r.resolution_method, "employer_alias_map")

    def test_staffing_poster_classified_not_resolved(self):
        with mock.patch.object(config, "KNOWN_STAFFING_EMPLOYERS", ["robert half"]):
            r = DR.recover_search_domain("", {"employer_name": "Robert Half"})
        self.assertEqual(r.resolved_domain, "")
        self.assertEqual(r.classification, DR.INTERMEDIARY_UNKNOWN_CLIENT)
        self.assertEqual(r.unresolved_reason, "staffing_or_hidden_client")

    def test_known_employer_acronym_when_nonintermediary_host_present(self):
        # A real non-intermediary host exists but is not name-consistent/aliased.
        job = {"employer_name": "HII", "employer_website": "https://hii-tsd.com"}
        r = DR.recover_search_domain("", job)
        # hii-tsd.com is a registrable non-intermediary host -> known-employer bucket
        self.assertIn(r.classification,
                      (DR.KNOWN_EMPLOYER_UNRESOLVED_DOMAIN, DR.DIRECT_EMPLOYER))

    def test_no_evidence_is_classified_unresolved_no_evidence(self):
        r = DR.recover_search_domain("", {"employer_name": "Ghost Co"})
        self.assertEqual(r.resolved_domain, "")
        self.assertEqual(r.classification, DR.UNRESOLVED_NO_EVIDENCE)

    def test_name_consistent_first_party_host_recovers(self):
        # A free-feed applicationLink (is_direct=False) that is a company host whose
        # domain matches the employer name is safely recovered.
        job = {"employer_name": "Fabrikam", "canonical_source_url": "https://fabrikam.com/careers/9",
               "apply_options": [{"is_direct": False, "apply_link": "https://fabrikam.com/careers/9"}]}
        r = DR.recover_search_domain("", job)
        self.assertEqual(r.resolved_domain, "fabrikam.com")
        self.assertEqual(r.resolution_method, "name_consistent_first_party_host")

    def test_name_inconsistent_job_board_host_is_never_employer(self):
        # A government/job-board host that does NOT match the employer name is rejected
        # (no misattribution), even though it clears the ATS denylist.
        job = {"employer_name": "Some Agency",
               "job_apply_link": "https://www.governmentjobs.com/careers/x/jobs/123"}
        r = DR.recover_search_domain("", job)
        self.assertEqual(r.resolved_domain, "")   # governmentjobs is not "Some Agency"

    def test_ats_source_without_domain_is_employer_known(self):
        job = {"employer_name": "Dragos", "_acquisition_source": "ats_greenhouse",
               "job_apply_link": "https://boards.greenhouse.io/dragos/jobs/1"}
        r = DR.recover_search_domain("", job)
        self.assertEqual(r.resolved_domain, "")            # ATS host is never the domain
        self.assertEqual(r.classification, DR.ATS_EMPLOYER_KNOWN)
        self.assertEqual(r.unresolved_reason, "ats_employer_known_domain_unresolved")

    def test_aggregator_only_host_is_aggregator_unresolved(self):
        job = {"employer_name": "Hidden Co", "_acquisition_source": "himalayas",
               "job_apply_link": "https://himalayas.app/jobs/1"}
        r = DR.recover_search_domain("", job)
        self.assertEqual(r.resolved_domain, "")
        self.assertEqual(r.classification, DR.AGGREGATOR_EMPLOYER_UNRESOLVED)

    def test_ats_platform_host_never_becomes_employer_domain(self):
        for host in ("boards.greenhouse.io/acme", "jobs.lever.co/acme",
                     "acme.wd1.myworkdayjobs.com", "jobs.ashbyhq.com/acme"):
            job = {"employer_name": "Acme", "job_apply_link": f"https://{host}/1",
                   "apply_options": [{"is_direct": True, "apply_link": f"https://{host}/1"}]}
            r = DR.recover_search_domain("", job)
            self.assertEqual(r.resolved_domain, "", host)

    def test_summary_reconciles(self):
        res = [
            DR.recover_search_domain("acme.com", {"employer_name": "Acme"}),
            DR.recover_search_domain("", {"employer_name": "Ghost"}),
            DR.recover_search_domain("", {"employer_name": "Ghost2"}),
        ]
        s = DR.summarize(res)
        self.assertEqual(s["total_companies"], 3)
        self.assertEqual(s["resolved"], 1)
        self.assertEqual(s["unresolved"], 2)
        self.assertEqual(s["resolved"] + s["unresolved"], s["total_companies"])


if __name__ == "__main__":
    unittest.main()
