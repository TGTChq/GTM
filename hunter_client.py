"""Hunter email verification and fallback email finder."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import config
from http_utils import QuotaExhaustedError, RetryWindowTooLong, request_with_retry, safe_json

logger = logging.getLogger(__name__)
HUNTER_BASE_URL = "https://api.hunter.io/v2"
_hunter_quota_exhausted_for_run = False
_hunter_skipped_calls = 0


@dataclass
class HunterResult:
    found: bool
    email: Optional[str] = None
    status: Optional[str] = None
    score: Optional[int] = None
    source: Optional[str] = None


def reset_run_state() -> None:
    """Clear the run-level circuit breaker at the start of a fresh run.

    Hunter is an OPTIONAL corroboration provider, never a throughput
    dependency: once the breaker trips on quota exhaustion / 429 / provider
    unavailability, no further Hunter call is attempted for the rest of that
    run (no retry loop), and every remaining opportunity continues to be
    processed -- an Apollo-verified, company-domain-matched email still
    reaches EmailGate PASS on its own. This resets the breaker so a later run
    with restored quota attempts Hunter again.
    """
    global _hunter_quota_exhausted_for_run, _hunter_skipped_calls
    _hunter_quota_exhausted_for_run = False
    _hunter_skipped_calls = 0


def run_status() -> dict:
    """Observable Hunter state for the run summary. When
    ``quota_unavailable`` is True, ``skipped_reason`` is the explicit
    ``hunter_skipped_quota_unavailable`` marker the delivery/observability
    layer records; it never blocks pipeline continuity."""
    return {
        "quota_unavailable": bool(_hunter_quota_exhausted_for_run),
        "calls_skipped": int(_hunter_skipped_calls),
        "skipped_reason": "hunter_skipped_quota_unavailable" if _hunter_quota_exhausted_for_run else "",
    }


def _quota_exhausted(exc: Exception) -> bool:
    # An explicit quota/retry-window exception trips the breaker regardless of
    # whether it carries an HTTP response (previously this check was
    # unreachable because the response-None guard returned early first --
    # Phase 13 section 1 hardening).
    if isinstance(exc, (QuotaExhaustedError, RetryWindowTooLong)):
        return True
    response = getattr(exc, "response", None)
    if response is None or getattr(response, "status_code", None) != 429:
        return False
    body = str(getattr(response, "text", "") or "").lower()
    return any(token in body for token in (
        "billing period", "monthly quota", "quota exceeded",
        "request limit", "credits exhausted", "upgrade your plan",
    ))


def _disable_for_run(exc: Exception) -> bool:
    global _hunter_quota_exhausted_for_run
    if not _quota_exhausted(exc):
        return False
    _hunter_quota_exhausted_for_run = True
    logger.warning("Hunter quota exhausted; skipping Hunter for the remainder of this run.")
    return True


def verify_email(email: str) -> HunterResult:
    global _hunter_skipped_calls
    if not email or not config.HUNTER_API_KEY:
        return HunterResult(found=False)
    if _hunter_quota_exhausted_for_run:
        # Breaker already open this run: skip without any further call or
        # retry, and count it explicitly. The caller keeps the (already
        # available) Apollo email; it is never downgraded for Hunter's absence.
        _hunter_skipped_calls += 1
        return HunterResult(found=False)
    try:
        response = request_with_retry(
            "GET",
            f"{HUNTER_BASE_URL}/email-verifier",
            params={"email": email, "api_key": config.HUNTER_API_KEY},
            timeout=20,
        )
        data = safe_json(response).get("data") or {}
    except Exception as exc:
        if _disable_for_run(exc):
            return HunterResult(found=False)
        logger.error("Hunter verification failed for %s: %s", email, exc)
        raise

    return HunterResult(
        found=bool(data),
        email=email,
        status=(data.get("status") or "").lower() or None,
        score=data.get("score"),
        source="hunter_verifier",
    )


def find_email(first_name: str, last_name: str, domain: str) -> HunterResult:
    global _hunter_skipped_calls
    if not all((first_name, last_name, domain, config.HUNTER_API_KEY)):
        return HunterResult(found=False)
    if _hunter_quota_exhausted_for_run:
        _hunter_skipped_calls += 1
        return HunterResult(found=False)
    try:
        response = request_with_retry(
            "GET",
            f"{HUNTER_BASE_URL}/email-finder",
            params={
                "domain": domain,
                "first_name": first_name,
                "last_name": last_name,
                "api_key": config.HUNTER_API_KEY,
            },
            timeout=20,
        )
        data = safe_json(response).get("data") or {}
    except Exception as exc:
        if _disable_for_run(exc):
            return HunterResult(found=False)
        logger.error("Hunter finder failed for %s %s: %s", first_name, last_name, exc)
        raise

    email = data.get("email")
    verification = data.get("verification") or {}
    status = verification.get("status") or data.get("status")
    return HunterResult(
        found=bool(email),
        email=email,
        status=(status or "").lower() or None,
        score=data.get("score"),
        source="hunter_finder" if email else None,
    )
