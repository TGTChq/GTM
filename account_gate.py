"""Strict account identity, firmographic and business-model gate."""

from __future__ import annotations

from typing import Dict, Optional

import config
from apollo_client import OrgEnrichment
from business_model_classifier import classify_business_model
from company_identity import (
    company_names_compatible,
    company_names_exactly_equal,
    domain_name_consistent,
    domains_equivalent,
    extract_domain_from_text,
    safe_company_domain,
)
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
        domain_recovery_reason = (
            "employer_website_verified" if input_safe
            else "apollo_org_domain_recovered" if apollo_domain
            else ""
        )

        if not org.found:
            review_reasons.append(ReasonCode.UNVERIFIED_ORGANIZATION)
            canonical_name = str(input_company_name or canonical_name or "").strip()
            canonical_domain = input_safe or canonical_domain

        # Last-resort fallback: an ATS-registry-sourced tenant-domain candidate
        # (e.g. a Workday tenant slug), only ever used as *search* evidence when
        # the registry's own company-name-vs-tenant compatibility check passed.
        # Never used to upgrade account state beyond NEEDS_CHECK -- it unlocks
        # person-search, it does not assert a verified account identity.
        if not canonical_domain:
            tenant_candidate = ""
            tenant_verified = False
            for job in jobs:
                candidate = str(job.get("_ats_tenant_domain_candidate") or "").strip()
                if candidate:
                    tenant_candidate = safe_company_domain(candidate, config.INTERMEDIARY_JOB_DOMAINS)
                    tenant_verified = bool(job.get("_ats_board_identity_verified"))
                    if tenant_candidate:
                        break
            if tenant_candidate and tenant_verified:
                canonical_domain = tenant_candidate
                domain_recovery_reason = "workday_tenant_domain_recovered"
                review_reasons.append(ReasonCode.UNVERIFIED_DOMAIN)

        # Further last-resort fallback: a company-branded URL already present
        # on the job record (official posting URL, apply link, or source
        # URL) whose host both clears the intermediary denylist AND is
        # name-consistent with the employer -- the denylist alone is not
        # sufficient (an unlisted third-party job board passes it just as
        # easily as a real corporate site; confirmed via direct evidence:
        # "Great Minds DC" hosted at californiaconstructores.com, "Corning"
        # on NC Biotech Center's board). Same search-only, NEEDS_CHECK-only
        # trust boundary as the Workday-tenant fallback above -- it unlocks
        # person-search, it never asserts a verified account identity.
        if not canonical_domain:
            for job in jobs:
                for url_field in ("official_job_url", "canonical_source_url", "job_apply_link"):
                    raw_url = str(job.get(url_field) or "").strip()
                    if not raw_url:
                        continue
                    candidate = safe_company_domain(raw_url, config.INTERMEDIARY_JOB_DOMAINS)
                    if candidate and domain_name_consistent(canonical_name or input_company_name, candidate):
                        canonical_domain = candidate
                        domain_recovery_reason = "company_url_domain_recovered"
                        review_reasons.append(ReasonCode.UNVERIFIED_DOMAIN)
                        break
                if canonical_domain:
                    break

        # Further last-resort fallback: a domain or contact email mentioned
        # in plain text on the job description itself (e.g. "visit us at
        # acme.com"), for companies with no usable URL anywhere on the
        # record at all. Confirmed via the 2026-07-29 corpus: recovers
        # companies (amcor, wipfli, samsara, toast, etc.) that the URL-based
        # fallback above cannot reach. Same denylist + name-consistency
        # double-check, same search-only/NEEDS_CHECK-only trust boundary.
        if not canonical_domain:
            for job in jobs:
                description = str(job.get("job_description") or "")
                if not description:
                    continue
                candidate = extract_domain_from_text(
                    description, canonical_name or input_company_name, config.INTERMEDIARY_JOB_DOMAINS
                )
                if candidate:
                    canonical_domain = candidate
                    domain_recovery_reason = "description_text_domain_recovered"
                    review_reasons.append(ReasonCode.UNVERIFIED_DOMAIN)
                    break

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
                    "domain_recovery_reason": "no_domain_evidence",
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

        # A name-only Apollo match with no domain to cross-check at all is a
        # single, uncorroborated signal -- not the "two independent sources
        # agree" confidence its VERIFIED_CROSS_SOURCE label implies elsewhere.
        # A short/common company name can collide with an unrelated org this
        # way (confirmed: an "Amazon" job posting matched Apollo's unrelated
        # 22-person "Amazon Group" and was auto-rejected as too-small --
        # ROOT_CAUSE_TABLE_STRUCTURAL.md row 7 / TECHNICAL_DESIGN.md D6). Route
        # the firmographic size check to human review instead of an
        # unrecoverable auto-reject in exactly this narrow situation; every
        # other reject path (domain-confirmed size, industry, business model,
        # staffing) is unaffected.
        exact_name_match = company_names_exactly_equal(input_company_name, canonical_name)
        domainless_name_only_match = bool(
            org.found and not input_safe and not domain_matches
            and name_matches and not exact_name_match
        )

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
                if domainless_name_only_match:
                    review_reasons.append(ReasonCode.UNVERIFIED_EMPLOYER_IDENTITY)
                else:
                    return self._reject(ReasonCode.REJECT_COMPANY_TOO_SMALL, bundle)
            elif org.employee_count > config.MAX_EMPLOYEES:
                if domainless_name_only_match:
                    review_reasons.append(ReasonCode.UNVERIFIED_EMPLOYER_IDENTITY)
                else:
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

        # Founding year is DELIBERATELY neutral for qualification (definitive
        # simplified ICP): it is enriched, persisted and shown in Airtable when
        # available, but it never rejects a company, never changes the
        # qualification state, and never enters suppression. Known-new,
        # known-old, and unknown are all treated identically here. (Record it as
        # evidence only when present, so Airtable can display it.)
        if org.founded_year is not None:
            bundle.add(FactValue(
                "founded_year", org.founded_year, EvidenceStatus.VERIFIED_CROSS_SOURCE,
                [EvidenceItem("founded_year", org.founded_year, EvidenceStatus.VERIFIED_CROSS_SOURCE,
                              "apollo", excerpt=str(org.founded_year), confidence=0.9)]
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
            "domain_recovery_reason": domain_recovery_reason,
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
