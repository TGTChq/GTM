"""Run-lock lifecycle: identity-aware stale recovery, ownership tokens, and the
15-point production run-lock contract.

The core defect this guards against: a lock left on the persistent volume by a
Railway container that is already gone must be recovered (safely, audited) rather
than blocking every future Run Now -- while a genuinely concurrent run is still
refused. Separate "processes" are modelled by separate RunLock instances (each
gets its own ownership token); separate "containers" by writing a lock whose
``boot_id`` differs from the current one. Everything here is zero-network.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import requests

import config
import run_orchestrator as R
from orchestrator import runlock
from orchestrator.modes import ExecutionMode, policy_for
from orchestrator.runlock import (
    DEFAULT_STALE_SECONDS,
    LEGACY_TOKEN_SENTINEL,
    RunLock,
    RunLockHeld,
    classify_lock,
    current_identity,
    describe_lock,
    read_lock,
    recover_stale_lock,
)


def _lockpath() -> Path:
    return Path(tempfile.mkdtemp()) / "orchestrator_v2" / ".run.lock"


def _write_raw(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class LifecycleTests(unittest.TestCase):
    # (1) First clean invocation proceeds.
    def test_first_clean_invocation_acquires(self):
        p = _lockpath()
        lock = RunLock(p, "RID-1").acquire()
        self.assertTrue(lock.acquired)
        self.assertFalse(lock.recovered_stale)
        info = read_lock(p)
        self.assertEqual(info["run_id"], "RID-1")
        self.assertEqual(info["pid"], os.getpid())
        self.assertTrue(info["ownership_token"])
        lock.release()
        self.assertFalse(p.exists())

    # (2) The process never rejects its own lock.
    def test_never_rejects_its_own_lock(self):
        p = _lockpath()
        lock = RunLock(p, "RID-1").acquire()
        # Re-acquiring the SAME instance recognises its own ownership token.
        again = lock.acquire()
        self.assertTrue(again.acquired)
        info = read_lock(p)
        cls, _ = classify_lock(info, self_identity=lock.identity,
                               self_token=lock.ownership_token,
                               stale_seconds=DEFAULT_STALE_SECONDS, now_epoch=time.time())
        self.assertEqual(cls, "current")
        lock.release()

    # (3) A genuine concurrent invocation (same live container) is refused.
    def test_genuine_concurrent_is_refused(self):
        p = _lockpath()
        # Force a deterministic "same container, live pid" foreign lock.
        orig_boot = runlock._boot_id
        orig_alive = runlock._pid_alive
        runlock._boot_id = lambda: "BOOT-SAME"
        runlock._pid_alive = lambda pid: True
        try:
            first = RunLock(p, "RID-A").acquire()   # writes boot_id=BOOT-SAME
            info = read_lock(p)
            cls, _ = classify_lock(info, self_identity=current_identity(),
                                   self_token=None, stale_seconds=DEFAULT_STALE_SECONDS,
                                   now_epoch=time.time())
            self.assertEqual(cls, "foreign_active")
            with self.assertRaises(RunLockHeld):
                RunLock(p, "RID-B").acquire()
            first.release()
        finally:
            runlock._boot_id = orig_boot
            runlock._pid_alive = orig_alive

    # (4) Successful execution releases the lock.
    def test_release_frees_lock(self):
        p = _lockpath()
        with RunLock(p, "RID") as lock:
            self.assertTrue(p.exists())
        self.assertFalse(p.exists())
        self.assertFalse(lock.acquired)

    # (8) PID reuse across containers is NOT treated as ownership.
    def test_pid_reuse_across_containers_is_stale_not_owner(self):
        p = _lockpath()
        # A lock from a DIFFERENT container that happens to record OUR live pid.
        _write_raw(p, {
            "schema_version": runlock.RUNLOCK_SCHEMA, "run_id": "OLD",
            "pid": os.getpid(),                      # a live pid in THIS process
            "ownership_token": "deadbeef",
            "boot_id": "BOOT-OTHER-CONTAINER",       # but a different container
            "created_at_epoch": time.time(),         # fresh, not aged out
        })
        ident = dict(current_identity(), boot_id="BOOT-THIS-CONTAINER")
        cls, _ = classify_lock(read_lock(p), self_identity=ident, self_token=None,
                               stale_seconds=DEFAULT_STALE_SECONDS, now_epoch=time.time())
        self.assertEqual(cls, "stale")   # container gone -> stale, pid ignored

    # (7) Abnormal termination leaves an inspectable stale candidate.
    def test_abnormal_termination_leaves_inspectable_stale(self):
        p = _lockpath()
        _write_raw(p, {
            "schema_version": runlock.RUNLOCK_SCHEMA, "run_id": "CRASHED",
            "pid": 4242, "ownership_token": "aaaa",
            "boot_id": "BOOT-GONE", "created_at": "2026-01-01T00:00:00+00:00",
            "created_at_epoch": time.time() - 60,
        })
        ident = dict(current_identity(), boot_id="BOOT-NOW")
        desc = describe_lock(p)
        self.assertTrue(desc["present"])
        self.assertEqual(desc["run_id"], "CRASHED")
        # classification via a different-container identity is stale
        cls, _ = classify_lock(read_lock(p), self_identity=ident, self_token=None,
                               stale_seconds=DEFAULT_STALE_SECONDS, now_epoch=time.time())
        self.assertEqual(cls, "stale")

    # (12) Railway-style separate-container invocation auto-recovers a gone lock.
    def test_separate_container_auto_recovers_gone_lock(self):
        p = _lockpath()
        _write_raw(p, {
            "schema_version": runlock.RUNLOCK_SCHEMA, "run_id": "CONTAINER-A",
            "pid": 1, "ownership_token": "tok-a", "boot_id": "BOOT-A",
            "created_at_epoch": time.time(),
        })
        orig = runlock._boot_id
        runlock._boot_id = lambda: "BOOT-B"   # we are container B
        try:
            lock = RunLock(p, "CONTAINER-B").acquire()
            self.assertTrue(lock.recovered_stale)
            self.assertEqual(lock.recovered_classification, "stale")
            self.assertEqual(read_lock(p)["run_id"], "CONTAINER-B")
            lock.release()
        finally:
            runlock._boot_id = orig

    def test_aged_out_legacy_lock_auto_recovers(self):
        # A legacy lock (no boot_id/token) older than the window is stale.
        p = _lockpath()
        _write_raw(p, {"run_id": "OLD", "pid": 999999,
                       "acquired_at_epoch": time.time() - 10 * 3600})
        lock = RunLock(p, "NEW", stale_seconds=6 * 3600).acquire()
        self.assertTrue(lock.recovered_stale)
        self.assertEqual(read_lock(p)["run_id"], "NEW")
        lock.release()

    def test_fresh_legacy_lock_is_indeterminate_not_recovered(self):
        # A legacy lock with no identity, still fresh -> indeterminate: refuse to
        # auto-recover (needs explicit operator action), never guess from pid.
        p = _lockpath()
        _write_raw(p, {"run_id": "OLD", "pid": 999999,
                       "acquired_at_epoch": time.time() - 60})
        lock = RunLock(p, "NEW", stale_seconds=6 * 3600)
        with self.assertRaises(RunLockHeld):
            lock.acquire()
        self.assertFalse(lock.recovered_stale)


class OwnershipReleaseTests(unittest.TestCase):
    def test_release_only_when_token_matches(self):
        # Never release another owner's lock: our release must not delete a lock
        # whose token changed out from under us.
        p = _lockpath()
        lock = RunLock(p, "RID").acquire()
        # Someone else overwrites the lock (different token).
        _write_raw(p, {"run_id": "OTHER", "ownership_token": "not-ours",
                       "created_at_epoch": time.time()})
        lock.release()
        self.assertTrue(p.exists())                       # not ours -> left intact
        self.assertEqual(read_lock(p)["run_id"], "OTHER")


class RecoveryTests(unittest.TestCase):
    def _stale_lock(self, p, *, run_id="OLD", token="tok", boot="BOOT-GONE"):
        _write_raw(p, {"schema_version": runlock.RUNLOCK_SCHEMA, "run_id": run_id,
                       "pid": 1, "ownership_token": token, "boot_id": boot,
                       "created_at_epoch": time.time() - 10 * 3600})

    # (9) Stale recovery requires exact matching identity.
    def test_recovery_requires_exact_run_id(self):
        p = _lockpath()
        self._stale_lock(p, run_id="OLD", token="tok")
        out = recover_stale_lock(p, expected_run_id="WRONG", expected_token="tok")
        self.assertFalse(out["recovered"])
        self.assertIn("run_id mismatch", out["reason"])
        self.assertTrue(p.exists())

    def test_recovery_requires_exact_token(self):
        p = _lockpath()
        self._stale_lock(p, run_id="OLD", token="tok")
        out = recover_stale_lock(p, expected_run_id="OLD", expected_token="WRONG")
        self.assertFalse(out["recovered"])
        self.assertIn("token mismatch", out["reason"])
        self.assertTrue(p.exists())

    def test_recovery_with_exact_identity_succeeds_and_audits(self):
        p = _lockpath()
        self._stale_lock(p, run_id="OLD", token="tok")
        out = recover_stale_lock(p, expected_run_id="OLD", expected_token="tok",
                                 audit_dir=p.parent / "run_lock_audit")
        self.assertTrue(out["recovered"])
        self.assertFalse(p.exists())                      # lock removed
        self.assertTrue(Path(out["audit_path"]).is_file())  # audit written
        audit = json.loads(Path(out["audit_path"]).read_text())
        self.assertEqual(audit["event"], "manual_stale_recovery")
        self.assertEqual(audit["lock_run_id"], "OLD")

    # (10) Recovery cannot delete a changed or active lock.
    def test_recovery_refuses_active_owner(self):
        p = _lockpath()
        orig_boot, orig_alive = runlock._boot_id, runlock._pid_alive
        runlock._boot_id = lambda: "BOOT-SAME"
        runlock._pid_alive = lambda pid: True
        try:
            _write_raw(p, {"schema_version": runlock.RUNLOCK_SCHEMA, "run_id": "LIVE",
                           "pid": os.getpid(), "ownership_token": "tok",
                           "boot_id": "BOOT-SAME", "created_at_epoch": time.time()})
            out = recover_stale_lock(p, expected_run_id="LIVE", expected_token="tok")
            self.assertFalse(out["recovered"])
            self.assertIn("active", out["reason"])
            self.assertTrue(p.exists())
        finally:
            runlock._boot_id, runlock._pid_alive = orig_boot, orig_alive

    def test_recovery_refuses_changed_lock(self):
        p = _lockpath()
        self._stale_lock(p, run_id="OLD", token="tok")
        # The lock changed since inspection (token rotated).
        self._stale_lock(p, run_id="OLD", token="rotated")
        out = recover_stale_lock(p, expected_run_id="OLD", expected_token="tok")
        self.assertFalse(out["recovered"])
        self.assertTrue(p.exists())

    def test_legacy_lock_recovered_with_sentinel(self):
        # A pre-hotfix lock has no ownership token: recover with the sentinel.
        p = _lockpath()
        _write_raw(p, {"run_id": "LEGACY", "pid": 999999,
                       "acquired_at_epoch": time.time() - 10 * 3600})
        bad = recover_stale_lock(p, expected_run_id="LEGACY", expected_token="whatever")
        self.assertFalse(bad["recovered"])                # wrong token for legacy
        good = recover_stale_lock(p, expected_run_id="LEGACY",
                                  expected_token=LEGACY_TOKEN_SENTINEL,
                                  audit_dir=p.parent / "audit")
        self.assertTrue(good["recovered"])
        self.assertFalse(p.exists())


class ZeroNetworkTests(unittest.TestCase):
    # (11) Inspection and recovery make zero network requests.
    def test_inspect_and_recover_touch_no_network(self):
        p = _lockpath()
        _write_raw(p, {"schema_version": runlock.RUNLOCK_SCHEMA, "run_id": "OLD",
                       "pid": 1, "ownership_token": "tok", "boot_id": "BOOT-GONE",
                       "created_at_epoch": time.time() - 10 * 3600})
        orig = requests.request

        def _boom(*a, **k):
            raise AssertionError("network contacted")

        requests.request = _boom
        try:
            desc = describe_lock(p)                       # must not touch network
            self.assertTrue(desc["present"])
            out = recover_stale_lock(p, expected_run_id="OLD", expected_token="tok",
                                     audit_dir=p.parent / "audit")
            self.assertTrue(out["recovered"])
        finally:
            requests.request = orig


class CliGateTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(config, k, None) for k in
                       ("RAPIDAPI_KEY", "APOLLO_API_KEY", "HUNTER_API_KEY",
                        "AIRTABLE_TOKEN", "AIRTABLE_BASE_ID", "AIRTABLE_TABLE_NAME")}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)

    def _all_keys(self, root):
        for k, v in (("RAPIDAPI_KEY", "x"), ("APOLLO_API_KEY", "a"),
                     ("AIRTABLE_TOKEN", "t"), ("AIRTABLE_BASE_ID", "b"),
                     ("AIRTABLE_TABLE_NAME", "L")):
            setattr(config, k, v)

    def _live_argv(self, root):
        return ["--mode", "live_acquisition_and_enrichment",
                "--lanes", "ats,jsearch,free_feeds", "--boards", "BOARDS_FINAL.json",
                "--airtable-write", "--artifact-root", root]

    # (5) Preflight failure before acquisition leaves no lock.
    def test_preflight_failure_leaves_no_lock(self):
        root = tempfile.mkdtemp()
        self._all_keys(root)
        config.APOLLO_API_KEY = ""            # force a strict-preflight refusal
        rc = R.main(self._live_argv(root))
        self.assertEqual(rc, 2)
        self.assertFalse((Path(root) / ".run.lock").exists())  # nothing acquired

    def test_stale_lock_does_not_block_strict_preflight(self):
        # The core fix: a stale lock must NOT make strict preflight refuse.
        root = tempfile.mkdtemp()
        self._all_keys(root)
        _write_raw(Path(root) / ".run.lock", {
            "schema_version": runlock.RUNLOCK_SCHEMA, "run_id": "OLD", "pid": 1,
            "ownership_token": "tok", "boot_id": "BOOT-GONE",
            "created_at_epoch": time.time() - 10 * 3600})   # aged out -> stale
        rc = R._strict_preflight(R.build_parser().parse_args(self._live_argv(root)),
                                 policy_for(ExecutionMode.LIVE_ACQUISITION_AND_ENRICHMENT))
        self.assertEqual(rc, 0)               # stale lock does not block

    def test_indeterminate_lock_blocks_strict_preflight(self):
        root = tempfile.mkdtemp()
        self._all_keys(root)
        _write_raw(Path(root) / ".run.lock", {
            "run_id": "MYSTERY", "pid": 999999,
            "acquired_at_epoch": time.time() - 60})         # fresh legacy -> indeterminate
        rc = R._strict_preflight(R.build_parser().parse_args(self._live_argv(root)),
                                 policy_for(ExecutionMode.LIVE_ACQUISITION_AND_ENRICHMENT))
        self.assertEqual(rc, 2)               # unprovable lock blocks

    def test_inspect_and_recover_cli_roundtrip(self):
        root = tempfile.mkdtemp()
        _write_raw(Path(root) / ".run.lock", {
            "schema_version": runlock.RUNLOCK_SCHEMA, "run_id": "OLD", "pid": 1,
            "ownership_token": "tok-visible", "boot_id": "BOOT-GONE",
            "created_at_epoch": time.time() - 10 * 3600})
        rc_i = R.main(["--inspect-run-lock", "--artifact-root", root])
        self.assertEqual(rc_i, 0)
        rc_r = R.main(["--recover-stale-run-lock", "--artifact-root", root,
                       "--expected-run-id", "OLD", "--expected-ownership-token", "tok-visible"])
        self.assertEqual(rc_r, 0)
        self.assertFalse((Path(root) / ".run.lock").exists())

    def test_recover_cli_refuses_wrong_identity(self):
        root = tempfile.mkdtemp()
        _write_raw(Path(root) / ".run.lock", {
            "schema_version": runlock.RUNLOCK_SCHEMA, "run_id": "OLD", "pid": 1,
            "ownership_token": "tok", "boot_id": "BOOT-GONE",
            "created_at_epoch": time.time() - 10 * 3600})
        rc = R.main(["--recover-stale-run-lock", "--artifact-root", root,
                     "--expected-run-id", "OLD", "--expected-ownership-token", "WRONG"])
        self.assertEqual(rc, 2)
        self.assertTrue((Path(root) / ".run.lock").exists())


class PipelineFinallyTests(unittest.TestCase):
    # (6) A failure after acquisition still releases the lock through finally.
    def test_failure_after_acquire_releases_lock(self):
        from orchestrator.runcontrol import RunContext
        from orchestrator.state import StateManager
        from orchestrator.pipeline import Orchestrator, OrchestratorPlan
        from orchestrator.enrichment import EnrichmentEngine, FakeEnrichmentAdapter
        from orchestrator.delivery import DeliveryManager, FakeAirtableAdapter, FakeInstantlyAdapter
        from retrieval_measurement.instrument import RequestBudget

        tmp = tempfile.mkdtemp()
        ctx = RunContext.create(ExecutionMode.FULL_DRY_RUN, {}, run_id="FAILRUN")
        state = StateManager(tmp, policy_for(ExecutionMode.FULL_DRY_RUN), run_id="FAILRUN")
        plan = OrchestratorPlan(
            lanes=[], lane_runners={},
            enrichment_engine=EnrichmentEngine(FakeEnrichmentAdapter()),
            delivery_manager=DeliveryManager(
                state=state, airtable=FakeAirtableAdapter(), instantly=FakeInstantlyAdapter(),
                enable_airtable_write=False, auto_approve=False, enable_instantly=False))
        orch = Orchestrator(ctx, state, RequestBudget(limit=100))

        def _boom(*a, **k):
            raise RuntimeError("boom mid-run")

        orch._run_body = _boom
        with self.assertRaises(RuntimeError):
            orch.run(plan)
        self.assertFalse((state.root / ".run.lock").exists())  # released in finally


class PreservedBehaviorTests(unittest.TestCase):
    # (13)(14) Airtable review-staging + Instantly/auto-approval disabled.
    def test_live_mode_keeps_airtable_review_staging_instantly_off(self):
        p = policy_for(ExecutionMode.LIVE_ACQUISITION_AND_ENRICHMENT)
        self.assertTrue(p.allow_airtable_write)
        self.assertFalse(p.allow_instantly_enrollment)     # Instantly unreachable
        self.assertFalse(p.allow_production_state_write)

    def test_railway_json_does_not_define_start_command(self):
        # GTM's Start Command is service-managed (Railway UI), NOT config-as-code,
        # so it can be edited/restored (incl. a maintenance 'sleep infinity') with no
        # Git change. railway.json must not pin it, and no acquisition flag may leak
        # into config-as-code. A service with no Start Command falls back to the safe
        # --preflight-only image CMD, never acquisition.
        rc = json.loads(Path("railway.json").read_text(encoding="utf-8"))
        self.assertNotIn("startCommand", rc.get("deploy", {}))
        blob = json.dumps(rc)
        self.assertNotIn("live_acquisition_and_enrichment", blob)
        self.assertNotIn("--airtable-write", blob)
        self.assertEqual(rc["deploy"]["restartPolicyType"], "NEVER")

    # (15) Package integrity passes (manifest covers runlock + entrypoint).
    def test_package_integrity_passes(self):
        if not Path("orchestrator.MANIFEST.sha256").is_file():
            self.skipTest("manifest not present in CWD")
        res, _ = R._preflight_checks(
            R.build_parser().parse_args(["--artifact-root", tempfile.mkdtemp()]))
        self.assertTrue(res["integrity_ok"])


if __name__ == "__main__":
    unittest.main()
