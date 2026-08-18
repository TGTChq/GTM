"""Strict professional-email identity and deliverability gate."""

from __future__ import annotations

from typing import Optional, Set

import re

from apollo_client import PersonMatch
from company_identity import email_matches_company
from decision_types import GateDecision, GateState
from evidence_types import EvidenceBundle, EvidenceItem, EvidenceStatus, FactValue
from hunter_client import HunterResult
from reason_codes import ReasonCode


class EmailGate:
    def evaluate(
        self,
        *,
        person: PersonMatch,
        hunter_result: Optional[HunterResult],
        company_domains: Set[str],
    ) -> GateDecision:
        bundle = EvidenceBundle()
        email = str(person.email or "").strip().lower()
        if not email:
            return GateDecision(
                "email", GateState.UNVERIFIED, ReasonCode.UNVERIFIED_EMAIL,
                retryable=True, next_action="try_next_contact_or_email",
            )
        local_part = email.split("@", 1)[0]
        if re.fullmatch(
            r"(?:info|hello|contact|sales|support|careers|jobs|recruiting|hr|admin|office|team|marketing)",
            local_part,
            re.I,
        ):
            return GateDecision(
                "email", GateState.REROUTE, ReasonCode.UNVERIFIED_EMAIL,
                retryable=True, next_action="try_next_contact_or_email",
                metadata={"generic_mailbox": True},
            )
        if not email_matches_company(email, company_domains):
            return GateDecision(
                "email", GateState.REROUTE, ReasonCode.REROUTE_EMAIL_IDENTITY_MISMATCH,
                retryable=True, next_action="try_next_contact_or_email",
            )
        apollo_status = str(person.email_status or "").strip().lower()
        hunter_status = str(hunter_result.status if hunter_result else "").strip().lower()

        # Apollo is the SINGLE authority for email verification. An Apollo-
        # "verified", company-domain email PASSES on Apollo's evidence alone and
        # can NEVER be downgraded by Hunter -- whether Hunter is unavailable,
        # quota-exhausted, disabled, or even returns a contrary
        # "invalid"/"disposable"/"webmail" opinion. This check runs BEFORE any
        # Hunter-based reroute so Hunter is structurally incapable of blocking a
        # verified contact from reaching FINAL_PASS.
        if apollo_status == "verified":
            bundle.add(FactValue(
                "professional_email", email, EvidenceStatus.VERIFIED_CROSS_SOURCE,
                [EvidenceItem("professional_email", email, EvidenceStatus.VERIFIED_CROSS_SOURCE, "apollo", excerpt=f"apollo=verified; hunter={hunter_status or 'not_run'}", confidence=0.98)]
            ))
            return GateDecision(
                "email", GateState.PASS, "EMAIL_PASS", evidence=bundle,
                next_action="final_decision",
                metadata={"apollo_status": apollo_status, "hunter_status": hunter_status, "authority": "apollo"},
            )

        # Not Apollo-verified. Hunter, when present, is OPTIONAL corroboration
        # only and fails open: it may flag a hard-undeliverable address for
        # reroute, but it can NEVER promote a non-verified email to verified --
        # Apollo is authoritative, so "VERIFIED" means Apollo said verified.
        if hunter_status in {"invalid", "disposable"}:
            return GateDecision(
                "email", GateState.REROUTE, ReasonCode.UNVERIFIED_EMAIL_DELIVERABILITY,
                retryable=True, next_action="try_next_contact_or_email",
                metadata={"apollo_status": apollo_status, "hunter_status": hunter_status},
            )
        # A professional, company-domain email that Apollo did not verify
        # (unverified / extrapolated / likely-to-engage / unavailable) remains
        # operationally useful. Surface it for human review; it is never silently
        # upgraded to verified and never counts toward the FINAL_PASS KPI.
        return GateDecision(
            "email", GateState.NEEDS_CHECK, ReasonCode.UNVERIFIED_EMAIL_DELIVERABILITY,
            retryable=False, next_action="write_review",
            metadata={"apollo_status": apollo_status, "hunter_status": hunter_status},
        )
