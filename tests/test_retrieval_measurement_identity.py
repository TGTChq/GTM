"""Run lineage, secret redaction, and the read-only seen snapshot.

The snapshot tests are the important ones. ``SeenJobsRegistry.__init__`` calls
``_load()``, which creates the parent directory (pipeline_state.py:38) and, on
a JSON error, MOVES the file aside with ``os.replace`` (pipeline_state.py:45-46).
A measurement run that did that to the production registry would destroy the
cross-day deduplication baseline. So these tests assert not just that the
harness reads correctly, but that it *cannot* write.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import config
from retrieval_measurement.identity import (
    ProductionStatePathRefused,
    ReadOnlySeenSnapshot,
    assert_no_secret_values,
    config_fingerprint,
    effective_config_snapshot,
    new_run_id,
    run_identity,
)
from retrieval_measurement.schema import EffectiveConfigEntry


def _write_snapshot(directory: Path, job_ids=None, dedup_keys=None, retention_days=30) -> Path:
    path = directory / "seen_snapshot.json"
    path.write_text(json.dumps({
        "updated_at": datetime.now().isoformat(),
        "retention_days": retention_days,
        "job_ids": job_ids or {},
        "dedup_keys": dedup_keys or {},
    }), encoding="utf-8")
    return path


class RunIdentityTests(unittest.TestCase):
    def test_run_ids_are_unique_and_sortable(self):
        ids = [new_run_id() for _ in range(50)]
        self.assertEqual(len(set(ids)), 50)
        self.assertEqual(ids, sorted(ids, key=lambda value: value[:16]) or ids)

    def test_run_identity_captures_code_and_config_lineage(self):
        identity = run_identity("fixture", {"mode": "fixture"})
        for key in ("run_id", "mode", "started_at", "python_version", "config_fingerprint"):
            self.assertTrue(identity[key], f"{key} must be populated")
        names = {entry["name"] for entry in identity["effective_config"]}
        self.assertIn("RUN_ARG_MODE", names)
        self.assertIn("FREE_JOB_SOURCES", names)


class ConfigRedactionTests(unittest.TestCase):
    def test_secret_values_are_replaced_not_truncated(self):
        entries = effective_config_snapshot()
        by_name = {entry.name: entry for entry in entries}
        self.assertIn("RAPIDAPI_KEY", by_name)
        entry = by_name["RAPIDAPI_KEY"]
        self.assertTrue(entry.redacted)
        # Presence only. A truncated or hashed secret is still a secret.
        self.assertEqual(set(entry.value.keys()), {"configured"})
        self.assertIsInstance(entry.value["configured"], bool)

    def test_credential_bearing_names_are_all_redacted(self):
        entries = {entry.name: entry for entry in effective_config_snapshot()}
        for name in entries:
            if any(token in name for token in ("KEY", "SECRET", "TOKEN", "PASSWORD")):
                if "KEYWORD" in name:
                    continue
                self.assertTrue(entries[name].redacted, f"{name} was not redacted")

    def test_keyword_lists_are_not_mistaken_for_secrets(self):
        entries = {entry.name: entry for entry in effective_config_snapshot()}
        self.assertIn("EXCLUDED_TITLE_KEYWORDS", entries)
        self.assertFalse(entries["EXCLUDED_TITLE_KEYWORDS"].redacted)
        self.assertIsInstance(entries["EXCLUDED_TITLE_KEYWORDS"].value, list)

    def test_provenance_distinguishes_env_from_code_default(self):
        environ = {"NUM_PAGES": "3"}
        entries = {entry.name: entry for entry in effective_config_snapshot(environ=environ)}
        self.assertEqual(entries["NUM_PAGES"].source, "env")
        self.assertEqual(entries["MIN_JOBS_PER_RUN"].source, "code_default")

    def test_fingerprint_is_stable_and_sensitive(self):
        entries = effective_config_snapshot()
        self.assertEqual(config_fingerprint(entries), config_fingerprint(list(entries)))
        mutated = list(entries) + [
            EffectiveConfigEntry(name="ZZZ_EXTRA", value=1, source="run_argument")
        ]
        self.assertNotEqual(config_fingerprint(entries), config_fingerprint(mutated))

    def test_unredacted_secret_is_refused(self):
        leaked = [EffectiveConfigEntry(name="RAPIDAPI_KEY", value="abc123", source="env")]
        with self.assertRaises(RuntimeError):
            assert_no_secret_values(leaked)


class ReadOnlySeenSnapshotTests(unittest.TestCase):
    def test_reads_job_ids_and_dedup_keys(self):
        today = datetime.now().strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_snapshot(
                Path(tmp),
                job_ids={"himalayas:abc": today},
                dedup_keys={"acme.com|senior engineer": today},
            )
            snapshot = ReadOnlySeenSnapshot.load(path)
        self.assertTrue(snapshot.has_job_id("himalayas:abc"))
        self.assertFalse(snapshot.has_job_id("himalayas:missing"))
        self.assertFalse(snapshot.has_job_id(""))
        self.assertTrue(snapshot.has_dedup_key(("acme.com", "senior engineer")))
        self.assertFalse(snapshot.has_dedup_key(("acme.com", "other")))
        self.assertEqual(snapshot.total_tracked, 1)

    def test_prunes_with_the_same_retention_semantics_as_production(self):
        recent = datetime.now().strftime("%Y-%m-%d")
        stale = (datetime.now() - timedelta(days=config.SEEN_JOBS_RETENTION_DAYS + 5)).strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_snapshot(
                Path(tmp),
                job_ids={"keep": recent, "drop": stale, "unparseable": "not-a-date"},
            )
            snapshot = ReadOnlySeenSnapshot.load(path)
        self.assertTrue(snapshot.has_job_id("keep"))
        self.assertFalse(snapshot.has_job_id("drop"))
        self.assertFalse(snapshot.has_job_id("unparseable"))
        self.assertEqual(snapshot.pruned, 2)

    def test_never_writes_when_loading(self):
        """os.replace and mkdir are the two mutations SeenJobsRegistry performs.
        Neither may be reachable from the snapshot loader."""
        today = datetime.now().strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_snapshot(Path(tmp), job_ids={"a": today})
            before = {item.name for item in Path(tmp).iterdir()}
            with patch("os.replace", side_effect=AssertionError("snapshot loader wrote via os.replace")), \
                 patch.object(Path, "mkdir", side_effect=AssertionError("snapshot loader created a directory")):
                snapshot = ReadOnlySeenSnapshot.load(path)
            after = {item.name for item in Path(tmp).iterdir()}
        self.assertEqual(before, after)
        self.assertTrue(snapshot.has_job_id("a"))

    def test_corrupt_snapshot_raises_and_leaves_the_file_untouched(self):
        """SeenJobsRegistry renames a corrupt file (pipeline_state.py:45-46).
        The harness must report the problem and leave the bytes alone."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen_snapshot.json"
            path.write_text("{not json", encoding="utf-8")
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                ReadOnlySeenSnapshot.load(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual([item.name for item in Path(tmp).iterdir()], ["seen_snapshot.json"])

    def test_refuses_a_production_state_path_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            production = Path(tmp) / "data" / "state"
            production.mkdir(parents=True)
            path = _write_snapshot(production)
            with self.assertRaises(ProductionStatePathRefused):
                ReadOnlySeenSnapshot.load(path)
            # Explicit opt-in is the only way through, and it is for copies.
            snapshot = ReadOnlySeenSnapshot.load(path, allow_production_path=True)
        self.assertEqual(snapshot.total_tracked, 0)

    def test_missing_snapshot_is_an_error_not_an_empty_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                ReadOnlySeenSnapshot.load(Path(tmp) / "absent.json")

    def test_describe_declares_itself_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = ReadOnlySeenSnapshot.load(_write_snapshot(Path(tmp)))
        described = snapshot.describe()
        self.assertTrue(described["available"])
        self.assertFalse(described["write_capable"])

    def test_empty_snapshot_reports_unavailable(self):
        snapshot = ReadOnlySeenSnapshot.empty()
        self.assertFalse(snapshot.describe()["available"])
        self.assertFalse(snapshot.has_job_id("anything"))

    def test_has_no_write_methods_at_all(self):
        """Structural, not behavioural: there is no code path to add one later
        without this test failing."""
        for forbidden in ("save", "mark_jobs", "_load", "_prune"):
            self.assertFalse(
                hasattr(ReadOnlySeenSnapshot, forbidden),
                f"ReadOnlySeenSnapshot must not expose {forbidden}",
            )


if __name__ == "__main__":
    unittest.main()
