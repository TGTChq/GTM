from __future__ import annotations

import unittest

from audit_queued_instantly import _sent_or_processed


class InstantlyQueuedAuditTests(unittest.TestCase):
    def test_active_untouched_lead_is_unsent(self):
        protected, reasons = _sent_or_processed({"status": 1})
        self.assertFalse(protected)
        self.assertEqual(reasons, [])

    def test_last_contact_protects_even_active_lead(self):
        protected, reasons = _sent_or_processed({
            "status": 1,
            "timestamp_last_contact": "2026-08-17T00:00:00Z",
        })
        self.assertTrue(protected)
        self.assertIn("timestamp_last_contact", reasons)

    def test_terminal_state_is_never_update_candidate(self):
        for status in (3, -1, -2, -3):
            with self.subTest(status=status):
                protected, reasons = _sent_or_processed({"status": status})
                self.assertTrue(protected)
                self.assertTrue(any(reason.startswith("terminal_status:") for reason in reasons))


if __name__ == "__main__":
    unittest.main()
