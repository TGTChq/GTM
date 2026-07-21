"""Canonical role eligibility and coverability gate."""

from __future__ import annotations

import re
from typing import Dict

from decision_types import GateDecision, GateState
from evidence_types import EvidenceBundle, EvidenceItem, EvidenceStatus, FactValue
from reason_codes import ReasonCode
from role_catalog import canonical_role_for_search, get_role_definition
from role_mapping import get_bucket_name_for_job
from role_relevance import assess_role


SENIORITY_PATTERN = re.compile(
    r"\b(?:intern(?:ship)?|senior|sr\.?|director|vice president|vp)\b", re.I
)
PHYSICAL_TITLE_SPECIALIZATION = re.compile(
    r"\b(?:clinical|patient care|bedside|laboratory|lab technician|warehouse|plant|factory|field operations|plc|scada|industrial controls|manufacturing operations)\b",
    re.I,
)
PHYSICAL_REQUIREMENT_CONTEXT = re.compile(
    r"\b(?:you will|responsible for|must|required|this role|the position|day[- ]to[- ]day|program|operate|maintain|install|work in)\b"
    r"[^.\n]{0,180}\b(?:patient care|bedside|laboratory|warehouse|plant|factory|field work|field operations|plc|scada|industrial controls|manufacturing operations)\b"
    r"|\b(?:patient care|bedside|laboratory|warehouse|plant|factory|field work|field operations|plc|scada|industrial controls|manufacturing operations)\b"
    r"[^.\n]{0,120}\b(?:is required|required experience|must have|responsibilities include)\b",
    re.I,
)


class RoleGate:
    def evaluate(self, job: Dict) -> GateDecision:
        canonical_title = str(job.get("canonical_job_title") or job.get("job_title") or "")
        description = str(job.get("official_job_description") or job.get("job_description") or "")
        target = canonical_role_for_search(job.get("_matched_role") or job.get("_search_role") or "")
        evidence = EvidenceBundle()
        evidence.add(FactValue(
            "canonical_role_title", canonical_title, EvidenceStatus.VERIFIED_OFFICIAL,
            [EvidenceItem("canonical_role_title", canonical_title, EvidenceStatus.VERIFIED_OFFICIAL, "official_job", job.get("official_job_url") or "", canonical_title, 0.99)]
        ))

        if SENIORITY_PATTERN.search(canonical_title):
            return GateDecision(
                "role", GateState.REJECT, ReasonCode.REJECT_EXCLUDED_SENIORITY,
                evidence=evidence, next_action="discard_and_replace",
            )
        if (
            PHYSICAL_TITLE_SPECIALIZATION.search(canonical_title)
            or PHYSICAL_REQUIREMENT_CONTEXT.search(description)
        ):
            return GateDecision(
                "role", GateState.REJECT, ReasonCode.REJECT_ROLE_NOT_GLOBALLY_COVERABLE,
                evidence=evidence, next_action="discard_and_replace",
            )
        if not target or not get_role_definition(target):
            return GateDecision(
                "role", GateState.UNVERIFIED, ReasonCode.UNVERIFIED_ROLE_CLASSIFICATION,
                evidence=evidence, next_action="discard_and_replace",
            )
        assessment_job = dict(job)
        assessment_job["job_title"] = canonical_title
        assessment_job["job_description"] = description
        assessment = assess_role(assessment_job, target)
        evidence.add(FactValue(
            "role_assessment", {"target": target, "status": assessment.status, "score": assessment.score},
            EvidenceStatus.VERIFIED_CROSS_SOURCE,
            [EvidenceItem("role_assessment", assessment.status, EvidenceStatus.VERIFIED_CROSS_SOURCE, "role_classifier", excerpt=" | ".join(assessment.reasons), confidence=min(1.0, max(0.0, assessment.score / 8 if assessment.score >= 0 else 0.0)))]
        ))
        if assessment.status == "reject":
            return GateDecision(
                "role", GateState.REJECT, ReasonCode.REJECT_ROLE_MISMATCH,
                secondary_reasons=assessment.reasons, evidence=evidence,
                next_action="discard_and_replace",
            )
        if assessment.status == "review":
            return GateDecision(
                "role", GateState.UNVERIFIED, ReasonCode.UNVERIFIED_ROLE_CLASSIFICATION,
                secondary_reasons=assessment.reasons, evidence=evidence,
                next_action="discard_and_replace",
            )
        campaign_bucket = get_bucket_name_for_job(assessment_job)
        return GateDecision(
            "role", GateState.PASS, "ROLE_PASS", assessment.reasons,
            evidence, next_action="continue_to_account_gate",
            metadata={"canonical_role": target, "campaign_bucket": campaign_bucket},
        )

    def annotate(self, job: Dict) -> Dict:
        decision = self.evaluate(job)
        result = dict(job)
        result.update({
            "_role_gate_state": decision.state_value,
            "_role_gate_reason": str(decision.primary_reason.value if hasattr(decision.primary_reason, "value") else decision.primary_reason),
            "_role_gate_secondary_reasons": [str(v.value if hasattr(v, "value") else v) for v in decision.secondary_reasons],
            "_role_gate_decision": decision.to_dict(),
        })
        if decision.metadata.get("campaign_bucket"):
            result["_role_bucket"] = decision.metadata["campaign_bucket"]
        return result
