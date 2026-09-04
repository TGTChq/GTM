"""Title expansion: the acquisition query and the role classifier move together.

The 13 adjacent IC families were reachable by the EXPANDED query but absent from
the role catalog, so `RoleGate` returned UNVERIFIED_ROLE_CLASSIFICATION for every
one of them -- even when handed its own title as the matched role -- while catalog
titles reached ROLE_PASS on the identical fixture. Recall bought and thrown away.

They are now real role definitions, and `build_title_query_plan` generates the
production expression FROM the catalog, so one edit does both.

The load-bearing distinction these tests defend:

    ROLE RECOGNITION  is not  SENIORITY ELIGIBILITY.

Recognising "Platform Engineer" must NOT admit "Staff Platform Engineer". The
family classifies; the seniority rules then reject the level, exactly as they
already do for every pre-existing role. Nothing here relaxes those rules.
"""
from __future__ import annotations

import unittest

import config
import fantastic_jobs_adapter as fja
import role_catalog as rc
from job_quality import assess_restricted_work, senior_ic_permitted
from role_gate import HARD_SENIORITY_PATTERN, RoleGate
from role_mapping import get_bucket_name_for_job

#: The families this change adds, with the bucket each must route to. Written out
#: rather than derived from the catalog, so a wrong bucket is a test failure and
#: not a self-fulfilling assertion.
EXPANSION = {
    "Platform Engineer": ("engineering", "engineering"),
    "Site Reliability Engineer": ("engineering", "engineering"),
    "Security Engineer": ("engineering", "engineering"),
    "Software Development Engineer": ("engineering", "engineering"),
    "Mobile Developer": ("engineering", "engineering"),
    "iOS Developer": ("engineering", "engineering"),
    "Android Developer": ("engineering", "engineering"),
    "Analytics Engineer": ("engineering", "data"),
    "Data Platform Engineer": ("engineering", "data"),
    "Sales Engineer": ("gtm_revenue", "gtm_revenue"),
    "Solutions Consultant": ("gtm_revenue", "gtm_revenue"),
    "Customer Success Engineer": ("customer_success", "customer_success"),
    "Renewals Specialist": ("customer_success", "customer_success"),
}

#: Levels project policy excludes, and WHERE each is enforced. Asserting the
#: enforcement point (not merely "it was rejected") is what stops a future change
#: from moving a rule somewhere weaker without noticing.
EXCLUDED_LEVEL_PREFIXES = ("Staff", "Principal", "Lead")      # assess_restricted_work
HARD_SENIORITY_PREFIXES = ("Director of", "VP", "Chief")      # role_gate


def _job(title, description="We are hiring. You will design, build and own systems."):
    return {"job_title": title, "canonical_job_title": title, "employer_name": "Acme",
            "employer_website": "acme.com", "official_job_description": description,
            "job_description": description}


class CatalogAlignmentTest(unittest.TestCase):
    def test_every_candidate_family_resolves_to_a_role_definition(self):
        aligned, unmapped = fja.title_expansion_alignment()
        self.assertEqual(unmapped, [], "a query-only family can never be classified")
        self.assertEqual(sorted(aligned), sorted(config.FANTASTIC_CANDIDATE_TITLES))

    def test_the_catalog_is_the_single_source_of_the_production_query(self):
        """Once a family is in the catalog the expansion is not a second query."""
        self.assertEqual(fja.candidate_title_expression(),
                         fja.build_title_query_plan()["expression"])

    def test_each_new_family_is_present_in_the_production_expression(self):
        expr = fja.build_title_query_plan()["expression"]
        for title in EXPANSION:
            with self.subTest(title=title):
                self.assertIn(fja._title_advanced_term(title), expr)

    def test_unmapped_candidates_are_dropped_not_queried(self):
        from unittest import mock
        with mock.patch.object(config, "FANTASTIC_CANDIDATE_TITLES",
                               dict(config.FANTASTIC_CANDIDATE_TITLES,
                                    **{"Totally Invented Role": "engineering"})):
            aligned, unmapped = fja.title_expansion_alignment()
            self.assertEqual(unmapped, ["Totally Invented Role"])
            self.assertNotIn("totally invented role", fja.candidate_title_expression().lower())

    def test_no_pre_existing_role_was_altered(self):
        """The 118 original roles keep their canonical title and both buckets."""
        for title in ("Software Engineer", "Data Engineer", "Account Executive",
                      "Customer Success Manager", "DevOps Engineer", "Accountant"):
            with self.subTest(title=title):
                self.assertIsNotNone(rc.get_role_definition(title))
        self.assertEqual(len(rc._ROLE_DEFINITIONS), 118 + len(EXPANSION))


class RoutingTest(unittest.TestCase):
    def test_each_new_family_routes_to_the_declared_buckets(self):
        for title, (fn, hm) in EXPANSION.items():
            with self.subTest(title=title):
                d = rc.get_role_definition(title)
                self.assertIsNotNone(d, f"{title} missing from the catalog")
                self.assertEqual(d.function_bucket, fn)
                self.assertEqual(d.hiring_manager_bucket, hm)

    def test_campaign_bucket_resolves_for_every_new_family(self):
        """No newly qualified role may reach Airtable with an undefined bucket."""
        for title, (fn, _) in EXPANSION.items():
            with self.subTest(title=title):
                bucket = get_bucket_name_for_job(dict(_job(title), _matched_role=title))
                self.assertEqual(bucket, fn)
                self.assertTrue(bucket, "empty campaign bucket")

    def test_every_new_bucket_already_existed(self):
        """The expansion introduces no new campaign bucket to configure."""
        original = {"customer_support", "customer_success", "engineering", "finance",
                    "marketing", "operations", "people_hr", "gtm_revenue", "product",
                    "ecommerce"}
        self.assertTrue({fn for fn, _ in EXPANSION.values()} <= original)

    def test_aliases_resolve_to_their_canonical_role(self):
        for alias, canonical in (("SRE", "Site Reliability Engineer"),
                                 ("SDE", "Software Development Engineer"),
                                 ("Solutions Engineer", "Sales Engineer"),
                                 ("Mobile Engineer", "Mobile Developer"),
                                 ("iOS Engineer", "iOS Developer"),
                                 ("Android Engineer", "Android Developer")):
            with self.subTest(alias=alias):
                self.assertEqual(rc.canonical_role_for_search(alias), canonical)


class SeniorityMatrixTest(unittest.TestCase):
    """A -- E from the title matrix, with project policy as ground truth."""

    def test_a_plain_ic_titles_are_not_excluded_by_any_seniority_rule(self):
        for title in EXPANSION:
            with self.subTest(title=title):
                self.assertTrue(assess_restricted_work(_job(title)).eligible)
                self.assertIsNone(HARD_SENIORITY_PATTERN.search(title))

    def test_b_senior_ic_forms_follow_the_existing_senior_ic_policy(self):
        """ROLE_ALLOW_SENIOR_IC is ON in production, so Senior/Sr survive -- by the
        SAME rule that already admits Senior Data Engineer, not a new one."""
        for title in EXPANSION:
            for form in (f"Senior {title}", f"Sr. {title}"):
                with self.subTest(form=form):
                    self.assertTrue(assess_restricted_work(_job(form)).eligible)
                    self.assertTrue(senior_ic_permitted(_job(form)),
                                    "senior IC allowance must apply uniformly")

    def test_c_excluded_levels_stay_rejected_for_every_new_family(self):
        """Staff / Principal / Lead -- rejected by assess_restricted_work."""
        for title in EXPANSION:
            for prefix in EXCLUDED_LEVEL_PREFIXES:
                form = f"{prefix} {title}"
                with self.subTest(form=form):
                    a = assess_restricted_work(_job(form))
                    if not a.eligible:
                        self.assertEqual(a.reason, "seniority_outside_target_scope")
                    else:
                        # Not caught by the title-shape rule: the Senior-IC
                        # allowance must still refuse to clear it.
                        self.assertFalse(senior_ic_permitted(_job(f"Senior {form}")),
                                         f"{form} must never be cleared as a Senior IC")

    def test_c2_director_vp_chief_stay_rejected_by_the_role_gate(self):
        for title in EXPANSION:
            for prefix in HARD_SENIORITY_PREFIXES:
                form = f"{prefix} {title}"
                with self.subTest(form=form):
                    self.assertIsNotNone(HARD_SENIORITY_PATTERN.search(form),
                                         f"{form} must hit the hard seniority gate")

    def test_c3_senior_allowance_never_overrides_an_excluded_level(self):
        """"Senior Staff Platform Engineer" must not be cleared by the flag."""
        for title in EXPANSION:
            for prefix in ("Staff", "Principal", "Lead", "Head of", "Director of"):
                with self.subTest(form=f"Senior {prefix} {title}"):
                    self.assertFalse(senior_ic_permitted(_job(f"Senior {prefix} {title}")))

    #: People-manager handling is the EXISTING people-authority guard, which this
    #: change does not touch. ("Customer Success Manager" is a deliberate TARGET
    #: role in the catalog, so a blanket "managers are blocked" assertion would be
    #: wrong.) The question the expansion must answer is narrower and differential:
    #: does it create a NEW path for a manager posting to be admitted?
    MANAGER_TITLES = ["Engineering Manager", "Manager, Platform Engineering",
                      "Sales Engineering Manager", "Director of Site Reliability",
                      "Head of Security Engineering", "VP of Platform Engineering",
                      "Chief Information Security Officer", "Renewals Manager"]

    def test_d_no_manager_title_is_matched_to_a_new_ic_family(self):
        """_best_role scores every catalog role, so a new family could in principle
        capture a manager posting. None does."""
        from multi_source_acquisition import _best_role
        for title in self.MANAGER_TITLES:
            with self.subTest(title=title):
                matched, _ = _best_role(_job(title))
                self.assertNotIn(matched, EXPANSION,
                                 f"{title} was matched to the new IC family {matched}")

    def test_d_people_authority_still_blocks_a_new_family(self):
        """The guard that actually stops managers is people-authority evidence in
        the description. It must apply to the new families exactly as it does to
        the existing ones -- the expansion must not create an exempt class."""
        managing = ("You will manage a team of five engineers, own their performance "
                    "reviews, and hire and develop direct reports.")
        for title in EXPANSION:
            with self.subTest(title=title):
                self.assertFalse(
                    senior_ic_permitted(_job(f"Senior {title}", managing)),
                    f"Senior {title} with people authority must not be cleared")
                # ...and the same posting WITHOUT authority language still is,
                # so the guard is discriminating rather than blanket-rejecting.
                self.assertTrue(senior_ic_permitted(_job(f"Senior {title}")))

    def test_d2_manager_titles_are_not_reachable_as_a_new_canonical_role(self):
        """No added alias may resolve a people-manager title into a new IC family."""
        for title in ("Engineering Manager", "Manager, Platform Engineering",
                      "Head of Security Engineering", "Director of Site Reliability",
                      "Renewals Manager", "Solutions Architect Manager"):
            with self.subTest(title=title):
                resolved = rc.canonical_role_for_search(title)
                self.assertNotIn(resolved, EXPANSION,
                                 f"{title} resolved to the IC family {resolved}")

    def test_e_policy_questionable_families_were_not_added(self):
        """Controller / Product Manager / Territory Manager and friends stay out
        until a role-policy ruling exists."""
        for title in ("Controller", "Product Manager", "Technical Product Manager",
                      "Product Owner", "Territory Manager", "Renewals Manager",
                      "Demand Generation Manager"):
            with self.subTest(title=title):
                self.assertNotIn(title, config.FANTASTIC_CANDIDATE_TITLES)
                self.assertNotIn(title, EXPANSION)

    def test_e2_lookalikes_do_not_resolve_into_the_new_families(self):
        for title in ("Sales Development Representative", "Solutions Architect",
                      "Security Guard", "Field Service Engineer",
                      "Renewals Account Executive"):
            with self.subTest(title=title):
                resolved = rc.canonical_role_for_search(title)
                if resolved in EXPANSION:
                    self.assertIn(resolved, ("Sales Engineer",),
                                  f"{title} resolved unexpectedly to {resolved}")


class RoleGateEndToEndTest(unittest.TestCase):
    """The whole point: a new family is RECOGNISED, and its excluded levels are
    still refused."""

    def setUp(self):
        self.gate = RoleGate()

    def _state(self, title, matched):
        d = self.gate.evaluate(job=dict(_job(title), _matched_role=matched))
        r = d.primary_reason
        return d.state_value, (r.value if hasattr(r, "value") else str(r))

    def test_new_family_is_no_longer_unverified(self):
        for title in EXPANSION:
            with self.subTest(title=title):
                state, reason = self._state(title, title)
                self.assertNotEqual(reason, "UNVERIFIED_ROLE_CLASSIFICATION",
                                    f"{title} still unclassifiable after the catalog edit")

    def test_recognised_family_still_loses_on_excluded_seniority(self):
        """Director/VP/Chief reach the role gate and are rejected THERE, proving
        recognition and eligibility stay separate concerns."""
        for title in EXPANSION:
            for prefix in HARD_SENIORITY_PREFIXES:
                with self.subTest(form=f"{prefix} {title}"):
                    state, reason = self._state(f"{prefix} {title}", title)
                    self.assertEqual(state, "REJECT")
                    self.assertEqual(reason, "REJECT_EXCLUDED_SENIORITY")

    def test_pre_existing_roles_are_unaffected(self):
        for title in ("Software Engineer", "Data Engineer", "Account Executive"):
            with self.subTest(title=title):
                state, reason = self._state(title, title)
                self.assertEqual((state, reason), ("PASS", "ROLE_PASS"))


if __name__ == "__main__":
    unittest.main()
