"""Strict preflight-before-paid gate + real-composition guarantees. Offline; the
network is patched to raise so a passing test proves no external call was made."""

from __future__ import annotations

import tempfile
import unittest

import requests

import config
import run_orchestrator as R
from orchestrator.modes import ExecutionMode, policy_for, DEFAULT_MODE


def _args(**over):
    argv = ["--mode", "live_acquisition_and_enrichment",
            "--lanes", "ats,jsearch,free_feeds", "--boards", "BOARDS_FINAL.json",
            "--airtable-write", "--artifact-root", over.pop("root", tempfile.mkdtemp())]
    return R.build_parser().parse_args(argv)


class StrictPreflightTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(config, k, None) for k in
                       ("RAPIDAPI_KEY", "APOLLO_API_KEY", "HUNTER_API_KEY",
                        "AIRTABLE_TOKEN", "AIRTABLE_BASE_ID", "AIRTABLE_TABLE_NAME")}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)

    def _set(self, **kv):
        for k, v in kv.items():
            setattr(config, k, v)

    def test_missing_required_key_refuses_before_any_network(self):
        # (10) APOLLO absent -> strict gate returns 2; requests.request patched to raise.
        self._set(RAPIDAPI_KEY="x", APOLLO_API_KEY="", AIRTABLE_TOKEN="t",
                  AIRTABLE_BASE_ID="b", AIRTABLE_TABLE_NAME="L")
        orig = requests.request
        requests.request = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network!"))
        try:
            rc = R.main(["--mode", "live_acquisition_and_enrichment",
                         "--lanes", "ats,jsearch,free_feeds", "--boards", "BOARDS_FINAL.json",
                         "--airtable-write", "--artifact-root", tempfile.mkdtemp()])
        finally:
            requests.request = orig
        self.assertEqual(rc, 2)   # refused before any external call

    def test_missing_hunter_does_not_block(self):
        # (11) HUNTER absent but all mandatory present -> gate passes (returns 0).
        self._set(RAPIDAPI_KEY="x", APOLLO_API_KEY="a", HUNTER_API_KEY="",
                  AIRTABLE_TOKEN="t", AIRTABLE_BASE_ID="b", AIRTABLE_TABLE_NAME="L")
        rc = R._strict_preflight(_args(), policy_for(ExecutionMode.LIVE_ACQUISITION_AND_ENRICHMENT))
        self.assertEqual(rc, 0)

    def test_missing_rapidapi_with_jsearch_refuses(self):
        self._set(RAPIDAPI_KEY="", APOLLO_API_KEY="a", AIRTABLE_TOKEN="t",
                  AIRTABLE_BASE_ID="b", AIRTABLE_TABLE_NAME="L")
        rc = R._strict_preflight(_args(), policy_for(ExecutionMode.LIVE_ACQUISITION_AND_ENRICHMENT))
        self.assertEqual(rc, 2)


class RealCompositionGuaranteeTests(unittest.TestCase):
    def test_no_offline_fallback_possible_for_live_mode(self):
        # (13) live_acquisition_and_enrichment can NEVER take the fake/offline path
        self.assertNotIn(ExecutionMode.LIVE_ACQUISITION_AND_ENRICHMENT, R.OFFLINE_MODES)
        self.assertEqual(R.OFFLINE_MODES,
                         {ExecutionMode.OFFLINE_REPLAY, ExecutionMode.FULL_DRY_RUN})
        self.assertIsNot(DEFAULT_MODE, ExecutionMode.PRODUCTION)

    def test_live_jsearch_composes_real_transport(self):
        # (12) selected jsearch lane uses the real transport (inner not None)
        from orchestrator.adapters_real import build_jsearch_transport
        self.assertIsNotNone(build_jsearch_transport(live=True).inner)
        self.assertIsNone(build_jsearch_transport(live=False).inner)


if __name__ == "__main__":
    unittest.main()
