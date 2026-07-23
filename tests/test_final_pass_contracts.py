import unittest

from decision_engine import decide
from decision_types import FinalState, GateDecision, GateState
from evidence_types import EvidenceBundle, EvidenceItem, EvidenceStatus, FactValue
from reason_codes import ReasonCode


class FinalPassContractTests(unittest.TestCase):
    def test_final_pass_requires_every_gate_to_pass(self):
        gates = {
            name: GateDecision(name, GateState.PASS, f"{name.upper()}_PASS")
            for name in ("job", "account", "role", "contact", "email")
        }
        result = decide(gates)
        self.assertEqual(result.state, FinalState.FINAL_PASS)
        self.assertTrue(result.counts_toward_target)
        self.assertEqual(result.airtable_relevance, "accept")

    def test_unknown_dominates_all_positive_gates(self):
        gates = {
            "job": GateDecision("job", GateState.PASS, "JOB_PASS"),
            "account": GateDecision(
                "account", GateState.UNVERIFIED, ReasonCode.UNVERIFIED_EMPLOYEE_COUNT
            ),
            "role": GateDecision("role", GateState.PASS, "ROLE_PASS"),
            "contact": GateDecision("contact", GateState.PASS, "CONTACT_PASS"),
            "email": GateDecision("email", GateState.PASS, "EMAIL_PASS"),
        }
        result = decide(gates)
        self.assertEqual(result.state, FinalState.UNVERIFIED)
        self.assertFalse(result.counts_toward_target)
        self.assertIsNone(result.airtable_relevance)

    def test_needs_check_never_counts(self):
        gates = {
            "job": GateDecision(
                "job",
                GateState.NEEDS_CHECK,
                ReasonCode.NEEDS_CHECK_ATS_TEMPORARILY_UNAVAILABLE,
            ),
            "account": GateDecision("account", GateState.PASS, "ACCOUNT_PASS"),
            "role": GateDecision("role", GateState.PASS, "ROLE_PASS"),
            "contact": GateDecision("contact", GateState.PASS, "CONTACT_PASS"),
            "email": GateDecision("email", GateState.PASS, "EMAIL_PASS"),
        }
        result = decide(gates)
        self.assertEqual(result.state, FinalState.NEEDS_CHECK)
        self.assertFalse(result.counts_toward_target)
        self.assertIsNone(result.airtable_relevance)

    def test_reject_has_precedence_over_reroute(self):
        gates = {
            "job": GateDecision("job", GateState.REJECT, ReasonCode.REJECT_FIXED_TERM),
            "contact": GateDecision(
                "contact", GateState.REROUTE, ReasonCode.REROUTE_TERRITORY_MISMATCH
            ),
        }
        self.assertEqual(decide(gates).state, FinalState.REJECT)

    def test_fact_requires_official_or_cross_source_to_be_verified(self):
        weak = FactValue(
            "employment_type",
            "full_time",
            EvidenceStatus.WEAK_PROVIDER_SIGNAL,
            [EvidenceItem("employment_type", "full_time", EvidenceStatus.WEAK_PROVIDER_SIGNAL, "jsearch")],
        )
        self.assertFalse(weak.verified)
        official = FactValue("employment_type", "full_time", EvidenceStatus.VERIFIED_OFFICIAL)
        self.assertTrue(official.verified)


if __name__ == "__main__":
    unittest.main()
