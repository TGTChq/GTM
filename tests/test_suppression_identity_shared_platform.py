"""A shared source platform is not a company identity -- in the SUPPRESSION keyer.

The full-flow release stopped the enrichment side deriving employer identity from a
recognized ATS host. It did not change `_company_identity_keys_from_fields`, which is
the keyer Airtable suppression actually decides on, so two unrelated employers both
posting through ApplicantPro still shared `domain:applicantpro.com`, matched, and each
suppressed the other.

That is a lead-volume defect, not a cosmetic one: `AIRTABLE_SUPPRESS_EXISTING_COMPANY_FUNCTION`
allows one active row per company x function, so a collapse means every employer after
the first on that platform is withheld as a duplicate of a company it has nothing to
do with. The 2026-09-06 calibration corpus shows the shape at scale -- nineteen
employers retained under one company at exactly that host.

Dropping the platform key does NOT weaken suppression, it sharpens it. `name:` still
carries the identity, a genuinely repeated employer still matches on its name, and
what stops matching is two DIFFERENT companies that happened to share a vendor. The
second class of test below pins that, because a fix that quietly stopped suppressing
real duplicates would be worse than the defect.
"""

from __future__ import annotations

import unittest

from airtable_client import (_company_function_keys_from_job,
                             _company_identity_keys_from_job)


def _job(name, domain, bucket="gtm_revenue"):
    # `_role_bucket`, not a job title: the function keyer reads the resolved bucket,
    # and a fixture without one yields an EMPTY set -- which would make the
    # shared-platform assertions below pass trivially on two empty sets.
    return {"employer_name": name, "company_domain": domain, "_role_bucket": bucket}


class ASharedPlatformNoLongerMergesEmployers(unittest.TestCase):
    def test_two_employers_on_one_ats_host_share_no_key(self):
        a = _company_identity_keys_from_job(_job("Dayton T. Brown, Inc.", "applicantpro.com"))
        b = _company_identity_keys_from_job(_job("OzarksGo", "applicantpro.com"))
        self.assertEqual(a & b, set(), "a vendor is not a company")

    def test_the_employer_still_has_an_identity(self):
        """Rejecting the host must not leave a row with NO identity at all -- that
        would make it unsuppressable rather than correctly suppressed."""
        keys = _company_identity_keys_from_job(_job("Dayton T. Brown, Inc.", "applicantpro.com"))
        self.assertTrue(keys)
        self.assertTrue(any(k.startswith("name:") for k in keys))

    def test_the_function_keyer_inherits_the_same_rule(self):
        a = _company_function_keys_from_job(_job("Dayton T. Brown, Inc.", "applicantpro.com"))
        b = _company_function_keys_from_job(_job("OzarksGo", "applicantpro.com"))
        self.assertTrue(a and b, "both must HAVE keys, or this passes on empty sets")
        self.assertEqual(a & b, set())

    def test_several_known_platforms_all_behave_the_same(self):
        for host in ("greenhouse.io", "lever.co", "myworkdayjobs.com",
                     "smartrecruiters.com", "icims.com", "applicantpro.com"):
            a = _company_identity_keys_from_job(_job("Alpha Industries", host))
            b = _company_identity_keys_from_job(_job("Beta Logistics", host))
            self.assertEqual(a & b, set(), f"{host} merged two employers")


class RealCompanyMatchingIsUNCHANGED(unittest.TestCase):
    """The direction that must not regress. A fix that stopped suppressing genuine
    duplicates would cost more than the defect it removed."""

    def test_the_same_company_domain_still_matches(self):
        a = _company_identity_keys_from_job(_job("Acme", "acme.com"))
        b = _company_identity_keys_from_job(_job("Acme Inc.", "acme.com"))
        self.assertIn("domain:acme.com", a & b)

    def test_the_same_company_still_matches_by_function(self):
        a = _company_function_keys_from_job(_job("Acme", "acme.com", "gtm_revenue"))
        b = _company_function_keys_from_job(_job("Acme Inc.", "acme.com", "gtm_revenue"))
        self.assertTrue(a & b)

    def test_different_functions_at_one_company_stay_distinct(self):
        a = _company_function_keys_from_job(_job("Acme", "acme.com", "gtm_revenue"))
        b = _company_function_keys_from_job(_job("Acme", "acme.com", "marketing"))
        self.assertEqual(a & b, set(), "a function is its own opportunity")

    def test_a_row_with_only_a_name_is_unaffected(self):
        keys = _company_identity_keys_from_job(
            {"employer_name": "Acme", "_role_bucket": "gtm_revenue"})
        self.assertEqual(keys, {"name:acme"})


if __name__ == "__main__":
    unittest.main()
