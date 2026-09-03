"""The quality-guard fallback must not claim a source-resolution failure.

`JobGate.evaluate` runs `assess_quality_guard` BEFORE resolving the posting, so a
rejection from that guard has had no posting lookup. Reporting it as
REJECT_UNRESOLVABLE_POSTING asserts a cause the pipeline never tested, and in a
measured 4,231-record corpus that mislabel hid 704 business-rule rejections
(seniority, employer identity, staffing/RPO) behind a source-resolution name.

These tests pin the fallback and prove the four specific mappings are untouched.
"""

from job_gate import _map_local_reason
from reason_codes import ReasonCode


class TestUnmappedReasonsFallBackToQualityGuard:
    def test_unmapped_reason_is_quality_guard_other(self):
        assert _map_local_reason("some_new_rule") is ReasonCode.REJECT_QUALITY_GUARD_OTHER

    def test_fallback_is_not_unresolvable_posting(self):
        for reason in ("hidden_senior_role", "seniority_outside_target_scope",
                       "description_employer_identity_conflict",
                       "untrustworthy_employer_identity",
                       "insufficient_direct_employer_evidence",
                       "peo_service_delivery_role", "unresolvable_generic_employer"):
            assert _map_local_reason(reason) is not ReasonCode.REJECT_UNRESOLVABLE_POSTING, (
                f"{reason} is a quality-guard rule; it never had a posting resolved")

    def test_the_real_corpus_clusters_all_map_to_the_fallback(self):
        """The exact reasons behind the 704 mislabelled records."""
        for reason in ("hidden_senior_role", "seniority_outside_target_scope",
                       "description_employer_identity_conflict",
                       "untrustworthy_employer_identity",
                       "insufficient_direct_employer_evidence",
                       "intermediary_delivery_model:foo", "peo_service_delivery_role"):
            assert _map_local_reason(reason) is ReasonCode.REJECT_QUALITY_GUARD_OTHER

    def test_empty_and_none_reasons_do_not_crash(self):
        assert _map_local_reason("") is ReasonCode.REJECT_QUALITY_GUARD_OTHER
        assert _map_local_reason(None) is ReasonCode.REJECT_QUALITY_GUARD_OTHER


class TestExistingMappingsUnchanged:
    def test_multi_job_roundup(self):
        assert _map_local_reason("multi_job_posting") is ReasonCode.REJECT_MULTI_JOB_ROUNDUP
        assert _map_local_reason("multi_role_listing") is ReasonCode.REJECT_MULTI_JOB_ROUNDUP

    def test_malformed_title(self):
        assert _map_local_reason("malformed_title") is ReasonCode.REJECT_MALFORMED_TITLE

    def test_internship_family(self):
        for reason in ("internship_role", "externship", "fellowship_program",
                       "apprenticeship"):
            assert _map_local_reason(reason) is ReasonCode.REJECT_INTERNSHIP

    def test_clearance_and_government(self):
        # Exactly the reasons that carry a "clearance"/"federal"/"government"
        # token. Verified against the 4,231-record corpus, where these accounted
        # for the 346 REJECT_SECURITY_CLEARANCE_REQUIRED rejections.
        for reason in ("top_secret_clearance", "security_clearance_required",
                       "named_federal_agency_delivery", "direct_federal_delivery",
                       "named_federal_agency_reverse_delivery",
                       "government_or_public_sector_title", "public_sector_government_role"):
            assert _map_local_reason(reason) is ReasonCode.REJECT_SECURITY_CLEARANCE_REQUIRED

    def test_clearance_adjacent_reasons_keep_their_historical_fallback(self):
        # `public_trust_required` and `cleared_role` contain no clearance/federal/
        # government token, so they have ALWAYS taken the fallback branch -- in the
        # corpus they sat inside the 704, not the 346. They move from
        # REJECT_UNRESOLVABLE_POSTING to REJECT_QUALITY_GUARD_OTHER along with every
        # other unmapped reason; their routing through the function is unchanged.
        for reason in ("public_trust_required", "cleared_role"):
            assert _map_local_reason(reason) is ReasonCode.REJECT_QUALITY_GUARD_OTHER

    def test_precedence_is_unchanged_for_overlapping_reasons(self):
        # "intern" is checked before "clearance"; an overlapping string must keep
        # the historical winner so no existing rejection silently changes code.
        assert _map_local_reason("intern_with_clearance") is ReasonCode.REJECT_INTERNSHIP


class TestSourceResolutionCodesAreSeparate:
    def test_unresolvable_posting_still_exists_for_real_source_failures(self):
        # The code is retained -- it is still the right label when a posting truly
        # cannot be resolved. It is simply no longer the fallback for a guard that
        # never attempted resolution.
        assert ReasonCode.REJECT_UNRESOLVABLE_POSTING.value == "REJECT_UNRESOLVABLE_POSTING"

    def test_genuine_source_states_have_their_own_codes(self):
        for code in (ReasonCode.UNVERIFIED_OFFICIAL_SOURCE,
                     ReasonCode.UNVERIFIED_SOURCE_TIMEOUT,
                     ReasonCode.REJECT_JOB_INACTIVE):
            assert code is not ReasonCode.REJECT_QUALITY_GUARD_OTHER
