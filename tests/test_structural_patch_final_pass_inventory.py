"""D3: FinalPassInventory must not silently drop a genuinely distinct lead at
an already-contacted account, and any suppression must be counted with a
reason, not silent.

Traces to ROOT_CAUSE_TABLE_STRUCTURAL.md row 4 (the real, demonstrated
GreatAmerica case: a fully-qualified FINAL_PASS lead vanished between
enrichment and Airtable delivery with zero reason code anywhere).
"""
from __future__ import annotations

import tempfile
import unittest

from recovery_inventory import FinalPassInventory


def _lead(*, domain, bucket, lead_key, state="FINAL_PASS"):
    return {
        "_final_state": state,
        "employer_website": f"https://{domain}",
        "canonical_domain": domain,
        "bucket": bucket,
        "lead_key": lead_key,
        "hiring_manager_email": f"{lead_key}@{domain}",
        "hiring_manager_name": "Test Person",
    }


class FinalPassInventoryDistinctSignalTests(unittest.TestCase):
    def _staged_inventory(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return FinalPassInventory(f"{temp.name}/final_pass_inventory.json")

    def test_same_account_same_bucket_repeat_is_suppressed_and_counted(self):
        inv = self._staged_inventory()
        first = _lead(domain="greatamerica.com", bucket="finance", lead_key="ga-1")
        inv.stage([first])
        available = inv.available()
        inv.reserve(available)
        inv.mark_persisted([lead["lead_key"] for lead in available])

        second = _lead(domain="greatamerica.com", bucket="finance", lead_key="ga-2")
        result = inv.stage([second])
        self.assertEqual(result["staged"], 0)
        self.assertEqual(result["already_sent_same_bucket_suppressed"], 1)
        self.assertEqual(result["already_sent_suppressed_lead_keys"], ["ga-2"])
        self.assertEqual(inv.available(), [])

    def test_same_account_different_bucket_is_not_silently_dropped(self):
        """The exact GreatAmerica failure mode: a genuinely distinct hiring
        signal (different function) at an already-contacted account must
        reach delivery, not vanish."""
        inv = self._staged_inventory()
        first = _lead(domain="greatamerica.com", bucket="finance", lead_key="ga-1")
        inv.stage([first])
        available = inv.available()
        inv.reserve(available)
        inv.mark_persisted([lead["lead_key"] for lead in available])

        second = _lead(domain="greatamerica.com", bucket="engineering", lead_key="ga-2")
        result = inv.stage([second])
        self.assertEqual(result["staged"], 1)
        self.assertEqual(result["already_sent_same_bucket_suppressed"], 0)
        available_after = inv.available()
        self.assertEqual([lead["lead_key"] for lead in available_after], ["ga-2"])

    def test_same_run_collapse_is_bucket_aware(self):
        inv = self._staged_inventory()
        same_bucket = [
            _lead(domain="acme.com", bucket="finance", lead_key="a-1"),
            _lead(domain="acme.com", bucket="finance", lead_key="a-2"),
        ]
        different_bucket = _lead(domain="acme.com", bucket="marketing", lead_key="a-3")
        inv.stage([*same_bucket, different_bucket])
        available = inv.available()
        keys = sorted(lead["lead_key"] for lead in available)
        # One of the two same-bucket leads collapses; the distinct-bucket lead
        # always survives.
        self.assertIn("a-3", keys)
        self.assertEqual(len(keys), 2)


if __name__ == "__main__":
    unittest.main()
