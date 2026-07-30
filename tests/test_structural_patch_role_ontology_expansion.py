"""Phase 6 of FINAL_30_PLUS_SYSTEM_SPEC.md: role ontology and query expansion.

role_catalog.py is already the single, consistently-consumed source of truth
across acquisition (jsearch_scraper.py/multi_source_acquisition.py/config.py),
relevance (role_relevance.py), gates (role_gate.py/job_quality.py), buyer
selection (role_mapping.py), and reporting (role_focus.py) -- confirmed by
repo-wide import trace, not assumed. The actual gap per spec section 15 was
specific named service buckets with no representation at all: Marketing
Operations, Revenue Systems, Revenue Automation, Sales Automation, Technical
Operations. Added as aliases to the closest existing, already-classified role
rather than new standalone roles, per the spec's explicit warning not to add
broad title variants without false-positive testing.
"""
from __future__ import annotations

import unittest

from role_catalog import (
    canonical_role_for_search,
    get_function_bucket,
    get_hiring_manager_bucket,
)


class NewOntologyAliasesResolveCorrectly(unittest.TestCase):
    def test_revenue_systems_analyst_resolves_to_revenue_operations_analyst(self):
        self.assertEqual(
            canonical_role_for_search("Revenue Systems Analyst"), "Revenue Operations Analyst"
        )
        self.assertEqual(get_function_bucket("Revenue Systems Analyst"), "gtm_revenue")

    def test_revenue_systems_engineer_resolves_to_revenue_operations_analyst(self):
        self.assertEqual(
            canonical_role_for_search("Revenue Systems Engineer"), "Revenue Operations Analyst"
        )

    def test_revenue_automation_specialist_resolves_to_revenue_operations_analyst(self):
        self.assertEqual(
            canonical_role_for_search("Revenue Automation Specialist"), "Revenue Operations Analyst"
        )

    def test_sales_automation_specialist_resolves_to_sales_operations_analyst(self):
        self.assertEqual(
            canonical_role_for_search("Sales Automation Specialist"), "Sales Operations Analyst"
        )
        self.assertEqual(get_hiring_manager_bucket("Sales Automation Specialist"), "gtm_revenue")

    def test_marketing_operations_specialist_resolves_to_marketing_automation_specialist(self):
        self.assertEqual(
            canonical_role_for_search("Marketing Operations Specialist"),
            "Marketing Automation Specialist",
        )
        self.assertEqual(get_function_bucket("Marketing Operations Specialist"), "marketing")

    def test_marketing_operations_manager_resolves_to_marketing_automation_specialist(self):
        self.assertEqual(
            canonical_role_for_search("Marketing Operations Manager"),
            "Marketing Automation Specialist",
        )

    def test_technical_operations_manager_resolves_to_devops_engineer(self):
        self.assertEqual(
            canonical_role_for_search("Technical Operations Manager"), "DevOps Engineer"
        )
        self.assertEqual(get_function_bucket("Technical Operations Manager"), "engineering")

    def test_technical_operations_specialist_resolves_to_devops_engineer(self):
        self.assertEqual(
            canonical_role_for_search("Technical Operations Specialist"), "DevOps Engineer"
        )


class NewAliasesDoNotCollideWithExistingRoles(unittest.TestCase):
    """The module already self-validates at import time (a duplicate alias
    raises RuntimeError) -- these tests confirm the specific new aliases
    don't silently shadow an unrelated existing canonical title."""

    def test_new_aliases_are_distinct_from_every_canonical_title(self):
        import role_catalog

        new_aliases = {
            "revenue systems analyst", "revenue systems engineer", "revenue automation specialist",
            "sales automation specialist", "marketing operations specialist",
            "marketing operations manager", "technical operations manager", "technical operations specialist",
        }
        canonical_titles_normalized = {
            role_catalog._normalize_title(title) for title in role_catalog.ROLE_DEFINITIONS
        }
        for alias in new_aliases:
            normalized = role_catalog._normalize_title(alias)
            if normalized in canonical_titles_normalized:
                self.fail(f"New alias {alias!r} collides with an existing canonical title")


if __name__ == "__main__":
    unittest.main()
