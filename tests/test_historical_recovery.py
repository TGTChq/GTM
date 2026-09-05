"""Covering an interruption longer than the frame.

The steady-state engine pages a `date_created` window by OFFSET, which is the method
the provider documents for 1h/24h/7d. It cannot cover a gap longer than the frame:
once the floor rises past those days, no windowed request can reach them whatever
offset it uses. That is not a bug in the windowed engine -- it is the shape of the
contract, and it is why a separate mechanism has to exist.

The documented way back is the OTHER pagination mode: `time_frame=6m` with `cursor`
set to the last `id` returned, ordering by `id` ASCENDING rather than `date_posted`
descending. The provider warns against resuming an offset run with a cursor or the
reverse, so this keeps its own state file and never touches `window_offsets`.

Everything here runs against a modelled feed. No provider request is made and no
credit is spent. What these tests establish is that the mechanism is implemented,
bounded, and resumable. What they cannot establish is that a live historical backfill
succeeds -- that needs a run with a budget, and none has happened.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import config
import fantastic_jobs_adapter as fja


def _rows(start, n):
    """Ascending ids, the order the provider returns in cursor mode."""
    return [{"id": f"id-{i:05d}", "title": "Account Executive",
             "organization": f"Co{i}", "source": "linkedin",
             "organization_url": f"https://co{i}.com",
             "date_posted": "2026-04-01T00:00:00Z",
             "date_created": "2026-04-01T00:00:00Z",
             "countries_derived": ["United States"],
             "employment_type": ["FULL_TIME"],
             "org_linkedin_headcount": 100,
             "org_linkedin_industry": "Software Development"}
            for i in range(start, start + n)]


class _CursorFeed:
    """A modelled 6m feed: ids ascending, `cursor` = last id seen."""

    def __init__(self, total=250):
        self.total = total
        self.calls = []

    def __call__(self, url, headers, params, timeout):
        self.calls.append(dict(params))
        after = params.get("cursor")
        start = 0 if not after else int(str(after).split("-")[1]) + 1
        limit = int(params.get("limit", 100))
        rows = _rows(start, max(0, min(limit, self.total - start)))

        class R:
            status_code = 200
            headers = {"x-api-jobs-remaining": "9000",
                       "x-api-requests-remaining": "9000"}
            def __init__(s, d): s._d = d
            def json(s): return s._d
        return R(rows)


class HistoricalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "hist.json")
        self.cfg = dict(
            FANTASTIC_JOBS_ENABLED=True, FANTASTIC_JOBS_API_KEY="k",
            FANTASTIC_JOBS_BASE_URL="https://data.fantastic.jobs",
            FANTASTIC_HISTORICAL_RECOVERY_ENABLED=True,
            FANTASTIC_HISTORICAL_RECOVERY_MAX_ROWS_PER_RUN=150,
            FANTASTIC_HISTORICAL_RECOVERY_STATE_PATH=self.state,
            FANTASTIC_JOBS_LOCATION="United States",
            FANTASTIC_JOBS_HEADCOUNT_MIN=25, FANTASTIC_JOBS_HEADCOUNT_MAX=1000,
            FANTASTIC_JOBS_AI_EMPLOYMENT_TYPE="FULL_TIME",
            FANTASTIC_JOBS_EXCLUDE_AGENCY=True, FANTASTIC_JOBS_MAX_RETRIES=0,
        )

    def _run(self, feed, seen=None, **over):
        metrics = {"segments": {}}
        with mock.patch.multiple(config, **dict(self.cfg, **over)):
            jobs = fja.run_historical_recovery(
                feed, fja._QuotaState(), seen if seen is not None else set(), metrics)
        return jobs, metrics["historical_recovery"], metrics

    # -- it is off, and stays off without a budget --------------------------

    def test_disabled_by_default_makes_no_request(self):
        feed = _CursorFeed()
        _jobs, block, _m = self._run(feed, FANTASTIC_HISTORICAL_RECOVERY_ENABLED=False)
        self.assertEqual(block["stop_reason"], "disabled")
        self.assertEqual(feed.calls, [], "not one request")

    def test_enabled_without_a_row_budget_still_makes_no_request(self):
        """There is no implicit budget: an unbounded backfill must not be reachable
        by flipping a single flag."""
        feed = _CursorFeed()
        _jobs, block, _m = self._run(feed, FANTASTIC_HISTORICAL_RECOVERY_MAX_ROWS_PER_RUN=0)
        self.assertEqual(block["stop_reason"], "no_row_budget")
        self.assertEqual(feed.calls, [])

    # -- the documented contract --------------------------------------------

    def test_it_uses_the_six_month_frame_and_an_id_cursor(self):
        feed = _CursorFeed()
        _jobs, block, _m = self._run(feed)

        self.assertEqual(feed.calls[0]["time_frame"], "6m")
        self.assertNotIn("offset", feed.calls[0], "cursor mode never sends an offset")
        self.assertNotIn("cursor", feed.calls[0], "the first page has no cursor")
        self.assertEqual(feed.calls[1]["cursor"], "id-00099",
                         "the cursor is the LAST ID of the previous page")
        self.assertEqual(block["pagination"], "cursor(id asc)")

    def test_it_never_bills_past_its_budget(self):
        feed = _CursorFeed(total=10_000)
        _jobs, block, _m = self._run(feed)
        self.assertEqual(block["billed"], 150)
        self.assertEqual(block["stop_reason"], "row_budget_reached")

    # -- resume, which is the whole point of a cursor ------------------------

    def test_progress_is_persisted_after_every_page(self):
        feed = _CursorFeed()
        self._run(feed)
        saved = json.load(open(self.state, encoding="utf-8"))
        self.assertEqual(saved["schema"], "fantastic-historical-recovery/1")
        self.assertEqual(saved["cursor"], "id-00149")

    def test_a_second_run_resumes_instead_of_restarting(self):
        first_feed = _CursorFeed(total=10_000)
        self._run(first_feed)
        second_feed = _CursorFeed(total=10_000)
        self._run(second_feed)

        self.assertEqual(second_feed.calls[0].get("cursor"), "id-00149",
                         "the second run starts where the first stopped")
        saved = json.load(open(self.state, encoding="utf-8"))
        self.assertEqual(saved["cursor"], "id-00299")

    def test_its_state_is_its_own_and_never_the_windowed_engine_s(self):
        """The provider warns against resuming an offset run with a cursor. Keeping
        the two in separate files is what makes that impossible rather than merely
        discouraged."""
        feed = _CursorFeed()
        self._run(feed)
        saved = json.load(open(self.state, encoding="utf-8"))
        self.assertNotIn("window_offsets", saved)
        self.assertNotIn("in_flight_window_end", saved)
        self.assertIn("cursor", saved)

    # -- it cannot double-count, and it cannot spin --------------------------

    def test_rows_already_held_are_deduped_not_re_emitted(self):
        seen = {f"fantastic_id-{i:05d}" for i in range(0, 100)}
        jobs, block, _m = self._run(_CursorFeed(), seen=seen)
        self.assertEqual(block["billed"], 150, "the provider still billed them")
        self.assertEqual(len(jobs), 50, "but only the unseen ones are emitted")

    def test_a_page_without_ids_stops_rather_than_repeating_itself(self):
        """No id means no cursor to advance. Re-issuing the same request would bill
        the same page forever."""
        class _NoIds(_CursorFeed):
            def __call__(self, url, headers, params, timeout):
                response = super().__call__(url, headers, params, timeout)
                for row in response._d:
                    row.pop("id", None)
                return response

        _jobs, block, _m = self._run(_NoIds())
        self.assertEqual(block["stop_reason"], "no_cursor_id")
        self.assertLessEqual(block["pages"], 1)

    def test_a_short_page_ends_the_backfill(self):
        _jobs, block, _m = self._run(_CursorFeed(total=120))
        self.assertEqual(block["stop_reason"], "exhausted")
        self.assertLessEqual(block["billed"], 120)


if __name__ == "__main__":
    unittest.main()
