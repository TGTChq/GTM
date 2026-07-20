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


@dataclass
class HunterResult:
    found: bool
    email: Optional[str] = None
    status: Optional[str] = None
    score: Optional[int] = None
    source: Optional[str] = None


def _quota_exhausted(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if response is None or getattr(response, "status_code", None) != 429:
        return False
    body = str(getattr(response, "text", "") or "").lower()
    return any(token in body for token in (
        "billing period", "monthly quota", "quota exceeded",
        "request limit", "credits exhausted", "upgrade your plan",
    )) or isinstance(exc, (QuotaExhaustedError, RetryWindowTooLong))


def _disable_for_run(exc: Exception) -> bool:
    global _hunter_quota_exhausted_for_run
    if not _quota_exhausted(exc):
        return False
    _hunter_quota_exhausted_for_run = True
    logger.warning("Hunter quota exhausted; skipping Hunter for the remainder of this run.")
    return True


def verify_email(email: str) -> HunterResult:
    if not email or not config.HUNTER_API_KEY or _hunter_quota_exhausted_for_run:
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
    if _hunter_quota_exhausted_for_run or not all((first_name, last_name, domain, config.HUNTER_API_KEY)):
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
