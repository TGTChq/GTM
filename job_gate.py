"""Strict Job Gate built from official source evidence."""

from __future__ import annotations

from typing import Dict, Optional

from decision_types import GateDecision, GateState
from evidence_types import EvidenceBundle, EvidenceItem, EvidenceStatus, FactValue
from job_fact_extractor import extract_job_facts
from job_quality import assess_quality_guard, normalize_job_identity
from job_source_resolver import JobSourceResolver, ResolvedJobSource, title_materially_differs
from job_signal import classify_url_source
from domain_utils import normalize_company_domain
from reason_codes import ReasonCode


_EMPLOYMENT_REASONS = {
    "part_time": ReasonCode.REJECT_PART_TIME,
    "contract": ReasonCode.REJECT_CONTRACT,
    "fixed_term": ReasonCode.REJECT_FIXED_TERM,
    "fractional": ReasonCode.REJECT_FRACTIONAL,
    "temporary": ReasonCode.REJECT_TEMPORARY,
    "freelance": ReasonCode.REJECT_FREELANCE,
    "seasonal": ReasonCode.REJECT_SEASONAL,
    "internship": ReasonCode.REJECT_INTERNSHIP,
    "unpaid": ReasonCode.REJECT_NON_PAYING,
}
_ARRANGEMENT_REASONS = {
    "onsite_required": ReasonCode.REJECT_ONSITE_REQUIRED,
    "hybrid_required": ReasonCode.REJECT_HYBRID_REQUIRED,
    "local_presence_required": ReasonCode.REJECT_LOCAL_PRESENCE_REQUIRED,
    "field_work_required": ReasonCode.REJECT_FIELD_WORK_REQUIRED,
    "travel_required": ReasonCode.REJECT_TRAVEL_REQUIRED,
    "relocation_required": ReasonCode.REJECT_RELOCATION_REQUIRED,
}


class JobGate:
    def __init__(self, resolver: Optional[JobSourceResolver] = None):
        self.resolver = resolver or JobSourceResolver()

    def evaluate(self, job: Dict, *, fetch: Optional[bool] = None) -> GateDecision:
        candidate = dict(job)
        normalize_job_identity(candidate)
        local = assess_quality_guard(candidate)
        if not local.eligible:
            reason = _map_local_reason(local.reason)
            return GateDecision(
                "job", GateState.REJECT, reason,
                metadata={"local_reason": local.reason, "local_stat": local.stat_name},
                next_action="discard_and_replace",
            )

        source = self.resolver.resolve(candidate, fetch=fetch)
        if source.state == "INACTIVE_VERIFIED":
            return GateDecision(
                "job", GateState.REJECT, ReasonCode.REJECT_JOB_INACTIVE,
                metadata={"source": source.to_dict()}, next_action="discard_and_replace",
            )
        if source.state == "SOURCE_TEMPORARILY_UNAVAILABLE":
            return GateDecision(
                "job", GateState.UNVERIFIED, ReasonCode.UNVERIFIED_SOURCE_TIMEOUT,
                retryable=True, next_action="retry_source",
                metadata={"source": source.to_dict()},
            )
        company_domain = normalize_company_domain(candidate.get("employer_website") or "")
        resolved_source_type = classify_url_source(source.source_url, company_domain)
        trusted_corroborated_aggregator = bool(
            source.state == "ACTIVE_CORROBORATED"
            and source.source_type == "corroborated"
            and source.corroborated
        )
        if (
            source.state not in {"ACTIVE_VERIFIED", "ACTIVE_CORROBORATED", "ACTIVE_DIRECT_STRUCTURED"}
            or not source.corroborated
            or (resolved_source_type == "aggregator" and not trusted_corroborated_aggregator)
        ):
            source_payload = source.to_dict()
            source_payload["defense_source_type"] = resolved_source_type
            return GateDecision(
                "job", GateState.UNVERIFIED, ReasonCode.UNVERIFIED_OFFICIAL_SOURCE,
                retryable=source.retryable, next_action="retry_source_then_replace",
                metadata={"source": source_payload},
            )

        facts = extract_job_facts(candidate, source)
        bundle = EvidenceBundle(facts=facts)
        official_title = source.canonical_title
        discovery_title = str(candidate.get("job_title") or "")
        secondary = []
        if title_materially_differs(discovery_title, official_title):
            secondary.append(ReasonCode.TITLE_MATERIAL_MISMATCH)

        active = facts["active_status"]
        if not active.verified:
            return _unknown("job", ReasonCode.UNVERIFIED_JOB_STATUS, bundle, source)
        if active.value is False:
            if active.verified:
                return _reject(ReasonCode.REJECT_JOB_INACTIVE, bundle, source, secondary)
            return _unknown("job", ReasonCode.UNVERIFIED_JOB_STATUS, bundle, source, secondary)

        employment = facts["employment_type"]
        if employment.value in _EMPLOYMENT_REASONS:
            return _reject(_EMPLOYMENT_REASONS[employment.value], bundle, source, secondary)
        if not employment.verified or employment.value != "full_time":
            return _unknown("job", ReasonCode.UNVERIFIED_EMPLOYMENT_TYPE, bundle, source, secondary)
        # Full-time without an explicit fixed-term/contract contradiction is
        # sufficient. Most ATSs do not publish a separate "open ended" field.

        arrangement = facts["work_arrangement"]
        if arrangement.value in _ARRANGEMENT_REASONS:
            return _reject(_ARRANGEMENT_REASONS[arrangement.value], bundle, source, secondary)
        if not arrangement.verified or arrangement.value != "remote":
            return _unknown("job", ReasonCode.UNVERIFIED_WORK_ARRANGEMENT, bundle, source, secondary)

        market = facts["intent_market"]
        if market.value == "foreign_only":
            return _reject(ReasonCode.REJECT_NON_US_SCOPE, bundle, source, secondary)
        if not market.verified or market.value != "us_market":
            return _unknown("job", ReasonCode.UNVERIFIED_INTENT_MARKET, bundle, source, secondary)

        if facts["security_clearance"].value == "required":
            return _reject(ReasonCode.REJECT_SECURITY_CLEARANCE_REQUIRED, bundle, source, secondary)
        if facts["professional_license"].value == "required":
            return _reject(ReasonCode.REJECT_MANDATORY_PROFESSIONAL_LICENSE, bundle, source, secondary)
        if facts["physical_facility"].value == "required":
            return _reject(ReasonCode.REJECT_PHYSICAL_FACILITY_REQUIREMENT, bundle, source, secondary)

        return GateDecision(
            "job", GateState.PASS, "JOB_PASS", secondary,
            bundle, next_action="continue_to_role_gate",
            metadata={
                "source": source.to_dict(),
                "canonical_title": official_title,
                "signal_confidence": (
                    "official" if source.official else
                    "direct_structured" if source.state == "ACTIVE_DIRECT_STRUCTURED" else
                    "corroborated"
                ),
            },
        )

    def annotate(self, job: Dict, *, fetch: Optional[bool] = None) -> Dict:
        decision = self.evaluate(job, fetch=fetch)
        result = dict(job)
        source = decision.metadata.get("source") or {}
        result.update(
            {
                "_job_gate_state": decision.state_value,
                "_job_gate_reason": str(decision.primary_reason.value if hasattr(decision.primary_reason, "value") else decision.primary_reason),
                "_job_gate_secondary_reasons": [str(value.value if hasattr(value, "value") else value) for value in decision.secondary_reasons],
                "_job_gate_decision": decision.to_dict(),
                "canonical_job_title": source.get("canonical_title") or result.get("job_title"),
                "canonical_employer_name": source.get("canonical_employer") or result.get("employer_name"),
                "official_job_url": source.get("source_url") or "",
                "official_job_source_type": source.get("source_type") or "",
                "official_job_status": source.get("state") or "",
                "job_signal_confidence": (
                    "official" if source.get("official") else
                    "direct_structured"
                    if source.get("state") == "ACTIVE_DIRECT_STRUCTURED" else
                    "corroborated" if source.get("corroborated") else "unresolved"
                ),
                # Canonical aliases are retained for downstream consumers that
                # predate the explicit official_job_* field names.
                "canonical_source_url": source.get("source_url") or "",
                "canonical_source_type": source.get("source_type") or "",
                "canonical_active_status": (
                    "verified"
                    if source.get("state") in {"ACTIVE_VERIFIED", "ACTIVE_CORROBORATED"}
                    else "unverified_review"
                    if source.get("state") == "ACTIVE_DIRECT_STRUCTURED"
                    else "broken"
                    if source.get("state") == "INACTIVE_VERIFIED"
                    else "unverified_review"
                    if source.get("state")
                    else ""
                ),
                "official_job_description": source.get("description") or "",
            }
        )
        return result


def _cross_source_minor_check(job: Dict, source: ResolvedJobSource) -> bool:
    description = str(job.get("job_description") or "")
    return bool(
        source.temporarily_unavailable
        and len(description) >= 800
        and str(job.get("job_employment_type") or "").lower().replace("-", " ") in {"full time", "fulltime"}
        and job.get("job_is_remote") is True
        and str(job.get("job_country") or "").lower() in {"us", "usa", "united states"}
    )


def _map_local_reason(reason: str):
    lower = str(reason or "").lower()
    if "multi_job" in lower or "multi_role" in lower:
        return ReasonCode.REJECT_MULTI_JOB_ROUNDUP
    if "malformed" in lower:
        return ReasonCode.REJECT_MALFORMED_TITLE
    if "intern" in lower or "extern" in lower or "fellow" in lower or "apprent" in lower:
        return ReasonCode.REJECT_INTERNSHIP
    if "clearance" in lower or "federal" in lower or "government" in lower:
        return ReasonCode.REJECT_SECURITY_CLEARANCE_REQUIRED
    return ReasonCode.REJECT_UNRESOLVABLE_POSTING


def _reject(reason, bundle, source, secondary=None):
    return GateDecision(
        "job", GateState.REJECT, reason, list(secondary or []), bundle,
        next_action="discard_and_replace", metadata={"source": source.to_dict()},
    )


def _unknown(gate, reason, bundle, source, secondary=None):
    weak_provider_signal = any(
        str(getattr(fact.status, "value", fact.status)) == EvidenceStatus.WEAK_PROVIDER_SIGNAL.value
        for fact in bundle.facts.values()
    )
    retryable = bool(source.retryable or weak_provider_signal)
    return GateDecision(
        gate, GateState.UNVERIFIED, reason, list(secondary or []), bundle,
        retryable=retryable,
        next_action="retry_source_then_replace" if retryable else "discard_and_replace",
        metadata={"source": source.to_dict(), "weak_provider_signal": weak_provider_signal},
    )
