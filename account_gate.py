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
        review_reasons: list[ReasonCode | str] = []
        input_safe = safe_company_domain(input_domain, config.INTERMEDIARY_JOB_DOMAINS)
        apollo_domain = safe_company_domain(
            org.domain or "", config.INTERMEDIARY_JOB_DOMAINS
        )
        canonical_domain = apollo_domain or input_safe
        canonical_name = str(org.name or input_company_name or "").strip()

        if not org.found:
            review_reasons.append(ReasonCode.UNVERIFIED_ORGANIZATION)
            canonical_name = str(input_company_name or canonical_name or "").strip()
            canonical_domain = input_safe or canonical_domain
        if not canonical_domain:
            return self._unknown(
                ReasonCode.UNVERIFIED_DOMAIN,
                bundle,
                retryable=False,
                metadata={
                    "canonical_company_name": canonical_name,
                    "canonical_domain": "",
                    "employee_count": org.employee_count,
                    "industry": str(org.industry or "").strip(),
                    "business_model": "unknown",
                },
            )

        domain_matches = bool(
            input_safe and apollo_domain and domains_equivalent(input_safe, apollo_domain)
        )
        name_matches = bool(
            not input_company_name
            or not canonical_name
            or company_names_compatible(input_company_name, canonical_name)
        )
        if input_safe and apollo_domain and not domain_matches:
            review_reasons.append(ReasonCode.UNVERIFIED_EMPLOYER_IDENTITY)
            canonical_domain = input_safe
            canonical_name = str(input_company_name or canonical_name).strip()
        elif not name_matches and not domain_matches:
            review_reasons.append(ReasonCode.UNVERIFIED_EMPLOYER_IDENTITY)
            canonical_domain = input_safe or canonical_domain
            canonical_name = str(input_company_name or canonical_name).strip()

        organization_status = (
            EvidenceStatus.VERIFIED_CROSS_SOURCE
            if org.found and name_matches
            else EvidenceStatus.WEAK_PROVIDER_SIGNAL
        )
        bundle.add(FactValue(
            "organization", canonical_name, organization_status,
            [EvidenceItem(
                "organization", canonical_name, organization_status,
                "apollo" if org.found else "job_input",
                excerpt=canonical_name,
                confidence=0.95 if organization_status == EvidenceStatus.VERIFIED_CROSS_SOURCE else 0.65,
            )]
        ))
        domain_status = (
            EvidenceStatus.VERIFIED_CROSS_SOURCE
            if domain_matches or (apollo_domain and not input_safe)
            else EvidenceStatus.WEAK_PROVIDER_SIGNAL
        )
        bundle.add(FactValue(
            "domain", canonical_domain, domain_status,
            [EvidenceItem(
                "domain", canonical_domain, domain_status,
                "apollo_and_job" if domain_status == EvidenceStatus.VERIFIED_CROSS_SOURCE else "job_input",
                excerpt=canonical_domain,
                confidence=0.97 if domain_status == EvidenceStatus.VERIFIED_CROSS_SOURCE else 0.7,
            )]
        ))

        if org.employee_count is None:
            review_reasons.append(ReasonCode.UNVERIFIED_EMPLOYEE_COUNT)
        else:
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
            review_reasons.append(ReasonCode.UNVERIFIED_INDUSTRY)
        else:
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
        metadata = {
            "canonical_company_name": canonical_name,
            "canonical_domain": canonical_domain,
            "employee_count": org.employee_count,
            "industry": industry,
            "business_model": model.category,
            "company_source": source.to_dict(),
            "review_reasons": [
                value.value if hasattr(value, "value") else str(value)
                for value in review_reasons
            ],
        }
        if review_reasons:
            return GateDecision(
                "account",
                GateState.NEEDS_CHECK,
                review_reasons[0],
                secondary_reasons=review_reasons[1:],
                evidence=bundle,
                next_action="continue_to_contact_gate_and_write_review",
                metadata=metadata,
            )
        return GateDecision(
            "account", GateState.PASS, "ACCOUNT_PASS", evidence=bundle,
            next_action="continue_to_contact_gate", metadata=metadata,
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
            "account", GateState.NEEDS_CHECK, reason, evidence=bundle,
            retryable=retryable,
            next_action="continue_to_contact_gate_and_write_review",
            metadata=metadata or {},
        )
