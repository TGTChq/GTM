"""Proof that every explicit TGTC target title has a deterministic acquisition
path (item: acquisition coverage of the expanded target-title universe).

Coverage model:
- JSearch is title-queried: DEFAULT_ACQUISITION_ROLES are searched directly.
- ATS and free-feeds are BROAD-FETCH lanes: they pull every board listing / feed
  item with NO title filter, and every acquired posting is classified against the
  FULL role catalog during qualification. So any target title present in those
  sources is acquired and classified -- a deterministic acquisition path, not
  incidental classification.

The one acquisition-stage mechanism that could drop a target title from the
broad-fetch lanes is the title-exclusion filter (is_excluded_title). Therefore
coverage holds iff no target title is title-excluded. This test asserts exactly
that, and that the classification catalog covers every target title.
"""

from __future__ import annotations

import unittest

from jsearch_scraper import is_excluded_title
from role_catalog import (
    DEFAULT_ACQUISITION_ROLES,
    DEFAULT_SEARCH_ROLES,
    ROLE_DEFINITIONS,
)

#: Production always runs these broad-fetch lanes (railway.json --lanes).
BROAD_FETCH_LANES = ("ats", "free_feeds")


def _titles_without_coverage():
    """A target title has NO acquisition coverage only if it is dropped by the
    title-exclusion filter (removing it from the broad-fetch lanes too). Directly
    queried titles are trivially covered; all others are covered by the broad-fetch
    lanes unless title-excluded."""
    return [t for t in ROLE_DEFINITIONS if is_excluded_title(t)]


class AcquisitionCoverageTests(unittest.TestCase):
    def test_no_target_title_without_acquisition_coverage(self):
        self.assertEqual(_titles_without_coverage(), [])

    def test_classification_catalog_covers_every_target_title(self):
        # Every target title is classifiable, so a broad-fetch posting of that
        # title is kept, not dropped as an unsupported role.
        self.assertEqual(set(DEFAULT_SEARCH_ROLES), set(ROLE_DEFINITIONS))

    def test_acquisition_roles_are_valid_catalog_titles(self):
        self.assertTrue(set(DEFAULT_ACQUISITION_ROLES).issubset(set(ROLE_DEFINITIONS)))

    def test_head_titles_are_not_excluded(self):
        # "Head" is not a documented job exclusion; IC postings containing "Head"
        # must remain acquirable.
        for title in ("Head of Growth", "Head of Data", "Department Head"):
            self.assertFalse(is_excluded_title(title))

    def test_documented_exclusions_still_apply(self):
        for title in ("VP of Sales", "Director of Marketing", "Marketing Intern",
                      "Event Marketing Manager", "Field Marketing Specialist"):
            self.assertTrue(is_excluded_title(title))

    def test_every_target_title_has_intentional_jsearch_query(self):
        # Definitive high-volume config: every target title is a direct JSearch
        # query, so TARGET_TITLES_WITHOUT_INTENTIONAL_JSEARCH_QUERY_COVERAGE = 0.
        targets = set(ROLE_DEFINITIONS)
        queried = set(DEFAULT_ACQUISITION_ROLES)
        without_jsearch = targets - queried
        self.assertEqual(without_jsearch, set())

    def test_coverage_counts(self):
        targets = set(ROLE_DEFINITIONS)
        queried = set(DEFAULT_ACQUISITION_ROLES)
        self.assertEqual(len(targets), 118)
        self.assertEqual(len(queried), 118)          # all directly queried
        self.assertEqual(len(targets - queried), 0)  # 0 depend on broad feeds only
        self.assertEqual(len(_titles_without_coverage()), 0)


if __name__ == "__main__":
    unittest.main()
