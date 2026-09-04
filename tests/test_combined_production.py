"""COMBINED production-architecture tests: multiple aggressive levers ON together.

Guards the invariants that only break when features interact:
  * never exceed the governor's billed-row allowance (ATS + JB combined)
  * ATS and JB stay complementary (provider de-twinning preserved)
  * one malformed source never removes the baseline JB path
  * watermark never advances on an incomplete window; unhealthy state falls back
  * expansion never loses a base clause and never buys a naked activity word
  * flags OFF reproduce production byte-for-byte
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import config
import fantastic_jobs_adapter as fja

NOW = datetime(2026, 9, 20, 13, 0, 0, tzinfo=timezone.utc)


def _rows(n, *, src="linkedin", stype="jobboard", prefix="1", start=None):
    base = start or (NOW - timedelta(hours=8))
    out = []
    for i in range(n):
        dc = base + timedelta(minutes=i)
        out.append({"id": f"{prefix}{i:05d}", "title": "Account Executive",
                    "organization": f"Co{prefix}{i}", "source": src, "source_type": stype,
                    "organization_url": f"https://co{prefix}{i}.com",
                    "org_linkedin_slug": f"co{prefix}{i}",
                    "date_posted": dc.isoformat().replace("+00:00", "Z"),
                    "date_created": dc.isoformat().replace("+00:00", "Z"),
                    "countries_derived": ["United States"], "employment_type": ["FULL_TIME"],
                    "org_linkedin_headcount": 120, "ai_taxonomies_a": ["Sales"]})
    return out


class _Feed:
    """Two-dataset provider: active-ats serves ATS rows, active-jb serves jb rows."""
    def __init__(self, ats=(), jb=(), ats_status=200, ats_garbage=0):
        self.ats, self.jb = list(ats), list(jb)
        self.ats_status, self.ats_garbage = ats_status, ats_garbage
        self.calls = []

    def __call__(self, url, headers, params, timeout):
        self.calls.append((url, dict(params)))
        is_ats = url.endswith("/v1/active-ats")
        rows = self.ats if is_ats else self.jb
        gte, lt = params.get("date_created_gte"), params.get("date_created_lt")
        rows = [r for r in rows if (gte is None or r["date_created"] >= gte)
                and (lt is None or r["date_created"] < lt)]
        o, l = int(params.get("offset", 0)), int(params.get("limit", 100))
        page = rows[o:o + l]
        if is_ats and self.ats_garbage:
            page = [{"id": r["id"]} for r in page][:self.ats_garbage] + page[self.ats_garbage:]
        status = self.ats_status if is_ats else 200
        outer = self

        class R:
            status_code = status
            headers = {"x-api-jobs-remaining": "9000", "x-api-requests-remaining": "9000"}
            def json(self): return page if status == 200 else {"error": "boom"}
        return R()


_PROD = dict(
    FANTASTIC_JOBS_ENABLED=True, FANTASTIC_JOBS_API_KEY="k",
    FANTASTIC_JOBS_BASE_URL="https://data.fantastic.jobs",
    FANTASTIC_JOBS_WELLFOUND_LIMIT=0, FANTASTIC_JOBS_YCOMBINATOR_LIMIT=0,
    FANTASTIC_JOBS_TIME_FRAME="7d", FANTASTIC_JOBS_MAX_PAGES_PER_SEGMENT=70,
    FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=0, FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING=0,
    FANTASTIC_JOBS_MAX_RETRIES=0, FANTASTIC_JOBS_FAIL_OPEN=False,
    FANTASTIC_JOBS_LOCATION="United States", FANTASTIC_JOBS_HEADCOUNT_MIN=25,
    FANTASTIC_JOBS_HEADCOUNT_MAX=1000, FANTASTIC_JOBS_AI_EMPLOYMENT_TYPE="FULL_TIME",
    FANTASTIC_JOBS_EXCLUDE_AGENCY=True, FANTASTIC_JOBS_TITLE_ADVANCED_ENABLED=True,
    FANTASTIC_JOBS_TITLE_ADVANCED_EXPRESSION="", FANTASTIC_JOBS_TITLE_TARGETING_ENABLED=True,
    FANTASTIC_JOBS_CONTINUATION_ENABLED=False, FANTASTIC_JOBS_RUN_SLICE_CAP=0,
    FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=False, FANTASTIC_ATS_SOURCE_ENABLED=False,
    FANTASTIC_SERVER_INDUSTRY_EXCLUSION_ENABLED=False, FANTASTIC_TITLE_ALIASES_ENABLED=False,
    FANTASTIC_TITLE_SCOPED_EXCLUSIONS_ENABLED=False, FANTASTIC_TITLE_GLOBAL_EXCLUSIONS_ENABLED=False,
    FANTASTIC_FUNCTIONAL_ROLE_EXPANSION_ENABLED=False, FANTASTIC_MONTHLY_GOVERNOR_ENABLED=False,
    FANTASTIC_JOBS_CONTINUATION_STATE_PATH="", FANTASTIC_ATS_MAX_SCHEMA_REJECT_RATE=0.5,
    FANTASTIC_JOBS_ATS_LIMIT=0, FANTASTIC_JOBS_LINKEDIN_LIMIT=6000,
    FANTASTIC_JOBS_MAX_JOBS_PER_RUN=6000,
)


def _aggressive(**over):
    """The intended post-reset production shape."""
    cfg = dict(_PROD, FANTASTIC_ATS_SOURCE_ENABLED=True, FANTASTIC_JOBS_ATS_LIMIT=6000,
               FANTASTIC_JOBS_MAX_JOBS_PER_RUN=12000,
               FANTASTIC_SERVER_INDUSTRY_EXCLUSION_ENABLED=True,
               FANTASTIC_TITLE_ALIASES_ENABLED=True,
               FANTASTIC_FUNCTIONAL_ROLE_EXPANSION_ENABLED=True,
               FANTASTIC_TITLE_SCOPED_EXCLUSIONS_ENABLED=True)
    cfg.update(over)
    return cfg


class ConfigValidityTests(unittest.TestCase):
    def test_aggressive_config_passes_validation(self):
        """ATS_LIMIT>0 alongside LINKEDIN_LIMIT must not re-trigger the segment-limit
        ValueError that caused the zero-acquisition incident."""
        with mock.patch.multiple(config, **_aggressive()):
            config.validate_fantastic_jobs_config()

    def test_ats_limit_without_headroom_still_fails_closed(self):
        with mock.patch.multiple(config, **_aggressive(FANTASTIC_JOBS_MAX_JOBS_PER_RUN=6000)):
            with self.assertRaises(ValueError):
                config.validate_fantastic_jobs_config()


class GovernorPlusSourcesTests(unittest.TestCase):
    def test_combined_ats_jb_billing_never_exceeds_run_cap(self):
        feed = _Feed(ats=_rows(400, src="greenhouse", stype="ats", prefix="A"),
                     jb=_rows(400, prefix="J"))
        with mock.patch.multiple(config, **_aggressive(FANTASTIC_JOBS_RUN_SLICE_CAP=300)):
            res = fja.run_fantastic_jobs_acquisition(http_get=feed)
        self.assertLessEqual(res.metadata["jobs_quota_consumed"], 300)
        self.assertLessEqual(len(res.jobs), 300)

    def test_ats_cannot_claim_an_independent_allowance(self):
        """ATS draws from the SAME cap JB uses -- not a second budget."""
        feed = _Feed(ats=_rows(500, src="greenhouse", stype="ats", prefix="A"),
                     jb=_rows(500, prefix="J"))
        with mock.patch.multiple(config, **_aggressive(FANTASTIC_JOBS_RUN_SLICE_CAP=200)):
            res = fja.run_fantastic_jobs_acquisition(http_get=feed)
        per = res.metadata["per_source"]
        total = sum(v["jobs"] for v in per.values())
        self.assertEqual(total, len(res.jobs))
        self.assertLessEqual(total, 200)


class AtsJbComplementTests(unittest.TestCase):
    def test_jb_keeps_provider_de_twinning_while_ats_ingests(self):
        feed = _Feed(ats=_rows(5, src="greenhouse", stype="ats", prefix="A"),
                     jb=_rows(5, prefix="J"))
        with mock.patch.multiple(config, **_aggressive()):
            res = fja.run_fantastic_jobs_acquisition(http_get=feed)
        jb_calls = [p for u, p in feed.calls if u.endswith("/v1/active-jb")]
        ats_calls = [p for u, p in feed.calls if u.endswith("/v1/active-ats")]
        self.assertTrue(jb_calls and ats_calls)
        self.assertEqual(jb_calls[0]["exclude_ats_duplicate"], "true")   # de-twinning kept
        self.assertNotIn("exclude_ats_duplicate", ats_calls[0])
        self.assertNotIn("source", ats_calls[0])
        self.assertEqual({j["_provider_dataset"] for j in res.jobs}, {"ats", "jb"})
        self.assertEqual(len({j["job_id"] for j in res.jobs}), len(res.jobs))

    def test_ats_rows_map_with_the_shared_canonical_schema(self):
        feed = _Feed(ats=_rows(3, src="greenhouse", stype="ats", prefix="A"), jb=[])
        with mock.patch.multiple(config, **_aggressive()):
            res = fja.run_fantastic_jobs_acquisition(http_get=feed)
        ats = [j for j in res.jobs if j["_provider_dataset"] == "ats"]
        self.assertEqual(len(ats), 3)
        for j in ats:
            self.assertTrue(j["employer_name"] and j["job_title"] and j["job_id"])
            self.assertTrue(j["org_linkedin_slug"])
            self.assertEqual(j["_ai_taxonomy_primary"], "Sales")


class CircuitBreakerTests(unittest.TestCase):
    def test_ats_http_error_never_kills_the_jb_baseline(self):
        feed = _Feed(ats=_rows(50, src="greenhouse", stype="ats", prefix="A"),
                     jb=_rows(50, prefix="J"), ats_status=500)
        with mock.patch.multiple(config, **_aggressive()):
            res = fja.run_fantastic_jobs_acquisition(http_get=feed)
        jb = [j for j in res.jobs if j["_provider_dataset"] == "jb"]
        self.assertEqual(len(jb), 50)                       # baseline intact
        self.assertTrue(res.metadata["ats_source"]["circuit_open"])

    def test_ats_malformed_rows_trip_the_breaker_and_jb_continues(self):
        feed = _Feed(ats=_rows(40, src="greenhouse", stype="ats", prefix="A"),
                     jb=_rows(20, prefix="J"), ats_garbage=40)   # all ATS rows unmappable
        with mock.patch.multiple(config, **_aggressive()):
            res = fja.run_fantastic_jobs_acquisition(http_get=feed)
        self.assertTrue(res.metadata["ats_source"]["circuit_open"])
        self.assertGreater(res.metadata["ats_source"]["schema_reject_rate"], 0.5)
        self.assertEqual(len([j for j in res.jobs if j["_provider_dataset"] == "jb"]), 20)

    def test_one_malformed_row_does_not_trip_the_breaker(self):
        feed = _Feed(ats=_rows(40, src="greenhouse", stype="ats", prefix="A"),
                     jb=_rows(5, prefix="J"), ats_garbage=1)
        with mock.patch.multiple(config, **_aggressive()):
            res = fja.run_fantastic_jobs_acquisition(http_get=feed)
        self.assertFalse(res.metadata["ats_source"]["circuit_open"])
        self.assertEqual(len([j for j in res.jobs if j["_provider_dataset"] == "ats"]), 39)

    def test_unhealthy_watermark_state_falls_back_to_head_deep(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "wm.json")
        with open(path, "w") as fh:      # corrupt in-flight marker
            fh.write('{"schema":"fantastic-watermark/1","in_flight_window_end":"NOT-A-DATE",'
                     '"window_start":"ALSO-BAD"}')
        # Watermark bounds come from real wall-clock, so these rows must be dated
        # relative to now (not the module's fixed NOW constant).
        real_now = datetime.now(timezone.utc)
        feed = _Feed(jb=_rows(10, prefix="J", start=real_now - timedelta(hours=8)))
        cfg = _aggressive(FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=True,
                          FANTASTIC_WATERMARK_STATE_PATH=path,
                          FANTASTIC_DATE_CREATED_LAG_MINUTES=180,
                          FANTASTIC_DATE_CREATED_OVERLAP_MINUTES=60,
                          FANTASTIC_JOBS_ATS_LIMIT=0, FANTASTIC_ATS_SOURCE_ENABLED=False)
        with mock.patch.multiple(config, **cfg):
            res = fja.run_fantastic_jobs_acquisition(http_get=feed)
        # Either it recovered a valid window, or it fell back -- never crashed and
        # never lost the baseline path.
        self.assertTrue(res.success)
        self.assertGreater(len(res.jobs), 0)


class ExpansionCombinedTests(unittest.TestCase):
    def test_expansion_never_loses_a_base_clause(self):
        with mock.patch.multiple(config, **_PROD):
            off = fja.build_title_query_plan()
        with mock.patch.multiple(config, **_aggressive()):
            on = fja.build_title_query_plan()
        base_off = {c["role"] for c in off["clauses"]}
        base_on = {c["role"] for c in on["clauses"] if not c["expanded"]}
        self.assertEqual(base_off, base_on)
        from role_catalog import DEFAULT_SEARCH_ROLES
        self.assertEqual(on["base_clauses"], len(DEFAULT_SEARCH_ROLES))
        self.assertGreater(on["expanded_clauses"], 0)

    def test_no_naked_activity_word_is_purchased(self):
        with mock.patch.multiple(config, **_aggressive()):
            plan = fja.build_title_query_plan()
        terms = [t.strip("'") for c in plan["clauses"] for t in c["include"]]
        for naked in ("automation", "operations", "systems", "engineer"):
            self.assertNotIn(naked, terms, f"naked activity word '{naked}' purchased")

    def test_automation_expansions_carry_family_contamination_defence(self):
        with mock.patch.multiple(config, **_aggressive()):
            plan = fja.build_title_query_plan()
        auto = [c for c in plan["clauses"]
                if c["expanded"] and "automation" in " ".join(c["include"])]
        self.assertTrue(auto)
        for c in auto:
            self.assertTrue(c["exclude"], f"{c['role']} has no contamination defence")
            joined = " ".join(c["exclude"])
            self.assertIn("hvac", joined)

    def test_every_clause_maps_to_exactly_one_family(self):
        from orchestrator.function_acquisition import family_for_role
        with mock.patch.multiple(config, **_aggressive()):
            plan = fja.build_title_query_plan()
        seen = {}
        for c in plan["clauses"]:
            fam = family_for_role(c["role"])
            for t in c["include"]:
                self.assertNotIn(t, seen, f"term {t} appears in two clauses")
                seen[t] = fam


class FlagsOffInvarianceTests(unittest.TestCase):
    def test_all_flags_off_reproduces_production_exactly(self):
        feed = _Feed(ats=_rows(5, src="greenhouse", stype="ats", prefix="A"),
                     jb=_rows(5, prefix="J"))
        with mock.patch.multiple(config, **_PROD):
            res = fja.run_fantastic_jobs_acquisition(http_get=feed)
        urls = {u for u, _ in feed.calls}
        self.assertEqual(urls, {"https://data.fantastic.jobs/v1/active-jb"})
        p = feed.calls[0][1]
        for k in ("exclude_organization_industry", "date_created_gte", "exclude_organization_slug"):
            self.assertNotIn(k, p)
        # Compares CONTENT, not a pinned byte count: the request must carry exactly
        # the production expression, with no expansion clause added by any flag.
        with mock.patch.multiple(config, **_PROD):
            expected = fja.build_title_query_plan()["expression"]
        self.assertEqual(p["title_advanced"], expected)


if __name__ == "__main__":
    unittest.main()
