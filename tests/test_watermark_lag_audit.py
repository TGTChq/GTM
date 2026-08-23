"""Watermark visibility-lag self-audit: measures real late-visibility using the
count endpoint only (0 Jobs credits) and never mutates acquisition."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import config
import fantastic_jobs_adapter as fja


class _CountFeed:
    def __init__(self, count, status=200):
        self.count, self.status = count, status
        self.calls = []

    def __call__(self, url, headers, params, timeout):
        self.calls.append((url, dict(params)))
        outer = self

        class R:
            status_code = outer.status
            headers = {"x-api-jobs-remaining": "9000"}
            def json(self): return {"count": outer.count}
        return R()


class LagAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "wm.json")

    def _seed(self, count_at_close, closed_hours_ago=6.0):
        closed_at = datetime.now(timezone.utc) - timedelta(hours=closed_hours_ago)
        state = {"schema": "fantastic-watermark/1",
                 "last_successful_watermark": "2026-09-20T10:00:00Z",
                 "closed_windows": [{"lower": "2026-09-20T09:00:00Z",
                                     "upper": "2026-09-20T10:00:00Z",
                                     "count_at_close": count_at_close,
                                     "closed_at": closed_at.isoformat(), "rechecks": 0}]}
        with open(self.path, "w") as fh:
            json.dump(state, fh)

    def _cfg(self, **over):
        b = dict(FANTASTIC_WATERMARK_STATE_PATH=self.path,
                 FANTASTIC_JOBS_BASE_URL="https://data.fantastic.jobs",
                 FANTASTIC_JOBS_API_KEY="k", FANTASTIC_JOBS_REQUEST_TIMEOUT_SECONDS=30,
                 FANTASTIC_DATE_CREATED_LAG_MINUTES=180,
                 FANTASTIC_WATERMARK_AUDIT_ENABLED=True, FANTASTIC_WATERMARK_AUDIT_KEEP=12)
        b.update(over)
        return b

    def test_uses_the_count_endpoint_only_zero_job_credits(self):
        self._seed(40)
        feed = _CountFeed(40)
        metrics = {}
        with mock.patch.multiple(config, **self._cfg()):
            out = fja.audit_closed_windows(feed, {"source": "linkedin"}, metrics)
        self.assertTrue(out["performed"])
        url, params = feed.calls[0]
        self.assertTrue(url.endswith("/v1/active-jb-count"))     # NEVER a row endpoint
        self.assertNotIn("limit", params)
        self.assertNotIn("offset", params)
        self.assertEqual(params["date_created_gte"], "2026-09-20T09:00:00Z")
        self.assertEqual(params["date_created_lt"], "2026-09-20T10:00:00Z")

    def test_no_late_growth_is_clean(self):
        self._seed(40)
        metrics = {}
        with mock.patch.multiple(config, **self._cfg()):
            out = fja.audit_closed_windows(_CountFeed(40), {"source": "linkedin"}, metrics)
        self.assertEqual(out["late_growth"], 0)
        self.assertNotIn("ALERT", out)
        self.assertEqual(metrics["watermark_audit"], out)

    def test_late_growth_raises_a_loud_alert_and_is_persisted(self):
        self._seed(40)
        metrics = {}
        with mock.patch.multiple(config, **self._cfg()):
            out = fja.audit_closed_windows(_CountFeed(47), {"source": "linkedin"}, metrics)
        self.assertEqual(out["count_at_close"], 40)
        self.assertEqual(out["later_count"], 47)
        self.assertEqual(out["late_growth"], 7)
        self.assertIn("ALERT", out)
        self.assertEqual(out["configured_lag_minutes"], 180)
        self.assertIsNotNone(out["hours_since_close"])
        st = json.load(open(self.path))
        self.assertEqual(st["observed_late_growth_max"], 7)      # measured evidence persists
        self.assertEqual(st["closed_windows"][0]["rechecks"], 1)

    def test_audit_never_advances_or_mutates_the_watermark(self):
        self._seed(40)
        before = json.load(open(self.path))["last_successful_watermark"]
        with mock.patch.multiple(config, **self._cfg()):
            fja.audit_closed_windows(_CountFeed(99), {"source": "linkedin"}, {})
        after = json.load(open(self.path))
        self.assertEqual(after["last_successful_watermark"], before)
        self.assertNotIn("in_flight_window_end", {k: v for k, v in after.items() if v})

    def test_no_closed_windows_is_a_clean_noop(self):
        with open(self.path, "w") as fh:
            json.dump({"schema": "fantastic-watermark/1"}, fh)
        metrics = {}
        feed = _CountFeed(10)
        with mock.patch.multiple(config, **self._cfg()):
            out = fja.audit_closed_windows(feed, {"source": "linkedin"}, metrics)
        self.assertFalse(out["performed"])
        self.assertEqual(out["reason"], "no_closed_windows_yet")
        self.assertEqual(feed.calls, [])                          # no request at all

    def test_count_failure_never_raises(self):
        self._seed(40)
        metrics = {}
        with mock.patch.multiple(config, **self._cfg()):
            out = fja.audit_closed_windows(_CountFeed(0, status=500), {"source": "linkedin"}, metrics)
        self.assertFalse(out["performed"])
        self.assertTrue(out["reason"].startswith("count_http_"))

    def test_transport_exception_never_raises(self):
        self._seed(40)
        def boom(*a, **k): raise RuntimeError("network")
        with mock.patch.multiple(config, **self._cfg()):
            out = fja.audit_closed_windows(boom, {"source": "linkedin"}, {})
        self.assertFalse(out["performed"])
        self.assertIn("audit_error", out["reason"])

    def test_commit_records_the_closed_window_for_future_audit(self):
        with open(self.path, "w") as fh:
            json.dump({"schema": "fantastic-watermark/1", "in_flight_window_end": "2026-09-20T10:00:00Z",
                       "window_start": "2026-09-20T09:00:00Z", "window_drained": True,
                       "window_acquired_ids": ["1", "2", "3"]}, fh)
        with mock.patch.multiple(config, **self._cfg()):
            out = fja.commit_watermark(success=True)
        self.assertTrue(out["committed"])
        st = json.load(open(self.path))
        cw = st["closed_windows"][-1]
        self.assertEqual(cw["upper"], "2026-09-20T10:00:00Z")
        self.assertEqual(cw["count_at_close"], 3)
        self.assertEqual(cw["rechecks"], 0)

    def test_retained_window_list_is_bounded(self):
        many = [{"lower": f"L{i}", "upper": f"U{i}", "count_at_close": i,
                 "closed_at": datetime.now(timezone.utc).isoformat(), "rechecks": 0}
                for i in range(30)]
        with open(self.path, "w") as fh:
            json.dump({"schema": "fantastic-watermark/1", "closed_windows": many}, fh)
        with mock.patch.multiple(config, **self._cfg(FANTASTIC_WATERMARK_AUDIT_KEEP=5)):
            fja.audit_closed_windows(_CountFeed(0), {"source": "linkedin"}, {})
        st = json.load(open(self.path))
        self.assertLessEqual(len(st["closed_windows"]), 5)


if __name__ == "__main__":
    unittest.main()
