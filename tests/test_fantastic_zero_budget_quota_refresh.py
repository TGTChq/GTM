"""Zero-budget quota-refresh recovery: full failure-mode / boundary audit.

Production defect (observed 2026-09-02, run 20260902T130429Z-a54f4f19):

    governor_budget 0 reason=provider_quota_floor remaining=236 reserve=2000
    stop_reason topup:governor_zero_budget   raw_postings 0

``_save_quota_snapshot`` is reachable ONLY from inside ``acquire()``. A zero grant
stops the run before acquisition, so the snapshot that caused the zero is never
rewritten and the zero survives even a real provider quota reset (a 0-credit probe
on 2026-09-03 found the provider holding a FULL 100,000 jobs while the pipeline was
still granting 0 off a stale 236).

REQUIRED vs OPTIONAL provider metadata
--------------------------------------
REQUIRED  x-api-jobs-remaining -- the ONLY field the governor treats as
          authoritative remaining. Absent/unparseable => refuse to refresh.
OPTIONAL  x-api-jobs-limit          -- sanity-clamps remaining; observability.
          x-api-requests-*          -- observability only.
          x-api-next-billing-date   -- improves the pacing horizon; absent or
                                       unparseable => persisted as "", which the
                                       governor treats as a conservative 30 days.

Guarantees asserted here: bounded, idempotent, fail-closed, provider-specific,
single-request, non-recursive, snapshot-compatible, and durable across runs.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import config
import fantastic_jobs_adapter as fja
from orchestrator import fantastic_governor as G
from orchestrator.pipeline import Orchestrator

# Provider truth captured by the ONE authorized live count request (2026-09-03).
PROVIDER_HEADERS = {
    "x-api-jobs-limit": "100000",
    "x-api-jobs-remaining": "100000",
    "x-api-requests-limit": "50000",
    "x-api-requests-remaining": "49999",
    "x-api-next-billing-date": "2026-10-01T23:39:15.948851Z",
}
BILLING_DATE = "2026-10-01T23:39:15.948851Z"
STALE_REMAINING = 236
QUOTA_FLOOR = 500          # FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING on GTM
NOW = datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc)
RESET = datetime(2026, 10, 1, 23, 39, 15, tzinfo=timezone.utc)


@contextmanager
def patched_config(**over):
    sentinel = object()
    old = {k: getattr(config, k, sentinel) for k in over}
    try:
        for k, v in over.items():
            setattr(config, k, v)
        yield
    finally:
        for k, v in old.items():
            if v is sentinel:
                delattr(config, k)
            else:
                setattr(config, k, v)


def _resp(status=200, headers=None):
    return SimpleNamespace(status_code=status, headers=dict(headers or {}))


def _decision(budget, reason, remaining=STALE_REMAINING):
    return G.GovernorDecision(
        run_budget=budget, reason=reason, remaining_credits=remaining,
        spendable_credits=max(0, remaining - 2000), reserve_credits=2000,
        base_daily_allowance=0, carry_forward_applied=0, days_remaining=30.0,
        inventory_capped=False, provider_authoritative=True)


def _ctx(budget, reason, remaining=STALE_REMAINING):
    return G.GovernorContext(enabled=True, decision=_decision(budget, reason, remaining),
                             ledger=None, armed=True)


def _inputs(remaining, *, limit=20000, floor=QUOTA_FLOOR, carry=0, spent=0,
            dmax=0, ceiling=12000, hint=None, has_runs=True):
    return G.GovernorInputs(
        monthly_limit=limit, now=NOW, cycle_reset_at=RESET, ledger_used_this_cycle=0,
        provider_jobs_remaining=remaining, reserve_pct=0.10, daily_min_jobs=100,
        daily_max_jobs=dmax, per_run_ceiling=ceiling, quota_floor=floor,
        carry_forward=carry, carry_forward_cap_days=3.0, inventory_hint=hint,
        spent_today=spent, ledger_has_runs=has_runs)


class _Recorder:
    def __init__(self, result=None, raises=None):
        self.result, self.raises, self.calls = result, raises, 0

    def __call__(self, *a, **k):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return dict(self.result)


class _Orch(Orchestrator):
    """Bare instance exposing only the collaborators the method under test uses."""

    def __init__(self, rebuilt):
        self._rebuilt = rebuilt
        self.rebuild_calls = 0

    def _build_governor(self):
        self.rebuild_calls += 1
        return self._rebuilt


class _SnapshotCase(unittest.TestCase):
    """Shared temp snapshot/ledger plumbing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.snap = os.path.join(self.tmp.name, "fantastic_quota_snapshot.json")
        self.ledger = os.path.join(self.tmp.name, "ledger.json")
        self.cfg = dict(FANTASTIC_QUOTA_SNAPSHOT_PATH=self.snap,
                        FANTASTIC_JOBS_API_KEY="test-key",
                        FANTASTIC_JOBS_BASE_URL="https://data.fantastic.jobs",
                        FANTASTIC_JOBS_REQUEST_TIMEOUT_SECONDS=30)

    def seed(self, **over):
        snap = {"schema": "fantastic-quota-snapshot/1", "jobs_remaining": STALE_REMAINING,
                "requests_remaining": 9000, "next_billing_date": "",
                "captured_at": "2026-09-02T13:04:00+00:00"}
        snap.update(over)
        with open(self.snap, "w", encoding="utf-8") as fh:
            json.dump(snap, fh)
        return snap

    def read_snap(self):
        with open(self.snap, encoding="utf-8") as fh:
            return json.load(fh)


# ==========================================================================
# A / C.  Governor boundary semantics, derived from code (not assumed).
# ==========================================================================
class GovernorBoundaryTest(unittest.TestCase):
    # (remaining, monthly_limit) -> (budget, reason)
    CASES = [
        # floor = 500, reserve = 10% of 20000 = 2000
        (0, 20000, 0, "provider_quota_floor"),
        (1, 20000, 0, "provider_quota_floor"),
        (499, 20000, 0, "provider_quota_floor"),
        (500, 20000, 0, "provider_quota_floor"),   # floor is INCLUSIVE (<=)
        (501, 20000, 0, "reserve"),                # above floor, still under reserve
        (2000, 20000, 0, "reserve"),
        (2001, 20000, 1, "reserve"),
        (2100, 20000, 100, "daily_min"),
        (100000, 20000, 3445, "pace"),
        # floor = 500, reserve = 10% of 100000 = 10000
        (499, 100000, 0, "provider_quota_floor"),
        (500, 100000, 0, "provider_quota_floor"),
        (501, 100000, 0, "reserve"),
        (10000, 100000, 0, "reserve"),
        (10001, 100000, 1, "reserve"),
        (100000, 100000, 3164, "pace"),
    ]

    def test_boundaries(self):
        for remaining, limit, budget, reason in self.CASES:
            with self.subTest(remaining=remaining, limit=limit):
                d = G.decide(_inputs(remaining, limit=limit))
                self.assertEqual(d.run_budget, budget)
                self.assertEqual(d.reason, reason)

    def test_every_zero_boundary_is_refresh_eligible(self):
        """A stale snapshot anywhere in the zero band must be recoverable."""
        for remaining, limit, budget, reason in self.CASES:
            if budget:
                continue
            with self.subTest(remaining=remaining, limit=limit):
                self.assertIn(reason, G.QUOTA_METADATA_ZERO_REASONS,
                              f"zero at remaining={remaining} would deadlock forever")

    def test_recovery_is_plan_size_independent(self):
        """The mechanism must not depend on one specific plan size."""
        for limit in (20000, 100000):
            with self.subTest(limit=limit):
                self.assertEqual(G.decide(_inputs(STALE_REMAINING, limit=limit)).run_budget, 0)
                self.assertGreater(G.decide(_inputs(100000, limit=limit)).run_budget, 0)


# ==========================================================================
# D / H.  Non-quota zero reasons must never trigger a probe.
# ==========================================================================
class NonQuotaZeroReasonTest(unittest.TestCase):
    def test_enumerated_from_code(self):
        """With provider quota HEALTHY, these are the zeros the governor can emit."""
        healthy = 100000
        observed = {}
        for label, kw in {"daily_allowance_spent": dict(spent=10 ** 9),
                          "inventory_hint": dict(hint=0)}.items():
            d = G.decide(_inputs(healthy, **kw))
            observed[label] = (d.run_budget, d.reason)
            self.assertEqual(d.run_budget, 0)
            self.assertEqual(d.reason, label)
            self.assertNotIn(d.reason, G.QUOTA_METADATA_ZERO_REASONS,
                             "re-reading quota cannot fix this zero")
        self.assertEqual(len(observed), 2)

    def test_zero_valued_ceilings_mean_no_ceiling(self):
        """Derived, not assumed: 0 is 'unlimited' for both ceilings, not 'zero'."""
        self.assertEqual(G.decide(_inputs(100000, ceiling=0)).reason, "pace")
        self.assertEqual(G.decide(_inputs(100000, dmax=0)).reason, "pace")

    def test_zero_reason_partition_is_exhaustive(self):
        """Property sweep: every reachable zero is classified, none is missed.

        Invariants:
          * a zero caused by quota metadata IS refresh-eligible (else it would
            deadlock forever);
          * a zero with HEALTHY provider quota is NEVER refresh-eligible (else
            recovery could bypass an unrelated governor limit).
        """
        import itertools
        grid = dict(rem=[0, 1, 499, 500, 501, 2000, 2001, 100000, -5],
                    limit=[0, 20000, 100000], floor=[0, 500, 5000],
                    carry=[0, 10 ** 6], spent=[0, 10 ** 9], dmax=[0, 100],
                    ceiling=[0, 12000], hint=[None, 0], has_runs=[True, False],
                    auth=[True, False])
        keys = list(grid)
        seen_zero, healthy_zero = set(), set()
        for combo in itertools.product(*(grid[k] for k in keys)):
            kw = dict(zip(keys, combo))
            d = G.decide(G.GovernorInputs(
                monthly_limit=kw["limit"], now=NOW, cycle_reset_at=RESET,
                ledger_used_this_cycle=0,
                provider_jobs_remaining=(kw["rem"] if kw["auth"] else None),
                reserve_pct=0.10, daily_min_jobs=100, daily_max_jobs=kw["dmax"],
                per_run_ceiling=kw["ceiling"], quota_floor=kw["floor"],
                carry_forward=kw["carry"], carry_forward_cap_days=3.0,
                inventory_hint=kw["hint"], spent_today=kw["spent"],
                ledger_has_runs=kw["has_runs"]))
            if d.run_budget:
                continue
            seen_zero.add(d.reason)
            # "healthy" = provider says there is plenty AND the floor is not binding
            if kw["auth"] and kw["rem"] == 100000 and kw["floor"] < 100000:
                healthy_zero.add(d.reason)

        self.assertEqual(seen_zero, {"provider_quota_floor", "reserve",
                                     "daily_allowance_spent", "inventory_hint"},
                         "a new zero reason appeared and must be classified")
        self.assertTrue(seen_zero & G.QUOTA_METADATA_ZERO_REASONS)
        self.assertEqual(healthy_zero & G.QUOTA_METADATA_ZERO_REASONS, set(),
                         "a zero with healthy quota must never be refresh-eligible")

    def test_pipeline_skips_probe_for_every_non_quota_reason(self):
        for reason in ("daily_allowance_spent", "inventory_hint", "per_run_ceiling",
                       "daily_max", "governor_error_conservative", "blind_conservative"):
            with self.subTest(reason=reason):
                rec = _Recorder({"refreshed": True, "reason": "ok", "requests_made": 1})
                orch = _Orch(_ctx(9999, "pace"))
                with mock.patch.object(fja, "refresh_quota_snapshot", rec), \
                        patched_config(FANTASTIC_JOBS_ENABLED=True):
                    gov, info = orch._maybe_refresh_quota(_ctx(0, reason))
                self.assertEqual(rec.calls, 0)
                self.assertEqual(orch.rebuild_calls, 0)
                self.assertEqual(gov.run_budget, 0)
                self.assertTrue(info["reason"].startswith("not_quota_metadata:"))


# ==========================================================================
# C.  Provider metadata validation (table-driven).
# ==========================================================================
class ProviderMetadataValidationTest(_SnapshotCase):
    # (label, header overrides, expect_refreshed, expected persisted jobs_remaining)
    CASES = [
        ("all headers present", {}, True, 100000),
        ("jobs_limit missing", {"x-api-jobs-limit": None}, True, 100000),
        ("requests_limit missing", {"x-api-requests-limit": None}, True, 100000),
        ("requests_remaining missing", {"x-api-requests-remaining": None}, True, 100000),
        ("next_billing_date missing", {"x-api-next-billing-date": None}, True, 100000),
        ("next_billing_date invalid", {"x-api-next-billing-date": "not-a-date"}, True, 100000),
        ("next_billing_date in the past", {"x-api-next-billing-date": "2020-01-01T00:00:00Z"},
         True, 100000),
        ("next_billing_date far future", {"x-api-next-billing-date": "2099-01-01T00:00:00Z"},
         True, 100000),
        ("zero limits", {"x-api-jobs-limit": "0", "x-api-requests-limit": "0"}, True, 100000),
        ("remaining zero", {"x-api-jobs-remaining": "0"}, True, 0),
        ("remaining negative -> exhausted", {"x-api-jobs-remaining": "-5"}, True, 0),
        ("remaining > limit -> clamped down", {"x-api-jobs-remaining": "999999",
                                               "x-api-jobs-limit": "100000"}, True, 100000),
        ("requests_remaining > requests_limit (optional, ignored)",
         {"x-api-requests-remaining": "99999"}, True, 100000),
        ("absurdly large remaining, no limit header",
         {"x-api-jobs-remaining": "999999999999", "x-api-jobs-limit": None}, True, 999999999999),
        # REQUIRED field failures -> refuse.
        ("jobs_remaining missing", {"x-api-jobs-remaining": None}, False, STALE_REMAINING),
        ("jobs_remaining non-numeric", {"x-api-jobs-remaining": "abc"}, False, STALE_REMAINING),
        ("jobs_remaining empty", {"x-api-jobs-remaining": ""}, False, STALE_REMAINING),
        ("jobs_remaining float string", {"x-api-jobs-remaining": "1.5"}, False, STALE_REMAINING),
        ("no headers at all", {k: None for k in PROVIDER_HEADERS}, False, STALE_REMAINING),
    ]

    def test_metadata_matrix(self):
        for label, over, expect_ok, expect_remaining in self.CASES:
            with self.subTest(case=label):
                self.seed()
                hdr = {k: v for k, v in dict(PROVIDER_HEADERS, **over).items() if v is not None}
                calls = []
                with patched_config(**self.cfg):
                    out = fja.refresh_quota_snapshot(
                        http_get=lambda *a, **k: calls.append(1) or _resp(200, hdr))
                self.assertEqual(len(calls), 1, "exactly one request in every case")
                self.assertEqual(out["refreshed"], expect_ok, label)
                self.assertEqual(self.read_snap()["jobs_remaining"], expect_remaining, label)
                if not expect_ok:
                    self.assertEqual(out["reason"], "missing_quota_metadata")

    def test_absurd_remaining_is_still_bounded_by_per_run_ceiling(self):
        """No provider value can authorize more than the configured ceiling."""
        d = G.decide(_inputs(10 ** 12, ceiling=12000))
        self.assertEqual(d.run_budget, 12000)
        self.assertEqual(d.reason, "per_run_ceiling")

    def test_invalid_billing_date_is_persisted_blank(self):
        for raw in ("not-a-date", "2026-13-45", "", None):
            with self.subTest(raw=raw):
                self.seed()
                hdr = dict(PROVIDER_HEADERS)
                if raw is None:
                    hdr.pop("x-api-next-billing-date")
                else:
                    hdr["x-api-next-billing-date"] = raw
                with patched_config(**self.cfg):
                    out = fja.refresh_quota_snapshot(http_get=lambda *a, **k: _resp(200, hdr))
                self.assertTrue(out["refreshed"])
                self.assertEqual(self.read_snap()["next_billing_date"], "")
                # And the governor can parse whatever we persisted.
                self.assertIsNone(G._parse_iso(self.read_snap()["next_billing_date"]))

    def test_valid_billing_date_round_trips_to_governor(self):
        self.seed()
        with patched_config(**self.cfg):
            fja.refresh_quota_snapshot(http_get=lambda *a, **k: _resp(200, PROVIDER_HEADERS))
        parsed = G._parse_iso(self.read_snap()["next_billing_date"])
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.year, 2026)


# ==========================================================================
# D.  HTTP / transport failures (table-driven).
# ==========================================================================
class TransportFailureTest(_SnapshotCase):
    STATUSES = [401, 403, 404, 429, 500, 502, 503, 301, 204]
    EXCEPTIONS = [TimeoutError("timeout"), ConnectionError("conn reset"),
                  OSError("dns failure"), ValueError("bad json"), RuntimeError("boom")]

    def test_status_matrix(self):
        for status in self.STATUSES:
            with self.subTest(status=status):
                self.seed()
                calls = []
                with patched_config(**self.cfg):
                    out = fja.refresh_quota_snapshot(
                        http_get=lambda *a, **k: calls.append(1) or _resp(status, PROVIDER_HEADERS))
                self.assertFalse(out["refreshed"])
                self.assertEqual(out["reason"], f"http_{status}")
                self.assertEqual(out["requests_made"], 1)
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.read_snap()["jobs_remaining"], STALE_REMAINING)

    def test_exception_matrix(self):
        for exc in self.EXCEPTIONS:
            with self.subTest(exc=type(exc).__name__):
                self.seed()
                calls = []

                def boom(*a, **k):
                    calls.append(1)
                    raise exc

                with patched_config(**self.cfg):
                    out = fja.refresh_quota_snapshot(http_get=boom)
                self.assertFalse(out["refreshed"])
                self.assertEqual(out["reason"], f"error:{type(exc).__name__}")
                self.assertEqual(out["requests_made"], 1, "an attempt still counts as the call")
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.read_snap()["jobs_remaining"], STALE_REMAINING)

    def test_unexpected_response_object(self):
        for bogus in ("a string", 42, None, object(), SimpleNamespace()):
            with self.subTest(kind=type(bogus).__name__):
                self.seed()
                with patched_config(**self.cfg):
                    out = fja.refresh_quota_snapshot(http_get=lambda *a, **k: bogus)
                self.assertFalse(out["refreshed"])
                self.assertEqual(self.read_snap()["jobs_remaining"], STALE_REMAINING)

    def test_missing_credentials_make_no_request(self):
        for field, value in (("FANTASTIC_JOBS_API_KEY", ""), ("FANTASTIC_JOBS_BASE_URL", "")):
            with self.subTest(field=field):
                self.seed()
                calls = []
                with patched_config(**dict(self.cfg, **{field: value})):
                    out = fja.refresh_quota_snapshot(
                        http_get=lambda *a, **k: calls.append(1) or _resp(200, PROVIDER_HEADERS))
                self.assertFalse(out["refreshed"])
                self.assertEqual(out["requests_made"], 0)
                self.assertEqual(calls, [])

    def test_no_credential_is_ever_returned_or_logged(self):
        self.seed()
        with patched_config(**dict(self.cfg, FANTASTIC_JOBS_API_KEY="SUPERSECRET")):
            out = fja.refresh_quota_snapshot(http_get=lambda *a, **k: _resp(500, {}))
        self.assertNotIn("SUPERSECRET", json.dumps(out))


# ==========================================================================
# B.  Stale / damaged snapshot states.
# ==========================================================================
class SnapshotStateTest(_SnapshotCase):
    def test_load_tolerates_damage_without_inventing_quota(self):
        cases = {
            "missing file": None,
            "unreadable/malformed json": "{not json",
            "empty file": "",
            "json list": "[1,2,3]",
            "partial fields": '{"schema":"fantastic-quota-snapshot/1"}',
            "null remaining": '{"jobs_remaining": null}',
            "no captured_at": '{"jobs_remaining": 236}',
        }
        for label, content in cases.items():
            with self.subTest(case=label):
                if content is None:
                    if os.path.exists(self.snap):
                        os.remove(self.snap)
                else:
                    with open(self.snap, "w", encoding="utf-8") as fh:
                        fh.write(content)
                with patched_config(**self.cfg):
                    snap = fja.load_quota_snapshot()
                self.assertIsInstance(snap, dict)
                jr = snap.get("jobs_remaining")
                self.assertIn(jr, (None, 236), "must never fabricate a spendable value")

    def test_stale_transitions(self):
        """stale -> provider, across the four directions that matter."""
        for label, stale, provider, expect_recovered in [
                ("stale low  -> provider high", 236, 100000, True),
                ("stale zero -> provider high", 0, 100000, True),
                ("stale low  -> provider low", 236, 236, False),
                ("stale high -> provider lower", 100000, 236, False)]:
            with self.subTest(case=label):
                self.seed(jobs_remaining=stale)
                hdr = dict(PROVIDER_HEADERS, **{"x-api-jobs-remaining": str(provider)})
                with patched_config(**self.cfg):
                    out = fja.refresh_quota_snapshot(http_get=lambda *a, **k: _resp(200, hdr))
                self.assertTrue(out["refreshed"])
                self.assertEqual(self.read_snap()["jobs_remaining"], provider,
                                 "provider is always the truth we persist")
                budget = G.decide(_inputs(provider)).run_budget
                self.assertEqual(budget > 0, expect_recovered)


# ==========================================================================
# I / J.  Persist failure and atomicity.
# ==========================================================================
class PersistenceTest(_SnapshotCase):
    def test_persist_failure_fails_closed(self):
        """Provider truth in memory is NOT enough to authorize spending.

        Invariant: the run may only acquire on state that will still be true for
        the NEXT run. If the snapshot cannot be persisted, acquiring now would
        spend credits while leaving the deadlock in place for every later run --
        so we keep the original zero decision.
        """
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        with patched_config(**dict(self.cfg,
                                   FANTASTIC_QUOTA_SNAPSHOT_PATH=os.path.join(blocker, "s.json"))):
            out = fja.refresh_quota_snapshot(http_get=lambda *a, **k: _resp(200, PROVIDER_HEADERS))
        self.assertFalse(out["refreshed"])
        self.assertEqual(out["reason"], "snapshot_not_persisted")

        rec = _Recorder(out)
        orch = _Orch(_ctx(9999, "pace"))
        original = _ctx(0, "provider_quota_floor")
        with mock.patch.object(fja, "refresh_quota_snapshot", rec), \
                patched_config(FANTASTIC_JOBS_ENABLED=True):
            gov, info = orch._maybe_refresh_quota(original)
        self.assertIs(gov, original, "no acquisition on unpersisted truth")
        self.assertEqual(orch.rebuild_calls, 0)

    # ---- durability -------------------------------------------------------
    def test_content_is_fsynced_before_publish(self):
        """Order must be: write -> flush -> fsync(file) -> replace -> fsync(dir)."""
        self.seed()
        order = []
        real_fsync, real_replace = os.fsync, os.replace

        def spy_fsync(fd):
            order.append("fsync")
            return real_fsync(fd)

        def spy_replace(src, dst):
            order.append("replace")
            return real_replace(src, dst)

        with patched_config(**self.cfg), \
                mock.patch.object(os, "fsync", spy_fsync), \
                mock.patch.object(os, "replace", spy_replace):
            ok = fja.refresh_quota_snapshot(http_get=lambda *a, **k: _resp(200, PROVIDER_HEADERS))
        self.assertTrue(ok["refreshed"])
        self.assertIn("fsync", order)
        self.assertEqual(order[0], "fsync", "content fsynced BEFORE publish")
        self.assertIn("replace", order)
        self.assertLess(order.index("fsync"), order.index("replace"))
        if os.name == "posix":
            self.assertEqual(order[-1], "fsync", "directory fsynced AFTER replace")

    def test_directory_fsync_is_never_fatal(self):
        """Unsupported platform / refusing filesystem must not fail a published write.

        It runs AFTER the atomic replace, so raising there would wrongly report
        failure for a snapshot that is already on disk.
        """
        self.seed()
        with patched_config(**self.cfg), \
                mock.patch.object(fja, "_fsync_dir", side_effect=RuntimeError("unsupported")):
            out = fja.refresh_quota_snapshot(http_get=lambda *a, **k: _resp(200, PROVIDER_HEADERS))
        self.assertTrue(out["refreshed"], "directory fsync is best-effort only")
        self.assertEqual(self.read_snap()["jobs_remaining"], 100000)

    def test_fsync_dir_swallows_every_platform_failure(self):
        """Direct unit test of the helper's best-effort contract."""
        fja._fsync_dir(os.path.join(self.tmp.name, "does-not-exist"))   # no raise
        fja._fsync_dir(self.tmp.name)                                    # no raise
        with mock.patch.object(os, "open", side_effect=OSError("denied")):
            fja._fsync_dir(self.tmp.name)
        with mock.patch.object(os, "name", "nt"):
            fja._fsync_dir(self.tmp.name)   # skipped entirely on Windows

    def test_failure_at_each_stage_before_publish_preserves_old_snapshot(self):
        stages = {
            "temp create": ("tempfile.mkstemp", OSError("no space")),
            "temp write": ("json.dump", OSError("disk full")),
            "fsync": ("os.fsync", OSError("io error")),
            "replace": ("os.replace", OSError("crash")),
        }
        for label, (target, exc) in stages.items():
            with self.subTest(stage=label):
                self.seed()
                mod, attr = target.rsplit(".", 1)
                obj = {"tempfile": tempfile, "json": json, "os": os}[mod]
                with patched_config(**self.cfg), \
                        mock.patch.object(obj, attr, side_effect=exc):
                    out = fja.refresh_quota_snapshot(
                        http_get=lambda *a, **k: _resp(200, PROVIDER_HEADERS))
                self.assertFalse(out["refreshed"], label)
                self.assertEqual(out["reason"], "snapshot_not_persisted")
                self.assertEqual(self.read_snap()["jobs_remaining"], STALE_REMAINING,
                                 "previous snapshot preserved and still valid")
                self.assertEqual(self._stray_temps(), [], "temp file cleaned up")

    def test_after_successful_publish_loader_reads_complete_snapshot(self):
        self.seed()
        with patched_config(**self.cfg):
            fja.refresh_quota_snapshot(http_get=lambda *a, **k: _resp(200, PROVIDER_HEADERS))
            # A fresh process would do exactly this:
            reloaded = fja.load_quota_snapshot()
        self.assertEqual(reloaded["jobs_remaining"], 100000)
        self.assertEqual(reloaded["schema"], "fantastic-quota-snapshot/1")
        self.assertEqual(self._stray_temps(), [])

    def _stray_temps(self):
        return [f for f in os.listdir(self.tmp.name) if f.endswith(".tmp")]

    # ---- concurrency ------------------------------------------------------
    def test_concurrent_writers_use_unique_temp_paths(self):
        self.seed()
        seen = []
        real_mkstemp = tempfile.mkstemp

        def spy(*a, **k):
            fd, p = real_mkstemp(*a, **k)
            seen.append(p)
            return fd, p

        with patched_config(**self.cfg), mock.patch.object(tempfile, "mkstemp", spy):
            for n in (1000, 2000, 3000):
                hdr = dict(PROVIDER_HEADERS, **{"x-api-jobs-remaining": str(n)})
                fja.refresh_quota_snapshot(http_get=lambda *a, **k: _resp(200, hdr))
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(set(seen)), 3, "temp paths must be unique per writer")
        self.assertTrue(all(os.path.dirname(p) == self.tmp.name for p in seen),
                        "temp must live in the DESTINATION directory")
        self.assertEqual(self._stray_temps(), [])

    def test_quota_writer_uses_no_fixed_temp_name(self):
        """Regression guard for THIS writer.

        (Two other writers in the module -- _save_continuation_state and the
        watermark engine -- still use a fixed `<path>.tmp`. They are pre-existing,
        unrelated to the quota snapshot, and deliberately untouched by this fix.)
        """
        import inspect
        src = inspect.getsource(fja._write_quota_snapshot)
        self.assertNotIn('.tmp"', src.replace('suffix=".tmp"', ""),
                         "no hand-built temp path in the quota writer")
        self.assertIn("mkstemp(dir=", src, "unique temp in the destination directory")

    def test_interleaved_writers_never_produce_a_partial_document(self):
        """A writes its temp, B completes fully, then A publishes.

        The destination must end up wholly A's document -- never a blend -- and
        B's temp must not have been disturbed by A.
        """
        self.seed()
        real_replace = os.replace
        state = {"inner_done": False}

        def replace_with_interleave(src, dst):
            if not state["inner_done"]:
                state["inner_done"] = True
                # B runs to completion INSIDE A's publish step.
                hdr_b = dict(PROVIDER_HEADERS, **{"x-api-jobs-remaining": "555"})
                with mock.patch.object(os, "replace", real_replace):
                    fja.refresh_quota_snapshot(http_get=lambda *a, **k: _resp(200, hdr_b))
                state["after_b"] = json.load(open(dst, encoding="utf-8"))["jobs_remaining"]
                self.assertTrue(os.path.exists(src), "A's temp survived B's write")
            return real_replace(src, dst)

        hdr_a = dict(PROVIDER_HEADERS, **{"x-api-jobs-remaining": "777"})
        with patched_config(**self.cfg), \
                mock.patch.object(os, "replace", replace_with_interleave):
            out = fja.refresh_quota_snapshot(http_get=lambda *a, **k: _resp(200, hdr_a))

        self.assertTrue(out["refreshed"])
        self.assertEqual(state["after_b"], 555, "B published cleanly first")
        final = self.read_snap()
        self.assertEqual(final["jobs_remaining"], 777, "last publish wins, wholly A")
        self.assertEqual(final["schema"], "fantastic-quota-snapshot/1")
        self.assertEqual(self._stray_temps(), [], "no writer leaked a temp file")

    def test_write_is_atomic_via_tmp_and_replace(self):
        self.seed()
        seen = {}
        real_replace = os.replace

        def spy(src, dst):
            # At swap time the destination still holds the OLD content: a reader
            # can never observe a partially written snapshot.
            seen["dst_before"] = json.load(open(dst, encoding="utf-8"))["jobs_remaining"]
            seen["src_is_tmp"] = src.endswith(".tmp")
            return real_replace(src, dst)

        with patched_config(**self.cfg), mock.patch.object(os, "replace", spy):
            fja.refresh_quota_snapshot(http_get=lambda *a, **k: _resp(200, PROVIDER_HEADERS))
        self.assertTrue(seen["src_is_tmp"])
        self.assertEqual(seen["dst_before"], STALE_REMAINING)
        self.assertEqual(self.read_snap()["jobs_remaining"], 100000)

    def test_crash_before_replace_leaves_old_snapshot_intact(self):
        self.seed()
        with patched_config(**self.cfg), \
                mock.patch.object(os, "replace", side_effect=OSError("crash")):
            out = fja.refresh_quota_snapshot(http_get=lambda *a, **k: _resp(200, PROVIDER_HEADERS))
        self.assertFalse(out["refreshed"])
        self.assertEqual(out["reason"], "snapshot_not_persisted")
        self.assertEqual(self.read_snap()["jobs_remaining"], STALE_REMAINING,
                         "old snapshot must remain valid and loadable")


# ==========================================================================
# O.  Backward / forward snapshot compatibility (no format fork).
# ==========================================================================
class ProviderHeadersDisabledTest(_SnapshotCase):
    """The ledger-authoritative mode must be left exactly as it is."""

    def _cfg_ns(self, use_headers, ledger_path):
        return SimpleNamespace(
            FANTASTIC_MONTHLY_GOVERNOR_ENABLED=True, FANTASTIC_MONTHLY_JOBS_LIMIT=20000,
            FANTASTIC_MONTHLY_RESERVE_PCT=0.10, FANTASTIC_DAILY_MIN_JOBS=100,
            FANTASTIC_DAILY_MAX_JOBS=0, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=12000,
            FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=QUOTA_FLOOR,
            FANTASTIC_GOVERNOR_USE_PROVIDER_HEADERS=use_headers,
            FANTASTIC_GOVERNOR_USE_COUNT_HINT=False, FANTASTIC_GOVERNOR_CARRY_CAP_DAYS=3.0,
            FANTASTIC_GOVERNOR_AUTO_ARM=False, FANTASTIC_BILLING_RESET_AT="",
            FANTASTIC_GOVERNOR_LEDGER_PATH=ledger_path)

    def _seed_ledger(self, used=19800):
        with open(self.ledger, "w", encoding="utf-8") as fh:
            json.dump({"schema": G.LEDGER_SCHEMA, "cycle_key": "unknown",
                       "cycle_reset_at": "", "used": used,
                       "runs": [{"run_id": "r", "day": "2026-09-01", "at": "2026-09-01",
                                 "billed": used, "granted": used, "reason": "pace"}],
                       "carry_forward": 0, "last_allowance_day": "2026-09-03",
                       "armed": True}, fh)

    def test_snapshot_metadata_is_ignored_when_headers_off(self):
        """Proves the probe could not possibly change the decision."""
        results = []
        for jr in (STALE_REMAINING, 100000):
            self._seed_ledger()
            ctx = G.build_context(self._cfg_ns(False, self.ledger), run_id="x",
                                  provider_jobs_remaining=jr, provider_reset_at=RESET, now=NOW)
            with open(self.ledger, encoding="utf-8") as fh:
                led = json.load(fh)
            results.append((ctx.decision.run_budget, ctx.decision.reason,
                            ctx.decision.remaining_credits, ctx.decision.provider_authoritative,
                            led["cycle_key"], led["used"], ctx.cycle_rolled))
        self.assertEqual(results[0], results[1],
                         "identical decision for a stale vs healthy snapshot")
        self.assertFalse(results[0][3], "ledger is authoritative, not the provider")
        self.assertEqual(results[0][4], "unknown", "billing date must NOT roll the cycle")
        self.assertEqual(results[0][5], 19800, "local spend counter untouched")
        self.assertFalse(results[0][6], "no cycle rollover")

    def test_no_billing_date_can_authorize_spending_when_headers_off(self):
        for nbd in (None, RESET, datetime(2099, 1, 1, tzinfo=timezone.utc)):
            with self.subTest(next_billing_date=str(nbd)):
                self._seed_ledger()
                ctx = G.build_context(self._cfg_ns(False, self.ledger), run_id="x",
                                      provider_jobs_remaining=100000,
                                      provider_reset_at=nbd, now=NOW)
                with open(self.ledger, encoding="utf-8") as fh:
                    led = json.load(fh)
                self.assertEqual(ctx.decision.run_budget, 0)
                self.assertEqual(led["used"], 19800, "ledger never reset")
                self.assertEqual(led["cycle_key"], "unknown")

    def test_headers_on_still_self_heals(self):
        self._seed_ledger()
        stale = G.build_context(self._cfg_ns(True, self.ledger), run_id="x",
                                provider_jobs_remaining=STALE_REMAINING,
                                provider_reset_at=None, now=NOW).decision
        self.assertEqual(stale.run_budget, 0)
        self._seed_ledger()
        healed = G.build_context(self._cfg_ns(True, self.ledger), run_id="x",
                                 provider_jobs_remaining=100000,
                                 provider_reset_at=RESET, now=NOW).decision
        self.assertGreater(healed.run_budget, 0)
        self.assertTrue(healed.provider_authoritative)


class SnapshotCompatibilityTest(_SnapshotCase):
    def test_legacy_snapshot_still_readable(self):
        """A snapshot written by the PREVIOUS code (no *_limit keys)."""
        legacy = {"schema": "fantastic-quota-snapshot/1", "jobs_remaining": 7000,
                  "requests_remaining": 9000, "next_billing_date": "",
                  "captured_at": "2026-08-22T20:20:36.459714+00:00"}
        with open(self.snap, "w", encoding="utf-8") as fh:
            json.dump(legacy, fh)
        with patched_config(**self.cfg):
            self.assertEqual(fja.load_quota_snapshot()["jobs_remaining"], 7000)
        self.assertGreater(G.decide(_inputs(7000)).run_budget, 0)

    def test_acquire_writer_keeps_historical_key_set(self):
        with patched_config(**self.cfg):
            fja._save_quota_snapshot(
                SimpleNamespace(jobs_remaining=5000, requests_remaining=9000),
                {"next_billing_date": BILLING_DATE})
        self.assertEqual(sorted(self.read_snap()),
                         ["captured_at", "jobs_remaining", "next_billing_date",
                          "requests_remaining", "schema"])

    def test_refreshed_snapshot_consumed_by_acquire_writer_and_governor(self):
        """New-format snapshot must round-trip through every existing consumer."""
        self.seed()
        with patched_config(**self.cfg):
            fja.refresh_quota_snapshot(http_get=lambda *a, **k: _resp(200, PROVIDER_HEADERS))
            new = fja.load_quota_snapshot()
            self.assertEqual(new["schema"], "fantastic-quota-snapshot/1")
            self.assertEqual(new["jobs_limit"], 100000)
            # acquire()'s writer overwrites it cleanly (back to the historical keys)
            fja._save_quota_snapshot(
                SimpleNamespace(jobs_remaining=4000, requests_remaining=8000),
                {"next_billing_date": BILLING_DATE})
            after = fja.load_quota_snapshot()
        self.assertEqual(after["jobs_remaining"], 4000)
        self.assertNotIn("jobs_limit", after)
        self.assertIsNotNone(G._parse_iso(new["next_billing_date"]))


# ==========================================================================
# E / F / G / M.  Pipeline gate: when we probe, and exactly how often.
# ==========================================================================
class PipelineGateTest(unittest.TestCase):
    def _run(self, gov, rebuilt, recorder, fantastic_enabled=True):
        orch = _Orch(rebuilt)
        with mock.patch.object(fja, "refresh_quota_snapshot", recorder), \
                patched_config(FANTASTIC_JOBS_ENABLED=fantastic_enabled):
            gov2, info = orch._maybe_refresh_quota(gov)
        return orch, gov2, info

    def test_a_stale_snapshot_self_heals(self):
        rec = _Recorder({"refreshed": True, "reason": "ok", "requests_made": 1,
                         "http_status": 200, "jobs_remaining": 100000,
                         "next_billing_date": BILLING_DATE})
        healthy = _ctx(3445, "pace", remaining=100000)
        orch, gov, info = self._run(_ctx(0, "provider_quota_floor"), healthy, rec)
        self.assertEqual((rec.calls, orch.rebuild_calls), (1, 1))
        self.assertEqual((info["budget_before"], info["budget_after"]), (0, 3445))
        self.assertIs(gov, healthy)

    def test_b_provider_actually_exhausted(self):
        rec = _Recorder({"refreshed": True, "reason": "ok", "requests_made": 1,
                         "jobs_remaining": STALE_REMAINING})
        orch, gov, info = self._run(_ctx(0, "provider_quota_floor"),
                                    _ctx(0, "provider_quota_floor"), rec)
        self.assertEqual((rec.calls, orch.rebuild_calls), (1, 1))
        self.assertEqual(gov.run_budget, 0)

    def test_c_refresh_raises_keeps_original(self):
        rec = _Recorder(raises=RuntimeError("provider down"))
        original = _ctx(0, "provider_quota_floor")
        orch, gov, info = self._run(original, _ctx(9999, "pace"), rec)
        self.assertEqual((rec.calls, orch.rebuild_calls), (1, 0))
        self.assertIs(gov, original)
        self.assertEqual(info["reason"], "error:RuntimeError")

    def test_d_failed_refresh_keeps_original(self):
        for reason in ("missing_quota_metadata", "http_429", "snapshot_not_persisted",
                       "no_api_key", "error:TimeoutError"):
            with self.subTest(reason=reason):
                rec = _Recorder({"refreshed": False, "reason": reason, "requests_made": 1})
                original = _ctx(0, "provider_quota_floor")
                orch, gov, info = self._run(original, _ctx(9999, "pace"), rec)
                self.assertEqual((rec.calls, orch.rebuild_calls), (1, 0))
                self.assertIs(gov, original)
                self.assertEqual(gov.run_budget, 0)

    def test_f_governor_other_constraints_remain_authoritative(self):
        """Quota refresh repairs METADATA; it may not bypass any other limit.

        The rebuild is a FULL re-decision, so whatever the governor says after the
        refresh stands -- including a zero for a completely different reason.
        """
        for reason in ("daily_allowance_spent", "inventory_hint", "per_run_ceiling"):
            with self.subTest(blocked_by=reason):
                rec = _Recorder({"refreshed": True, "reason": "ok", "requests_made": 1,
                                 "jobs_remaining": 100000})
                still_zero = _ctx(0, reason, remaining=100000)
                orch, gov, info = self._run(_ctx(0, "provider_quota_floor"), still_zero, rec)
                self.assertEqual(rec.calls, 1)
                self.assertEqual(gov.run_budget, 0, "other governor limits still bind")
                self.assertEqual(info["budget_after"], 0)

    def test_rebuild_failure_is_named_and_fails_closed(self):
        """Snapshot repaired but the rebuild blew up: keep the original zero."""
        rec = _Recorder({"refreshed": True, "reason": "ok", "requests_made": 1,
                         "jobs_remaining": 100000})

        class _Boom(_Orch):
            def _build_governor(self):
                self.rebuild_calls += 1
                raise RuntimeError("ledger unreadable")

        orch = _Boom(None)
        original = _ctx(0, "provider_quota_floor")
        with mock.patch.object(fja, "refresh_quota_snapshot", rec), \
                patched_config(FANTASTIC_JOBS_ENABLED=True):
            gov, info = orch._maybe_refresh_quota(original)
        self.assertEqual(rec.calls, 1, "still exactly one request")
        self.assertIs(gov, original, "original zero-budget decision retained")
        self.assertEqual(info["reason"], "rebuild_failed:RuntimeError")
        self.assertEqual(info["budget_after"], 0)

    def test_provider_headers_disabled_never_probes(self):
        rec = _Recorder({"refreshed": True, "reason": "ok", "requests_made": 1,
                         "jobs_remaining": 100000})
        original = _ctx(0, "provider_quota_floor")
        orch = _Orch(_ctx(9999, "pace"))
        with mock.patch.object(fja, "refresh_quota_snapshot", rec), \
                patched_config(FANTASTIC_JOBS_ENABLED=True,
                               FANTASTIC_GOVERNOR_USE_PROVIDER_HEADERS=False):
            gov, info = orch._maybe_refresh_quota(original)
        self.assertEqual(rec.calls, 0, "no request when the result cannot be acted on")
        self.assertEqual(orch.rebuild_calls, 0)
        self.assertIs(gov, original)
        self.assertEqual(gov.run_budget, 0)
        self.assertEqual(info["reason"], "provider_headers_disabled")
        self.assertFalse(info["attempted"])

    def test_g_positive_budget_never_probes(self):
        rec = _Recorder({"refreshed": True, "reason": "ok", "requests_made": 1})
        original = _ctx(3445, "pace", remaining=100000)
        orch, gov, info = self._run(original, _ctx(1, "pace"), rec)
        self.assertEqual((rec.calls, orch.rebuild_calls), (0, 0))
        self.assertIs(gov, original)
        self.assertEqual(info["reason"], "budget_positive")

    def test_governor_disabled_never_probes(self):
        rec = _Recorder({"refreshed": True, "reason": "ok"})
        off = G.GovernorContext(enabled=False, decision=None, ledger=None)
        orch, gov, info = self._run(off, _ctx(1, "pace"), rec)
        self.assertEqual(rec.calls, 0)
        self.assertIs(gov, off)

    def test_unarmed_governor_never_probes(self):
        rec = _Recorder({"refreshed": True, "reason": "ok"})
        unarmed = G.GovernorContext(enabled=True, decision=_decision(0, "provider_quota_floor"),
                                    ledger=None, armed=False)
        orch, gov, info = self._run(unarmed, _ctx(1, "pace"), rec)
        self.assertEqual(rec.calls, 0)
        self.assertEqual(info["reason"], "governor_not_governing")

    def test_fantastic_disabled_never_probes(self):
        rec = _Recorder({"refreshed": True, "reason": "ok"})
        original = _ctx(0, "provider_quota_floor")
        orch, gov, info = self._run(original, _ctx(1, "pace"), rec, fantastic_enabled=False)
        self.assertEqual(rec.calls, 0)
        self.assertEqual(info["reason"], "fantastic_disabled")

    def test_reason_set_is_exactly_as_documented(self):
        self.assertEqual(G.QUOTA_METADATA_ZERO_REASONS,
                         frozenset({"provider_quota_floor", "reserve",
                                    "monthly_remaining", "exhausted"}))

    @staticmethod
    def _ast_of(func):
        import ast
        import inspect
        import textwrap
        return ast.parse(textwrap.dedent(inspect.getsource(func))).body[0]

    def _assert_loop_free(self, func):
        """No loop CONSTRUCT of any kind can enclose the provider call."""
        import ast
        tree = self._ast_of(func)
        loops = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.For, ast.AsyncFor, ast.While, ast.ListComp,
                                   ast.SetComp, ast.DictComp, ast.GeneratorExp))]
        self.assertEqual(loops, [], f"{func.__name__} must contain no loop construct")

    def _call_names(self, func):
        import ast
        names = []
        for n in ast.walk(self._ast_of(func)):
            if isinstance(n, ast.Call):
                f = n.func
                names.append(f.attr if isinstance(f, ast.Attribute) else
                             getattr(f, "id", ""))
        return names

    def test_m_one_request_is_structurally_enforced(self):
        import inspect

        import orchestrator.pipeline as P

        # Exactly ONE call site in the whole pipeline module.
        self.assertEqual(inspect.getsource(P).count("self._maybe_refresh_quota("), 1)

        # The recovery method: one refresh, one rebuild, no loop, no recursion,
        # and no path back into acquisition.
        names = self._call_names(P.Orchestrator._maybe_refresh_quota)
        self.assertEqual(names.count("refresh_quota_snapshot"), 1)
        self.assertEqual(names.count("_build_governor"), 1)
        self.assertNotIn("_acquire", names)
        self.assertNotIn("_maybe_refresh_quota", names, "no recursion")
        self._assert_loop_free(P.Orchestrator._maybe_refresh_quota)

        # The adapter: exactly one provider invocation, no loop, no recursion.
        adapter_names = self._call_names(fja.refresh_quota_snapshot)
        self.assertEqual(adapter_names.count("getter"), 1)
        self.assertNotIn("refresh_quota_snapshot", adapter_names, "no recursion")
        self.assertEqual(adapter_names.count("_write_quota_snapshot"), 1)
        self._assert_loop_free(fja.refresh_quota_snapshot)

    def test_m_only_one_count_request_path_exists(self):
        """Guards against a second, ungoverned quota-probe path appearing."""
        import inspect
        src = inspect.getsource(fja)
        # The endpoint path is defined ONCE; no other code may hand-roll the URL.
        self.assertEqual(src.count('_COUNT_ENDPOINT = "/v1/active-jb-count"'), 1)
        self.assertEqual(src.count('"/v1/active-jb-count"'), 1,
                         "the count path must not be duplicated as a literal")
        # Exactly two places build a count URL: the watermark visibility audit and
        # this refresh. A third would be an ungoverned quota-probe path.
        import re
        urls = re.findall(r'f"[^"]*\{_COUNT_ENDPOINT\}', src)
        self.assertEqual(len(urls), 2, f"unexpected count-URL construction sites: {urls}")


# ==========================================================================
# 5 / K / L / M / N.  End-to-end through the REAL orchestration path.
# ==========================================================================
class EndToEndRecoveryTest(unittest.TestCase):
    """Drives the real Orchestrator.run() -> _run_body_topup with fakes only.

    Counts ACTUAL provider requests by patching the adapter's module-level
    ``_http_get``, so the one-request guarantee is proven behaviourally rather
    than by source inspection.
    """

    def _run(self, *, snapshot_remaining, provider=None, provider_exc=None,
             provider_status=200, supply=300, monthly_limit=20000, seed_ledger=None,
             snapshot_nbd="", fail_lane=False, use_headers=True):
        from retrieval_measurement.instrument import RequestBudget
        from orchestrator.enrichment import EnrichmentReport
        from orchestrator.adapters_real import RealDeliveryReport
        from orchestrator.lanes import LaneResult
        from orchestrator.modes import ExecutionMode, policy_for
        from orchestrator.runcontrol import RunContext
        from orchestrator.state import StateManager
        from orchestrator.pipeline import OrchestratorPlan

        tmp = tempfile.mkdtemp()
        snap_p = os.path.join(tmp, "snap.json")
        ledger_p = os.path.join(tmp, "ledger.json")
        with open(snap_p, "w", encoding="utf-8") as fh:
            json.dump({"schema": "fantastic-quota-snapshot/1",
                       "jobs_remaining": snapshot_remaining, "requests_remaining": 9000,
                       "next_billing_date": snapshot_nbd,
                       "captured_at": "2026-09-02T13:04:00+00:00"}, fh)
        if seed_ledger is not None:
            with open(ledger_p, "w", encoding="utf-8") as fh:
                json.dump(seed_ledger, fh)

        http_calls, acquire_calls = [], []

        def fake_http(url, headers=None, params=None, timeout=None):
            http_calls.append(url)
            if provider_exc is not None:
                raise provider_exc
            hdr = dict(PROVIDER_HEADERS, **{"x-api-jobs-remaining": str(provider)}) \
                if provider is not None else dict(PROVIDER_HEADERS)
            return _resp(provider_status, hdr)

        def runner(manager):
            acquire_calls.append(1)
            if fail_lane:
                return LaneResult(lane="fantastic", status="failed", jobs=[],
                                  physical_requests=1, attribution={},
                                  error="provider_parse_error")
            n = min(supply, fja._effective_run_cap())
            jobs = [{"job_id": f"J{i}", "employer_name": "Co", "job_title": "Engineer",
                     "_fantastic_internal_id": str(i)} for i in range(n)]
            billed = int(n * 1.1)
            return LaneResult(lane="fantastic", status="complete", jobs=jobs,
                              physical_requests=1,
                              attribution={"source": "fantastic_jobs", "records": n,
                                           "raw_records": billed, "jobs_quota_consumed": billed,
                                           "jobs_quota_remaining": 100000 - billed,
                                           "stop_reason": "", "per_source": {}})

        class _Enr:
            def run(self, opps, **k):
                return EnrichmentReport(leads=[], stages=[])

        class _Del:
            def deliver(self, leads, **k):
                return RealDeliveryReport(mode="review_staging")

        mode = ExecutionMode.FULL_DRY_RUN
        ctx = RunContext.create(mode, {"t": 1}, run_id="QREC")
        st = StateManager(tmp, policy_for(mode), run_id="QREC")
        plan = OrchestratorPlan(lanes=["fantastic"], lane_runners={"fantastic": runner},
                                enrichment_engine=_Enr(), delivery_manager=_Del(), target=5)
        with mock.patch.multiple(
                config, NET_NEW_SEND_SAFE_TARGET=1000, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=6000,
                FANTASTIC_TOPUP_SLICE_JOBS=500, FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=QUOTA_FLOOR,
                TOPUP_MAX_ITERATIONS=40, PRE_APOLLO_EXISTING_DEDUPE=False,
                FANTASTIC_MONTHLY_GOVERNOR_ENABLED=True, FANTASTIC_MONTHLY_JOBS_LIMIT=monthly_limit,
                FANTASTIC_MONTHLY_RESERVE_PCT=0.10, FANTASTIC_DAILY_MIN_JOBS=100,
                FANTASTIC_DAILY_MAX_JOBS=0, FANTASTIC_GOVERNOR_USE_PROVIDER_HEADERS=use_headers,
                FANTASTIC_GOVERNOR_USE_COUNT_HINT=False, FANTASTIC_GOVERNOR_CARRY_CAP_DAYS=3.0,
                FANTASTIC_GOVERNOR_AUTO_ARM=False, FANTASTIC_BILLING_RESET_AT="",
                FANTASTIC_GOVERNOR_LEDGER_PATH=ledger_p, FANTASTIC_QUOTA_SNAPSHOT_PATH=snap_p,
                YIELD_LEDGER_ENABLED=False, FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=False,
                FANTASTIC_JOBS_ENABLED=True, FANTASTIC_JOBS_API_KEY="test-key",
                FANTASTIC_JOBS_BASE_URL="https://data.fantastic.jobs"), \
                mock.patch.object(fja, "_http_get", fake_http), \
                mock.patch.object(Orchestrator, "_count_net_new_send_safe",
                                  staticmethod(lambda l, d: 0)):
            res = Orchestrator(ctx, st, RequestBudget(limit=10_000)).run(plan)
        with open(snap_p, encoding="utf-8") as fh:
            final_snap = json.load(fh)
        return res, http_calls, acquire_calls, final_snap

    # SCENARIO 1: stale 236, provider 100000 -> heal, persist, acquire.
    def test_scenario_1_recovers_and_acquires(self):
        res, http, acq, snap = self._run(snapshot_remaining=236, provider=100000)
        self.assertEqual(len(http), 1, "exactly ONE provider request")
        self.assertEqual(snap["jobs_remaining"], 100000, "persistent state healed")
        self.assertGreaterEqual(len(acq), 1, "acquisition ran")
        qr = res["governor"]["quota_refresh"]
        self.assertTrue(qr["attempted"] and qr["refreshed"])
        self.assertEqual(qr["budget_before"], 0)
        self.assertGreater(qr["budget_after"], 0)
        self.assertNotEqual(res["topup"]["final_stop_reason"], "governor_zero_budget")

    # SCENARIO 2 (K): the NEXT run needs no recovery at all.
    def test_scenario_2_next_run_needs_no_recovery(self):
        res, http, acq, snap = self._run(snapshot_remaining=100000, snapshot_nbd=BILLING_DATE)
        self.assertEqual(len(http), 0, "healthy snapshot must cost ZERO extra requests")
        self.assertGreaterEqual(len(acq), 1)
        qr = res["governor"]["quota_refresh"]
        self.assertFalse(qr["attempted"])
        self.assertEqual(qr["reason"], "budget_positive")

    # SCENARIO 3: provider genuinely exhausted.
    def test_scenario_3_provider_exhausted_blocks(self):
        res, http, acq, snap = self._run(snapshot_remaining=236, provider=236)
        self.assertEqual(len(http), 1)
        self.assertEqual(acq, [], "no acquisition")
        self.assertEqual(res["topup"]["final_stop_reason"], "governor_zero_budget")
        self.assertEqual(snap["jobs_remaining"], 236)

    # SCENARIO 4: provider request fails.
    def test_scenario_4_provider_failure_blocks(self):
        res, http, acq, snap = self._run(snapshot_remaining=236,
                                         provider_exc=TimeoutError("timeout"))
        self.assertEqual(len(http), 1, "one attempt, no retry")
        self.assertEqual(acq, [])
        self.assertEqual(res["topup"]["final_stop_reason"], "governor_zero_budget")
        self.assertEqual(snap["jobs_remaining"], 236, "stale snapshot preserved")
        self.assertFalse(res["governor"]["quota_refresh"]["refreshed"])

    def test_scenario_4b_http_error_blocks(self):
        res, http, acq, snap = self._run(snapshot_remaining=236, provider=100000,
                                         provider_status=429)
        self.assertEqual(len(http), 1)
        self.assertEqual(acq, [])
        self.assertEqual(res["governor"]["quota_refresh"]["reason"], "http_429")

    # SCENARIO 5 (F): quota healthy, but another governor rule still says zero.
    def test_scenario_5_other_governor_limit_still_blocks(self):
        today = datetime.now(timezone.utc).date().isoformat()
        seed = {"schema": G.LEDGER_SCHEMA, "cycle_key": "2026-10-01",
                "cycle_reset_at": BILLING_DATE, "used": 0, "carry_forward": 0,
                "last_allowance_day": today,
                "runs": [{"run_id": "earlier", "day": today, "at": today,
                          "billed": 10 ** 9, "granted": 10 ** 9, "reason": "pace"}],
                "armed": True}
        res, http, acq, snap = self._run(snapshot_remaining=236, provider=100000,
                                         seed_ledger=seed)
        self.assertEqual(len(http), 1, "one refresh, because the initial zero DID qualify")
        self.assertEqual(snap["jobs_remaining"], 100000, "metadata was repaired")
        self.assertEqual(acq, [], "but the daily allowance still blocks acquisition")
        self.assertEqual(res["topup"]["final_stop_reason"], "governor_zero_budget")
        self.assertEqual(res["governor"]["decision"]["reason"], "daily_allowance_spent")

    # N: acquisition failing afterwards must not trigger a second probe, and must
    # keep its own error semantics.
    def test_n_acquire_failure_after_refresh_makes_no_second_probe(self):
        res, http, acq, snap = self._run(snapshot_remaining=236, provider=100000,
                                         fail_lane=True)
        self.assertEqual(len(http), 1, "still exactly one recovery request")
        self.assertEqual(len(acq), 1, "acquisition was attempted once")
        self.assertTrue(res["governor"]["quota_refresh"]["refreshed"])
        self.assertEqual(snap["jobs_remaining"], 100000, "the heal still persisted")
        # A failed lane keeps its OWN semantics -- never reported as a zero-budget
        # stop, and never silently 'complete'.
        self.assertNotEqual(res["topup"]["final_stop_reason"], "governor_zero_budget")
        self.assertEqual(res["run"]["status"], "failed")

    # L: crash after persist -> the next run recovers with no cleanup.
    def test_l_persisted_state_survives_for_the_next_run(self):
        _, http1, _, snap1 = self._run(snapshot_remaining=236, provider=100000)
        self.assertEqual(len(http1), 1)
        self.assertEqual(snap1["jobs_remaining"], 100000)
        # Simulate the process dying right after persistence: a brand-new run that
        # only inherits the snapshot must behave like a normal healthy run.
        res2, http2, acq2, _ = self._run(snapshot_remaining=snap1["jobs_remaining"],
                                         snapshot_nbd=snap1["next_billing_date"])
        self.assertEqual(len(http2), 0)
        self.assertGreaterEqual(len(acq2), 1)

    def test_provider_headers_off_zero_state_makes_zero_requests(self):
        """Ledger-authoritative mode is exhausted: no probe, and no ledger change."""
        exhausted = {"schema": G.LEDGER_SCHEMA, "cycle_key": "unknown",
                     "cycle_reset_at": "", "used": 19900, "carry_forward": 0,
                     "last_allowance_day": datetime.now(timezone.utc).date().isoformat(),
                     "runs": [{"run_id": "old", "day": "2026-09-01", "at": "2026-09-01",
                               "billed": 19900, "granted": 19900, "reason": "pace"}],
                     "armed": True}
        res, http, acq, snap = self._run(snapshot_remaining=236, provider=100000,
                                         use_headers=False, seed_ledger=exhausted)
        self.assertEqual(http, [], "ledger-authoritative mode must not probe")
        self.assertEqual(acq, [], "and must remain blocked")
        self.assertEqual(snap["jobs_remaining"], 236, "snapshot untouched")
        self.assertEqual(res["governor"]["quota_refresh"]["reason"],
                         "provider_headers_disabled")
        self.assertFalse(res["governor"]["quota_refresh"]["attempted"])
        self.assertEqual(res["topup"]["final_stop_reason"], "governor_zero_budget")
        self.assertFalse(res["governor"]["decision"]["provider_authoritative"])

    def test_provider_headers_off_healthy_ledger_is_unchanged(self):
        """The alternate mode's normal behaviour must be untouched by this patch."""
        res, http, acq, snap = self._run(snapshot_remaining=236, provider=100000,
                                         use_headers=False)
        self.assertEqual(http, [], "still no probe")
        self.assertGreaterEqual(len(acq), 1, "ledger-authoritative budget still granted")
        self.assertEqual(res["governor"]["quota_refresh"]["reason"], "budget_positive")
        self.assertFalse(res["governor"]["decision"]["provider_authoritative"])

    def test_recovery_works_under_both_plan_sizes(self):
        for limit in (20000, 100000):
            with self.subTest(monthly_limit=limit):
                res, http, acq, snap = self._run(snapshot_remaining=236, provider=100000,
                                                 monthly_limit=limit)
                self.assertEqual(len(http), 1)
                self.assertGreaterEqual(len(acq), 1)
                self.assertGreater(res["governor"]["quota_refresh"]["budget_after"], 0)


if __name__ == "__main__":
    unittest.main()
