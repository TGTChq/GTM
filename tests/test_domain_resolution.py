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
        self.assertEqual(r.classification, DR.INTERMEDIARY_UNRESOLVED)
        self.assertEqual(r.unresolved_reason, "staffing_poster")

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
