"""Phase 3 of FINAL_30_PLUS_SYSTEM_SPEC.md: domain recovery extended beyond
Workday via a company-branded URL already present on the job record, gated
by a name-consistency check -- not the intermediary denylist alone.

Cases drawn directly from the 65-company classification of the real
2026-07-29 production run (audit_output/DOMAIN_RECOVERY_65_CLASSIFICATION.md):
6 real recoverable companies, and 2 real false-positive traps the denylist
alone would not have caught.
"""
from __future__ import annotations

import unittest

import config
from company_identity import domain_name_consistent, extract_domain_from_text


class DomainNameConsistentRecoverableCases(unittest.TestCase):
    def test_cornell_university(self):
        self.assertTrue(domain_name_consistent("Cornell University", "cornell.edu"))

    def test_thermo_fisher_scientific_concatenated_brand(self):
        self.assertTrue(domain_name_consistent("Thermo Fisher Scientific", "thermofisher.com"))

    def test_envista(self):
        self.assertTrue(domain_name_consistent("Envista", "envistaco.com"))

    def test_total_wine_more_concatenated_brand(self):
        self.assertTrue(domain_name_consistent("Total Wine & More", "totalwine.com"))

    def test_mgm_resorts_concatenated_brand(self):
        self.assertTrue(domain_name_consistent("MGM Resorts", "mgmresorts.com"))

    def test_exact_brand_match(self):
        self.assertTrue(domain_name_consistent("Acme", "acme.com"))


class DomainNameConsistentConservativeRejections(unittest.TestCase):
    def test_genentech_short_brand_prefix_is_not_auto_accepted(self):
        """A 4-char brand ("gene") matched only as a prefix of a much longer
        company name is exactly the kind of weak signal this check must not
        accept automatically -- reuses the same >=5-char threshold already
        proven conservative in company_names_compatible."""
        self.assertFalse(domain_name_consistent("Genentech", "gene.com"))

    def test_third_party_regional_job_board_is_rejected(self):
        """Real false positive found in the 2026-07-29 corpus: a 'Great
        Minds DC' listing hosted on an unrelated regional job board. Not a
        known aggregator, but also not the employer's domain."""
        self.assertFalse(domain_name_consistent("Great Minds DC", "californiaconstructores.com"))

    def test_third_party_industry_board_is_rejected(self):
        """Real false positive: a 'Corning' listing on NC Biotech Center's
        own careers board (URL path even contains 'corning-incorporated'),
        not Corning's own domain."""
        self.assertFalse(domain_name_consistent("Corning", "ncbiotech.org"))

    def test_unrelated_company_and_domain(self):
        self.assertFalse(domain_name_consistent("Acme Corp", "unrelatedbrand.com"))

    def test_empty_inputs_are_rejected(self):
        self.assertFalse(domain_name_consistent("", "acme.com"))
        self.assertFalse(domain_name_consistent("Acme", ""))


class ExtractDomainFromTextTests(unittest.TestCase):
    """Real cases from the 2026-07-29 corpus: companies recoverable via a
    plain-text domain or email mention in the job description, with no
    usable URL anywhere else on the record."""

    def test_finds_domain_mentioned_in_prose(self):
        text = "Amcor is a global packaging leader. Learn more at amcor.com and apply today."
        self.assertEqual(
            extract_domain_from_text(text, "Amcor", config.INTERMEDIARY_JOB_DOMAINS), "amcor.com"
        )

    def test_finds_domain_from_contact_email(self):
        text = "Interested candidates should send a resume to careers@samsara.com for consideration."
        self.assertEqual(
            extract_domain_from_text(text, "Samsara", config.INTERMEDIARY_JOB_DOMAINS), "samsara.com"
        )

    def test_ignores_name_inconsistent_domains(self):
        text = "This role was originally posted on linkedin.com and requires 3+ years experience."
        self.assertEqual(
            extract_domain_from_text(text, "Acme Corp", config.INTERMEDIARY_JOB_DOMAINS), ""
        )

    def test_ignores_known_intermediary_domains_even_if_name_looks_consistent(self):
        text = "Apply via greenhouse.io for this Greenhouse-hosted role."
        self.assertEqual(
            extract_domain_from_text(text, "Greenhouse", config.INTERMEDIARY_JOB_DOMAINS), ""
        )

    def test_does_not_false_positive_on_version_numbers_or_decimals(self):
        text = "Requires Python 3.11 and experience with 5.0 release cycles."
        self.assertEqual(
            extract_domain_from_text(text, "Acme Corp", config.INTERMEDIARY_JOB_DOMAINS), ""
        )

    def test_empty_description_returns_empty(self):
        self.assertEqual(extract_domain_from_text("", "Acme", config.INTERMEDIARY_JOB_DOMAINS), "")

    def test_empty_company_name_returns_empty(self):
        self.assertEqual(
            extract_domain_from_text("Visit acme.com today", "", config.INTERMEDIARY_JOB_DOMAINS), ""
        )


if __name__ == "__main__":
    unittest.main()
