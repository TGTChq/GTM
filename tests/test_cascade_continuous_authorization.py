"""The alternate-contact ceiling counts ADVANCES, and 100 of them is a small run.

`ALTERNATE_CONTACT_MAX_ENRICHMENTS_PER_RUN` defaults to 100 and is unset in
production, so the default applies. It bounds how many buckets may move past their
first ranked candidate in a whole RUN -- not credits, not calls. The 2026-09-08 run
considered 1,506 companies and found 536 contacts; a hundred advances is spent long
before the candidates are, and every later bucket then stops at its first candidate
however much authorization Apollo has.

So an ABSENT operator limit now inherits continuous authorization, the same shape the
org-id fallback ceiling already uses. What that must NOT do is remove the things that
make advancing terminate, which is what the last group of tests is for: candidates are
walked by index and never revisited, and a bucket's paid matches stay capped.

No provider is contacted: `ci_no_network` blocks sockets and DNS for the whole run.
"""

import unittest
from unittest import mock

import config
import hiring_manager as hm


class TheCeilingCountsAdvancesNotCredits(unittest.TestCase):
    def setUp(self):
        hm.reset_alternate_contact_budget()

    def cfg(self, **over):
        base = dict(ALTERNATE_CONTACT_CASCADE_ENABLED=True,
                    ALTERNATE_CONTACT_MAX_ENRICHMENTS_PER_RUN=100,
                    ALTERNATE_CONTACT_BUDGET_CONFIGURED=False,
                    APOLLO_CONTINUOUS_MODE=False)
        base.update(over)
        return mock.patch.multiple(config, **base)

    def test_the_default_stops_the_run_at_a_hundred_advances(self):
        """The behaviour being corrected, stated as it stands today."""
        with self.cfg():
            for _ in range(100):
                self.assertTrue(hm._alternate_budget_available())
                hm._ALTERNATE_BUDGET["used"] += 1
            self.assertFalse(hm._alternate_budget_available(),
                             "bucket 101 onward stops at its first candidate")

    def test_continuous_mode_inherits_authorization_when_none_was_set(self):
        with self.cfg(APOLLO_CONTINUOUS_MODE=True):
            hm._ALTERNATE_BUDGET["used"] = 100
            self.assertTrue(hm._alternate_budget_available())
            hm._ALTERNATE_BUDGET["used"] = 5000
            self.assertTrue(hm._alternate_budget_available())

    def test_an_explicit_operator_limit_still_applies_in_continuous_mode(self):
        with self.cfg(APOLLO_CONTINUOUS_MODE=True,
                      ALTERNATE_CONTACT_BUDGET_CONFIGURED=True,
                      ALTERNATE_CONTACT_MAX_ENRICHMENTS_PER_RUN=40):
            hm._ALTERNATE_BUDGET["used"] = 39
            self.assertTrue(hm._alternate_budget_available())
            hm._ALTERNATE_BUDGET["used"] = 40
            self.assertFalse(hm._alternate_budget_available())

    def test_an_explicit_zero_still_disables_advancing(self):
        """Zero remains OFF. The fix must not be reachable by setting zero."""
        with self.cfg(APOLLO_CONTINUOUS_MODE=True,
                      ALTERNATE_CONTACT_BUDGET_CONFIGURED=True,
                      ALTERNATE_CONTACT_MAX_ENRICHMENTS_PER_RUN=0):
            self.assertFalse(hm._alternate_budget_available())

    def test_the_cascade_flag_still_gates_everything(self):
        with self.cfg(APOLLO_CONTINUOUS_MODE=True, ALTERNATE_CONTACT_CASCADE_ENABLED=False):
            self.assertFalse(hm._alternate_budget_available())

    def test_outside_continuous_mode_nothing_changes(self):
        with self.cfg(APOLLO_CONTINUOUS_MODE=False):
            hm._ALTERNATE_BUDGET["used"] = 100
            self.assertFalse(hm._alternate_budget_available())

    def test_advances_are_still_counted_when_the_ceiling_is_inherited(self):
        """Running without a run-level cap must not mean running without a count."""
        with self.cfg(APOLLO_CONTINUOUS_MODE=True):
            for _ in range(250):
                self.assertTrue(hm._alternate_budget_available())
                hm._ALTERNATE_BUDGET["used"] += 1
            self.assertEqual(hm.alternate_contact_budget_used(), 250)


class AdvancingStillTerminates(unittest.TestCase):
    """Why removing a RUN-level ceiling cannot loop.

    Two bounds survive it and are what actually stop a bucket: the ranked candidate
    list is walked by index so no candidate is revisited, and a bucket's paid matches
    are capped independently.
    """

    def setUp(self):
        hm.reset_alternate_contact_budget()
        hm.reset_paid_match_budget()

    def test_a_bucket_walks_its_candidates_by_index_and_never_revisits_one(self):
        source = open(hm.__file__, encoding="utf-8").read()
        self.assertIn("_has_more_candidates = _index + 1 < len(_considered)", source)
        self.assertIn("_more2 = _index2 + 1 < len(_considered2)", source)

    def test_the_per_bucket_paid_match_cap_is_independent_of_the_run_ceiling(self):
        with mock.patch.multiple(config, APOLLO_CONTINUOUS_MODE=True,
                                 ALTERNATE_CONTACT_CASCADE_ENABLED=True,
                                 ALTERNATE_CONTACT_BUDGET_CONFIGURED=False,
                                 APOLLO_MAX_PERSON_MATCH_ATTEMPTS_PER_BUCKET=3):
            self.assertTrue(hm._alternate_budget_available())
            self.assertEqual(config.APOLLO_MAX_PERSON_MATCH_ATTEMPTS_PER_BUCKET, 3,
                             "a bucket's paid attempts stay bounded")

    def test_an_overall_person_match_ceiling_still_stops_paid_work(self):
        """The cascade ceiling is not the only control on spend."""
        with mock.patch.multiple(config, APOLLO_MAX_PERSON_MATCH_CALLS_PER_RUN=2,
                                 APOLLO_CONTINUOUS_MODE=True):
            hm._PAID_MATCH_BUDGET["used"] = 2
            self.assertFalse(hm._paid_match_allowed(False))


class ThroughTheRealCompanyPath(unittest.TestCase):
    """`process_company` itself, over more buckets than the old ceiling allowed.

    The unit tests above establish the gate. This establishes that the gate is what
    was stopping real buckets: the production path is driven over 130 company x
    function buckets, with the run-level budget deliberately NOT reset between them,
    which is how a real run accumulates. It also shows the run ends -- 130 buckets
    produce exactly 130 advances and stop, not an unbounded walk.
    """

    ORG = None

    def _bucket(self, tmp_path, index, *, continuous):
        """One bucket through the real `process_company`, providers simulated."""
        from contextlib import ExitStack
        from apollo_client import OrgEnrichment, PersonMatch
        from decision_types import GateDecision, GateState

        org = OrgEnrichment(found=True, name="Acme", domain="acme.com",
                            employee_count=100, organization_id="org-acme",
                            industry="Software", raw={"description": "Accounting software"})
        account = GateDecision("account", GateState.PASS, "ACCOUNT_PASS", metadata={
            "canonical_domain": "acme.com", "canonical_company_name": "Acme",
            "business_model": "commercial_product_or_service"})
        job = {
            "job_id": f"job-{index}", "job_title": "Staff Accountant",
            "canonical_job_title": "Staff Accountant", "employer_name": "Acme",
            "canonical_employer_name": "Acme", "employer_website": "https://acme.com",
            "_employer_domain_input": "acme.com", "_matched_role": "Staff Accountant",
            "_search_role": "Staff Accountant", "_job_gate_state": "PASS",
            "_role_gate_state": "PASS",
            "_job_gate_decision": GateDecision("job", GateState.PASS, "JOB_PASS").to_dict(),
            "_role_gate_decision": GateDecision("role", GateState.PASS, "ROLE_PASS").to_dict(),
        }

        def person(payload):
            ident = payload["id"]
            return PersonMatch(
                True, person_id=ident, first_name="A", last_name=ident,
                title="Controller", organization_name="Acme",
                organization_domain="acme.com", email=f"{ident}-{index}@acme.com",
                email_status="verified", country="United States",
                linkedin_url=f"https://linkedin.com/in/{ident}",
                raw={"current_organization": {"name": "Acme", "domain": "acme.com"}})

        decisions = [
            GateDecision("contact", GateState.NEEDS_CHECK, "NEEDS_CHECK_CURRENT_EMPLOYMENT"),
            GateDecision("contact", GateState.PASS, "CONTACT_PASS"),
        ]
        candidates = [{"id": f"p{i}", "title": "Controller"} for i in range(len(decisions))]
        with ExitStack() as stack:
            stack.enter_context(mock.patch.multiple(
                config, REROUTE_STATE_FILE=str(tmp_path / f"reroute-{index}.json"),
                APOLLO_RATE_LIMIT_DELAY=0, HUNTER_RATE_LIMIT_DELAY=0, HUNTER_API_KEY="",
                APOLLO_CACHE_ENABLED=False, CONTACT_MAX_REROUTE_ATTEMPTS_PER_BUCKET=3,
                ALTERNATE_CONTACT_CASCADE_ENABLED=True,
                ALTERNATE_CONTACT_MAX_ENRICHMENTS_PER_RUN=100,
                ALTERNATE_CONTACT_BUDGET_CONFIGURED=False,
                APOLLO_CONTINUOUS_MODE=continuous,
                APOLLO_ORG_ID_ZERO_PEOPLE_FALLBACK_ENABLED=False))
            stack.enter_context(mock.patch.object(hm, "_cached_enrich_organization", return_value=org))
            stack.enter_context(mock.patch.object(hm, "_cached_verified_person", return_value=None))
            stack.enter_context(mock.patch.object(hm, "_remember_verified_person"))
            stack.enter_context(mock.patch.object(hm.AccountGate, "evaluate", return_value=account))
            stack.enter_context(mock.patch.object(hm.ContactGate, "evaluate", side_effect=decisions))
            stack.enter_context(mock.patch.object(
                hm.apollo, "search_people_at_company", return_value=candidates))
            stack.enter_context(mock.patch.object(hm.apollo, "match_person", side_effect=person))
            # DELIBERATELY no budget reset: a run accumulates across its buckets.
            _leads, stats = hm.process_company([job])
        return int(stats.get("alternate_cascade_advanced", 0) or 0)

    def _drive(self, count, *, continuous):
        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp())
        hm.reset_alternate_contact_budget()
        hm.reset_paid_match_budget()
        return sum(self._bucket(tmp, i, continuous=continuous) for i in range(count))

    def test_the_production_path_advances_past_a_hundred_and_still_terminates(self):
        advanced = self._drive(130, continuous=True)
        self.assertEqual(advanced, 130, "every bucket advanced; none stopped at 100")
        self.assertEqual(hm.alternate_contact_budget_used(), 130)

    def test_the_same_path_stops_at_a_hundred_without_continuous_mode(self):
        advanced = self._drive(130, continuous=False)
        self.assertEqual(advanced, 100, "the default ceiling bound the run")
        self.assertEqual(hm.alternate_contact_budget_used(), 100)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
