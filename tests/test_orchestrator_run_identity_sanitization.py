"""Run-identity argument sanitization.

Regression: PR #31 added ``--expected-ownership-token`` for lock recovery. Its
argparse dest (``expected_ownership_token``, default ``""``) lands in
``vars(args)`` on EVERY invocation, becomes ``RUN_ARG_EXPECTED_OWNERSHIP_TOKEN``
in the run identity, matches the secret-name detector, and -- because run-arg
entries are not redacted -- trips ``assert_no_secret_values`` right after
PREFLIGHT OK, before acquisition. The fix sanitizes the arguments handed to
``RunContext.create`` (drop maintenance-only + secret-named args) WITHOUT
touching the secret detector. Everything here is zero-network.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import requests

import config
import run_orchestrator as R
from orchestrator.modes import ExecutionMode, policy_for
from orchestrator.runcontrol import RunContext
from retrieval_measurement.identity import (
    _is_secret,
    assert_no_secret_values,
    effective_config_snapshot,
)

DEFINITIVE = [
    "--mode", "live_acquisition_and_enrichment",
    "--lanes", "ats,jsearch,free_feeds", "--boards", "BOARDS_FINAL.json",
    "--target", "300", "--airtable-write",
    "--global-budget", "500", "--ats-lane-budget", "400", "--reserved-non-ats", "100",
    "--board-budget", "100", "--provider-budget", "120", "--max-boards", "8",
]


def _def(root):
    return DEFINITIVE + ["--artifact-root", root]


class SanitizationBoundaryTests(unittest.TestCase):
    # (1) Parser carries maintenance defaults; sanitized identity excludes them.
    def test_parser_has_maintenance_defaults_but_identity_excludes_them(self):
        a = R.build_parser().parse_args(_def(tempfile.mkdtemp()))
        for k in ("expected_ownership_token", "expected_run_id",
                  "inspect_run_lock", "recover_stale_run_lock"):
            self.assertIn(k, vars(a))          # present in the parsed namespace
        san = R._identity_arguments(a)
        for k in ("expected_ownership_token", "expected_run_id",
                  "inspect_run_lock", "recover_stale_run_lock"):
            self.assertNotIn(k, san)           # excluded from the run identity
        for k in ("mode", "lanes", "boards", "target", "airtable_write",
                  "global_budget", "max_boards"):
            self.assertIn(k, san)              # execution args survive

    # (2) The normal definitive command reaches RunContext.create with no leak.
    def test_definitive_reaches_runcontext_without_secret_leak(self):
        a = R.build_parser().parse_args(_def(tempfile.mkdtemp()))
        mode = ExecutionMode(a.mode)
        # Unsanitized args reproduce the production crash ...
        with self.assertRaises(RuntimeError):
            RunContext.create(mode, vars(a))
        # ... sanitized args build the identity cleanly.
        ctx = RunContext.create(mode, R._identity_arguments(a))
        self.assertTrue(ctx.run_id)
        names = [e["name"] for e in ctx.effective_config]
        self.assertNotIn("RUN_ARG_EXPECTED_OWNERSHIP_TOKEN", names)

    # (3) expected_ownership_token is never persisted, even via the recovery cmd.
    def test_token_never_persisted_even_when_supplied(self):
        argv = ["--recover-stale-run-lock", "--artifact-root", tempfile.mkdtemp(),
                "--expected-run-id", "RID", "--expected-ownership-token", "SUPERSECRET-TOKEN"]
        a = R.build_parser().parse_args(argv)
        san = R._identity_arguments(a)
        self.assertNotIn("expected_ownership_token", san)
        self.assertNotIn("SUPERSECRET-TOKEN", json.dumps(san))
        ctx = RunContext.create(ExecutionMode(a.mode), san)
        self.assertNotIn("SUPERSECRET-TOKEN", json.dumps(ctx.to_dict()))


class DetectorNotWeakenedTests(unittest.TestCase):
    # (4) Genuine secrets are still rejected by the unchanged detector.
    def test_secret_named_run_arg_still_rejected(self):
        entries = effective_config_snapshot({"apollo_api_key": "sk-live-should-not-persist"})
        with self.assertRaises(RuntimeError):
            assert_no_secret_values(entries)

    def test_detector_still_flags_secret_names(self):
        for name in ("RUN_ARG_EXPECTED_OWNERSHIP_TOKEN", "APOLLO_API_KEY",
                     "RAPIDAPI_KEY", "SOME_SECRET", "DB_PASSWORD", "X_CREDENTIAL"):
            self.assertTrue(_is_secret(name), name)
        for name in ("RUN_ARG_MODE", "RUN_ARG_TARGET", "RUN_ARG_LANES", "KEYWORD"):
            self.assertFalse(_is_secret(name), name)

    def test_sanitizer_uses_the_same_detector(self):
        # A future --db-password style maintenance arg would be dropped too.
        class NS:
            pass
        ns = NS()
        ns.__dict__.update({"mode": "full_dry_run", "some_password": "hunter2",
                            "api_token": "abc", "target": 5})
        san = R._identity_arguments(ns)
        self.assertNotIn("some_password", san)
        self.assertNotIn("api_token", san)
        self.assertIn("mode", san)
        self.assertIn("target", san)


class CredentialCliTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(config, k, None) for k in
                       ("RAPIDAPI_KEY", "APOLLO_API_KEY", "AIRTABLE_TOKEN",
                        "AIRTABLE_BASE_ID", "AIRTABLE_TABLE_NAME")}
        for k, v in (("RAPIDAPI_KEY", "x"), ("APOLLO_API_KEY", "a"),
                     ("AIRTABLE_TOKEN", "t"), ("AIRTABLE_BASE_ID", "b"),
                     ("AIRTABLE_TABLE_NAME", "L")):
            setattr(config, k, v)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)

    def _plant_legacy_stale_lock(self, root):
        p = Path(root) / ".run.lock"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "run_id": "20260805T100858Z-233a6adb", "pid": 1,
            "acquired_at_epoch": time.time() - 90000}))   # ~25h -> stale legacy
        return p

    # (5) Strict preflight + stale legacy lock proceeds INTO RunLock.acquire,
    #     auto-recovers (audited), acquires a new lock, and releases it.
    def test_definitive_reaches_and_recovers_through_acquire(self):
        from orchestrator.pipeline import Orchestrator
        root = tempfile.mkdtemp()
        self._plant_legacy_stale_lock(root)

        def _fake_body(self, plan, *, resume=False, lock=None):
            return {"run": {"status": "complete"},
                    "waterfall": {"unit_totals": {"postings": 0}, "final_pass_count": 0},
                    "delivery": None, "all_reconcile": True}

        with mock.patch.object(Orchestrator, "_run_body", _fake_body):
            rc = R.main(_def(root))
        self.assertEqual(rc, 0)                                   # reached + completed
        audits = list((Path(root) / "run_lock_audit").glob("*auto_stale_recovery*.json"))
        self.assertTrue(audits, "auto stale-recovery must be audited")
        self.assertFalse((Path(root) / ".run.lock").exists())    # new lock released

    # (6) A failure before acquisition neither creates nor overwrites a lock.
    def test_preacquire_failure_leaves_lock_untouched(self):
        root = tempfile.mkdtemp()
        p = self._plant_legacy_stale_lock(root)
        before = p.read_bytes()
        with mock.patch.object(R.RunContext, "create",
                               side_effect=RuntimeError("boom pre-acquire")):
            with self.assertRaises(RuntimeError):
                R.main(_def(root))
        self.assertEqual(p.read_bytes(), before)                 # not overwritten
        # no second lock artifact created
        self.assertEqual(len(list(Path(root).glob("*.run.lock*"))), 1)


class ZeroNetworkTests(unittest.TestCase):
    # (10) Sanitization + identity build touch no network.
    def test_identity_build_touches_no_network(self):
        orig = requests.request
        requests.request = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network!"))
        try:
            a = R.build_parser().parse_args(_def(tempfile.mkdtemp()))
            san = R._identity_arguments(a)
            ctx = RunContext.create(ExecutionMode(a.mode), san)
            self.assertTrue(ctx.run_id)
            entries = effective_config_snapshot({"apollo_api_key": "x"})
            with self.assertRaises(RuntimeError):
                assert_no_secret_values(entries)
        finally:
            requests.request = orig


class PreservedBehaviorTests(unittest.TestCase):
    # (7) Railway Start Command unchanged.
    def test_railway_start_command_unchanged(self):
        rc = json.loads(Path("railway.json").read_text(encoding="utf-8"))
        cmd = rc["deploy"]["startCommand"]
        self.assertIn("--mode live_acquisition_and_enrichment", cmd)
        self.assertIn("--airtable-write", cmd)
        self.assertIn("--artifact-root /app/data/state/orchestrator_v2", cmd)
        self.assertNotIn("--auto-approve", cmd)
        self.assertNotIn("--instantly", cmd)
        self.assertEqual(rc["deploy"]["restartPolicyType"], "NEVER")

    # (8)(9) Airtable review-staging; Instantly + auto-approval disabled.
    def test_live_policy_keeps_review_staging_and_instantly_off(self):
        p = policy_for(ExecutionMode.LIVE_ACQUISITION_AND_ENRICHMENT)
        self.assertTrue(p.allow_airtable_write)
        self.assertFalse(p.allow_instantly_enrollment)
        self.assertFalse(p.allow_production_state_write)

    # (11) Package integrity passes (manifest covers the changed entrypoint).
    def test_package_integrity_passes(self):
        if not Path("orchestrator.MANIFEST.sha256").is_file():
            self.skipTest("manifest not present in CWD")
        res, _ = R._preflight_checks(
            R.build_parser().parse_args(["--artifact-root", tempfile.mkdtemp()]))
        self.assertTrue(res["integrity_ok"])


if __name__ == "__main__":
    unittest.main()
