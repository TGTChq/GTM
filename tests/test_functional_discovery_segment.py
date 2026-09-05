"""Reaching relevant work whose TITLE we do not recognise.

Title synonyms have a ceiling. They lengthen the list of titles we match, and a
posting whose title resembles none of them stays invisible however many aliases are
added. The catalog is 111 families and the production expression is 4,222 characters;
the next alias buys less than the last.

The provider's own contract closes the obvious workaround. Its `_advanced` parameters
are **ANDed together at the query level**, so attaching `description_advanced` to the
existing `title_advanced` request can only NARROW it. A description expression cannot
rescue a job the title filter already excluded, because the title filter still has to
match.

So functional discovery is a SEPARATE REQUEST: description expression, no title
filter, every other ICP filter intact. These tests hold that shape, and hold the line
that it widens what is CONSIDERED and never what is APPROVED.

Default OFF and unevaluated. What is proven here is that the path is correctly built,
correctly gated, correctly budgeted and correctly attributed. What is NOT proven here
is its incremental yield, which no offline test can establish.
"""

from __future__ import annotations

import unittest
from unittest import mock

import config
import fantastic_jobs_adapter as fja

EXPR = "('pipeline generation' | 'quota carrying') & !intern"


def _enabled(**over):
    base = dict(
        FANTASTIC_FUNCTIONAL_DISCOVERY_ENABLED=True,
        FANTASTIC_JOBS_FUNCTIONAL_LIMIT=500,
        FANTASTIC_FUNCTIONAL_DESCRIPTION_ADVANCED=EXPR,
        FANTASTIC_JOBS_TIME_FRAME="7d",
        FANTASTIC_JOBS_LINKEDIN_LIMIT=3000,
        FANTASTIC_JOBS_MAX_JOBS_PER_RUN=12000,
        FANTASTIC_JOBS_ATS_LIMIT=0,
        FANTASTIC_JOBS_WELLFOUND_LIMIT=0,
        FANTASTIC_JOBS_YCOMBINATOR_LIMIT=0,
        FANTASTIC_ATS_SOURCE_ENABLED=False,
        FANTASTIC_WELLFOUND_SOURCE_ENABLED=False,
        FANTASTIC_YCOMBINATOR_SOURCE_ENABLED=False,
    )
    base.update(over)
    return base


class TheRequestShape(unittest.TestCase):
    def test_it_carries_a_description_expression_and_no_title_filter(self):
        """The whole point. A title filter here would confine the segment to the jobs
        the main query already reaches."""
        with mock.patch.multiple(config, **_enabled()):
            params = fja.build_functional_params()

        self.assertEqual(params["description_advanced"], EXPR)
        self.assertNotIn("title_advanced", params)
        self.assertNotIn("title_filter", params)

    def test_every_other_icp_filter_is_still_applied(self):
        """It widens the query on ONE axis. Location, headcount and employment type
        are not part of the question being asked."""
        with mock.patch.multiple(config, **_enabled()):
            params = fja.build_functional_params()
            baseline = fja.build_jb_params("linkedin")

        shared = {k: v for k, v in baseline.items()
                  if k not in ("source", "title_advanced")}
        for key, value in shared.items():
            self.assertEqual(params.get(key), value, f"{key} must be preserved")

    def test_an_empty_expression_produces_no_request_at_all(self):
        """A description query with no expression is an unrestricted description
        search. It must be impossible to issue one by leaving a variable unset."""
        with mock.patch.multiple(config, **_enabled(
                FANTASTIC_FUNCTIONAL_DESCRIPTION_ADVANCED="")):
            self.assertEqual(fja.build_functional_params(), {})


class TheGate(unittest.TestCase):
    def test_it_is_off_unless_a_flag_and_a_limit_and_an_expression_all_say_yes(self):
        """The same two-key shape Wellfound and Y Combinator use, plus the
        expression: a code deploy can never start paying for this."""
        cases = {
            "flag off": _enabled(FANTASTIC_FUNCTIONAL_DISCOVERY_ENABLED=False),
            "no limit": _enabled(FANTASTIC_JOBS_FUNCTIONAL_LIMIT=0),
            "no expression": _enabled(FANTASTIC_FUNCTIONAL_DESCRIPTION_ADVANCED=""),
        }
        for name, cfg in cases.items():
            with self.subTest(name), mock.patch.multiple(config, **cfg):
                plan = fja.build_source_plan(title_advanced_active=True,
                                             used_title_families=False)
                self.assertNotIn("functional", [s.key for s in plan], name)

    def test_fully_configured_it_is_planned_as_its_own_segment(self):
        with mock.patch.multiple(config, **_enabled()):
            plan = fja.build_source_plan(title_advanced_active=True,
                                         used_title_families=False)

        seg = next(s for s in plan if s.key == "functional")
        self.assertEqual(seg.label, fja.FUNCTIONAL_SOURCE)
        self.assertEqual(seg.endpoint, "/v1/active-jb")
        self.assertEqual(seg.limit, 500)
        self.assertEqual(seg.dispatch, "functional")
        self.assertFalse(seg.feeds_cursor,
                         "it must not drive the LinkedIn date_posted cursor")

    def test_the_config_refuses_the_frame_the_provider_rejects(self):
        """`description_advanced` returns HTTP 400 on `time_frame=6m`. Failing the
        deploy is better than discovering it as a run-time 400."""
        with mock.patch.multiple(config, **_enabled(
                FANTASTIC_JOBS_TIME_FRAME="6m", FANTASTIC_JOBS_ENABLED=True,
                FANTASTIC_JOBS_API_KEY="k")):
            with self.assertRaises(ValueError) as caught:
                config.validate_fantastic_jobs_config()
        self.assertIn("6m", str(caught.exception))

    def test_the_config_refuses_a_funded_segment_with_no_expression(self):
        with mock.patch.multiple(config, **_enabled(
                FANTASTIC_FUNCTIONAL_DESCRIPTION_ADVANCED="",
                FANTASTIC_JOBS_ENABLED=True, FANTASTIC_JOBS_API_KEY="k")):
            with self.assertRaises(ValueError) as caught:
                config.validate_fantastic_jobs_config()
        self.assertIn("unrestricted description search", str(caught.exception))


class ItSharesTheRunBudgetLikeEverySource(unittest.TestCase):
    def test_it_is_one_claimant_among_the_others_under_fair_share(self):
        """It must not get an independent allowance: the governor funds one run cap
        and every segment draws from it. Equally, it must not be starved by a source
        with proven yield before it has any -- which is what the allocator's
        exploration floor is for."""
        with mock.patch.multiple(config, **_enabled()):
            plan = fja.build_source_plan(title_advanced_active=True,
                                         used_title_families=False)
            allocator = fja._SourceBudgetAllocator(
                budget=1000, segments=plan, policy="fair_share")
            grants = {s.key: allocator.grant(s, 0) for s in plan}

        self.assertIn("functional", grants)
        self.assertGreater(grants["functional"], 0, "it must actually be funded")
        self.assertLessEqual(sum(grants.values()), 1000,
                             "no segment may claim budget outside the run cap")


class WhatItIsNotAllowedToChange(unittest.TestCase):
    def test_its_rows_are_labelled_so_its_yield_stands_on_its_own(self):
        """Billing, novelty and drain state are attributed to this segment
        separately, so its incremental value can be measured rather than argued."""
        with mock.patch.multiple(config, **_enabled()):
            plan = fja.build_source_plan(title_advanced_active=True,
                                         used_title_families=False)
        labels = [s.label for s in plan]
        self.assertEqual(len(labels), len(set(labels)), "labels are distinct")
        self.assertIn(fja.FUNCTIONAL_SOURCE, labels)

    def test_it_does_not_relax_any_downstream_gate(self):
        """It widens what is CONSIDERED. RoleGate, ICP, firmographics and send-safe
        approval decide what is delivered, and this segment touches none of them.
        Asserted on the request itself: no parameter it sends bypasses a downstream
        decision, because the only thing it removes is the title restriction."""
        with mock.patch.multiple(config, **_enabled()):
            params = fja.build_functional_params()
            # The production shape: LinkedIn WITH the role-catalog title expression.
            baseline = fja.build_jb_params("linkedin", title_advanced_expr="x | y")

        removed = set(baseline) - set(params)
        self.assertEqual(removed, {"source", "title_advanced"},
                         "the ONLY filter dropped is the title restriction")


if __name__ == "__main__":
    unittest.main()


class TheFrameContract(unittest.TestCase):
    """`6m` is a documented provider frame and is not a frame this engine can page.

    The provider assigns offset+limit to 1h/24h/7d and reserves id-cursor pagination
    for 6m, which also reorders results from `date_posted` DESC to `id` ASC, and
    warns against resuming an offset run with a cursor or the reverse. This adapter
    pages and persists OFFSETS and implements no id cursor, so the combination is
    rejected rather than half-supported. Parsing the string does not create a cursor
    mode, a separate state namespace, or a budget for historical recovery.
    """

    def test_six_months_is_refused_because_we_page_by_offset(self):
        with mock.patch.multiple(config, FANTASTIC_JOBS_TIME_FRAME="6m",
                                 FANTASTIC_JOBS_ENABLED=True,
                                 FANTASTIC_JOBS_API_KEY="k"):
            with self.assertRaises(ValueError) as caught:
                config.validate_fantastic_jobs_config()
        message = str(caught.exception)
        self.assertIn("cursor", message)
        self.assertIn("OFFSET", message)

    def test_the_documented_short_frames_are_accepted(self):
        for frame in ("1h", "24h", "7d"):
            with self.subTest(frame), mock.patch.multiple(
                    config, FANTASTIC_JOBS_TIME_FRAME=frame,
                    FANTASTIC_JOBS_ENABLED=True, FANTASTIC_JOBS_API_KEY="k",
                    FANTASTIC_FUNCTIONAL_DISCOVERY_ENABLED=False):
                config.validate_fantastic_jobs_config()

    def test_a_month_horizon_is_calendar_exact_not_an_average(self):
        """The horizon is a floor the provider enforces. Approximating it with an
        average month either invents a floor above the provider's -- discarding
        inventory the feed would still serve -- or one below it, leaving a dead zone
        no request can reach."""
        from datetime import datetime, timezone

        # 31 August minus six months lands in February: the day must clamp, not roll.
        horizon = fja._frame_horizon(
            datetime(2026, 8, 31, 12, tzinfo=timezone.utc), "6m", 0)
        self.assertEqual((horizon.year, horizon.month, horizon.day), (2026, 2, 28))

        # And a real calendar span, not 180 days.
        span = (datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
                - fja._frame_horizon(datetime(2026, 9, 5, 12, tzinfo=timezone.utc), "6m", 0)).days
        self.assertGreaterEqual(span, 181, "six calendar months is never 180 days")

    def test_hours_and_days_are_unchanged(self):
        from datetime import datetime, timedelta, timezone

        now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
        self.assertEqual(fja._frame_horizon(now, "7d", 0), now - timedelta(days=7))
        self.assertEqual(fja._frame_horizon(now, "24h", 0), now - timedelta(hours=24))


class FunctionalRowsReachRoleGateWithATarget(unittest.TestCase):
    """RoleGate verifies a title AGAINST A TARGET ROLE. A functional result has no
    matched title family by construction, so without a classification step every row
    the segment returns is UNVERIFIED whatever it is -- which reads as "functional
    discovery finds nothing relevant" when it means "the gate was never told what to
    check against". That was an integration gap in this path, not evidence about the
    postings.

    `_classify` is the existing supported answer, the same step the external-batch
    path runs. It supplies the missing input and nothing else: a catalog role when
    the title maps to one, no role when it does not. Commercial fit is never inferred
    from task vocabulary.
    """

    def test_a_catalog_title_gets_its_role_target(self):
        rows = [{"job_title": "Lifecycle Marketing Specialist",
                 "job_description": "own the lifecycle programme",
                 "employer_name": "Acme", "job_id": "j1"}]
        metrics = {}
        fja._classify_functional_rows(rows, metrics)

        self.assertTrue(rows[0].get("_matched_role"),
                        "a catalog title must reach RoleGate with a target")
        self.assertNotEqual(rows[0].get("_role_relevance_status"), "reject")
        self.assertEqual(metrics["functional_discovery"]["role_relevant"], 1)

    def test_a_title_outside_the_catalog_stays_reviewable(self):
        """Preserved uncertainty. The row keeps its acquisition and reaches review
        with no asserted role -- it is never promoted on the strength of its
        description."""
        rows = [{"job_title": "Underwater Basket Weaver",
                 "job_description": "quota carrying pipeline generation",
                 "employer_name": "Acme", "job_id": "j2"}]
        metrics = {}
        fja._classify_functional_rows(rows, metrics)

        # `_classify` always names a best-fit role; the ASSESSMENT is what carries
        # the verdict, and here it rejects. Task words in the description did not
        # manufacture relevance, which is the property that matters.
        self.assertEqual(rows[0].get("_role_relevance_status"), "reject")
        self.assertEqual(metrics["functional_discovery"]["role_rejected_reviewable"], 1)

    def test_the_split_is_reported_so_the_segment_can_be_judged(self):
        rows = [{"job_title": "Account Executive", "job_description": "d",
                 "employer_name": "A", "job_id": "a"},
                {"job_title": "Underwater Basket Weaver", "job_description": "d",
                 "employer_name": "B", "job_id": "b"}]
        metrics = {}
        fja._classify_functional_rows(rows, metrics)

        block = metrics["functional_discovery"]
        self.assertEqual(block["rows"], 2)
        self.assertEqual(block["role_relevant"] + block["role_rejected_reviewable"]
                         + block["classify_errors"], 2)

    def test_a_classifier_error_never_costs_the_acquisition(self):
        rows = [{"job_title": None, "job_description": None}]
        metrics = {}
        fja._classify_functional_rows(rows, metrics)  # must not raise
        self.assertEqual(metrics["functional_discovery"]["rows"], 1)
