"""Phase 1 of FINAL_30_PLUS_SYSTEM_SPEC.md: the age-recovery pruning defect and
the tiered freshness policy that replaces the single hard MAX_JOB_AGE_DAYS cutoff.

Core regression: a FINAL_PASS lead admitted through the 15-30 day recovery
window has job_age_days >= 15 by construction. recovery_inventory.py's prune
step previously checked that against the 14-day PRIMARY ceiling, so it was
always true -- the lead was pruned out of FinalPassInventory before it could
ever be reserved or delivered, in the same run that found it. See
SUPPLY_ARCHITECTURE_AND_SLA_PLAN.md section 3.1 and
FINAL_30_PLUS_SYSTEM_SPEC.md section 7.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import freshness_policy
import job_filter
import job_signal
from recovery_inventory import FinalPassInventory


def _lead(*, domain, bucket, lead_key, job_age_days, tier, state="FINAL_PASS", stored_days_ago=0):
    validation_time = datetime.now(timezone.utc) - timedelta(days=stored_days_ago)
    return {
        "_final_state": state,
        "_freshness_tier": tier,
        "_validation_timestamp": validation_time.isoformat(),
        "job_age_days": job_age_days,
        "employer_website": f"https://{domain}",
        "canonical_domain": domain,
        "bucket": bucket,
        "lead_key": lead_key,
        "hiring_manager_email": f"{lead_key}@{domain}",
        "hiring_manager_name": "Test Person",
    }


class TierClassificationTests(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(freshness_policy.classify_age_tier(0), freshness_policy.TIER_PRIMARY)
        self.assertEqual(freshness_policy.classify_age_tier(14), freshness_policy.TIER_PRIMARY)
        self.assertEqual(freshness_policy.classify_age_tier(15), freshness_policy.TIER_RECOVERY)
        self.assertEqual(freshness_policy.classify_age_tier(30), freshness_policy.TIER_RECOVERY)
        self.assertEqual(freshness_policy.classify_age_tier(31), freshness_policy.TIER_EXTENDED)
        self.assertEqual(freshness_policy.classify_age_tier(60), freshness_policy.TIER_EXTENDED)
        self.assertEqual(freshness_policy.classify_age_tier(61), freshness_policy.TIER_DEEP)
        self.assertEqual(freshness_policy.classify_age_tier(90), freshness_policy.TIER_DEEP)
        self.assertEqual(freshness_policy.classify_age_tier(91), freshness_policy.TIER_BEYOND)

    def test_primary_and_recovery_never_require_evidence(self):
        eligible, reason = freshness_policy.is_age_tier_eligible(freshness_policy.TIER_PRIMARY, {})
        self.assertTrue(eligible)
        self.assertEqual(reason, "")
        eligible, reason = freshness_policy.is_age_tier_eligible(freshness_policy.TIER_RECOVERY, {})
        self.assertTrue(eligible)

    def test_extended_tier_requires_active_evidence(self):
        eligible, reason = freshness_policy.is_age_tier_eligible(freshness_policy.TIER_EXTENDED, {})
        self.assertFalse(eligible)
        self.assertEqual(reason, "tier_31_60_requires_active_evidence")
        eligible, _ = freshness_policy.is_age_tier_eligible(
            freshness_policy.TIER_EXTENDED, {"_ats_board_identity_verified": True}
        )
        self.assertTrue(eligible)

    def test_deep_tier_requires_active_and_difficult_to_fill(self):
        job = {"_ats_board_identity_verified": True}
        eligible, reason = freshness_policy.is_age_tier_eligible(freshness_policy.TIER_DEEP, job)
        self.assertFalse(eligible)
        self.assertEqual(reason, "tier_61_90_requires_difficult_to_fill_signal")

        job["job_title"] = "Senior Revenue Systems Engineer"
        eligible, _ = freshness_policy.is_age_tier_eligible(freshness_policy.TIER_DEEP, job)
        self.assertTrue(eligible)

    def test_deep_tier_rejects_active_only_without_seniority(self):
        job = {"_ats_board_identity_verified": True, "job_title": "Revenue Systems Engineer"}
        eligible, reason = freshness_policy.is_age_tier_eligible(freshness_policy.TIER_DEEP, job)
        self.assertFalse(eligible)
        self.assertEqual(reason, "tier_61_90_requires_difficult_to_fill_signal")

    def test_beyond_90_requires_recent_refresh_not_just_active(self):
        job = {"_ats_board_identity_verified": True, "job_title": "Senior Engineer"}
        eligible, reason = freshness_policy.is_age_tier_eligible(freshness_policy.TIER_BEYOND, job)
        self.assertFalse(eligible)
        self.assertEqual(reason, "tier_90_plus_excluded_by_default")

        job["_ats_source_updated_at"] = datetime.now(timezone.utc).isoformat()
        eligible, _ = freshness_policy.is_age_tier_eligible(freshness_policy.TIER_BEYOND, job)
        self.assertTrue(eligible)


class IsStaleJobTierIntegrationTests(unittest.TestCase):
    def _job(self, age_days, **extra):
        posted = datetime.now(timezone.utc) - timedelta(days=age_days)
        job = {
            "job_posted_at_datetime_utc": posted.isoformat(),
            "job_title": "Revenue Systems Engineer",
        }
        job.update(extra)
        return job

    def test_recovery_window_job_uses_recovery_policy_no_evidence_needed(self):
        job = self._job(20)
        stale, reason = job_filter.is_stale_job(job, max_age_days=30, min_age_days=15)
        self.assertFalse(stale, reason)
        self.assertEqual(job["_freshness_tier"], freshness_policy.TIER_RECOVERY)

    def test_base_lane_job_uses_base_policy(self):
        job = self._job(10)
        stale, reason = job_filter.is_stale_job(job, max_age_days=14, min_age_days=None)
        self.assertFalse(stale, reason)
        self.assertEqual(job["_freshness_tier"], freshness_policy.TIER_PRIMARY)

    def test_base_lane_job_beyond_14_days_is_rejected_by_outer_bound(self):
        job = self._job(20)
        stale, reason = job_filter.is_stale_job(job, max_age_days=14, min_age_days=None)
        self.assertTrue(stale)
        self.assertIn("stale_job", reason)

    def test_extended_window_without_active_evidence_is_rejected(self):
        job = self._job(45)
        stale, reason = job_filter.is_stale_job(job, max_age_days=90, min_age_days=31)
        self.assertTrue(stale)
        self.assertEqual(reason, "tier_31_60_requires_active_evidence")

    def test_extended_window_with_active_evidence_is_accepted(self):
        job = self._job(45, _ats_board_identity_verified=True)
        stale, reason = job_filter.is_stale_job(job, max_age_days=90, min_age_days=31)
        self.assertFalse(stale, reason)
        self.assertEqual(job["_freshness_tier"], freshness_policy.TIER_EXTENDED)

    def test_deep_window_requires_difficult_to_fill_on_top_of_active(self):
        job = self._job(75, _ats_board_identity_verified=True, job_title="Account Manager")
        stale, reason = job_filter.is_stale_job(job, max_age_days=90, min_age_days=31)
        self.assertTrue(stale)
        self.assertEqual(reason, "tier_61_90_requires_difficult_to_fill_signal")

    def test_freshness_and_active_status_are_separate_concepts(self):
        """A fresh (0-14 day) job needs no active-status evidence at all --
        the requirement only attaches once age itself stops being a reliable
        freshness proxy (31+ days)."""
        job = self._job(5)
        self.assertNotIn("_ats_board_identity_verified", job)
        stale, reason = job_filter.is_stale_job(job, max_age_days=14, min_age_days=None)
        self.assertFalse(stale, reason)


class FinalPassInventoryAgeRecoveryPruningTests(unittest.TestCase):
    def _staged_inventory(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return FinalPassInventory(f"{temp.name}/final_pass_inventory.json")

    def test_recovery_window_lead_survives_same_run_pruning(self):
        """The exact reported defect: a 15-30 day recovery-pass lead must not
        be pruned out of the inventory it must pass through before delivery,
        in the same run that found it."""
        inv = self._staged_inventory()
        lead = _lead(
            domain="recovered.com",
            bucket="finance",
            lead_key="rec-1",
            job_age_days=20,
            tier=freshness_policy.TIER_RECOVERY,
        )
        inv.stage([lead])
        available = inv.available()
        self.assertEqual([item["lead_key"] for item in available], ["rec-1"])

    def test_extended_window_lead_survives_same_run_pruning(self):
        inv = self._staged_inventory()
        lead = _lead(
            domain="extended.com",
            bucket="finance",
            lead_key="ext-1",
            job_age_days=45,
            tier=freshness_policy.TIER_EXTENDED,
        )
        inv.stage([lead])
        available = inv.available()
        self.assertEqual([item["lead_key"] for item in available], ["ext-1"])

    def test_base_lane_lead_still_expires_past_its_own_ceiling(self):
        """Base-lane (0-14 day) leads must still use the base policy -- aging
        past day 14 while sitting in inventory expires them, same as before
        this fix."""
        inv = self._staged_inventory()
        lead = _lead(
            domain="base.com",
            bucket="finance",
            lead_key="base-1",
            job_age_days=10,
            tier=freshness_policy.TIER_PRIMARY,
            stored_days_ago=6,  # 10 + 6 = 16 >= 14-day primary ceiling
        )
        inv.stage([lead])
        available = inv.available()
        self.assertEqual(available, [])

    def test_recovery_lead_eventually_expires_past_its_own_ceiling(self):
        inv = self._staged_inventory()
        lead = _lead(
            domain="recovered2.com",
            bucket="finance",
            lead_key="rec-2",
            job_age_days=28,
            tier=freshness_policy.TIER_RECOVERY,
            stored_days_ago=5,  # 28 + 5 = 33 >= 30-day recovery ceiling
        )
        inv.stage([lead])
        available = inv.available()
        self.assertEqual(available, [])

    def test_legacy_lead_without_tier_field_falls_back_to_reclassification(self):
        """Leads written before this fix have no _freshness_tier field.
        _prune must reclassify from job_age_days rather than defaulting to
        the old blanket-14-day behavior, so old state files migrate safely."""
        inv = self._staged_inventory()
        lead = _lead(
            domain="legacy.com",
            bucket="finance",
            lead_key="legacy-1",
            job_age_days=20,
            tier=freshness_policy.TIER_RECOVERY,
        )
        del lead["_freshness_tier"]
        inv.stage([lead])
        available = inv.available()
        self.assertEqual([item["lead_key"] for item in available], ["legacy-1"])


class FreshnessBoundaryTests(unittest.TestCase):
    """Phase 13 section 3: admission (is_stale_job) and inventory pruning must
    agree at every tier boundary -- a job exactly at the permitted maximum
    remains eligible for that day and expires only after exceeding it."""

    def _job(self, age_days, **extra):
        posted = datetime.now(timezone.utc) - timedelta(days=age_days)
        job = {"job_posted_at_datetime_utc": posted.isoformat(), "job_title": "Senior Engineer"}
        job.update(extra)
        return job

    def _staged(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return FinalPassInventory(f"{temp.name}/inv.json")

    def test_admission_boundary_is_strictly_greater_than(self):
        # Base window: exactly 14 admitted, 15 rejected by the outer max bound.
        at = self._job(14)
        stale, _ = job_filter.is_stale_job(at, max_age_days=14, min_age_days=None)
        self.assertFalse(stale)
        over = self._job(15)
        stale, reason = job_filter.is_stale_job(over, max_age_days=14, min_age_days=None)
        self.assertTrue(stale)
        self.assertIn("stale_job", reason)

    def _prune_at(self, job_age_days, tier, stored_days_ago):
        inv = self._staged()
        validation = datetime.now(timezone.utc) - timedelta(days=stored_days_ago)
        lead = {
            "_final_state": "FINAL_PASS", "_freshness_tier": tier,
            "_validation_timestamp": validation.isoformat(), "job_age_days": job_age_days,
            "employer_website": "https://acme.com", "canonical_domain": "acme.com",
            "bucket": "finance", "lead_key": "k", "hiring_manager_email": "x@acme.com",
        }
        inv.stage([lead])
        return len(inv.available()) == 1  # True = still eligible

    def test_pruning_boundary_primary_14(self):
        self.assertTrue(self._prune_at(14, freshness_policy.TIER_PRIMARY, 0))   # exactly 14 -> eligible
        self.assertFalse(self._prune_at(14, freshness_policy.TIER_PRIMARY, 1))  # 15 -> expired

    def test_pruning_boundary_recovery_30(self):
        self.assertTrue(self._prune_at(30, freshness_policy.TIER_RECOVERY, 0))   # exactly 30 -> eligible
        self.assertFalse(self._prune_at(30, freshness_policy.TIER_RECOVERY, 1))  # 31 -> expired

    def test_pruning_boundary_extended_60(self):
        self.assertTrue(self._prune_at(60, freshness_policy.TIER_EXTENDED, 0))   # exactly 60 -> eligible
        self.assertFalse(self._prune_at(60, freshness_policy.TIER_EXTENDED, 1))  # 61 -> expired

    def test_pruning_boundary_deep_90(self):
        self.assertTrue(self._prune_at(90, freshness_policy.TIER_DEEP, 0))   # exactly 90 -> eligible
        self.assertFalse(self._prune_at(90, freshness_policy.TIER_DEEP, 1))  # 91 -> expired

    def test_recovery_lead_admitted_at_ceiling_not_pruned_same_run(self):
        # A 30-day recovery lead admitted at exactly the recovery ceiling must
        # survive the same-run prune (elapsed 0) -- the core no-same-run-prune
        # guarantee, now holding at the boundary too.
        self.assertTrue(self._prune_at(30, freshness_policy.TIER_RECOVERY, 0))


class ExtendedRecoveryDefaultOffTests(unittest.TestCase):
    """Phase 13 section 2: the 31-90 day extended pass must be OFF unless
    explicitly enabled, and enabling it must not disturb the base/recovery
    lanes."""

    def test_config_default_is_false(self):
        import importlib
        import config as _cfg
        # The committed default (no env var set) must be False.
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("EXTENDED_AGE_RECOVERY_ENABLED", None)
            importlib.reload(_cfg)
            self.assertFalse(_cfg.EXTENDED_AGE_RECOVERY_ENABLED)

    def test_explicit_enable_is_respected(self):
        import importlib
        import config as _cfg
        with patch.dict("os.environ", {"EXTENDED_AGE_RECOVERY_ENABLED": "1"}):
            importlib.reload(_cfg)
            self.assertTrue(_cfg.EXTENDED_AGE_RECOVERY_ENABLED)
        os.environ.pop("EXTENDED_AGE_RECOVERY_ENABLED", None)
        importlib.reload(_cfg)


class ExtendedRecoveryDataFlowReconciliationTests(unittest.TestCase):
    """job_age_days is set by job_signal.annotate_job() (called downstream in
    hiring_manager.py), via its own fresh classify_freshness() call -- a
    *different* function than job_filter.is_stale_job(), which is what sets
    _freshness_tier during the filter stage. This proves the two agree and
    that _freshness_tier survives the copy-and-extend pattern annotate_job()
    uses, rather than assuming it from reading the code alone."""

    def _job(self, age_days, **extra):
        posted = datetime.now(timezone.utc) - timedelta(days=age_days)
        job = {"job_posted_at_datetime_utc": posted.isoformat(), "job_title": "Revenue Systems Engineer"}
        job.update(extra)
        return job

    def test_freshness_tier_survives_annotate_job_and_matches_job_age_days(self):
        job = self._job(20)
        stale, reason = job_filter.is_stale_job(job, max_age_days=30, min_age_days=15)
        self.assertFalse(stale, reason)
        self.assertEqual(job["_freshness_tier"], freshness_policy.TIER_RECOVERY)

        annotated = job_signal.annotate_job(job, probe_url=False)
        self.assertEqual(annotated["_freshness_tier"], freshness_policy.TIER_RECOVERY)
        self.assertIn("job_age_days", annotated)
        self.assertEqual(
            freshness_policy.classify_age_tier(annotated["job_age_days"]),
            annotated["_freshness_tier"],
        )

    def test_extended_tier_survives_annotate_job(self):
        job = self._job(45, _ats_board_identity_verified=True)
        stale, reason = job_filter.is_stale_job(job, max_age_days=90, min_age_days=31)
        self.assertFalse(stale, reason)
        annotated = job_signal.annotate_job(job, probe_url=False)
        self.assertEqual(annotated["_freshness_tier"], freshness_policy.TIER_EXTENDED)

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        inv = FinalPassInventory(f"{temp.name}/final_pass_inventory.json")
        lead = dict(annotated)
        lead["_final_state"] = "FINAL_PASS"
        lead["lead_key"] = "ext-flow-1"
        lead["employer_website"] = "https://acme.com"
        lead["canonical_domain"] = "acme.com"
        lead["bucket"] = "finance"
        lead["hiring_manager_email"] = "x@acme.com"
        inv.stage([lead])
        self.assertEqual([item["lead_key"] for item in inv.available()], ["ext-flow-1"])


if __name__ == "__main__":
    unittest.main()
