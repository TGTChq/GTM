"""Phase 8 of FINAL_30_PLUS_SYSTEM_SPEC.md: candidate identity, extended
beyond a bare Apollo provider ID.

Identity-key audit finding: a candidate with a LinkedIn URL or email but no
provider person ID was never tracked as "attempted" at all -- every
attempted_id_reasons/attempted_ids write site in hiring_manager.py guarded
on a non-empty raw id -- so it could be re-selected and re-attempted every
reroute round indefinitely. hiring_manager._candidate_identity_key() and
company_identity.canonical_candidate_key() close that gap while preserving
the existing raw-provider-ID key shape RerouteRegistry has always persisted
(reused directly by test_phase4_root_cause_corrections.py's
test_all_candidates_previously_attempted_counted, which must keep passing
unchanged).
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import hiring_manager
from apollo_client import OrgEnrichment
from company_identity import canonical_candidate_key
from decision_types import GateDecision, GateState
from reroute_state import RerouteRegistry


class CanonicalCandidateKeyTests(unittest.TestCase):
    def test_provider_person_id_is_highest_confidence_tier(self):
        self.assertEqual(
            canonical_candidate_key(provider_person_id="p1", linkedin_url="https://linkedin.com/in/x", email="a@b.com"),
            "pid:p1",
        )

    def test_linkedin_used_when_no_provider_id(self):
        self.assertEqual(
            canonical_candidate_key(linkedin_url="https://linkedin.com/in/janedoe/", email="a@b.com"),
            "li:https://linkedin.com/in/janedoe",
        )

    def test_email_used_when_no_id_or_linkedin(self):
        self.assertEqual(canonical_candidate_key(email="Jane@Acme.com"), "email:jane@acme.com")

    def test_name_plus_company_used_as_last_resort_before_unresolved(self):
        key = canonical_candidate_key(name="Jane Doe", company_key="domain:acme.com")
        self.assertEqual(key, "name:jane doe|domain:acme.com")

    def test_fully_unresolved_candidates_never_collide(self):
        first = canonical_candidate_key()
        second = canonical_candidate_key()
        self.assertTrue(first.startswith("unresolved:"))
        self.assertNotEqual(first, second)


def _finance_job() -> dict:
    return {
        "job_id": "j1",
        "job_title": "Staff Accountant",
        "canonical_job_title": "Staff Accountant",
        "employer_name": "Acme",
        "canonical_employer_name": "Acme",
        "employer_website": "https://acme.com",
        "_employer_domain_input": "acme.com",
        "_matched_role": "Staff Accountant",
        "_search_role": "Staff Accountant",
        "_job_gate_state": "PASS",
        "_role_gate_state": "PASS",
        "_job_gate_decision": GateDecision("job", GateState.PASS, "JOB_PASS").to_dict(),
        "_role_gate_decision": GateDecision("role", GateState.PASS, "ROLE_PASS").to_dict(),
    }


def _account_pass() -> GateDecision:
    return GateDecision(
        "account", GateState.PASS, "ACCOUNT_PASS",
        metadata={
            "canonical_domain": "acme.com",
            "canonical_company_name": "Acme",
            "business_model": "commercial_product_or_service",
        },
    )


class LinkedInFallbackTrackingTests(unittest.TestCase):
    """Previously: a candidate with no 'id'/'person_id' field was never
    recorded in RerouteRegistry regardless of outcome, so it would be
    re-attempted every round. Now it's tracked via its LinkedIn URL."""

    def _run(self, *, people, reroute_path):
        org = OrgEnrichment(
            found=True, name="Acme", domain="acme.com", employee_count=100,
            industry="Software", raw={"description": "Acme builds accounting software."},
        )
        with (
            patch.object(config, "REROUTE_STATE_FILE", reroute_path),
            patch.object(config, "APOLLO_RATE_LIMIT_DELAY", 0),
            patch.object(hiring_manager.apollo, "enrich_organization", return_value=org),
            patch.object(hiring_manager.AccountGate, "evaluate", return_value=_account_pass()),
            patch.object(hiring_manager.apollo, "search_people_at_company", return_value=people),
            patch.object(
                hiring_manager.apollo, "match_person",
                side_effect=RuntimeError("no email available"),
            ),
        ):
            return hiring_manager.process_company([_finance_job()])

    def test_candidate_with_only_linkedin_url_gets_tracked_and_filtered_on_replay(self):
        candidate = {
            "title": "Controller",
            "linkedin_url": "https://linkedin.com/in/janedoe",
            "organization": {"name": "Acme", "domain": "acme.com"},
        }
        with tempfile.TemporaryDirectory() as temp:
            reroute_path = f"{temp}/reroute.json"

            # First pass: candidate has no id, previously would vanish into
            # the void with no persisted tracking at all.
            self._run(people=[candidate], reroute_path=reroute_path)
            registry = RerouteRegistry(reroute_path)
            tracked = registry.attempted_ids("acme.com|finance")
            self.assertIn("li:https://linkedin.com/in/janedoe", tracked)

            # Second pass, same account/bucket: the same LinkedIn-identified
            # candidate must now be recognized as already attempted.
            leads, stats = self._run(people=[candidate], reroute_path=reroute_path)
            self.assertEqual(stats.get("bucket_all_candidates_previously_attempted", 0), 1)
            self.assertEqual(stats.get("person_match_attempts", 0), 0)


if __name__ == "__main__":
    unittest.main()
