"""Quality-preserving HM recovery -- mechanism A (second-pass title broadening).

Proves: OFF => no second pass; ON => exactly one broadened second search WITHIN the
same function when the first yields no match; recovered candidates still pass the
normal ranker; the broadened set never contains junior/IC titles and never crosses
functions; second pass is skipped once the first pass already found someone.
"""

import types
import unittest
from unittest.mock import patch

import config
import hiring_manager
import role_mapping
from role_mapping import (
    get_broadened_target_titles_for_jobs,
    get_target_titles_for_jobs,
    BUCKET_BROADENED_TITLES,
    BUCKET_TITLES,
)


def _org(n=200):
    return types.SimpleNamespace(employee_count=n)


def _jobs(bucket_role="VP Information Technology"):
    # _matched_role drives get_hiring_manager_bucket_for_job.
    return [{"_matched_role": bucket_role, "job_title": bucket_role}]


class BroadenedTitleMapTests(unittest.TestCase):
    def test_broadened_is_superset_and_same_function_no_junior(self):
        jobs = _jobs()
        base = get_target_titles_for_jobs(jobs, 200)
        broad = get_broadened_target_titles_for_jobs(jobs, 200)
        self.assertTrue(set(base).issubset(set(broad)))            # never removes
        added = [t for t in broad if t not in base]
        self.assertTrue(added)                                     # actually broadens
        junk = ("coordinator", "associate", "intern", "assistant", "specialist",
                "entry", "junior", "clerk")
        for t in broad:
            self.assertFalse(any(j in t.lower() for j in junk), f"junior title leaked: {t}")

    def test_every_bucket_broadening_is_senior_only(self):
        for bucket, titles in BUCKET_BROADENED_TITLES.items():
            self.assertIn(bucket, BUCKET_TITLES)                   # real function bucket
            for t in titles:
                low = t.lower()
                self.assertTrue(
                    any(s in low for s in ("svp", "evp", "senior vice president",
                                           "head of", "vp ", "vp of", "general manager",
                                           "managing director", "director")),
                    f"{bucket}: {t} is not a recognized senior leader title")


class SecondPassHelperTests(unittest.TestCase):
    def _run(self, *, flag, first_ranked, second_people, second_ranked):
        stats = {}
        from collections import defaultdict
        stats = defaultdict(int)
        calls = {"search": 0}

        def fake_search(domain, titles):
            calls["search"] += 1
            return second_people

        def fake_rank(people, titles):
            return second_ranked if people is second_people else first_ranked

        with (
            patch.object(config, "HM_SECOND_PASS_TITLE_BROADENING", flag),
            patch.object(config, "APOLLO_RATE_LIMIT_DELAY", 0),
            patch.object(hiring_manager.apollo, "search_people_at_company", side_effect=fake_search),
            patch.object(hiring_manager, "rank_candidates", side_effect=fake_rank),
        ):
            base_titles = get_target_titles_for_jobs(_jobs(), 200)
            out_titles, out_ranked, out_people = hiring_manager._hm_second_pass(
                _jobs(), _org(), "acme.com", base_titles, first_ranked, ["p0"], stats)
        return out_titles, out_ranked, out_people, stats, calls

    def test_off_does_not_second_search(self):
        _, ranked, _, stats, calls = self._run(
            flag=False, first_ranked=[], second_people=["p1"], second_ranked=[{"title": "SVP Sales"}])
        self.assertEqual(ranked, [])
        self.assertEqual(calls["search"], 0)
        self.assertEqual(stats.get("hm_second_pass_attempts", 0), 0)

    def test_on_recovers_when_first_pass_empty(self):
        person = {"title": "SVP Marketing"}
        titles, ranked, people, stats, calls = self._run(
            flag=True, first_ranked=[], second_people=["p1"], second_ranked=[person])
        self.assertEqual(ranked, [person])                # recovered
        self.assertEqual(people, ["p1"])                  # switched to 2nd-pass people
        self.assertEqual(calls["search"], 1)              # exactly one extra search
        self.assertEqual(stats["hm_second_pass_attempts"], 1)
        self.assertEqual(stats["hm_second_pass_recovered"], 1)

    def test_on_but_first_pass_already_found_skips_second(self):
        first = [{"title": "VP Marketing"}]
        _, ranked, _, stats, calls = self._run(
            flag=True, first_ranked=first, second_people=["p1"], second_ranked=[{"title": "x"}])
        self.assertEqual(ranked, first)                   # unchanged
        self.assertEqual(calls["search"], 0)              # no second search
        self.assertEqual(stats.get("hm_second_pass_attempts", 0), 0)

    def test_on_but_second_pass_still_empty_keeps_miss(self):
        titles, ranked, people, stats, calls = self._run(
            flag=True, first_ranked=[], second_people=["p1"], second_ranked=[])
        self.assertEqual(ranked, [])                       # still a miss
        self.assertEqual(calls["search"], 1)               # tried once
        self.assertEqual(stats["hm_second_pass_attempts"], 1)
        self.assertEqual(stats.get("hm_second_pass_recovered", 0), 0)


if __name__ == "__main__":
    unittest.main()
