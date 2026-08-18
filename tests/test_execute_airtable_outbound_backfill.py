from __future__ import annotations

import copy
import unittest

import execute_airtable_outbound_backfill as executor


class AirtableOutboundBackfillExecutionTests(unittest.TestCase):
    def test_schema_contract_is_exact(self):
        self.assertEqual(executor.SCHEMA_NAMES, (
            "Outbound Company", "Outbound Company Confidence", "Outbound Company Identity",
            "Outbound Company Evidence", "Outbound Hold", "Outbound Role", "Outbound Roles",
            "Outbound Role Confidence", "Outbound Role Evidence",
        ))
        self.assertEqual(len(executor.SCHEMA_FIELDS), 9)

    def test_manifest_comparison_detects_fingerprint_contact_drift(self):
        columns = ["airtable_record_id", *executor.MATERIAL_MANIFEST_FIELDS]
        row = ["rec1", *("same" for _ in executor.MATERIAL_MANIFEST_FIELDS)]
        reviewed = {"columns": columns, "rows": [row]}
        fresh = copy.deepcopy(reviewed)
        fingerprint_index = columns.index("proposed_validation_fingerprint")
        fresh["rows"][0][fingerprint_index] = "changed"
        differences = executor.compare_manifests(reviewed, fresh)
        self.assertEqual(differences[0]["fields"], ["proposed_validation_fingerprint"])

    def test_selection_excludes_sent_and_keeps_hold_partition(self):
        columns = ["airtable_record_id", "backfill_eligibility"]
        manifest = {"columns": columns, "rows": [
            ["safe", "eligible"],
            ["hold", "held"],
            ["sent", "excluded_instantly_sent_or_processed"],
            ["sent_hold", "held_and_instantly_sent_or_processed"],
        ]}
        safe, held, protected = executor._selection(manifest)
        self.assertEqual(safe, ["safe"])
        self.assertEqual(held, ["hold"])
        self.assertEqual(protected, ["sent", "sent_hold"])


if __name__ == "__main__":
    unittest.main()
