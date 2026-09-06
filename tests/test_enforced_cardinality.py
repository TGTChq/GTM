"""What the suppression rules actually enforce, read off the real builders.

I claimed these two flags meant "one approved lead per employer" and used that to
bound capacity. They do not, and the names are why the mistake was easy:

``AIRTABLE_SUPPRESS_EXISTING_COMPANY_FUNCTION``
    Key: COMPANY + ROLE BUCKET (``_company_function_keys_from_job`` builds it from
    Website/Company plus Role Bucket). Scope: records that ALREADY EXIST in Airtable
    when the run starts, and only those in an ACTIVE status -- Pending, Approved,
    Enrolled or blank. Error and Rejected stay retryable. So it caps a company at one
    ACTIVE row PER FUNCTION, across runs. A second function at the same company is
    untouched.

``ENROLLMENT_PERSON_EMPLOYER_UNIQUENESS``
    Key: ``normalized_domain|normalized_email`` -- a PERSON-EMPLOYER PAIR, explicitly
    bucket-agnostic. It collapses the SAME PERSON appearing under two role buckets,
    and refuses a person who is already an active Airtable row. It does NOT cap an
    employer: its own docstring says distinct emails at the same employer are all
    kept.

Neither rule imposes one lead per employer, and nothing else read here does either.
An employer can produce as many approved rows as it has distinct eligible functions,
and as many enrollments as it has distinct people.
"""

from __future__ import annotations

import unittest
from unittest import mock

import airtable_client
import config
from orchestrator.adapters_real import (_collapse_person_employer,
                                        _person_employer_key)
from orchestrator.enrichment import Lead
from orchestrator.reasons import Disposition, ReasonCode


def _job(company="Acme", domain="acme.com", bucket="finance", email="a@acme.com"):
    return {
        "employer_name": company,
        "company_domain": domain,
        "_role_bucket": bucket,
        "hiring_manager_email": email,
        "lead_key": f"{domain}|{email}|{bucket}",
    }


def _existing(status="Approved", company="Acme", domain="acme.com", bucket="finance"):
    return {
        f"{domain}|{bucket}": {
            "fields": {
                "Status": status,
                "Company": company,
                "Website": f"https://{domain}",
                "Role Bucket": bucket,
            }
        }
    }


def _lead(email="a@acme.com", domain="acme.com", bucket="finance"):
    return Lead(
        posting_id=f"p-{email}-{bucket}",
        company={"name": "Acme", "website": f"https://{domain}"},
        contact={"email": email,
                 "_airtable_row": {"company_domain": domain, "_role_bucket": bucket}},
        disposition=Disposition.FINAL_PASS,
        primary_reason=ReasonCode.OK,
        contact_key=f"{domain}|{email}|{bucket}",
    )


class TheAirtableRuleIsCompanyTimesFunction(unittest.TestCase):
    def test_one_company_two_functions_are_two_distinct_keys(self):
        finance = airtable_client._company_function_keys_from_job(_job(bucket="finance"))
        sales = airtable_client._company_function_keys_from_job(_job(bucket="sales"))
        self.assertTrue(finance)
        self.assertTrue(sales)
        self.assertFalse(finance & sales,
                         "a second FUNCTION at the same company is its own opportunity")

    def test_an_active_row_blocks_only_the_same_function(self):
        existing = _existing(status="Approved", bucket="finance")
        blocked = airtable_client._active_existing_company_function_keys(existing)
        self.assertTrue(
            airtable_client._company_function_keys_from_job(_job(bucket="finance")) & blocked)
        self.assertFalse(
            airtable_client._company_function_keys_from_job(_job(bucket="sales")) & blocked,
            "Acme+Sales survives while Acme+Finance is active")

    def test_rejected_and_error_rows_do_not_block(self):
        for status in (config.AIRTABLE_STATUS_REJECTED, config.AIRTABLE_STATUS_ERROR):
            blocked = airtable_client._active_existing_company_function_keys(
                _existing(status=status))
            self.assertEqual(blocked, set(), f"{status} must stay retryable")

    def test_pending_approved_and_enrolled_all_block(self):
        for status in (config.AIRTABLE_STATUS_PENDING, config.AIRTABLE_STATUS_APPROVED,
                       config.AIRTABLE_STATUS_ENROLLED, ""):
            blocked = airtable_client._active_existing_company_function_keys(
                _existing(status=status))
            self.assertTrue(blocked, f"{status or '<blank>'} is an active row")

    def test_the_scope_is_pre_existing_records_only(self):
        """The blocked set is computed ONCE from the records that existed when the
        run started. Two NEW rows for the same company+function inside one run are
        not suppressed by this rule -- the one-per-function property across a run
        comes from enrichment emitting one lead per company x role bucket, not from
        here."""
        blocked = airtable_client._active_existing_company_function_keys({})
        self.assertEqual(blocked, set())


class TheEnrollmentRuleIsAPersonEmployerPair(unittest.TestCase):
    def setUp(self):
        self.on = mock.patch.object(config, "ENROLLMENT_PERSON_EMPLOYER_UNIQUENESS", True)
        self.on.start()
        self.addCleanup(self.on.stop)

    def test_the_key_is_domain_and_email_not_domain_alone(self):
        a = _person_employer_key(_lead(email="a@acme.com"))
        b = _person_employer_key(_lead(email="b@acme.com"))
        self.assertTrue(a and b)
        self.assertNotEqual(a, b, "two people at one employer are two keys")

    def test_two_different_contacts_at_one_employer_are_BOTH_kept(self):
        """The claim this file exists to correct."""
        kept, losers = _collapse_person_employer(
            [_lead(email="a@acme.com"), _lead(email="b@acme.com")])
        self.assertEqual(len(kept), 2, "an employer is not capped at one lead")
        self.assertEqual(losers, [])

    def test_the_same_person_under_two_buckets_collapses_to_one(self):
        kept, losers = _collapse_person_employer(
            [_lead(email="a@acme.com", bucket="finance"),
             _lead(email="a@acme.com", bucket="sales")])
        self.assertEqual(len(kept), 1, "one enrollment per person per employer")
        self.assertEqual(len(losers), 1)

    def test_the_same_person_at_two_employers_is_kept_at_both(self):
        kept, _losers = _collapse_person_employer(
            [_lead(email="a@acme.com", domain="acme.com"),
             _lead(email="a@acme.com", domain="beta.com")])
        self.assertEqual(len(kept), 2)

    def test_a_person_already_active_in_airtable_is_not_re_enrolled(self):
        # The existing-leads snapshot fetches a NARROW field set that omits Email,
        # so the person index is built from `Lead Key` (domain|email|bucket).
        existing = {"r1": {"fields": {"Status": "Approved",
                                      "Lead Key": "acme.com|a@acme.com|finance"}}}
        kept, losers = _collapse_person_employer(
            [_lead(email="a@acme.com"), _lead(email="b@acme.com")],
            existing=existing)
        emails = sorted(l.contact["email"] for l in kept)
        self.assertEqual(emails, ["b@acme.com"])
        self.assertEqual(len(losers), 1)

    def test_with_the_flag_off_nothing_is_collapsed_at_all(self):
        with mock.patch.object(config, "ENROLLMENT_PERSON_EMPLOYER_UNIQUENESS", False):
            kept, losers = _collapse_person_employer(
                [_lead(email="a@acme.com", bucket="finance"),
                 _lead(email="a@acme.com", bucket="sales")])
        self.assertEqual(len(kept), 2)
        self.assertEqual(losers, [])


class NoRuleImposesOneLeadPerEmployer(unittest.TestCase):
    def test_the_two_rules_together_still_allow_several_leads_at_one_employer(self):
        """Two functions, two people, one company: two Airtable opportunities and
        two enrollments. Read back rather than inferred from the flag names."""
        finance = airtable_client._company_function_keys_from_job(
            _job(bucket="finance", email="a@acme.com"))
        sales = airtable_client._company_function_keys_from_job(
            _job(bucket="sales", email="b@acme.com"))
        self.assertFalse(finance & sales)

        with mock.patch.object(config, "ENROLLMENT_PERSON_EMPLOYER_UNIQUENESS", True):
            kept, _ = _collapse_person_employer(
                [_lead(email="a@acme.com", bucket="finance"),
                 _lead(email="b@acme.com", bucket="sales")])
        self.assertEqual(len(kept), 2)


if __name__ == "__main__":
    unittest.main()
