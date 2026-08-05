"""Dispositions and reason codes -- the shared vocabulary of the waterfall.

Every record that crosses a stage boundary carries a disposition and a primary
(optionally secondary) reason code. Reconciliation is only meaningful because
these are a closed set: a record is passed, rejected, deferred or errored, and
never two of those at once.
"""

from __future__ import annotations

from enum import Enum


class Disposition(str, Enum):
    """Terminal quality classification of an opportunity/contact.

    FINAL_PASS is the ONLY disposition that may satisfy the business target.
    NEEDS_CHECK / UNVERIFIED are reviewable and must never auto-approve.
    """

    FINAL_PASS = "FINAL_PASS"
    NEEDS_CHECK = "NEEDS_CHECK"
    UNVERIFIED = "UNVERIFIED"
    REJECT = "REJECT"
    REROUTE = "REROUTE"


#: Dispositions that are reviewable and can never satisfy the FINAL_PASS target
#: or be auto-approved for outbound.
REVIEWABLE = frozenset({Disposition.NEEDS_CHECK, Disposition.UNVERIFIED})


class StageOutcome(str, Enum):
    """The four mutually exclusive fates at a stage boundary.

    entered = passed + rejected + deferred + errored
    """

    PASSED = "passed"
    REJECTED = "rejected"
    DEFERRED = "deferred"
    ERRORED = "errored"


class ReasonCode(str, Enum):
    """Why a record took the fate it took. Instrumented at every material loss."""

    # -- pass-through
    OK = "ok"

    # -- acquisition
    DUPLICATE_IN_RUN = "duplicate_in_run"
    PREVIOUSLY_SEEN = "previously_seen"
    SUPPRESSED = "suppressed"
    MISSING_JOB_ID = "missing_job_id"

    # -- company
    COMPANY_UNRESOLVED = "company_unresolved"
    COMPANY_SIZE_REJECTED = "company_size_rejected"
    NOT_ICP = "not_icp"
    IN_CRM = "in_crm"

    # -- contact
    HIRING_MANAGER_NOT_FOUND = "hiring_manager_not_found"
    CONTACT_NOT_FOUND = "contact_not_found"
    EMAIL_UNVERIFIED = "email_unverified"
    DUPLICATE_CONTACT = "duplicate_contact"

    # -- gates
    NO_ACTIVE_HIRING_SIGNAL = "no_active_hiring_signal"
    NOT_SAME_DAY_ELIGIBLE = "not_same_day_eligible"

    # -- delivery
    ALREADY_DELIVERED = "already_delivered"
    ADAPTER_ERROR = "adapter_error"
    NOT_FINAL_PASS = "not_final_pass"

    # -- generic
    STAGE_ERROR = "stage_error"
    DEFERRED_BUDGET = "deferred_budget"
