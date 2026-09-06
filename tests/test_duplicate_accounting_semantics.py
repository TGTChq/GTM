"""What a `duplicates` count actually means, reproduced against realistic pages.

I reported that the 2026-09-06 run's 5,218 duplicates were "rows billed more than
once inside the same run". That was wrong, and the error was in reading
``seen_ids`` as a per-run set. It is initialised empty per fetch and then SEEDED at
window open from persisted state:

    for key in ("boundary_ids", "overlap_band_ids", "window_acquired_ids"):
        self.seen_ids |= {f"fantastic_{i}" for i in (self.state.get(key) or [])}

``window_acquired_ids`` holds every id KEPT in the open window by PREVIOUS runs. The
2026-09-04 run kept 6,205 postings in the window that was still in flight on
2026-09-06, so that run's ids were in ``seen_ids`` before the first request went
out. A duplicate is therefore predominantly a CROSS-RUN re-delivery that dedupe
correctly suppressed -- not a within-run double purchase.

These tests pin the distinctions that reading actually requires:

  * a seeded id produces a duplicate whose ``cross_source_duplicates`` stays ZERO,
    because that counter fires only when ``_first_seen`` names a DIFFERENT source,
    and ``_first_seen`` is written only for rows KEPT IN THIS RUN. That is the whole
    reconciliation of "ATS billed 2,722, kept 0, cross_source_duplicates 0";
  * genuine cross-source overlap DOES set it, so 0 really means "not cross-source";
  * a first occurrence is never discarded -- insertion happens only after a row is
    kept, so nothing pre-inserts an id it then rejects;
  * billing counts every RETURNED row regardless of disposition, which is why a
    duplicate is still a paid delivery.

A limit worth stating: production retains no response-level id list. ``postings.json``
holds only KEPT rows and ``window_acquired_ids`` only kept ids, so the actual
first-occurrence/duplicate trace for the 5,218 events cannot be reconstructed from
the 2026-09-06 evidence. These reproductions establish the mechanism, not the
history.
"""

from __future__ import annotations

import unittest
from unittest import mock

import config
import fantastic_jobs_adapter as fja


def _row(i, source="linkedin"):
    return {"id": f"id-{i:05d}", "title": "Account Executive",
            "organization": f"Co{i}", "source": source,
            "organization_url": f"https://co{i}.com",
            "date_posted": "2026-09-01T00:00:00Z",
            "date_created": "2026-09-01T00:00:00Z",
            "countries_derived": ["United States"],
            "employment_type": ["FULL_TIME"],
            "org_linkedin_headcount": 100,
            "org_linkedin_industry": "Software Development"}


class _Feed:
    """Pages of `page_size` rows, ids advancing with the offset -- a well-behaved
    feed, so nothing here depends on provider misbehaviour."""

    def __init__(self, total=300, page_size=100, source="linkedin"):
        self.total, self.page_size, self.source = total, page_size, source
        self.calls = []

    def __call__(self, url, headers, params, timeout):
        self.calls.append(dict(params))
        start = int(params.get("offset", 0) or 0)
        limit = int(params.get("limit", self.page_size) or self.page_size)
        rows = [_row(i, self.source)
                for i in range(start, min(self.total, start + limit))]

        class R:
            status_code = 200
            headers = {"x-api-jobs-remaining": "90000",
                       "x-api-requests-remaining": "9000"}
            def __init__(s, d): s._d = d
            def json(s): return s._d
        return R(rows)


CFG = dict(
    FANTASTIC_JOBS_ENABLED=True, FANTASTIC_JOBS_API_KEY="k",
    FANTASTIC_JOBS_BASE_URL="https://data.fantastic.jobs",
    FANTASTIC_JOBS_LOCATION="United States",
    FANTASTIC_JOBS_HEADCOUNT_MIN=25, FANTASTIC_JOBS_HEADCOUNT_MAX=1000,
    FANTASTIC_JOBS_AI_EMPLOYMENT_TYPE="FULL_TIME",
    FANTASTIC_JOBS_EXCLUDE_AGENCY=True, FANTASTIC_JOBS_MAX_RETRIES=0,
    FANTASTIC_MAX_CONSECUTIVE_DUPLICATE_PAGES=40,
)


def _fetch(feed, seen, cap=300, label="fantastic_jobs_linkedin",
           accept=("linkedin",), metrics=None, **over):
    metrics = metrics if metrics is not None else {"segments": {}}
    quota = fja._QuotaState()
    with mock.patch.multiple(config, **dict(CFG, **over)):
        jobs = fja._fetch_segment(
            "/active-jb-7d", {"source": "linkedin"}, label, cap, quota, feed,
            seen, metrics, accept_source=accept, durable_cursor=True)
    return jobs, metrics["segments"][label], metrics


class ASeededIdIsACrossRunDuplicate(unittest.TestCase):
    """The reconciliation of ATS: 2,722 billed, 0 kept, cross_source_duplicates 0."""

    def test_every_row_already_acquired_by_an_earlier_run_is_suppressed(self):
        feed = _Feed(total=300)
        # Exactly what `open()` seeds from `window_acquired_ids`.
        seen = {f"fantastic_id-{i:05d}" for i in range(300)}
        jobs, seg, _m = _fetch(feed, seen)

        self.assertEqual(jobs, [], "nothing new: an earlier run kept all of it")
        self.assertEqual(seg["duplicates"], 300)
        self.assertEqual(seg["returned"], 300, "and we were billed for all 300")

    def test_and_cross_source_duplicates_stays_zero(self):
        """Because `_first_seen` only names sources for rows KEPT IN THIS RUN, a
        seeded id has no owner -- so the cross-source counter cannot fire. A zero
        there does NOT mean the sources failed to overlap."""
        feed = _Feed(total=200)
        seen = {f"fantastic_id-{i:05d}" for i in range(200)}
        _jobs, seg, _m = _fetch(feed, seen)

        self.assertEqual(seg["duplicates"], 200)
        self.assertEqual(seg["cross_source_duplicates"], 0)

    def test_genuine_cross_source_overlap_does_set_it(self):
        """So the zero above is informative, not vacuous."""
        metrics = {"segments": {}}
        seen: set = set()
        _fetch(_Feed(total=100, source="linkedin"), seen, cap=100,
               label="fantastic_jobs_linkedin", accept=("linkedin",), metrics=metrics)
        _jobs, seg, _m = _fetch(_Feed(total=100, source="ats"), seen, cap=100,
                                label="fantastic_jobs_ats", accept=("ats",),
                                metrics=metrics)
        self.assertEqual(seg["duplicates"], 100)
        self.assertEqual(seg["cross_source_duplicates"], 100,
                         "same postings, second source: this is what overlap looks like")


class FirstOccurrencesSurvive(unittest.TestCase):
    def test_a_new_row_among_seeded_ones_is_kept(self):
        feed = _Feed(total=100)
        seen = {f"fantastic_id-{i:05d}" for i in range(100) if i != 42}
        jobs, seg, _m = _fetch(feed, seen, cap=100)

        self.assertEqual([j["job_id"] for j in jobs], ["fantastic_id-00042"])
        self.assertEqual(seg["duplicates"], 99)

    def test_nothing_inserts_an_id_it_then_rejects(self):
        """A row filtered out by source never enters seen_ids, so the same posting
        arriving later under an accepted source is still a first occurrence."""
        seen: set = set()
        metrics = {"segments": {}}
        _jobs, seg, _m = _fetch(_Feed(total=50, source="ats"), seen, cap=50,
                                label="seg_reject", accept=("linkedin",),
                                metrics=metrics)
        self.assertEqual(seg["source_filtered_out"], 50)
        self.assertEqual(seen, set(), "a filtered row must not claim the id")

        jobs, seg2, _m2 = _fetch(_Feed(total=50, source="linkedin"), seen, cap=50,
                                 label="seg_accept", accept=("linkedin",),
                                 metrics=metrics)
        self.assertEqual(len(jobs), 50, "the accepted arrival is still a first occurrence")

    def test_a_repeat_inside_one_run_is_suppressed_but_still_billed(self):
        seen: set = set()
        metrics = {"segments": {}}
        jobs1, seg1, _m = _fetch(_Feed(total=100), seen, cap=100,
                                 label="pass_one", metrics=metrics)
        jobs2, seg2, _m = _fetch(_Feed(total=100), seen, cap=100,
                                 label="pass_two", metrics=metrics)
        self.assertEqual(len(jobs1), 100)
        self.assertEqual(jobs2, [], "the second pass adds nothing")
        self.assertEqual(seg2["duplicates"], 100)
        self.assertEqual(seg2["returned"], 100, "and it was paid for")


class TheAllDuplicateGuardCannotFireWithinThisBudget(unittest.TestCase):
    """`FANTASTIC_MAX_CONSECUTIVE_DUPLICATE_PAGES` exists so that "one run cannot
    spend its entire budget discovering" a fully-overlapping window. In production
    it is 40 pages, and the 2026-09-06 governor grant bought 28 pages per source.
    So ATS spent its whole allocation on all-duplicate pages without the guard ever
    being reachable. Pinned as arithmetic, not adjusted: the right threshold depends
    on why the window is being re-paged at all, which is still unresolved.
    """

    def test_a_28_page_budget_never_reaches_a_40_page_guard(self):
        feed = _Feed(total=10_000)
        seen = {f"fantastic_id-{i:05d}" for i in range(10_000)}
        _jobs, seg, _m = _fetch(feed, seen, cap=2800,
                                FANTASTIC_MAX_CONSECUTIVE_DUPLICATE_PAGES=40)
        self.assertEqual(seg["returned"], 2800, "the whole grant was spent")
        self.assertGreaterEqual(seg["duplicate_pages_skipped"], 27,
                                "consecutive all-duplicate pages, every one paid for")
        self.assertLess(seg["duplicate_pages_skipped"], 40,
                        "and never enough of them to reach the guard")
        self.assertNotEqual(seg["stop_reason"], "duplicate_page_cap",
                            "the guard needs 40 and the budget only buys 28")

    def test_a_reachable_threshold_stops_it_early(self):
        feed = _Feed(total=10_000)
        seen = {f"fantastic_id-{i:05d}" for i in range(10_000)}
        _jobs, seg, _m = _fetch(feed, seen, cap=2800,
                                FANTASTIC_MAX_CONSECUTIVE_DUPLICATE_PAGES=5)
        self.assertEqual(seg["stop_reason"], "duplicate_page_cap")
        self.assertLess(seg["returned"], 2800,
                        "a guard below the page budget actually saves credits")


if __name__ == "__main__":
    unittest.main()
