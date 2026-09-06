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
