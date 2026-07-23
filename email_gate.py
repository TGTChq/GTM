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
        if hunter_status in {"invalid", "disposable", "webmail"}:
            return GateDecision(
                "email", GateState.UNVERIFIED, ReasonCode.UNVERIFIED_EMAIL_DELIVERABILITY,
                retryable=True, next_action="try_next_contact_or_email",
                metadata={"apollo_status": apollo_status, "hunter_status": hunter_status},
            )
        verified = apollo_status == "verified" or hunter_status == "valid"
        if not verified:
            # accept_all, risky, guessed, unavailable and unknown never surface.
            return GateDecision(
                "email", GateState.UNVERIFIED, ReasonCode.UNVERIFIED_EMAIL_DELIVERABILITY,
                retryable=True, next_action="try_next_contact_or_email",
                metadata={"apollo_status": apollo_status, "hunter_status": hunter_status},
            )
        bundle.add(FactValue(
            "professional_email", email, EvidenceStatus.VERIFIED_CROSS_SOURCE,
            [EvidenceItem("professional_email", email, EvidenceStatus.VERIFIED_CROSS_SOURCE, "apollo_hunter", excerpt=f"apollo={apollo_status}; hunter={hunter_status or 'not_run'}", confidence=0.98)]
        ))
        return GateDecision(
            "email", GateState.PASS, "EMAIL_PASS", evidence=bundle,
            next_action="final_decision",
            metadata={"apollo_status": apollo_status, "hunter_status": hunter_status},
        )
