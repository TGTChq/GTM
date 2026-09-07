"""Loss reasons, read with their units. Reproduced on the real 2026-09-07 record.

The record below is verbatim from run ``20260907T062915Z-f79f4de1``'s
reporting-ledger entry, recovered from the deployment that carried it. It is here
because the arithmetic everyone does with a flat reason map -- add it up, call the
total "losses", attribute the biggest line to the provider -- is wrong on this record
in three separate ways, and a synthetic fixture would not have shown any of them.
"""

import unittest

from orchestrator import loss_units as lu

#: Verbatim `loss_reasons`, `delivery_skip_breakdown` and `metrics` of the run.
RECORD = {
    "run_id": "20260907T062915Z-f79f4de1",
    "state": "incomplete",
    "stop_reason": "topup:apollo_circuit_open",
    "loss_reasons": {
        "REJECT_QUALITY_GUARD_OTHER": 226, "REJECT_ROLE_MISMATCH": 69,
        "REJECT_ROLE_NOT_GLOBALLY_COVERABLE": 13,
        "REJECT_SECURITY_CLEARANCE_REQUIRED": 17,
        "email_unverified": 155, "hiring_manager_not_found": 169, "needs_check": 22,
        "no_contact": 144, "not_icp": 19, "rejected": 19, "send_safe_withheld": 27,
        "unverified": 155,
    },
    "delivery_skip_breakdown": {
        "account_suppressed": 0, "company_function_suppressed": 0, "no_contact": 144,
        "other": 0, "send_safe_withheld": 27, "skipped_existing": 0,
        "updated_existing": 0,
    },
    "metrics": {
        "airtable_candidates": 199, "companies_considered": 184, "contacts_found": 56,
        "final_pass_leads": 23, "jobs_captured": 328, "jobs_reviewed": 2328,
        "postings_resumed": 2000, "qualified_opportunities": 203,
        "role_qualified_postings": 1984, "sent_to_airtable": 28,
        "unique_opportunities": 222, "verified_emails": 55,
    },
}

#: The observability layer's hiring-manager summary for the same run.
HM = {"hm_not_found_unit": "company_x_role_bucket", "eligible_company_buckets": 203,
      "hm_searches": 203, "hm_found": 56, "hm_not_found": 147}


class TheFlatMapIsNotAddable(unittest.TestCase):
    def test_the_reasons_span_four_units_and_are_summed_only_within_each(self):
        by_unit = lu.classify(RECORD["loss_reasons"])["by_unit"]
        self.assertEqual(set(by_unit), {lu.POSTING, lu.OPPORTUNITY, lu.DELIVERY_ROW,
                                        lu.DISPOSITION})
        self.assertEqual(by_unit[lu.POSTING]["sum_within_unit"], 325)
        self.assertEqual(by_unit[lu.DELIVERY_ROW]["sum_within_unit"], 171)

    def test_no_label_is_left_unclassified(self):
        self.assertEqual(lu.classify(RECORD["loss_reasons"])["unclassified_labels"], {})

    def test_an_unknown_label_is_reported_rather_than_guessed_into_a_category(self):
        out = lu.classify({"some_future_reason": 5})
        self.assertEqual(out["unclassified_labels"], {"some_future_reason": 5})
        self.assertEqual(out["by_unit"], {})


class TwoLabelsOnePopulation(unittest.TestCase):
    def test_unverified_and_email_unverified_are_the_same_155(self):
        aliases = {g["group"]: g for g in lu.overlaps(RECORD["loss_reasons"])["alias_groups"]}
        group = aliases["email_validation_failures"]
        self.assertTrue(group["counts_agree"])
        self.assertEqual(group["double_counted_if_summed"], 155)

    def test_not_icp_and_rejected_are_the_same_19(self):
        aliases = {g["group"]: g for g in lu.overlaps(RECORD["loss_reasons"])["alias_groups"]}
        self.assertEqual(aliases["account_gate_rejections"]["double_counted_if_summed"], 19)

    def test_only_eleven_opportunities_had_an_email_that_failed_verification(self):
        """The correction that changes the diagnosis.

        Read flat, ``email_unverified: 155`` says email verification is the biggest
        single loss and points at the provider. It is not a separate population:
        ``unverified`` CONTAINS ``no_contact``, because an opportunity with no
        contact at all has no verified email either. 155 minus 144 leaves **11**
        opportunities that actually had an email and failed to verify it. The other
        144 never had a contact to verify.
        """
        contained = lu.overlaps(RECORD["loss_reasons"])["containments"]
        self.assertEqual(len(contained), 1)
        self.assertEqual(contained[0]["outer_excluding_inner"], 11)
        self.assertTrue(contained[0]["consistent"])


class TheDeliveryRowUnitIsFullyExplained(unittest.TestCase):
    def test_submitted_equals_created_plus_every_skip(self):
        d = lu.delivery_reconciles(199, 28, RECORD["delivery_skip_breakdown"])
        self.assertTrue(d["reconciles"])
        self.assertEqual(d["unexplained"], 0)
        self.assertEqual(d["skips"], 171)

    def test_a_gap_is_reported_rather_than_absorbed(self):
        d = lu.delivery_reconciles(199, 28, {"no_contact": 100})
        self.assertFalse(d["reconciles"])
        self.assertEqual(d["unexplained"], 71)


class SearchedVersusNeverSearched(unittest.TestCase):
    def test_every_eligible_bucket_was_actually_searched_in_this_run(self):
        c = lu.contact_discovery(HM, RECORD["loss_reasons"],
                                 RECORD["delivery_skip_breakdown"])
        self.assertEqual(c[lu.NEVER_SEARCHED], 0)
        self.assertEqual(c[lu.SEARCHED_NO_RESULT], 147)
        self.assertEqual(c["searched_and_found"], 56)
        self.assertTrue(c["partition_closes"])

    def test_the_two_not_found_counters_disagree_and_that_is_reported(self):
        """169 against 147, and neither artifact says which is right.

        A consumer that takes the larger books 22 buckets as "nobody found" that the
        other counter says had somebody. Surfacing it is the fix available here;
        deciding it needs per-bucket evidence this run did not retain.
        """
        c = lu.contact_discovery(HM, RECORD["loss_reasons"],
                                 RECORD["delivery_skip_breakdown"])
        d = c["counter_disagreement"]
        self.assertFalse(d["agree"])
        self.assertEqual(d["difference"], 22)
        self.assertIn("neither artifact", d["consequence"])

    def test_without_the_summary_the_section_says_so_instead_of_subtracting(self):
        out = lu.decompose(RECORD)["contact_discovery"]
        self.assertIn("unavailable", out)

    def test_found_but_withheld_is_its_own_category(self):
        c = lu.contact_discovery(HM, RECORD["loss_reasons"],
                                 RECORD["delivery_skip_breakdown"])
        self.assertEqual(c[lu.FOUND_BUT_WITHHELD], 27)


class WorkNeverAttemptedIsNotCoverage(unittest.TestCase):
    def test_opportunities_without_an_outcome_are_attributed_to_the_interruption(self):
        r = lu.decompose(RECORD, HM)["reach"]
        self.assertEqual(r["opportunities_without_outcome"], 4)
        self.assertEqual(r["category_for_those_without_outcome"], lu.INTERRUPTED)

    def test_the_posting_to_opportunity_ratio_is_a_collapse_not_a_loss(self):
        """1,984 qualified postings against 203 opportunities is 9.77 postings each.

        Read as a loss it looks catastrophic. It is the company x function collapse
        doing exactly its job, and labelling it keeps it out of the loss column.
        """
        r = lu.decompose(RECORD, HM)["reach"]
        self.assertEqual(r["qualified_postings_per_formed_opportunity"], 9.77)
        self.assertIn("collapse factor", r["caution"])

    def test_a_completed_run_does_not_call_its_remainder_an_interruption(self):
        r = lu.opportunity_reach(qualified_postings=100, opportunities_formed=10,
                                 opportunities_with_outcome=10,
                                 stop_reason="run_approved_target_met")
        self.assertEqual(r["opportunities_without_outcome"], 0)
        self.assertEqual(r["category_for_those_without_outcome"], lu.NEVER_SEARCHED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
