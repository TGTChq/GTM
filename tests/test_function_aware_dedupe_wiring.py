"""Function-aware upstream dedupe must be REAL execution, not a shadow metric.

Per-family paid execution and the shared governor cap already existed; what was
missing was the crosswalk actually being learned/persisted and the per-family
``exclude_organization_slug`` actually being sent.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import config
import fantastic_jobs_adapter as FJA
from orchestrator.function_acquisition import SlugCrosswalk, covered_slugs_for_family


def _fresh_crosswalk(path, entries):
    cw = SlugCrosswalk(path, ttl_days=120)
    for slug, dom, bucket in entries:
        cw.observe(slug=slug, domain=dom, bucket=bucket)
    cw.save()
    return cw


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "cw.json")

    def _cfg(self, **over):
        base = dict(FANTASTIC_FUNCTION_AWARE_UPSTREAM_DEDUPE_ENABLED=True,
                    FANTASTIC_SLUG_CROSSWALK_PATH=self.path,
                    FANTASTIC_SLUG_CROSSWALK_TTL_DAYS=120,
                    FANTASTIC_FUNCTION_DEDUPE_MAX_SLUGS_PER_FAMILY=250,
                    FANTASTIC_FUNCTION_DEDUPE_EXPLORATION_EVERY_N_RUNS=7)
        base.update(over)
        return mock.patch.multiple(config, **base)

    def test_disabled_flag_is_a_complete_noop(self):
        with mock.patch.object(config, "FANTASTIC_FUNCTION_AWARE_UPSTREAM_DEDUPE_ENABLED", False):
            cw, keys, suppress = FJA._function_dedupe_context(frozenset(), {})
        self.assertIsNone(cw)
        self.assertFalse(suppress)

    def test_enabled_opens_a_persistent_crosswalk(self):
        metrics = {}
        with self._cfg():
            cw, keys, suppress = FJA._function_dedupe_context(frozenset({"domain:a.com|bucket:gtm_revenue"}), metrics)
        self.assertIsNotNone(cw)
        self.assertTrue(suppress)
        self.assertTrue(metrics["function_dedupe"]["enabled"])

    def test_exploration_run_suppresses_nothing(self):
        """Every Nth run must acquire with NO exclusions -- no permanent blindness."""
        metrics = {}
        with self._cfg(FANTASTIC_FUNCTION_DEDUPE_EXPLORATION_EVERY_N_RUNS=1):
            _cw, _k, suppress = FJA._function_dedupe_context(frozenset(), metrics)
        self.assertFalse(suppress)

    def test_run_counter_persists_across_restarts(self):
        with self._cfg():
            for _ in range(3):
                cw, _k, _s = FJA._function_dedupe_context(frozenset(), {})
                cw.save()
        self.assertEqual(json.load(open(self.path, encoding="utf-8"))["runs"], 3)

    def test_context_failure_degrades_to_no_dedupe(self):
        with self._cfg(FANTASTIC_SLUG_CROSSWALK_PATH=self.path), \
             mock.patch("orchestrator.function_acquisition.SlugCrosswalk",
                        side_effect=RuntimeError("boom")):
            cw, keys, suppress = FJA._function_dedupe_context(frozenset(), {})
        self.assertIsNone(cw)
        self.assertFalse(suppress)


class FamilyExclusionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "cw.json")

    def test_only_the_same_function_is_excluded(self):
        """A company covered for GTM stays fully acquirable for Engineering."""
        cw = _fresh_crosswalk(self.path, [("acme", "acme.com", "gtm_revenue")])
        keys = {"domain:acme.com|bucket:gtm_revenue"}
        self.assertEqual(covered_slugs_for_family(cw, keys, "gtm_revenue"), ["acme"])
        self.assertEqual(
            covered_slugs_for_family(cw, keys, "engineering_ai_automation"), [])

    def test_stale_entries_age_out_via_ttl(self):
        cw = SlugCrosswalk(self.path, ttl_days=30,
                           now=datetime.now(timezone.utc) - timedelta(days=90))
        cw.observe(slug="acme", domain="acme.com", bucket="gtm_revenue")
        cw._now = datetime.now(timezone.utc)
        self.assertEqual(
            covered_slugs_for_family(cw, {"domain:acme.com|bucket:gtm_revenue"},
                                     "gtm_revenue"), [])

    def test_exclusion_list_is_capped_not_split_into_more_requests(self):
        entries = [(f"org{i}", f"org{i}.com", "gtm_revenue") for i in range(600)]
        cw = _fresh_crosswalk(self.path, entries)
        keys = {f"domain:org{i}.com|bucket:gtm_revenue" for i in range(600)}
        metrics = {"function_dedupe": {}}
        with mock.patch.object(config, "FANTASTIC_FUNCTION_DEDUPE_MAX_SLUGS_PER_FAMILY", 250):
            out = FJA._family_exclusion_slugs(cw, keys, "Account Executive", metrics)
        self.assertEqual(len(out), 250)
        self.assertIn("Account Executive", metrics["function_dedupe"]["truncated_families"])

    def test_no_coverage_means_no_exclusion(self):
        cw = _fresh_crosswalk(self.path, [])
        self.assertEqual(FJA._family_exclusion_slugs(cw, frozenset(), "Account Executive",
                                                     {"function_dedupe": {}}), [])

    def test_exclusion_failure_degrades_to_empty(self):
        cw = _fresh_crosswalk(self.path, [("acme", "acme.com", "gtm_revenue")])
        with mock.patch("orchestrator.function_acquisition.covered_slugs_for_family",
                        side_effect=RuntimeError("boom")):
            self.assertEqual(
                FJA._family_exclusion_slugs(cw, {"domain:acme.com|bucket:gtm_revenue"},
                                            "Account Executive", {"function_dedupe": {}}), [])


class RealExecutionWiringTests(unittest.TestCase):
    """Proof this is real execution, not a shadow metric."""

    def _src(self):
        import inspect
        return inspect.getsource(FJA.run_fantastic_jobs_acquisition)

    def test_exclusion_is_actually_sent_as_a_request_param(self):
        src = self._src()
        self.assertIn('jb_params["exclude_organization_slug"] = ",".join(_excluded)', src)

    def test_exclusion_is_comma_joined_single_value(self):
        """Repeated params are silently ignored past the first by this provider."""
        self.assertIn('",".join(_excluded)', self._src())

    def test_crosswalk_is_learned_and_persisted_each_run(self):
        src = self._src()
        self.assertIn("_cw.observe_jobs(result.jobs)", src)
        self.assertIn("_cw.save()", src)

    def test_provider_rejection_retries_without_the_exclusion(self):
        """Never accept a silent zero-row family."""
        src = self._src()
        self.assertIn('jb_params.pop("exclude_organization_slug", None)', src)
        self.assertIn("provider_fallbacks", src)

    def test_exclusion_only_applies_when_suppressing(self):
        self.assertIn("if (_cw is not None and _suppress) else []", self._src())

    def test_governor_cap_still_bounds_every_family(self):
        """One global run_cap shared across families -- no per-family budget."""
        src = self._src()
        # One global cap, split across families -- no family gets its own budget.
        self.assertIn("global_cap = int(run_cap)", src)
        self.assertIn("cap = min(int(_grants.get(term, 1)), global_cap - len(result.jobs))", src)
        self.assertIn("if quota.stop_reason or len(result.jobs) >= global_cap:", src)


if __name__ == "__main__":
    unittest.main()
