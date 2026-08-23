"""A URL shortener must never become a trusted employer identity.

Observed in production: a posting stored ``Website=https://bit.ly`` with
``Company="Ágora Investimentos"`` while its Job URL pointed at a Barrett-Jackson
Auction Company listing. The shortener became the company domain, so
``email_matches_company`` accepted an ``@bit.ly`` address and the row recorded
Contact Alignment PASS -- a real Bitly employee would have been contacted about a
different company's job.
"""

from __future__ import annotations

import unittest

import company_identity as CI
import config


class ShortenerRejectionTests(unittest.TestCase):
    def setUp(self):
        CI.reset_shortener_rejection_metrics()
        self.addCleanup(CI.reset_shortener_rejection_metrics)

    def test_the_exact_production_defect_is_closed(self):
        self.assertEqual(CI.safe_company_domain("https://bit.ly", config.INTERMEDIARY_JOB_DOMAINS), "")

    def test_every_known_shortener_is_rejected(self):
        for host in sorted(CI.URL_SHORTENER_DOMAINS):
            with self.subTest(host=host):
                self.assertEqual(CI.safe_company_domain(f"https://{host}/abc123", []), "")

    def test_rejection_is_structural_not_configured(self):
        """contact_gate passes an EMPTY blocked list -- the guard must still fire."""
        self.assertEqual(CI.safe_company_domain("bit.ly", []), "")

    def test_legitimate_short_domains_are_never_rejected(self):
        """No length or shape heuristic: real employers have short domains too."""
        for host in ("hex.tech", "m8th.com", "az.gov", "hsi.com", "cbs.com",
                     "alt.com", "codal.com", "bws.net", "is.com", "x.ai"):
            with self.subTest(host=host):
                self.assertEqual(CI.safe_company_domain(f"https://{host}", []), host)

    def test_subdomains_of_a_shortener_are_rejected(self):
        self.assertEqual(CI.safe_company_domain("https://links.bit.ly/x", []), "")

    def test_provenance_metric_is_recorded(self):
        CI.safe_company_domain("https://bit.ly", [])
        CI.safe_company_domain("https://t.co", [])
        metrics = CI.shortener_rejection_metrics()
        self.assertEqual(metrics["employer_domain_rejected_shortener"], 2)
        self.assertEqual(metrics["employer_domain_rejected_shortener:bit.ly"], 1)

    def test_non_shortener_rejection_does_not_record_the_metric(self):
        CI.safe_company_domain("https://acme.com", [])
        self.assertEqual(CI.shortener_rejection_metrics(), {})


class ShortenerEmailTests(unittest.TestCase):
    def test_shortener_email_never_matches_a_company(self):
        self.assertFalse(CI.email_matches_company("someone@bit.ly", {"bit.ly"}))

    def test_shortener_cannot_enter_the_allowed_set(self):
        self.assertFalse(CI.email_matches_company("someone@bit.ly", {"bit.ly", "acme.com"}))

    def test_a_real_company_email_still_matches(self):
        self.assertTrue(CI.email_matches_company("hm@acme.com", {"acme.com"}))

    def test_short_legitimate_domain_email_still_matches(self):
        self.assertTrue(CI.email_matches_company("hm@hex.tech", {"hex.tech"}))

    def test_is_url_shortener_domain_predicate(self):
        self.assertTrue(CI.is_url_shortener_domain("https://bit.ly/xyz"))
        self.assertFalse(CI.is_url_shortener_domain("https://hex.tech"))
        self.assertFalse(CI.is_url_shortener_domain(""))


if __name__ == "__main__":
    unittest.main()
