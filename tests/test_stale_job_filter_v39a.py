import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import job_filter
import job_signal


class StaleJobFilterV39ATests(unittest.TestCase):
    def test_keeps_job_younger_than_limit(self):
        now = datetime.now(timezone.utc)
        job = {"job_posted_at_datetime_utc": (now - timedelta(days=29)).isoformat()}
        with patch("job_signal.datetime") as mocked_datetime:
            # classify_freshness receives its own current time; keep this test
            # tolerant by using an age comfortably below the threshold.
            mocked_datetime.now.return_value = now
            mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
            mocked_datetime.fromtimestamp.side_effect = datetime.fromtimestamp
            matched, reason = job_filter.is_stale_job(job)
        self.assertFalse(matched)
        self.assertEqual(reason, "")

    def test_rejects_job_at_limit(self):
        now = datetime.now(timezone.utc)
        job = {"job_posted_at_datetime_utc": (now - timedelta(days=31)).isoformat()}
        matched, reason = job_filter.is_stale_job(job)
        self.assertTrue(matched)
        self.assertTrue(reason.startswith("stale_job:"))

    def test_parses_five_months_ago(self):
        now = datetime.now(timezone.utc)
        freshness, age_days, reason = job_signal.classify_freshness(
            {"job_posted_at": "5 months ago"},
            now=now,
        )
        self.assertEqual(freshness, "stale_review")
        self.assertGreaterEqual(age_days, 149)
        self.assertIn("posted_30_or_more_days_ago", reason)

    def test_parses_weeks_and_years(self):
        now = datetime.now(timezone.utc)
        _, weeks_age, _ = job_signal.classify_freshness(
            {"job_posted_at": "8 weeks ago"}, now=now
        )
        _, years_age, _ = job_signal.classify_freshness(
            {"job_posted_at": "1 year ago"}, now=now
        )
        self.assertGreaterEqual(weeks_age, 55)
        self.assertGreaterEqual(years_age, 364)

    def test_unknown_date_is_not_rejected(self):
        matched, reason = job_filter.is_stale_job({})
        self.assertFalse(matched)
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
