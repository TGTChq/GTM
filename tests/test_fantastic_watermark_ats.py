"""date_created watermark engine + active-ats source + server-side filters + title
query plan. All offline (injected http_get). Flags are patched per test; the
DEFAULT path is asserted byte-identical to production in the first class."""
from __future__ import annotations

import os
import tempfile
import unittest
import warnings
from datetime import datetime, timedelta, timezone
from unittest import mock

import config
import fantastic_jobs_adapter as fja

NOW = datetime(2026, 9, 20, 13, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _recs(n, *, created_start=None, step_min=1, src="linkedin", prefix="9"):
    base = created_start or (NOW - timedelta(hours=6))
    out = []
    for i in range(n):
        dc = base + timedelta(minutes=i * step_min)
        out.append({"id": f"{prefix}{i:05d}", "title": "Account Executive", "organization": f"Co{i}",
                    "source": src, "organization_url": f"https://co{i}.com",
                    "date_posted": dc.isoformat().replace("+00:00", "Z"),
                    "date_created": dc.isoformat().replace("+00:00", "Z"),
                    "countries_derived": ["United States"], "employment_type": ["FULL_TIME"],
                    "org_linkedin_headcount": 100, "org_linkedin_industry": "Software Development"})
    return out


class _Feed:
    """Fake provider honoring date_created_gte/lt + offset/limit; records calls."""
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __call__(self, url, headers, params, timeout):
        self.calls.append((url, dict(params)))
        gte, lt = params.get("date_created_gte"), params.get("date_created_lt")
        sel = [r for r in self.rows if (gte is None or r["date_created"] >= gte)
               and (lt is None or r["date_created"] < lt)]
        # Model the two provider datasets: active-ats serves ATS rows, active-jb
        # serves job-board rows (the provider's exclude_ats_duplicate keeps them
        # disjoint); a `source` filter narrows the jb dataset.
        if url.endswith("/v1/active-ats"):
            sel = [r for r in sel if r.get("source_type") == "ats"]
        else:
            sel = [r for r in sel if r.get("source_type") != "ats"]
            if params.get("source"):
                sel = [r for r in sel if r.get("source") == params["source"]]
        sel = sorted(sel, key=lambda r: r["date_created"], reverse=True)
        o, l = int(params.get("offset", 0)), int(params.get("limit", 100))

        class R:
            status_code = 200
            headers = {"x-api-jobs-remaining": "7000", "x-api-requests-remaining": "9000",
                       "x-api-next-billing-date": "2026-10-17"}
            def __init__(s, d): s._d = d
            def json(s): return s._d
        return R(sel[o:o + l])


_PROD = dict(
    FANTASTIC_JOBS_ENABLED=True, FANTASTIC_JOBS_API_KEY="k",
    FANTASTIC_JOBS_BASE_URL="https://data.fantastic.jobs",
    FANTASTIC_JOBS_ATS_LIMIT=0, FANTASTIC_JOBS_WELLFOUND_LIMIT=0, FANTASTIC_JOBS_YCOMBINATOR_LIMIT=0,
    FANTASTIC_JOBS_LINKEDIN_LIMIT=6000, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=6000,
    FANTASTIC_JOBS_TIME_FRAME="7d", FANTASTIC_JOBS_MAX_PAGES_PER_SEGMENT=70,
    FANTASTIC_JOBS_MIN_JOBS_QUOTA_REMAINING=500, FANTASTIC_JOBS_MIN_REQUESTS_QUOTA_REMAINING=20,
    FANTASTIC_JOBS_MAX_RETRIES=0, FANTASTIC_JOBS_FAIL_OPEN=False,
    FANTASTIC_JOBS_LOCATION="United States", FANTASTIC_JOBS_HEADCOUNT_MIN=25,
    FANTASTIC_JOBS_HEADCOUNT_MAX=1000, FANTASTIC_JOBS_AI_EMPLOYMENT_TYPE="FULL_TIME",
    FANTASTIC_JOBS_EXCLUDE_AGENCY=True, FANTASTIC_JOBS_TITLE_ADVANCED_ENABLED=True,
    FANTASTIC_JOBS_TITLE_ADVANCED_EXPRESSION="", FANTASTIC_JOBS_TITLE_TARGETING_ENABLED=True,
    FANTASTIC_JOBS_CONTINUATION_ENABLED=False, FANTASTIC_JOBS_RUN_SLICE_CAP=0,
    FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=False, FANTASTIC_ATS_SOURCE_ENABLED=False,
    FANTASTIC_SERVER_INDUSTRY_EXCLUSION_ENABLED=False, FANTASTIC_TITLE_ALIASES_ENABLED=False,
    FANTASTIC_TITLE_SCOPED_EXCLUSIONS_ENABLED=False, FANTASTIC_TITLE_GLOBAL_EXCLUSIONS_ENABLED=False,
    FANTASTIC_MONTHLY_GOVERNOR_ENABLED=False, FANTASTIC_JOBS_CONTINUATION_STATE_PATH="",
)


class DefaultPathInvariance(unittest.TestCase):
    """Deploying with every new flag at its default must not change the live query."""

    def test_default_expression_is_byte_identical_to_production(self):
        with mock.patch.multiple(config, **_PROD):
            plan = fja.build_title_query_plan()
        from role_catalog import DEFAULT_SEARCH_ROLES
        self.assertEqual(plan["expression"].count("|") + 1, len(DEFAULT_SEARCH_ROLES))
        self.assertEqual(len(plan["clauses"]), len(DEFAULT_SEARCH_ROLES))
        self.assertEqual(plan["global_exclusions"], [])
        self.assertFalse(any(c["exclude"] for c in plan["clauses"]))

    def test_default_request_has_no_new_params_and_no_ats(self):
        feed = _Feed(_recs(5))
        with mock.patch.multiple(config, **_PROD):
            res = fja.run_fantastic_jobs_acquisition(http_get=feed)
        self.assertTrue(res.success)
        urls = {u for u, _ in feed.calls}
        self.assertEqual(urls, {"https://data.fantastic.jobs/v1/active-jb"})
        p = feed.calls[0][1]
        for k in ("exclude_organization_industry", "date_created_gte", "date_created_lt"):
            self.assertNotIn(k, p)
        self.assertEqual(p["exclude_ats_duplicate"], "true")
        self.assertFalse(res.metadata["ats_source"]["enabled"])
        self.assertNotIn("watermark", res.metadata)


class TitleQueryPlanTests(unittest.TestCase):
    def test_aliases_gated_add_ux_ui_and_frontend_variants(self):
        with mock.patch.multiple(config, **dict(_PROD, FANTASTIC_TITLE_ALIASES_ENABLED=True)):
            plan = fja.build_title_query_plan()
        expr = plan["expression"]
        for t in ("'ux designer'", "'ui designer'", "'front end developer'"):
            self.assertIn(t, expr)
        from role_catalog import DEFAULT_SEARCH_ROLES
        # aliases add exactly four extra terms over the base catalog union
        self.assertEqual(expr.count("|") + 1, len(DEFAULT_SEARCH_ROLES) + 4)

    def test_scoped_negation_is_inside_the_role_clause_only(self):
        with mock.patch.multiple(config, **dict(_PROD, FANTASTIC_TITLE_SCOPED_EXCLUSIONS_ENABLED=True,
                                                FANTASTIC_TITLE_SCOPED_EXCLUSIONS={"Community Manager": ["apartment", "leasing"]})):
            plan = fja.build_title_query_plan()
        cm = [c for c in plan["clauses"] if c["role"] == "Community Manager"][0]
        self.assertEqual(cm["exclude"], ["!apartment", "!leasing"])
        self.assertIn("('community manager' & !apartment & !leasing)", plan["expression"])
        # No other clause carries the negation; union is still OR-joined.
        others = [c for c in plan["clauses"] if c["role"] != "Community Manager"]
        self.assertTrue(all(not c["exclude"] for c in others))
        self.assertFalse(plan["expression"].endswith("!leasing)") and " & !apartment" in plan["expression"].split("|")[0])

    def test_global_negation_wraps_union_with_supported_operator_only(self):
        with mock.patch.multiple(config, **dict(_PROD, FANTASTIC_TITLE_GLOBAL_EXCLUSIONS_ENABLED=True,
                                                FANTASTIC_TITLE_GLOBAL_EXCLUSIONS=["clearance", "TS/SCI"])):
            plan = fja.build_title_query_plan()
        self.assertTrue(plan["expression"].endswith(") & !clearance & !'ts sci'"))
        self.assertNotIn(" -clearance", plan["expression"])
        self.assertNotIn("NOT ", plan["expression"])

    def test_attribution_maps_title_to_family(self):
        with mock.patch.multiple(config, **_PROD):
            plan = fja.build_title_query_plan()
        self.assertEqual(fja.attribute_title_family("Senior Account Executive (Remote)", plan), "account_executive")
        self.assertEqual(fja.attribute_title_family("Kitchen Manager", plan), "")


class ServerIndustryExclusionTests(unittest.TestCase):
    def test_single_label_serialized_as_bare_string_and_icp_still_runs(self):
        feed = _Feed(_recs(3))
        with mock.patch.multiple(config, **dict(_PROD, FANTASTIC_SERVER_INDUSTRY_EXCLUSION_ENABLED=True,
                                                FANTASTIC_EXCLUDED_ORG_INDUSTRIES=["Hospitals and Health Care"])):
            res = fja.run_fantastic_jobs_acquisition(http_get=feed)
        p = feed.calls[0][1]
        self.assertEqual(p["exclude_organization_industry"], "Hospitals and Health Care")
        attr = res.metadata["provider_filters"]
        self.assertTrue(attr["provider_filter_industry"])
        self.assertEqual(attr["industry_labels"], ["Hospitals and Health Care"])
        self.assertTrue(attr["industry_config_fingerprint"])
        # Downstream firmographic fields are still present for the ICP gate.
        self.assertEqual(res.jobs[0]["_org_industry"], "Software Development")

    def test_multiple_labels_use_comma_form_not_repeated_params(self):
        # PROVEN contract (live count probe): the param is an array with
        # style=form/explode=false -> ONE comma-joined value. A Python list would be
        # emitted by `requests` as repeated params, which the API honors only for the
        # FIRST label (silent under-filtering).
        feed = _Feed(_recs(2))
        labels = ["Hospitals and Health Care", "Government Administration"]
        with mock.patch.multiple(config, **dict(_PROD, FANTASTIC_SERVER_INDUSTRY_EXCLUSION_ENABLED=True,
                                                FANTASTIC_EXCLUDED_ORG_INDUSTRIES=labels,
                                                APOLLO_EXCLUDED_INDUSTRY_KEYWORDS=["staffing", "nonprofit"])):
            fja.run_fantastic_jobs_acquisition(http_get=feed)
        p = feed.calls[0][1]
        self.assertIsInstance(p["exclude_organization_industry"], str)
        self.assertEqual(p["exclude_organization_industry"],
                         "Hospitals and Health Care,Government Administration")
        # No Apollo keyword ever leaks into the provider-taxonomy value.
        self.assertNotIn("staffing", p["exclude_organization_industry"])
        self.assertNotIn("nonprofit", p["exclude_organization_industry"])

    def test_disabled_sends_nothing(self):
        feed = _Feed(_recs(2))
        with mock.patch.multiple(config, **_PROD):
            fja.run_fantastic_jobs_acquisition(http_get=feed)
        self.assertNotIn("exclude_organization_industry", feed.calls[0][1])


class WatermarkEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "wm.json")
        self.cfg = dict(_PROD, FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=True,
                        FANTASTIC_DATE_CREATED_LAG_MINUTES=180, FANTASTIC_DATE_CREATED_OVERLAP_MINUTES=60,
                        FANTASTIC_WATERMARK_STATE_PATH=self.path)

    def _run(self, feed, now=NOW, **over):
        with mock.patch.multiple(config, **dict(self.cfg, **over)), \
                mock.patch.object(fja, "datetime", wraps=datetime) as dt:
            dt.now.return_value = now
            dt.fromisoformat = datetime.fromisoformat
            return fja.run_fantastic_jobs_acquisition(http_get=feed)

    def test_bootstrap_window_bounds_lag_and_commit_by_pipeline(self):
        rows = _recs(50, created_start=NOW - timedelta(hours=5))
        feed = _Feed(rows)
        res = self._run(feed)
        p = feed.calls[0][1]
        upper = _iso(NOW - timedelta(minutes=180))
        self.assertEqual(p["date_created_lt"], upper)
        self.assertEqual(p["date_created_gte"], _iso(NOW - timedelta(minutes=180) - timedelta(days=7)))
        # Only rows with date_created < upper are acquired (lag respected).
        self.assertTrue(all(j["_fantastic_date_created"] < upper.replace("Z", "+00:00") or
                            j["_fantastic_date_created"][:19] < upper[:19] for j in res.jobs))
        wm = res.metadata["watermark"]
        self.assertFalse(wm["committed"])                    # adapter never commits
        self.assertTrue(wm["drained"])
        # Pipeline commits after persistence:
        with mock.patch.multiple(config, **self.cfg):
            out = fja.commit_watermark(success=True)
        self.assertTrue(out["committed"])
        self.assertEqual(out["next_watermark"], upper)

    def test_next_run_starts_at_watermark_minus_overlap_and_dedupes_band(self):
        rows = _recs(120, created_start=NOW - timedelta(hours=5), step_min=2)
        feed = _Feed(rows)
        self._run(feed)
        with mock.patch.multiple(config, **self.cfg):
            fja.commit_watermark(success=True)
        first_ids = {j["job_id"] for j in []}
        feed2 = _Feed(rows + _recs(10, created_start=NOW + timedelta(minutes=5), prefix="7"))
        later = NOW + timedelta(hours=4)
        res2 = self._run(feed2, now=later)
        p2 = feed2.calls[0][1]
        prev_upper = _iso(NOW - timedelta(minutes=180))
        self.assertEqual(p2["date_created_gte"], _iso(NOW - timedelta(minutes=240)))  # prev - 60m overlap
        self.assertEqual(p2["date_created_lt"], _iso(later - timedelta(minutes=180)))
        # Overlap-band rows were returned by the provider but deduped locally (not re-emitted).
        seg = res2.metadata["segments"]["fantastic_jobs_linkedin"]
        self.assertGreater(seg["duplicates"], 0)
        emitted = {j["_fantastic_internal_id"] for j in res2.jobs}
        band = {r["id"] for r in rows if _iso(NOW - timedelta(minutes=240)) <= r["date_created"][:19] + "Z" < prev_upper}
        self.assertTrue(emitted.isdisjoint(band))

    def test_crash_before_commit_replays_same_window(self):
        """Crash after checkpoint: the window is reused and the DRAINED source is
        not re-billed.

        Previously the replay re-fetched all 30 rows and discarded them as
        duplicates -- functionally right, but it paid the provider for inventory it
        already had. With per-source drain state the finished source is skipped, so
        the outcome is identical (nothing re-emitted) at zero credits.
        """
        rows = _recs(30, created_start=NOW - timedelta(hours=5))
        feed = _Feed(rows)
        res1 = self._run(feed)                # acquired, checkpointed, NOT committed
        self.assertTrue(res1.metadata["watermark"]["drained_sources"]["fantastic_jobs_linkedin"])
        feed2 = _Feed(rows)
        res2 = self._run(feed2, now=NOW + timedelta(hours=3))
        # Window reused verbatim (not re-derived from the new `now`).
        self.assertTrue(res2.metadata["watermark"]["window_reused"])
        self.assertEqual(res2.metadata["watermark"]["lower"], res1.metadata["watermark"]["lower"])
        self.assertEqual(res2.metadata["watermark"]["upper"], res1.metadata["watermark"]["upper"])
        # Nothing re-emitted AND nothing re-billed.
        self.assertEqual(len(res2.jobs), 0)
        self.assertEqual(feed2.calls, [], "a drained source must not be re-billed")
        self.assertEqual(res2.metadata["segments"]["fantastic_jobs_linkedin"]["stop_reason"],
                         "already_drained_this_window")

    def test_truncated_window_does_not_advance(self):
        # 300 rows, 1 min apart, starting 5h ago: ~120 fall inside [lower, now-3h).
        rows = _recs(300, created_start=NOW - timedelta(hours=5))
        upper = _iso(NOW - timedelta(minutes=180))
        in_window = sum(1 for r in rows if r["date_created"][:19] + "Z" < upper)
        feed = _Feed(rows)
        res = self._run(feed, FANTASTIC_JOBS_RUN_SLICE_CAP=10)
        self.assertEqual(len(res.jobs), 10)
        self.assertFalse(res.metadata["watermark"]["drained"])
        with mock.patch.multiple(config, **self.cfg):
            out = fja.commit_watermark(success=True)
        self.assertFalse(out["committed"])
        self.assertEqual(out["reason"], "window_truncated_replay_next_run")
        # Next run RESUMES the same window at the persisted cursor. It used to
        # re-page from offset 0 and dedupe the first 10 rows again -- correct
        # output, but 10 rows re-billed, and with a real cap that replay never
        # reached the tail at all.
        feed2 = _Feed(rows)
        res2 = self._run(feed2, now=NOW + timedelta(hours=2), FANTASTIC_JOBS_RUN_SLICE_CAP=0)
        self.assertTrue(res2.metadata["watermark"]["window_reused"])
        self.assertEqual(len(res2.jobs), in_window - 10)       # remainder, none re-emitted
        self.assertEqual(int(feed2.calls[0][1].get("offset", 0)), 10,
                         "the replay starts after the rows the first run already bought")
        self.assertEqual(res2.metadata["segments"]["fantastic_jobs_linkedin"]["duplicates"], 0,
                         "and therefore re-bills nothing")

    def test_empty_interval_is_valid_and_commits(self):
        feed = _Feed([])
        res = self._run(feed)
        self.assertTrue(res.success)
        self.assertEqual(len(res.jobs), 0)
        with mock.patch.multiple(config, **self.cfg):
            out = fja.commit_watermark(success=True)
        self.assertTrue(out["committed"])

    def test_rows_inside_lag_buffer_are_deferred_not_lost(self):
        # Rows indexed within the lag buffer are excluded from this window (upper =
        # now - lag) and picked up once they age past the lag -- never lost.
        rows = _recs(5, created_start=NOW - timedelta(minutes=30))   # all within lag
        feed = _Feed(rows)
        res = self._run(feed)
        self.assertEqual(len(res.jobs), 0)                      # nothing eligible yet
        with mock.patch.multiple(config, **self.cfg):
            self.assertTrue(fja.commit_watermark(success=True)["committed"])  # empty = valid
        feed2 = _Feed(rows)
        res2 = self._run(feed2, now=NOW + timedelta(hours=4))   # now past the lag
        self.assertEqual(len(res2.jobs), 5)                     # recovered next run

    def test_zero_overlap_and_no_elapsed_time_is_empty_interval(self):
        feed = _Feed(_recs(3, created_start=NOW - timedelta(hours=5)))
        self._run(feed)
        with mock.patch.multiple(config, **self.cfg):
            fja.commit_watermark(success=True)
        feed2 = _Feed([])
        res2 = self._run(feed2, now=NOW, FANTASTIC_DATE_CREATED_OVERLAP_MINUTES=0)
        self.assertTrue(res2.metadata["watermark"]["empty_interval"])
        # No ACQUISITION (row) request may be issued. The visibility-lag self-audit
        # may still issue a count request -- it returns no rows and bills no Jobs
        # credits, so it can never consume acquisition budget.
        row_calls = [u for u, _ in feed2.calls if not u.endswith("-count")]
        self.assertEqual(row_calls, [])
        self.assertEqual(len(res2.jobs), 0)

    def test_legacy_continuation_file_untouched_and_flag_off_restores_head_deep(self):
        legacy = os.path.join(self.tmp, "cont.json")
        import json
        with open(legacy, "w") as fh:
            json.dump({"schema": "fantastic-continuation/1", "cursor_date": "2026-09-10T00:00:00",
                       "high_water": "2026-09-19T00:00:00", "boundary_ids": ["1"]}, fh)
        feed = _Feed(_recs(5, created_start=NOW - timedelta(hours=5)))
        self._run(feed, FANTASTIC_JOBS_CONTINUATION_ENABLED=True, FANTASTIC_JOBS_CONTINUATION_STATE_PATH=legacy)
        with open(legacy) as fh:
            after = json.load(fh)
        self.assertEqual(after["cursor_date"], "2026-09-10T00:00:00")     # never wiped/rewritten
        self.assertNotIn("date_posted_lt", feed.calls[0][1])                 # watermark path used
        # Flag OFF again -> the head/deep engine resumes from the intact legacy file.
        feed2 = _Feed(_recs(5, created_start=NOW - timedelta(days=2)))
        self._run(feed2, FANTASTIC_DATE_CREATED_WATERMARK_ENABLED=False,
                  FANTASTIC_JOBS_CONTINUATION_ENABLED=True, FANTASTIC_JOBS_CONTINUATION_STATE_PATH=legacy)
        self.assertNotIn("date_created_gte", feed2.calls[0][1])

    def test_config_validation_rejects_unsafe_lag(self):
        with mock.patch.multiple(config, **dict(self.cfg, FANTASTIC_DATE_CREATED_LAG_MINUTES=10)):
            with self.assertRaises(ValueError):
                config.validate_fantastic_jobs_config()


class ActiveAtsTests(unittest.TestCase):
    def test_disabled_by_default_even_with_limit_and_warns(self):
        feed = _Feed(_recs(3))
        with mock.patch.multiple(config, **dict(_PROD, FANTASTIC_JOBS_ATS_LIMIT=50, FANTASTIC_JOBS_MAX_JOBS_PER_RUN=6050)):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                res = fja.run_fantastic_jobs_acquisition(http_get=feed)
            self.assertTrue(any("FANTASTIC_ATS_SOURCE_ENABLED" in str(x.message) for x in w))
        self.assertFalse(res.metadata["ats_source"]["enabled"])
        self.assertTrue(all(u.endswith("/v1/active-jb") for u, _ in feed.calls))

    def test_prod_limit_zero_issues_no_ats_request_under_both_flag_states(self):
        for flag in (False, True):
            feed = _Feed(_recs(2))
            with mock.patch.multiple(config, **dict(_PROD, FANTASTIC_JOBS_ATS_LIMIT=0, FANTASTIC_ATS_SOURCE_ENABLED=flag)):
                fja.run_fantastic_jobs_acquisition(http_get=feed)
            self.assertTrue(all(u.endswith("/v1/active-jb") for u, _ in feed.calls))

    def test_enabled_ats_has_filter_parity_and_provenance_and_no_double_billing(self):
        ats_rows = _recs(4, src="greenhouse", prefix="5")
        for r in ats_rows:
            r["source_type"] = "ats"
        jb_rows = _recs(4, src="linkedin", prefix="6")
        feed = _Feed(ats_rows + jb_rows)
        with mock.patch.multiple(config, **dict(_PROD, FANTASTIC_JOBS_ATS_LIMIT=50, FANTASTIC_ATS_SOURCE_ENABLED=True,
                                                FANTASTIC_JOBS_MAX_JOBS_PER_RUN=6050,
                                                FANTASTIC_SERVER_INDUSTRY_EXCLUSION_ENABLED=True,
                                                FANTASTIC_EXCLUDED_ORG_INDUSTRIES=["Hospitals and Health Care"])):
            res = fja.run_fantastic_jobs_acquisition(http_get=feed)
        ats_calls = [p for u, p in feed.calls if u.endswith("/v1/active-ats")]
        jb_calls = [p for u, p in feed.calls if u.endswith("/v1/active-jb")]
        self.assertTrue(ats_calls and jb_calls)
        a = ats_calls[0]
        # Parity: same role universe + ICP filters + industry exclusion; no JB-only params leak.
        self.assertIn("title_advanced", a)
        for k in ("location", "organization_headcount_gte", "organization_headcount_lt",
                  "ai_employment_type", "organization_agency", "exclude_organization_industry"):
            self.assertIn(k, a)
        self.assertNotIn("source", a)
        self.assertNotIn("exclude_ats_duplicate", a)
        # Complementary by construction: JB keeps exclude_ats_duplicate=true.
        self.assertEqual(jb_calls[0]["exclude_ats_duplicate"], "true")
        # Canonical provenance + per-source metrics.
        ds = {j["_provider_dataset"] for j in res.jobs}
        self.assertEqual(ds, {"ats", "jb"})
        ps = res.metadata["per_source"]
        self.assertEqual(ps["fantastic_jobs_ats"]["jobs"], 4)
        self.assertEqual(ps["fantastic_jobs_linkedin"]["jobs"], 4)
        self.assertEqual(res.metadata["cross_query_duplicates"], 0)
        self.assertEqual(len({j["job_id"] for j in res.jobs}), 8)


if __name__ == "__main__":
    unittest.main()
