"""A loss reason recorded as zero must never explain anything.

Found in Brett's own report on 2026-09-06. It carried this action item:

    2. Account-level suppression removed deliverable leads. Verify
       AIRTABLE_SUPPRESS_ACCOUNT_LEVEL is deliberately on.

`AIRTABLE_SUPPRESS_ACCOUNT_LEVEL` is **off** in production and had suppressed
exactly nothing. The delivery skip breakdown is a fixed-shape dataclass -- every
bucket is emitted on every run -- so a policy that never fired still contributed its
name with a count of 0, and the census kept the key.

Two separate harms, both pinned here:

* the action plan asserted a cause that did not occur, and told the reader to go
  verify a setting on the strength of it;
* a boundary whose two counters are NOT nested (`contacts_found` ->
  `sent_to_airtable`) may be named THE bottleneck only when a reason code
  attributes it. An all-zero reason set satisfied that gate, so the report's
  headline finding could rest on reasons that attribute nothing.
"""

from __future__ import annotations

import unittest

from weekly_report import bottleneck as bn
from weekly_report import metrics as mx


class _Run:
    """Minimal RunRecord stand-in: `artifact(stem)` is the whole interface used."""

    def __init__(self, **artifacts):
        self._artifacts = artifacts

    def artifact(self, stem):
        return self._artifacts.get(stem)


class ZeroReasonsLeaveTheCensus(unittest.TestCase):
    def test_a_reason_that_never_fired_is_absent(self):
        run = _Run(delivery={"skip_breakdown": {
            "account_suppressed": 0, "company_function_suppressed": 12}})
        census = mx.reason_census([run])
        self.assertNotIn("account_suppressed", census)
        self.assertEqual(census["company_function_suppressed"], 12)

    def test_a_zero_from_one_run_still_merges_with_a_real_count(self):
        """Dropped at the end, not per run -- otherwise a run that happened to record
        0 would change the total, which it must not."""
        runs = [_Run(delivery={"skip_breakdown": {"not_icp": 0}}),
                _Run(delivery={"skip_breakdown": {"not_icp": 7}})]
        self.assertEqual(mx.reason_census(runs)["not_icp"], 7)

    def test_an_all_zero_breakdown_yields_an_empty_census(self):
        run = _Run(delivery={"skip_breakdown": {"account_suppressed": 0,
                                                "not_final_pass": 0}})
        self.assertEqual(mx.reason_census([run]), {})

    def test_ordering_is_unchanged_for_the_reasons_that_remain(self):
        run = _Run(delivery={"skip_breakdown": {
            "not_icp": 3, "account_suppressed": 0, "no_contact": 9}})
        self.assertEqual(list(mx.reason_census([run])), ["no_contact", "not_icp"])


class TheActionPlanDoesNotAssertAThingThatDidNotHappen(unittest.TestCase):
    def _metric(self, key, value):
        m = mx.Metric(key=key, label=key, unit="company_role_bucket_opportunity",
                      value=value)
        m.status = mx.STATUS_MEASURED
        m.counted_unit = "company_role_bucket_opportunity"
        m.cohort = "run_window"
        m.source = mx.SOURCE_RUN_ARTIFACTS
        m.contributing_run_ids = ["r1"]
        m.evidence = ["r1"]
        return m

    def _plan(self, reasons):
        metrics = {"contacts_found": self._metric("contacts_found", 1048),
                   "sent_to_airtable": self._metric("sent_to_airtable", 781)}
        found = bn.identify(metrics, run_count=1, reasons=reasons)
        return found, bn.action_plan(found, metrics)

    def test_a_zero_reason_produces_no_action_naming_it(self):
        _found, plan = self._plan({"account_suppressed": 0,
                                   "company_function_suppressed": 200})
        text = " ".join(str(getattr(step, "text", step)) for step in plan)
        self.assertNotIn("AIRTABLE_SUPPRESS_ACCOUNT_LEVEL", text)
        self.assertIn("company+function", text)

    def test_a_non_nested_boundary_is_not_named_on_all_zero_reasons(self):
        """The eligibility gate. `contacts_found` -> `sent_to_airtable` is not a
        nested pair, so with no reason code firing the difference is not
        attributable and must not be reported as the bottleneck."""
        found, _plan = self._plan({"account_suppressed": 0, "not_final_pass": 0})
        self.assertNotEqual(found.kind, "funnel_boundary",
                            "an all-zero reason set attributes nothing")

    def test_the_same_boundary_IS_named_when_a_reason_really_fired(self):
        found, _plan = self._plan({"company_function_suppressed": 200})
        self.assertEqual(found.kind, "funnel_boundary")
        self.assertEqual(found.lost, 267)


if __name__ == "__main__":
    unittest.main()


class WithheldBeforeSubmissionIsAReasonToo(unittest.TestCase):
    """`skip_breakdown` partitions only rows that WERE submitted and not created.
    Rows withheld earlier -- already delivered, or a person-employer collapse loser
    -- never reach it. Both names are already admissible at `airtable_delivery`, and
    nothing was reading them, so a window whose entire difference was idempotency
    reported "no reason code was recorded there" while the counts sat in the
    artifact."""

    def _run(self, delivery):
        return _Run(delivery=delivery)

    def test_already_delivered_is_counted(self):
        census = mx.reason_census([self._run({
            "skip_breakdown": {"account_suppressed": 0},
            "skipped_already_delivered": 260, "person_employer_duplicate": 0})])
        self.assertEqual(census["already_delivered"], 260)
        self.assertNotIn("person_employer_duplicate", census)

    def test_person_employer_collapse_is_counted(self):
        census = mx.reason_census([self._run({
            "skip_breakdown": {}, "person_employer_duplicate": 7})])
        self.assertEqual(census["person_employer_duplicate"], 7)

    def test_it_does_not_double_count_with_the_skip_breakdown(self):
        """`skip_breakdown` has no bucket for either, so there is nothing to double."""
        census = mx.reason_census([self._run({
            "skip_breakdown": {"skipped_existing": 5, "updated_existing": 3},
            "skipped_already_delivered": 11})])
        self.assertEqual(census, {"already_delivered": 11,
                                  "skipped_existing": 5, "updated_existing": 3})


class AnEmptyLedgerBlockIsNotAnAnswer(unittest.TestCase):
    """The compact ledger's copy wins over the artifacts, which is right when it
    holds something. An EMPTY or all-zero block is not a record that nothing was
    lost -- it is the absence of a record, and letting it win masked artifacts that
    were still on disk and could answer."""

    def test_an_all_zero_ledger_block_falls_through_to_the_artifacts(self):
        run = _Run(**{"reporting_ledger": {"loss_reasons": {"not_icp": 0}},
                      "delivery": {"skip_breakdown": {"company_function_suppressed": 42}}})
        run._artifacts[mx.LEDGER_STEM] = run._artifacts.pop("reporting_ledger")
        self.assertEqual(mx.reason_census([run]),
                         {"company_function_suppressed": 42})

    def test_a_populated_ledger_block_still_wins(self):
        """Reading both would double-count: the ledger copy IS the same merge."""
        run = _Run(**{mx.LEDGER_STEM: {"loss_reasons": {"not_icp": 9}},
                      "delivery": {"skip_breakdown": {"not_icp": 9}}})
        self.assertEqual(mx.reason_census([run]), {"not_icp": 9})


class AFailedReconciliationIsTheStrongestThingSayable(unittest.TestCase):
    """The 2026-09-04 production record: 1,681 submitted, 781 created, 0 failed, and
    a skip breakdown summing to zero. `reviewable_reconciles` reports False -- the run
    itself says it cannot account for 900 rows -- and the report said "no reason code
    was recorded there", which is weaker AND less true.

    A failed identity check is recorded AT that boundary and is exactly a reason."""

    def _delivery(self, **kw):
        base = {"reviewable_submitted": 1681, "created": 781, "failed": 0,
                "skip_breakdown": {"account_suppressed": 0, "other": 0},
                "reviewable_reconciles": False}
        base.update(kw)
        return _Run(delivery=base)

    def test_the_unaccounted_remainder_becomes_a_reason(self):
        self.assertEqual(mx.reason_census([self._delivery()]),
                         {"delivery_unreconciled": 900})

    def test_named_skips_are_subtracted_from_the_remainder(self):
        census = mx.reason_census([self._delivery(
            skip_breakdown={"no_contact": 400, "other": 0})])
        self.assertEqual(census["no_contact"], 400)
        self.assertEqual(census["delivery_unreconciled"], 500)

    def test_a_reconciling_run_contributes_no_such_reason(self):
        census = mx.reason_census([self._delivery(
            reviewable_reconciles=True, skip_breakdown={"no_contact": 900})])
        self.assertNotIn("delivery_unreconciled", census)

    def test_a_missing_flag_is_not_read_as_a_failure(self):
        run = _Run(delivery={"reviewable_submitted": 10, "created": 10,
                             "skip_breakdown": {}})
        self.assertNotIn("delivery_unreconciled", mx.reason_census([run]))

    def test_it_makes_the_boundary_attributable_and_actionable(self):
        metrics = {}
        for key, value in (("contacts_found", 1048), ("sent_to_airtable", 781)):
            m = mx.Metric(key=key, label=key, unit="company_role_bucket_opportunity",
                          value=value)
            m.status = mx.STATUS_MEASURED
            m.counted_unit = "company_role_bucket_opportunity"
            m.cohort = "run_window"
            m.source = mx.SOURCE_RUN_ARTIFACTS
            m.contributing_run_ids = ["r1"]
            m.evidence = ["r1"]
            metrics[key] = m
        found = bn.identify(metrics, run_count=1,
                            reasons={"delivery_unreconciled": 900})
        self.assertEqual(found.kind, "funnel_boundary")
        plan = " ".join(str(getattr(s, "text", s))
                        for s in bn.action_plan(found, metrics))
        self.assertIn("could not account for", plan)


class TheDerivedReasonSurvivesAWinningLedger(unittest.TestCase):
    """The compact ledger's `loss_reasons` wins over the artifacts, and rightly so --
    reading both double-counts, because the ledger copy IS the same merge. But
    `delivery_unreconciled` is DERIVED from counters rather than merged from a
    source, so a ledger written before the reason existed cannot contain it. Letting
    the ledger win over it would leave the question permanently unanswered on exactly
    the runs that need it -- which is what happened on the seventh production pass:
    the report was unchanged because the 09-04 ledger block won.
    """

    def _run(self, ledger, delivery):
        return _Run(**{mx.LEDGER_STEM: {"loss_reasons": ledger}, "delivery": delivery})

    DELIVERY = {"reviewable_submitted": 1681, "created": 781, "failed": 0,
                "skip_breakdown": {"other": 0}, "reviewable_reconciles": False}

    def test_a_winning_ledger_does_not_suppress_it(self):
        census = mx.reason_census([self._run({"not_icp": 12}, self.DELIVERY)])
        self.assertEqual(census["not_icp"], 12)
        self.assertEqual(census["delivery_unreconciled"], 900)

    def test_a_ledger_that_already_carries_it_is_not_double_counted(self):
        census = mx.reason_census([self._run(
            {"not_icp": 12, "delivery_unreconciled": 900}, self.DELIVERY)])
        self.assertEqual(census["delivery_unreconciled"], 900)

    def test_it_is_added_once_when_the_artifacts_answer(self):
        census = mx.reason_census([self._run({}, self.DELIVERY)])
        self.assertEqual(census["delivery_unreconciled"], 900)

    def test_a_negative_or_zero_gap_adds_nothing(self):
        census = mx.reason_census([self._run({"not_icp": 3}, {
            "reviewable_submitted": 10, "created": 10, "failed": 0,
            "skip_breakdown": {"no_contact": 5}, "reviewable_reconciles": False})])
        self.assertNotIn("delivery_unreconciled", census)


class BothCensusImplementationsMustAgree(unittest.TestCase):
    """`run_ledger.reason_census_from_parts` and `weekly_report.metrics.reason_census`
    are two implementations of one concept, and the ledger's docstring promises they
    match "so a ledger-only week and an artifact-backed week produce identical
    totals". Changing one and not the other broke the production A/B on the eighth
    pass: `ACCEPTED: False`, because a derived reason the report could compute from
    the heavy delivery artifact had no way into the durable record.
    """

    DELIVERY = {"reviewable_submitted": 1681, "created": 781, "failed": 0,
                "skip_breakdown": {"account_suppressed": 0, "no_contact": 0},
                "skipped_already_delivered": 0, "person_employer_duplicate": 0,
                "reviewable_reconciles": False}

    def _both(self, delivery, waterfall=None, qual=None, loss=None):
        from orchestrator.run_ledger import reason_census_from_parts

        ledger_side = reason_census_from_parts(waterfall, loss, delivery,
                                               qual_reasons=qual)
        run = _Run(delivery=delivery, waterfall=waterfall,
                   orchestrator_result={"enrichment": {
                       "loss_census": loss or {},
                       "funnel": {"qual_reason_counts": qual or {}}}})
        return ledger_side, mx.reason_census([run])

    def test_they_agree_on_the_unreconciled_remainder(self):
        ledger_side, report_side = self._both(self.DELIVERY)
        self.assertEqual(ledger_side, {"delivery_unreconciled": 900})
        self.assertEqual(ledger_side, report_side)

    def test_they_agree_when_zeros_are_the_only_skips(self):
        delivery = dict(self.DELIVERY, reviewable_reconciles=True)
        ledger_side, report_side = self._both(delivery)
        self.assertEqual(ledger_side, {})
        self.assertEqual(ledger_side, report_side)

    def test_they_agree_on_pre_submission_withholding(self):
        delivery = dict(self.DELIVERY, reviewable_reconciles=True,
                        skipped_already_delivered=260, person_employer_duplicate=7)
        ledger_side, report_side = self._both(delivery)
        self.assertEqual(ledger_side,
                         {"already_delivered": 260, "person_employer_duplicate": 7})
        self.assertEqual(ledger_side, report_side)

    def test_they_agree_with_qualification_reasons_mixed_in(self):
        ledger_side, report_side = self._both(
            self.DELIVERY, qual={"not_icp": 40, "in_crm": 0},
            loss={"no_search_domain": 12})
        self.assertEqual(ledger_side, report_side)
        self.assertNotIn("in_crm", ledger_side)
        self.assertEqual(ledger_side["not_icp"], 40)
