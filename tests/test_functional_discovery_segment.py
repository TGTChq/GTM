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
