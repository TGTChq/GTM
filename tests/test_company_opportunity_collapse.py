"""Company-level opportunity collapse: at most ONE opportunity per employer.

Two invariants are load-bearing and are asserted directly:

  APOLLO_ENRICHED_OPPORTUNITIES_PER_COMPANY <= 1
  SEND_SAFE_LEADS_PER_COMPANY               <= 1

Both hold only if the collapse grouping is never FINER than the grouping the
enrichment stage itself uses (``hiring_manager.company_key_for_job``) -- if it
were, two elected representatives would be re-merged downstream and the same
employer would be enriched twice.
"""

from __future__ import annotations

import unittest

import config
import company_opportunity_collapse as COC
from hiring_manager import company_key_for_job


def job(job_id, *, slug="", website="", name="Acme Inc", title="Sales Operations Manager",
        posted="2026-08-20T00:00:00", **extra):
    row = {
        "job_id": job_id,
        "org_linkedin_slug": slug,
        "employer_website": website,
        "employer_name": name,
        "job_title": title,
        "_fantastic_date_posted": posted,
    }
    row.update(extra)
    return row


class IdentityResolutionTests(unittest.TestCase):
    def test_slug_alone_resolves_identity(self):
        self.assertTrue(COC.identity_resolved(job("a", slug="acme")))

    def test_domain_alone_resolves_identity(self):
        self.assertTrue(COC.identity_resolved(job("a", website="acme.com")))

    def test_bare_employer_name_does_not_resolve_identity(self):
        self.assertFalse(COC.identity_resolved(job("a")))

    def test_an_intermediary_host_is_not_a_company_identity_anchor(self):
        """A job-board / ATS host must never stand in for the employer."""
        intermediary = next(iter(config.INTERMEDIARY_JOB_DOMAINS))
        self.assertFalse(COC.identity_resolved(job("a", website=str(intermediary))))


class FailClosedTests(unittest.TestCase):
    def test_unresolved_identity_is_withheld_not_treated_as_its_own_company(self):
        rows = [job("a"), job("b", name="Other Co")]
        result = COC.collapse_company_opportunities(rows)
        self.assertEqual(result.representatives, [])
        self.assertEqual([r for _j, r in result.withheld],
                         [COC.UNRESOLVED_IDENTITY, COC.UNRESOLVED_IDENTITY])

    def test_unresolved_postings_are_counted_in_the_pre_collapse_company_total(self):
        """Hiding them would understate what the fail-closed rule actually costs."""
        result = COC.collapse_company_opportunities(
            [job("a", slug="acme"), job("b"), job("c", name="Zed")])
        self.assertEqual(result.metrics["unique_companies_before_collapse"], 3)
        self.assertEqual(result.metrics["identity_unresolved_withheld"], 2)


class GroupingTests(unittest.TestCase):
    def test_same_slug_different_domain_is_one_company(self):
        rows = [job("a", slug="acme", website="acme.com"),
                job("b", slug="acme", website="acme.io")]
        result = COC.collapse_company_opportunities(rows)
        self.assertEqual(len(result.representatives), 1)

    def test_same_domain_different_slug_is_one_company(self):
        rows = [job("a", slug="acme-us", website="acme.com"),
                job("b", slug="acme-global", website="acme.com")]
        result = COC.collapse_company_opportunities(rows)
        self.assertEqual(len(result.representatives), 1)

    def test_transitive_closure_links_three_postings_through_two_anchors(self):
        rows = [job("a", slug="acme", website="acme.com"),
                job("b", slug="acme", website="acme.io"),
                job("c", slug="acme-eu", website="acme.io")]
        result = COC.collapse_company_opportunities(rows)
        self.assertEqual(len(result.representatives), 1)
        self.assertEqual(result.metrics["jobs_suppressed_by_company_collapse"], 2)

    def test_genuinely_different_companies_are_not_merged(self):
        rows = [job("a", slug="acme", website="acme.com", name="Acme Inc"),
                job("b", slug="acmecorp", website="acmecorp.com", name="Acme Corp")]
        result = COC.collapse_company_opportunities(rows)
        self.assertEqual(len(result.representatives), 2)

    def test_similar_names_alone_never_merge_two_companies(self):
        """Both have trusted, DIFFERENT anchors; only the names look alike."""
        rows = [job("a", slug="northwind-tech", website="northwindtech.com",
                    name="Northwind Technologies"),
                job("b", slug="northwind-traders", website="northwindtraders.com",
                    name="Northwind Trading")]
        result = COC.collapse_company_opportunities(rows)
        self.assertEqual(len(result.representatives), 2)


class InvariantTests(unittest.TestCase):
    def test_representatives_are_unique_under_the_downstream_grouping_key(self):
        """The load-bearing invariant: enrichment cannot re-merge two winners."""
        rows = [
            job("a", slug="acme-us", website="acme.com"),
            job("b", slug="acme-eu", website="acme.com"),
            job("c", slug="beta", website="beta.com"),
            job("d", slug="beta-labs", website="beta.com"),
            job("e", slug="gamma", website="gamma.com"),
        ]
        result = COC.collapse_company_opportunities(rows)
        keys = [company_key_for_job(j) for j in result.representatives]
        self.assertEqual(len(keys), len(set(keys)), keys)
        self.assertEqual(len(result.representatives), 3)

    def test_one_opportunity_per_company_across_a_large_mixed_batch(self):
        rows = []
        for company in range(20):
            for opening in range(4):
                rows.append(job(f"c{company}-o{opening}", slug=f"co{company}",
                                website=f"co{company}.com", name=f"Company {company}"))
        result = COC.collapse_company_opportunities(rows)
        self.assertEqual(len(result.representatives), 20)
        self.assertEqual(result.metrics["jobs_suppressed_by_company_collapse"], 60)
        self.assertEqual(result.metrics["multi_job_companies"], 20)


class ElectionTests(unittest.TestCase):
    def test_a_posting_with_a_trusted_domain_beats_one_without(self):
        rows = [job("a", slug="acme"), job("b", slug="acme", website="acme.com")]
        result = COC.collapse_company_opportunities(rows)
        self.assertEqual(result.representatives[0]["job_id"], "b")

    def test_stronger_role_match_wins_when_identity_is_equal(self):
        rows = [job("a", slug="acme", website="acme.com",
                    _role_relevance_status="review", _role_relevance_score=3),
                job("b", slug="acme", website="acme.com",
                    _role_relevance_status="accept", _role_relevance_score=9)]
        result = COC.collapse_company_opportunities(rows)
        self.assertEqual(result.representatives[0]["job_id"], "b")

    def test_a_passing_role_gate_outranks_an_unverified_one(self):
        rows = [job("a", slug="acme", website="acme.com", _role_gate_state="UNVERIFIED"),
                job("b", slug="acme", website="acme.com", _role_gate_state="PASS")]
        result = COC.collapse_company_opportunities(rows)
        self.assertEqual(result.representatives[0]["job_id"], "b")

    def test_newest_posting_wins_when_everything_else_ties(self):
        rows = [job("a", slug="acme", website="acme.com", posted="2026-08-01T00:00:00"),
                job("b", slug="acme", website="acme.com", posted="2026-08-22T00:00:00")]
        result = COC.collapse_company_opportunities(rows)
        self.assertEqual(result.representatives[0]["job_id"], "b")

    def test_exact_tie_resolves_to_the_lowest_stable_job_id(self):
        rows = [job("z9", slug="acme", website="acme.com"),
                job("a1", slug="acme", website="acme.com")]
        self.assertEqual(
            COC.collapse_company_opportunities(rows).representatives[0]["job_id"], "a1")

    def test_election_is_independent_of_input_order(self):
        rows = [job("z9", slug="acme", website="acme.com"),
                job("a1", slug="acme", website="acme.com"),
                job("m5", slug="acme", website="acme.com")]
        first = COC.collapse_company_opportunities(rows).representatives[0]["job_id"]
        second = COC.collapse_company_opportunities(
            list(reversed(rows))).representatives[0]["job_id"]
        self.assertEqual(first, second)


class ProvenanceTests(unittest.TestCase):
    def test_suppressed_siblings_are_preserved_as_evidence_on_the_winner(self):
        rows = [job("a", slug="acme", website="acme.com", title="Sales Ops Manager"),
                job("b", slug="acme", website="acme.com", title="Revenue Analyst"),
                job("c", slug="acme", website="acme.com", title="Account Manager")]
        winner = COC.collapse_company_opportunities(rows).representatives[0]
        collapse = winner["_company_collapse"]
        self.assertEqual(collapse["represented_postings"], 3)
        self.assertEqual(collapse["suppressed_postings"], 2)
        self.assertEqual(len(collapse["related_job_titles"]), 2)

    def test_provenance_extends_rather_than_replaces_existing_related_ids(self):
        rows = [job("a", slug="acme", website="acme.com", related_job_ids=["legacy-1"]),
                job("b", slug="acme", website="acme.com")]
        winner = COC.collapse_company_opportunities(rows).representatives[0]
        self.assertIn("legacy-1", winner["related_job_ids"])
        self.assertIn("b", winner["related_job_ids"])

    def test_the_original_input_jobs_are_not_mutated(self):
        rows = [job("a", slug="acme", website="acme.com"),
                job("b", slug="acme", website="acme.com")]
        COC.collapse_company_opportunities(rows)
        self.assertNotIn("_company_collapse", rows[0])
        self.assertNotIn("_company_collapse", rows[1])


class MetricsTests(unittest.TestCase):
    def test_every_input_posting_is_accounted_for_exactly_once(self):
        rows = [job("a", slug="acme", website="acme.com"),
                job("b", slug="acme", website="acme.com"),
                job("c", slug="beta", website="beta.com"),
                job("d")]
        result = COC.collapse_company_opportunities(rows)
        self.assertEqual(len(result.representatives) + len(result.withheld), len(rows))
        m = result.metrics
        self.assertEqual(
            m["company_opportunities_after_collapse"]
            + m["jobs_suppressed_by_company_collapse"]
            + m["identity_unresolved_withheld"],
            m["input_postings"])

    def test_empty_input_produces_empty_output_without_error(self):
        result = COC.collapse_company_opportunities([])
        self.assertEqual(result.representatives, [])
        self.assertEqual(result.metrics["input_postings"], 0)


class AlreadyActiveTests(unittest.TestCase):
    """Never elect a candidate the existing-state dedupe will immediately drop."""

    @staticmethod
    def keys(job):
        return {f"fn:{job.get('_role_bucket')}"}

    def test_an_already_covered_opening_is_not_elected_when_a_free_one_exists(self):
        rows = [job("a", slug="acme", website="acme.com", _role_bucket="gtm_revenue",
                    _role_relevance_status="accept", _role_relevance_score=9),
                job("b", slug="acme", website="acme.com", _role_bucket="marketing",
                    _role_relevance_status="review", _role_relevance_score=2)]
        result = COC.collapse_company_opportunities(
            rows, suppressed_function_keys={"fn:gtm_revenue"},
            function_keys_for_job=self.keys)
        # 'a' is stronger but already covered; the employer is still worth one lead.
        self.assertEqual(result.representatives[0]["job_id"], "b")

    def test_a_fully_covered_employer_is_withheld_not_re_enriched(self):
        rows = [job("a", slug="acme", website="acme.com", _role_bucket="gtm_revenue"),
                job("b", slug="acme", website="acme.com", _role_bucket="gtm_revenue")]
        result = COC.collapse_company_opportunities(
            rows, suppressed_function_keys={"fn:gtm_revenue"},
            function_keys_for_job=self.keys)
        self.assertEqual(result.representatives, [])
        self.assertEqual(result.metrics["companies_already_active"], 1)
        self.assertEqual(result.metrics["postings_already_active"], 2)

    def test_accounting_stays_exact_when_some_openings_are_already_active(self):
        rows = [job("a", slug="acme", website="acme.com", _role_bucket="gtm_revenue"),
                job("b", slug="acme", website="acme.com", _role_bucket="marketing"),
                job("c", slug="acme", website="acme.com", _role_bucket="marketing")]
        result = COC.collapse_company_opportunities(
            rows, suppressed_function_keys={"fn:gtm_revenue"},
            function_keys_for_job=self.keys)
        self.assertEqual(len(result.representatives) + len(result.withheld), 3)
        m = result.metrics
        self.assertEqual(
            m["company_opportunities_after_collapse"]
            + m["jobs_suppressed_by_company_collapse"]
            + m["identity_unresolved_withheld"] + m["postings_already_active"],
            m["input_postings"])

    def test_no_suppression_set_means_no_behaviour_change(self):
        rows = [job("a", slug="acme", website="acme.com", _role_bucket="gtm_revenue"),
                job("b", slug="acme", website="acme.com", _role_bucket="marketing")]
        plain = COC.collapse_company_opportunities(rows)
        self.assertEqual(len(plain.representatives), 1)
        self.assertEqual(plain.metrics["postings_already_active"], 0)

    def test_a_key_helper_that_raises_never_suppresses_a_posting(self):
        def boom(_job):
            raise RuntimeError("key derivation exploded")

        rows = [job("a", slug="acme", website="acme.com", _role_bucket="gtm_revenue")]
        result = COC.collapse_company_opportunities(
            rows, suppressed_function_keys={"fn:gtm_revenue"}, function_keys_for_job=boom)
        self.assertEqual(len(result.representatives), 1)


class EnrichmentWiringTests(unittest.TestCase):
    def test_collapse_is_off_by_default(self):
        self.assertFalse(config.COMPANY_OPPORTUNITY_COLLAPSE_ENABLED)

    def test_a_collapse_failure_never_drops_qualified_inventory(self):
        """A defect in the collapse must degrade to 'enrich everything', not to
        'silently enrich nothing'."""
        import tempfile
        from pathlib import Path
        from orchestrator.adapters_real import RealEnrichmentStage

        stage = RealEnrichmentStage(workdir=tempfile.mkdtemp())
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "not.json"
            bad.write_text("{ this is not json", encoding="utf-8")
            self.assertEqual(stage._collapse_qualified(str(bad), Path(tmp)), str(bad))
        self.assertIsNone(stage.collapse)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
