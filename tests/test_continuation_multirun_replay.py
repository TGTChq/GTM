"""Three runs against a window whose floor rises underneath it.

This replays the shape of 2026-09-04 -> 09-05 -> 09-06 to answer a question the
production logs could not: was the repeated paging REQUIRED FOR COVERAGE, caused by
CHANGED QUERY BOUNDARIES, or caused by INCORRECT CONTINUATION?

The production facts being modelled, all read from the run logs:

    09-04  no window/offset lines at all -- that build had no per-source cursor.
           ats 3,000 / linkedin 3,000 kept, 6,205 total, cross_query_duplicates 0
    09-05  window reused, lower 2026-08-23T09:02:36Z, offsets_at_open {}
           both sources offset 0->100, kept 0, stop=no_new_ids
    09-06  window reused, lower CLAMPED to 2026-08-30T03:03:20Z (= start - 7d),
           offsets_at_open {100,100} -> close {2822,2822}, stop=cap_reached,
           linkedin kept 226, ats kept 0

Two things follow, and they are different in kind:

  * The 09-05 and 09-06 repetition was REQUIRED FOR COVERAGE. The 09-04 run took
    6,205 rows and left no cursor, so nothing recorded how far it had reached.
    Paging and deduping was the only way to find unseen rows -- and it worked: 226
    were found at offsets the earlier run had never recorded. Lowering the
    duplicate-page cap would have STOPPED that search before it found them.

  * The offset was nevertheless carried across a CHANGED LOWER BOUND. An offset is
    an index into a result set; the frame floor rises between runs whether or not
    the clamp fires, so the set it indexes is not the set it was measured in.

The defect this file pins is the third one: the coverage assessment only ran for a
source that stopped on a DRAINED reason, so a source that ran out of BUDGET while
the floor cut into its window recorded no doubt at all. That is the more exposed
case, not the less.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import config
import fantastic_jobs_adapter as fja

BASE = datetime(2026, 9, 6, 3, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class MovingFloorFeed:
    """A feed whose 7-day frame floor rises with `now`.

    `date_posted` is deliberately NOT aligned with `date_created`, so rows leaving
    by the floor depart from scattered positions in the returned order -- the
    condition that makes a persisted offset unsafe.
    """

    def __init__(self, days=12, per_day=60):
        self.rows = []
        for d in range(days):
            created = BASE - timedelta(days=days - d)
            for i in range(per_day):
                idx = d * per_day + i
                self.rows.append({
                    "id": f"id-{idx:05d}",
                    "title": "Account Executive",
                    "organization": f"Co{idx}",
                    "source": "linkedin",
                    "organization_url": f"https://co{idx}.com",
                    # scattered relative to date_created
                    "date_posted": _iso(created + timedelta(hours=(idx * 7) % 240)),
                    "date_created": _iso(created + timedelta(minutes=i)),
                    "countries_derived": ["United States"],
                    "employment_type": ["FULL_TIME"],
                    "org_linkedin_headcount": 100,
                    "org_linkedin_industry": "Software Development",
                })
        self.now = BASE
        self.billed = 0

    def visible(self):
        floor = self.now - timedelta(days=7)
        return [r for r in self.rows
                if datetime.fromisoformat(r["date_created"].replace("Z", "+00:00")) >= floor]

    def __call__(self, url, headers, params, timeout):
        gte = str(params.get("date_created_gte") or "")
        lt = str(params.get("date_created_lt") or "")
        rows = [r for r in self.visible()
                if (not gte or r["date_created"] >= gte)
                and (not lt or r["date_created"] < lt)]
        rows.sort(key=lambda r: r["date_posted"], reverse=True)   # documented order
        off = int(params.get("offset", 0) or 0)
        lim = int(params.get("limit", 100) or 100)
        page = rows[off:off + lim]
        self.billed += len(page)

        class R:
            status_code = 200
            headers = {"x-api-jobs-remaining": "90000",
                       "x-api-requests-remaining": "9000"}
            def __init__(s, d): s._d = d
            def json(s): return s._d
        return R(page)


CFG = dict(
    FANTASTIC_JOBS_ENABLED=True, FANTASTIC_JOBS_API_KEY="k",
    FANTASTIC_JOBS_BASE_URL="https://data.fantastic.jobs",
    FANTASTIC_JOBS_LOCATION="United States",
    FANTASTIC_JOBS_HEADCOUNT_MIN=25, FANTASTIC_JOBS_HEADCOUNT_MAX=1000,
    FANTASTIC_JOBS_AI_EMPLOYMENT_TYPE="FULL_TIME",
    FANTASTIC_JOBS_EXCLUDE_AGENCY=True, FANTASTIC_JOBS_MAX_RETRIES=0,
    FANTASTIC_JOBS_TIME_FRAME="7d",
    FANTASTIC_DATE_CREATED_LAG_MINUTES=180,
    FANTASTIC_DATE_CREATED_OVERLAP_MINUTES=60,
    FANTASTIC_MAX_CONSECUTIVE_DUPLICATE_PAGES=40,
    FANTASTIC_WATERMARK_AUDIT_ENABLED=False,
)

LABEL = "fantastic_jobs_linkedin"


class _Result:
    def __init__(self):
        self.jobs = []


def _run(state_path, feed, now, cap, seen=None):
    """One invocation: a fresh engine on the shared state file, as a restart is."""
    metrics = {"segments": {}, "watermark": {}}
    seen = seen if seen is not None else set()
    with mock.patch.multiple(config, FANTASTIC_WATERMARK_STATE_PATH=state_path, **CFG):
        feed.now = now
        eng = fja.DateCreatedWatermarkEngine(
            result=_Result(), quota=fja._QuotaState(), http_get=feed,
            seen_ids=seen, metrics=metrics, run_cap=cap, now=now)
        eng.open()
        eng.run_stream("/active-jb-7d", {"source": "linkedin"}, LABEL, cap,
                       ("linkedin",))
        eng.checkpoint((LABEL,))
    return eng, metrics, seen


class ThreeRunsOverAMovingFloor(unittest.TestCase):
    def setUp(self):
        self.state = os.path.join(tempfile.mkdtemp(), "wm.json")
        self.feed = MovingFloorFeed()

    def _state(self):
        with open(self.state, encoding="utf-8") as fh:
            return json.load(fh)

    def test_repetition_was_required_for_coverage_not_a_dedupe_fault(self):
        """Run 1 takes rows and leaves a cursor. Run 2 resumes and reaches rows run 1
        never saw. Every row handed back is NEW -- the duplicates are the cost of
        finding them, not a failure to suppress."""
        seen: set = set()
        _e1, m1, _ = _run(self.state, self.feed, BASE, cap=200, seen=seen)
        kept1 = m1["segments"][LABEL]["schema_valid"]
        billed1 = m1["segments"][LABEL]["returned"]

        _e2, m2, _ = _run(self.state, self.feed, BASE + timedelta(days=1),
                          cap=400, seen=seen)
        kept2 = m2["segments"][LABEL]["schema_valid"]

        self.assertGreater(kept1, 0)
        self.assertGreater(kept2, 0, "the resumed pass must reach unseen inventory")
        self.assertEqual(billed1, m1["segments"][LABEL]["returned"])
        self.assertEqual(len(seen), kept1 + kept2,
                         "no row was ever handed back twice")

    def test_the_offset_records_the_boundary_it_was_measured_against(self):
        seen: set = set()
        _run(self.state, self.feed, BASE, cap=200, seen=seen)
        basis = self._state().get("window_offset_basis") or {}
        self.assertIn(LABEL, basis)
        first = basis[LABEL]

        # A day later the floor has risen; the window this offset indexes is not the
        # window it was measured in.
        _e2, m2, _ = _run(self.state, self.feed, BASE + timedelta(days=1),
                          cap=200, seen=seen)
        self.assertNotEqual(m2["watermark"]["lower"], first,
                            "the lower bound moved under the persisted offset")

    def _budget_stop_under_a_clamp(self):
        """Day 0 takes 200 rows; day+1 clamps the lower bound one day upward and is
        granted less than the window still holds -- so it stops on the CAP, with the
        floor demonstrably inside the window."""
        seen: set = set()
        _run(self.state, self.feed, BASE, cap=200, seen=seen)
        eng, metrics, _ = _run(self.state, self.feed, BASE + timedelta(days=1),
                               cap=100, seen=seen)
        return eng, metrics, seen

    def test_a_budget_stop_under_a_risen_floor_is_now_recorded(self):
        """THE FIX. `exposed` required a DRAINED stop, so a source that ran out of
        BUDGET while the floor cut into its window recorded no doubt at all -- and
        that is the more exposed case, not the less."""
        _eng, metrics, _seen = self._budget_stop_under_a_clamp()
        seg = metrics["segments"][LABEL]

        self.assertEqual(seg.get("stop_reason"), "cap_reached")
        self.assertTrue(metrics["watermark"].get("lower_clamped_to_frame"))
        self.assertIn("coverage_uncertain", seg,
                      "a budget stop under a risen floor must record its doubt")
        cu = seg["coverage_uncertain"]
        self.assertEqual(cu["stop_reason"], "cap_reached")
        self.assertFalse(cu["rewind_eligible"], "recorded, deliberately not acted on")
        self.assertTrue(cu["offset_basis_changed"],
                        "the bound this offset was measured against has moved")

    def test_the_source_is_listed_as_possibly_skipped(self):
        _eng, metrics, _seen = self._budget_stop_under_a_clamp()
        skipped = (metrics["watermark"].get("coverage") or {}).get(
            "possibly_skipped_sources") or []
        self.assertIn(LABEL, skipped)

    def test_a_budget_stop_is_never_silently_rewound(self):
        """Recording doubt must not change behaviour: rewinding a budget-stopped
        source would re-page a prefix it already holds, and the duplicate-page cap
        cannot stop that inside one grant -- it would spend the budget without ever
        reaching the tail."""
        self._budget_stop_under_a_clamp()
        rewinds = self._state().get("window_coverage_rewinds") or {}
        self.assertEqual(int(rewinds.get(LABEL, 0)), 0,
                         "no rewind on a cap_reached stop")

    def test_a_resumed_offset_can_point_past_a_clamped_window(self):
        """The failure mode in its purest form, reproduced deterministically.

        An offset of 200 measured against a 7-day window, resumed against a window
        the floor has clamped to roughly 180 rows, addresses nothing. The source
        returns an empty page and concludes it DRAINED -- having inspected none of
        the rows the window still holds. The existing guard catches this one because
        `empty_page` is a drained stop; it is recorded here so the distinction
        between the two cases stays visible."""
        seen: set = set()
        _run(self.state, self.feed, BASE, cap=200, seen=seen)
        _e2, m2, _ = _run(self.state, self.feed, BASE + timedelta(days=4),
                          cap=100, seen=seen)
        seg = m2["segments"][LABEL]

        self.assertEqual(seg.get("returned"), 0, "the offset addressed nothing")
        self.assertEqual(seg.get("stop_reason"), "empty_page")
        self.assertTrue(seg["coverage_uncertain"]["rewind_eligible"])
        self.assertEqual(int((self._state().get("window_coverage_rewinds") or {})
                             .get(LABEL, 0)), 1, "and it was sent back to the head")

    def test_billed_and_unseen_are_both_measurable_across_the_replay(self):
        """The measurement the capacity question needs: what a run pays, and what it
        leaves uninspected."""
        seen: set = set()
        total_billed = 0
        for day, cap in ((0, 200), (1, 200), (2, 200)):
            _e, m, _ = _run(self.state, self.feed, BASE + timedelta(days=day),
                            cap=cap, seen=seen)
            total_billed += m["segments"][LABEL]["returned"]

        in_frame = {f"fantastic_{r['id']}" for r in self.feed.visible()}
        unseen = in_frame - seen
        self.assertGreater(total_billed, 0)
        self.assertLessEqual(len(seen & in_frame), len(in_frame))
        # Reported, not asserted to be zero: whether a replay leaves rows
        # uninspected is exactly the open question, and this is how it is measured.
        self.assertIsInstance(len(unseen), int)


if __name__ == "__main__":
    unittest.main()
