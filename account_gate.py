"""Strict account identity, firmographic and business-model gate."""

from __future__ import annotations

from typing import Dict, Optional

import config
from apollo_client import OrgEnrichment
from business_model_classifier import classify_business_model
from company_identity import company_names_compatible, domains_equivalent, safe_company_domain
from company_source_resolver import CompanySourceResolver
from decision_types import GateDecision, GateState
from evidence_types import EvidenceBundle, EvidenceItem, EvidenceStatus, FactValue
from reason_codes import ReasonCode
from job_filter import normalize_text


def _excluded_industry_keyword(industry_norm: str) -> str | None:
    """Match Apollo taxonomy categories without broad substring false positives."""
    normalized = normalize_text(industry_norm)
    exact = {normalize_text(value): value for value in config.APOLLO_EXCLUDED_INDUSTRY_KEYWORDS}
    if normalized in exact:
        return exact[normalized]
    # Apollo occasionally appends a narrow qualifier after a canonical category.
    # Permit only delimiter-bounded extensions, never arbitrary occurrences.
    for key, original in exact.items():
        if normalized.startswith(key + " / ") or normalized.startswith(key + " - "):
            return original
    return None


class AccountGate:
    def __init__(self, resolver: Optional[CompanySourceResolver] = None):
        self.resolver = resolver or CompanySourceResolver()

    def evaluate(
        self,
        *,
        org: OrgEnrichment,
        input_company_name: str,
        input_domain: str,
        jobs: list[Dict],
        fetch_company: Optional[bool] = None,
    ) -> GateDecision:
        bundle = EvidenceBundle()
        canonical_domain = safe_company_domain(
            org.domain or input_domain, config.INTERMEDIARY_JOB_DOMAINS
        )
        canonical_name = str(org.name or input_company_name or "").strip()

        if not org.found:
            return self._unknown(ReasonCode.UNVERIFIED_ORGANIZATION, bundle, retryable=True)
        if not canonical_domain:
            return self._unknown(ReasonCode.UNVERIFIED_DOMAIN, bundle, retryable=False)
        input_safe = safe_company_domain(input_domain, config.INTERMEDIARY_JOB_DOMAINS)
        domain_matches = bool(input_safe and domains_equivalent(input_safe, canonical_domain))
        # A canonical domain match is stronger identity evidence than a brand-name
        # mismatch caused by a rebrand, parent company or legal suffix. Name-only
        # lookups remain conservative.
        if (
            input_company_name
            and canonical_name
            and not company_names_compatible(input_company_name, canonical_name)
            and not domain_matches
        ):
            return self._unknown(ReasonCode.UNVERIFIED_EMPLOYER_IDENTITY, bundle, retryable=False)
        if input_safe and not domain_matches:
            return self._unknown(ReasonCode.UNVERIFIED_EMPLOYER_IDENTITY, bundle, retryable=False)

        bundle.add(FactValue(
            "organization", canonical_name, EvidenceStatus.VERIFIED_CROSS_SOURCE,
            [EvidenceItem("organization", canonical_name, EvidenceStatus.VERIFIED_CROSS_SOURCE, "apollo", excerpt=canonical_name, confidence=0.95)]
        ))
        bundle.add(FactValue(
            "domain", canonical_domain, EvidenceStatus.VERIFIED_CROSS_SOURCE,
            [EvidenceItem("domain", canonical_domain, EvidenceStatus.VERIFIED_CROSS_SOURCE, "apollo_and_job", excerpt=canonical_domain, confidence=0.97)]
        ))

        if org.employee_count is None:
            return self._unknown(ReasonCode.UNVERIFIED_EMPLOYEE_COUNT, bundle, retryable=False)
        bundle.add(FactValue(
            "employee_count", org.employee_count, EvidenceStatus.VERIFIED_CROSS_SOURCE,
            [EvidenceItem("employee_count", org.employee_count, EvidenceStatus.VERIFIED_CROSS_SOURCE, "apollo", confidence=0.9)]
        ))
        if org.employee_count < config.MIN_EMPLOYEES:
            return self._reject(ReasonCode.REJECT_COMPANY_TOO_SMALL, bundle)
        if org.employee_count > config.MAX_EMPLOYEES:
            return self._reject(ReasonCode.REJECT_COMPANY_TOO_LARGE, bundle)

        industry = str(org.industry or "").strip()
        if not industry:
            return self._unknown(ReasonCode.UNVERIFIED_INDUSTRY, bundle, retryable=False)
        industry_norm = normalize_text(industry)
        excluded_industry = _excluded_industry_keyword(industry_norm)
        if excluded_industry:
            reason = ReasonCode.REJECT_EXCLUDED_INDUSTRY
            if excluded_industry in {"staffing and recruiting", "staffing", "recruiting", "human resources services"}:
                reason = ReasonCode.REJECT_STAFFING
            elif excluded_industry in {"hospital & health care", "hospitals and health care", "health care", "healthcare", "mental health care", "mental health", "medical practice"}:
                reason = ReasonCode.REJECT_HEALTHCARE
            elif excluded_industry == "government administration":
                reason = ReasonCode.REJECT_GOVERNMENT
            elif excluded_industry == "outsourcing/offshoring":
                reason = ReasonCode.REJECT_OUTSOURCING
            bundle.add(FactValue(
                "industry", industry, EvidenceStatus.VERIFIED_CROSS_SOURCE,
                [EvidenceItem("industry", industry, EvidenceStatus.VERIFIED_CROSS_SOURCE, "apollo", excerpt=industry, confidence=0.9)]
            ))
            return self._reject(reason, bundle, metadata={"excluded_industry_keyword": excluded_industry})
        bundle.add(FactValue(
            "industry", industry, EvidenceStatus.VERIFIED_CROSS_SOURCE,
            [EvidenceItem("industry", industry, EvidenceStatus.VERIFIED_CROSS_SOURCE, "apollo", excerpt=industry, confidence=0.82)]
        ))

        source = self.resolver.resolve(canonical_domain, fetch=fetch_company)
        raw = org.raw or {}
        apollo_description = " ".join(
            str(raw.get(key) or "")
            for key in ("short_description", "seo_description", "description", "keywords")
        ).strip()
        job_text = " ".join(
            str(job.get("official_job_description") or job.get("job_description") or "")
            for job in jobs
        )[:50_000]
        model = classify_business_model(
            company_text=source.text,
            apollo_industry=industry,
            apollo_description=apollo_description,
            source_url=f"https://{canonical_domain}",
            job_text=job_text,
        )
        if model.state == "EXCLUDED":
            reason = getattr(ReasonCode, model.reason_code, ReasonCode.REJECT_EXCLUDED_BUSINESS_MODEL)
            evidence_statuses = {
                item.status.value if hasattr(item.status, "value") else str(item.status)
                for item in model.evidence
            }
            model_status = (
                EvidenceStatus.VERIFIED_OFFICIAL
                if EvidenceStatus.VERIFIED_OFFICIAL.value in evidence_statuses
                else EvidenceStatus.VERIFIED_CROSS_SOURCE
            )
            bundle.add(FactValue("business_model", model.category, model_status, model.evidence))
            return self._reject(reason, bundle, metadata={"company_source": source.to_dict()})
        # Passing means that bounded first-party/Apollo evidence contained no
        # excluded-model signal. It is not a claim that the model itself was
        # positively verified, so do not manufacture cross-source evidence.
        allowed_evidence = list(model.evidence) or [EvidenceItem(
            "business_model_exclusion_check",
            "no_excluded_model_detected",
            EvidenceStatus.WEAK_PROVIDER_SIGNAL,
            "policy",
            confidence=0.75,
        )]
        bundle.add(FactValue(
            "business_model_exclusion_check",
            "no_excluded_model_detected",
            EvidenceStatus.WEAK_PROVIDER_SIGNAL,
            allowed_evidence,
        ))
        return GateDecision(
            "account", GateState.PASS, "ACCOUNT_PASS", evidence=bundle,
            next_action="continue_to_contact_gate",
            metadata={
                "canonical_company_name": canonical_name,
                "canonical_domain": canonical_domain,
                "employee_count": org.employee_count,
                "industry": industry,
                "business_model": model.category,
                "company_source": source.to_dict(),
            },
        )

    @staticmethod
    def _reject(reason, bundle, metadata=None):
        return GateDecision(
            "account", GateState.REJECT, reason, evidence=bundle,
            next_action="discard_and_replace", metadata=metadata or {},
        )

    @staticmethod
    def _unknown(reason, bundle, retryable=False, metadata=None):
        return GateDecision(
            "account", GateState.UNVERIFIED, reason, evidence=bundle,
            retryable=retryable,
            next_action="retry_account_fallbacks_then_replace" if retryable else "discard_and_replace",
            metadata=metadata or {},
        )
