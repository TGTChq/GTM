"""Phase 1A: lineage, waterfall, fresh inventory, ATS persistence, budgets.

The numbers used as fixtures come from the exact reconstructed run
(2026-07-30 04:56): 2,045 raw, 1,596 into the filter, 159 kept, 1,437 rejected
across primary reason codes that sum exactly, 255 companies considered, 30
hiring managers identified, 15 FINAL_PASS, 28 Airtable rows. Using real numbers
means a test failing here means the accounting is wrong, not the fixture.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from retrieval_measurement.drivers import PartialAtsLane, run_ats_lane
from retrieval_measurement.evidence import (
    CompanyLedger,
    RunLineage,
    Waterfall,
    WaterfallUnreconciled,
    company_identity,
    depletion,
    measure_fresh_inventory,
)
from retrieval_measurement.instrument import (
    MeasuringFetcher,
    RequestBudget,
    RequestCeilingReached,
)
from retrieval_measurement.schema import BoardResult

FILTER_REASONS = {
    "excluded_role_mismatch": 672, "excluded_posting_integrity": 141,
    "excluded_stale": 138, "excluded_restricted_role": 129,
    "excluded_previously_seen": 87, "excluded_industry": 69,
    "excluded_staffing": 55, "excluded_duplicate": 46,
    "excluded_non_full_time": 39, "excluded_non_us": 33,
    "excluded_outsourcing": 19, "excluded_aggregator": 5,
    "excluded_non_paying": 2, "excluded_crm": 2,
}


class Wire:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs.get("url") or (args[1] if len(args) > 1 else ""))
        return mock.Mock(status_code=200, text="{}", url="https://example.test/x")


# --------------------------------------------------------------------------
# A -- lineage and target semantics
# --------------------------------------------------------------------------


class LineageTests(unittest.TestCase):
    def test_every_artifact_carries_run_id_commit_and_fingerprint(self):
        lineage = RunLineage(run_arguments={"mode": "test"})
        stamp = lineage.stamp()
        for key in ("run_id", "git_commit", "config_fingerprint"):
            self.assertIn(key, stamp)
            self.assertTrue(str(stamp[key]), key)
        self.assertEqual(stamp["git_commit"], lineage.git_commit)

    def test_no_secret_value_reaches_the_lineage(self):
        import config

        secret = "zz-phase1a-unmistakable-secret-zz"
        with mock.patch.object(config, "RAPIDAPI_KEY", secret):
            blob = json.dumps(RunLineage().to_dict())
        self.assertNotIn(secret, blob)
        for entry in json.loads(blob)["effective_config"]:
            if entry["redacted"]:
                self.assertEqual(set(entry["value"]), {"configured"})

    def test_the_stop_metric_is_named_not_only_the_reason(self):
        lineage = RunLineage()
        lineage.declare_stop("final_pass_leads", "final_pass_target_reached")
        self.assertEqual(lineage.to_dict()["stop_metric"], "final_pass_leads")
        self.assertEqual(lineage.to_dict()["stop_reason"], "final_pass_target_reached")


class TargetSemanticsTests(unittest.TestCase):
    """The production condition, exercised directly."""

    @staticmethod
    def _stops(*, reviewable, final_pass, target, continue_after):
        # Mirrors hiring_manager.py:1282-1300 exactly.
        return bool(
            True                       # strict_input
            and target is not None
            and final_pass >= target
            and not continue_after
        )

    def test_reviewable_thirty_with_final_pass_fifteen_does_not_satisfy_the_target(self):
        self.assertFalse(
            self._stops(reviewable=30, final_pass=15, target=30, continue_after=False),
            "review rows satisfied a FINAL_PASS target -- this is the 04:56 defect",
        )

    def test_the_target_is_satisfied_only_by_reconciled_final_pass(self):
        self.assertTrue(self._stops(reviewable=30, final_pass=30, target=30, continue_after=False))
        self.assertTrue(self._stops(reviewable=99, final_pass=31, target=30, continue_after=False))
        self.assertFalse(self._stops(reviewable=99, final_pass=29, target=30, continue_after=False))

    def test_production_behaviour_is_unchanged_while_the_continue_flag_is_set(self):
        # CONTINUE_AFTER_FINAL_PASS_TARGET defaults True, so the break stays
        # inert exactly as before. This change corrects semantics, not output.
        self.assertFalse(self._stops(reviewable=30, final_pass=99, target=30, continue_after=True))

    def test_the_live_condition_matches_this_model(self):
        import config
        import hiring_manager

        source = Path(hiring_manager.__file__).read_text(encoding="utf-8")
        self.assertIn("and final_pass_leads >= target_final_pass_leads", source)
        self.assertNotIn("and reviewable_leads >= target_final_pass_leads", source)
        self.assertTrue(config.CONTINUE_AFTER_FINAL_PASS_TARGET)


# --------------------------------------------------------------------------
# B -- waterfall
# --------------------------------------------------------------------------


class WaterfallTests(unittest.TestCase):
    def _run_0456(self):
        w = Waterfall(run_id="r-0456")
        w.add("acquisition", unit="posting", entered=2045, passed=1486, rejected=559,
              primary_reasons={"cross_source_duplicate": 430, "excluded_by_seniority": 91,
                               "previously_seen": 38})
        w.add("filter", unit="posting", entered=1596, passed=159, rejected=1437,
              primary_reasons=FILTER_REASONS,
              note="1,486 selected + 110 recoverable re-injected")
        w.add("qualification", unit="posting", entered=159, passed=149, rejected=10)
        w.add("company_expansion", unit="company", entered=255, passed=141, rejected=114,
              note="unit changes here: postings become companies")
        w.add("hiring_manager", unit="company", entered=255, passed=30, rejected=111,
              deferred=114)
        w.add("contact_verification", unit="contact", entered=30, passed=15, rejected=15)
        w.record_states(final_pass=15, needs_check=45, unverified=79, reject=122,
                        reroute=2, airtable_created=28, outbound_enrolled=0)
        return w

    def test_every_boundary_reconciles(self):
        self._run_0456().assert_reconciled()

    def test_the_real_filter_reason_codes_sum_exactly_to_rejections(self):
        self.assertEqual(sum(FILTER_REASONS.values()), 1437)
        self.assertEqual(self._run_0456().reason_mismatches(), [])

    def test_an_unaccounted_record_is_refused(self):
        w = Waterfall()
        w.add("filter", unit="posting", entered=1596, passed=159, rejected=1000)
        with self.assertRaises(WaterfallUnreconciled):
            w.assert_reconciled()

    def test_reason_codes_that_do_not_sum_are_refused(self):
        w = Waterfall()
        w.add("filter", unit="posting", entered=100, passed=40, rejected=60,
              primary_reasons={"a": 30})
        with self.assertRaises(WaterfallUnreconciled):
            w.assert_reconciled()

    def test_units_never_mix_silently(self):
        w = self._run_0456()
        transitions = w.unit_transitions()
        self.assertIn("posting->company", transitions)
        self.assertIn("company->contact", transitions)
        for b in w.boundaries:
            self.assertIn(b.unit, ("posting", "company", "contact"))

    def test_cumulative_survival_is_not_computed_across_a_unit_change(self):
        w = self._run_0456()
        company_first = next(b for b in w.boundaries if b.unit == "company")
        # 141/255 within the company unit -- never 141/2045 across units.
        self.assertAlmostEqual(company_first.cumulative_survival, 141 / 255, places=6)
        posting_last = [b for b in w.boundaries if b.unit == "posting"][-1]
        self.assertAlmostEqual(posting_last.cumulative_survival, 149 / 2045, places=6)

    def test_terminal_states_are_reported_separately(self):
        d = self._run_0456().to_dict()
        self.assertEqual(d["final_pass_leads"], 15)
        self.assertEqual(d["reviewable_rows"], 60)         # 15 + 45
        self.assertEqual(d["airtable_created"], 28)
        self.assertEqual(d["outbound_enrolled"], 0)
        self.assertNotEqual(d["final_pass_leads"], d["airtable_created"])
        self.assertTrue(d["reconciled"])

    def test_an_unknown_unit_is_refused(self):
        with self.assertRaises(ValueError):
            Waterfall().add("x", unit="leads", entered=1, passed=1)


# --------------------------------------------------------------------------
# C -- fresh inventory
# --------------------------------------------------------------------------


class FreshInventoryTests(unittest.TestCase):
    JOBS = [
        {"employer_name": "Acme, Inc.", "employer_website": "https://acme.com"},
        {"employer_name": "Acme", "employer_website": "https://www.acme.com/careers"},
        {"employer_name": "Globex LLC"},
        {"employer_name": "Initech", "employer_website": "https://initech.io"},
    ]

    def test_company_identity_is_stable_across_naming_variation(self):
        a = company_identity(self.JOBS[0])
        b = company_identity(self.JOBS[1])
        self.assertEqual(a, b, "same company under two spellings must share an identity")
        self.assertTrue(a.startswith("domain:"))
        self.assertEqual(company_identity(self.JOBS[2]), "name:globex")

    def test_new_and_previously_processed_reconcile(self):
        ledger = CompanyLedger({company_identity(self.JOBS[0])})
        fresh = measure_fresh_inventory(self.JOBS, run_id="r1", ledger=ledger)
        self.assertEqual(fresh.total_companies_observed, 3)
        self.assertEqual(fresh.new_companies, 2)
        self.assertEqual(fresh.previously_processed_companies, 1)
        self.assertTrue(fresh.reconciles)

    def test_without_a_ledger_nothing_is_claimed_as_evidence_of_fresh_supply(self):
        fresh = measure_fresh_inventory(self.JOBS)
        self.assertFalse(fresh.snapshot_available)
        self.assertEqual(fresh.new_companies, 3)
        self.assertTrue(fresh.reconciles)

    def test_eligible_and_suppressed_counts_are_tracked_separately(self):
        eligible = {company_identity(self.JOBS[3])}
        suppressed = {company_identity(self.JOBS[2])}
        fresh = measure_fresh_inventory(
            self.JOBS, ledger=CompanyLedger({"domain:other.com"}),
            eligible_companies=eligible, suppressed_companies=suppressed,
        )
        self.assertEqual(fresh.new_icp_eligible_companies, 1)
        self.assertEqual(fresh.suppressed_companies, 1)

    def test_the_ledger_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            path.write_text(json.dumps({"companies": ["domain:acme.com"]}), encoding="utf-8")
            before = path.read_bytes()
            ledger = CompanyLedger.load(path)
            self.assertTrue(ledger.has("domain:acme.com"))
            self.assertEqual(path.read_bytes(), before)

    def test_depletion_detects_the_real_255_131_78_curve(self):
        d = depletion([{"new_companies": 255}, {"new_companies": 131}, {"new_companies": 78}])
        self.assertEqual(d["trend"], "declining")
        self.assertFalse(d["sustainable"])
        self.assertEqual(d["series"], [255, 131, 78])

    def test_depletion_reports_a_flat_curve_as_sustainable(self):
        self.assertTrue(depletion([{"new_companies": 200}] * 3)["sustainable"])


# --------------------------------------------------------------------------
# D/E -- ATS persistence and scoped budgets
# --------------------------------------------------------------------------


def board(provider, identifier, company="Co"):
    return {"provider": provider, "identifier": identifier, "company_name": company}


class AtsPersistenceTests(unittest.TestCase):
    def _fetcher(self, fail_on=None, per_board=2):
        state = {"n": 0}

        def inner(url, **_kw):
            state["n"] += 1
            return mock.Mock(status_code=200, text="{}", url=url)

        fetcher = MeasuringFetcher(inner=inner)
        return fetcher

    def test_completed_boards_survive_a_later_board_failure(self):
        boards = [board("greenhouse", f"b{i}") for i in range(5)]
        seen = []

        def failing_fetch(b, _f):
            if b["identifier"] == "b3":
                raise RuntimeError("board 3 exploded")
            return [{"job_id": f"{b['identifier']}-1", "employer_name": "Co",
                     "job_title": "Engineer"}], ""

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("retrieval_measurement.drivers.fetch_board_jobs", failing_fetch):
                outputs = run_ats_lane(
                    self._fetcher(), boards, checkpoint_dir=tmp,
                    on_board=seen.append,
                )
            files = sorted(p.name for p in Path(tmp).glob("*.json"))
            self.assertEqual(len(files), 5, "every attempted board must be persisted")
            self.assertEqual(len(seen), 5)
            errored = [r for r in seen if r.error]
            self.assertEqual(len(errored), 1)
            self.assertIn("RuntimeError", errored[0].error)
            # Boards 0,1,2,4 kept their records despite board 3 failing.
            kept = sum(r.canonical_records for r in seen)
            self.assertEqual(kept, 4)
            self.assertTrue(outputs, "the lane still returned its completed work")

    def test_each_board_record_carries_the_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "retrieval_measurement.drivers.fetch_board_jobs",
                lambda b, f: ([{"job_id": "1", "employer_name": "Co", "job_title": "Eng"}], ""),
            ):
                seen = []
                run_ats_lane(self._fetcher(), [board("lever", "acme")],
                             checkpoint_dir=tmp, on_board=seen.append)
            r = seen[0]
            for field in ("provider", "identifier", "company_name", "started_at",
                          "completed_at", "physical_requests", "pages", "redirects",
                          "retries", "listing_records", "detail_records",
                          "canonical_records", "unique_posting_identity",
                          "unique_production_equivalent", "stop_reason", "error",
                          "checkpoint_path"):
                self.assertIn(field, r.to_dict(), field)
            self.assertTrue(Path(r.checkpoint_path).is_file())

    def test_physical_requests_are_attributed_to_the_right_board_and_provider(self):
        budget = RequestBudget(100)
        wire = Wire()

        def fetch(b, _f):
            for _ in range(3):
                requests.request("GET", f"https://{b['identifier']}.example/api")
            return [], ""

        with mock.patch.object(requests, "request", wire), budget.installed():
            with mock.patch("retrieval_measurement.drivers.fetch_board_jobs", fetch):
                run_ats_lane(MeasuringFetcher(inner=lambda u, **k: None),
                             [board("workday", "alpha"), board("greenhouse", "beta")],
                             budget=budget)
        self.assertEqual(budget.per_board["workday:alpha"], 3)
        self.assertEqual(budget.per_board["greenhouse:beta"], 3)
        self.assertEqual(budget.per_provider["ats_workday"], 3)
        self.assertEqual(budget.per_provider["ats_greenhouse"], 3)
        self.assertEqual(budget.per_lane["ats_board"], 6)


class ScopedBudgetTests(unittest.TestCase):
    def test_board_budget_blocks_before_network_and_spares_the_next_board(self):
        budget = RequestBudget(1000, board_limit=2)
        wire = Wire()

        def fetch(b, _f):
            for _ in range(5):
                requests.request("GET", f"https://{b['identifier']}.example/api")
            return [], ""

        with mock.patch.object(requests, "request", wire), budget.installed():
            with mock.patch("retrieval_measurement.drivers.fetch_board_jobs", fetch):
                run_ats_lane(MeasuringFetcher(inner=lambda u, **k: None),
                             [board("workday", "a"), board("workday", "b")],
                             budget=budget)
        self.assertEqual(len(wire.calls), 4, "2 per board, then blocked before the wire")
        self.assertEqual(budget.per_board["workday:a"], 2)
        self.assertEqual(budget.per_board["workday:b"], 2)
        self.assertFalse(budget.exhausted, "a board budget is not a run stop")

    def test_provider_budget_stops_that_provider_only(self):
        budget = RequestBudget(1000, provider_limits={"ats_workday": 2})
        wire = Wire()

        def fetch(b, _f):
            for _ in range(3):
                requests.request("GET", f"https://{b['identifier']}.example/api")
            return [], ""

        with mock.patch.object(requests, "request", wire), budget.installed():
            with mock.patch("retrieval_measurement.drivers.fetch_board_jobs", fetch):
                run_ats_lane(MeasuringFetcher(inner=lambda u, **k: None),
                             [board("workday", "a"), board("workday", "b"),
                              board("lever", "c")],
                             budget=budget)
        self.assertEqual(budget.per_provider["ats_workday"], 2)
        self.assertEqual(budget.per_provider["ats_lever"], 3, "lever must be unaffected")
        self.assertFalse(budget.exhausted)

    def test_reserved_capacity_stops_ats_starving_jsearch(self):
        # The exact failure of 20260805T015708Z-1ad3ef58: ATS spent 971 of 1000
        # and JSearch never ran.
        budget = RequestBudget(10, reserved_for_lanes={"jsearch": 4})
        wire = Wire()
        with mock.patch.object(requests, "request", wire), budget.installed():
            with budget.context(lane="ats"):
                for _ in range(6):
                    requests.request("GET", "https://board.example/api")
                with self.assertRaises(RequestCeilingReached):
                    requests.request("GET", "https://board.example/api")
            with budget.context(lane="jsearch"):
                for _ in range(4):
                    requests.request("GET", "https://jsearch.p.rapidapi.com/search-v2")
        self.assertEqual(budget.per_lane["ats"], 6)
        self.assertEqual(budget.per_lane["jsearch"], 4, "reserved capacity was honoured")
        self.assertEqual(len(wire.calls), 10)

    def test_lane_budget_does_not_stop_an_unrelated_lane(self):
        budget = RequestBudget(100, lane_limits={"ats": 2})
        wire = Wire()
        with mock.patch.object(requests, "request", wire), budget.installed():
            with budget.context(lane="ats"):
                requests.request("GET", "https://a.example")
                requests.request("GET", "https://a.example")
                with self.assertRaises(RequestCeilingReached):
                    requests.request("GET", "https://a.example")
            with budget.context(lane="free_feeds"):
                requests.request("GET", "https://himalayas.app/jobs/api")
        self.assertEqual(budget.per_lane["ats"], 2)
        self.assertEqual(budget.per_lane["free_feeds"], 1)
        self.assertFalse(budget.exhausted)

    def test_budget_exhaustion_is_never_provider_exhaustion(self):
        from retrieval_measurement.schema import TRUNCATION_KINDS

        budget = RequestBudget(1, board_limit=1)
        with mock.patch.object(requests, "request", Wire()), budget.installed():
            with budget.context(lane="ats", source="ats_workday", board="workday:a"):
                requests.request("GET", "https://a.example")
                with self.assertRaises(RequestCeilingReached):
                    requests.request("GET", "https://a.example")
        scope = budget.exhausted_scopes[0]
        self.assertEqual(scope["scope"], "board")
        self.assertNotIn(scope["scope"], TRUNCATION_KINDS)
        self.assertNotIn("request_ceiling_reached", TRUNCATION_KINDS)

    def test_omitting_every_new_budget_argument_preserves_prior_behaviour(self):
        budget = RequestBudget(None)
        original = requests.request
        with budget.installed():
            self.assertIs(requests.request, original)
        self.assertEqual(budget.lane_limits, {})
        self.assertEqual(budget.provider_limits, {})
        self.assertIsNone(budget.board_limit)
        self.assertEqual(budget.reserved_for_lanes, {})
        self.assertFalse(budget.to_dict()["enforced"])

    def test_a_run_budget_alone_behaves_exactly_as_before(self):
        budget = RequestBudget(3)
        wire = Wire()
        with mock.patch.object(requests, "request", wire), budget.installed():
            for _ in range(3):
                requests.request("GET", "https://x.example")
            with self.assertRaises(RequestCeilingReached):
                requests.request("GET", "https://x.example")
        self.assertTrue(budget.exhausted)
        self.assertEqual(budget.stop_reason, "request_ceiling_reached")
        self.assertEqual(len(wire.calls), 3)


if __name__ == "__main__":
    unittest.main()
