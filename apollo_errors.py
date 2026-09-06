"""Apollo error classification and sanitized evidence.

HTTP 422 from Apollo's ``/organizations/enrich`` endpoint is a *validation*
failure whose body identifies the specific problem -- it is NOT evidence that the
account is out of credits. The production incident of 2026-08-06 aborted a full
run because a single record-level 422 (and, separately, a long 429 retry window)
was conflated with credit exhaustion. This module is the single, testable place
that decides what an Apollo failure actually is, and produces a secret-free
evidence record for it.

Nothing here makes a network call. The classifier reads only the response object
already attached to the raised exception.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

#: Explicit, unambiguous phrases Apollo uses when shared credits are exhausted.
#: A category is CREDIT_EXHAUSTED only when the body says so in these terms --
#: never merely because the status was 422/429.
CREDIT_MARKERS = (
    "shared credits",
    "credits are used up",
    "credits used up",
    "out of credits",
    "insufficient credits",
    "credit limit",
    "no credits remaining",
    "buy more credits",
    "you have run out of",
)

#: Response-body keys that could carry a secret echoed back by the provider.
#: They are dropped from any extracted message.
_SENSITIVE_KEYS = re.compile(
    r"(api[_-]?key|authorization|token|password|secret|cookie|x-api-key)",
    re.IGNORECASE,
)

_MAX_MESSAGE = 500


class ApolloErrorCategory(str, Enum):
    """The five operational categories a single Apollo failure can take."""

    AUTHORIZATION = "authorization"        # 401/403 -- key/scope problem, global
    RATE_LIMIT = "rate_limit"              # 429 / excessive retry window, global
    CREDIT_EXHAUSTED = "credit_exhausted"  # body explicitly says credits gone, global
    VALIDATION = "validation"              # 404/422 -- one record, not the account
    SERVER = "server"                      # 5xx / network -- retried, then per-record
    UNKNOWN = "unknown"                    # anything unrecognized -- per-record


#: Categories that mean Apollo is unusable for the WHOLE run (open the circuit and
#: stop making credit-consuming calls) rather than for a single record.
_GLOBAL_FATAL_CATEGORIES = frozenset(
    {
        ApolloErrorCategory.AUTHORIZATION,
        ApolloErrorCategory.RATE_LIMIT,
        ApolloErrorCategory.CREDIT_EXHAUSTED,
    }
)


@dataclass
class ApolloErrorClassification:
    """A sanitized, self-describing verdict about one Apollo failure."""

    category: ApolloErrorCategory
    status: Optional[int] = None
    error_code: Optional[str] = None
    message: str = ""
    retry_after: Optional[float] = None
    endpoint: str = ""
    #: Scalar facts Apollo attaches under ``error_details.context``. For a credit
    #: stop these are the whole diagnosis -- ``credit_type``, ``credit_balance``
    #: and ``next_billing_date`` say which pool ran out, that Apollo reads it as
    #: zero, and when the cycle resets. Dropping them is what forced a stop to be
    #: investigated by hand against the account.
    context: Dict[str, Any] = field(default_factory=dict)

    @property
    def global_fatal(self) -> bool:
        """True when the whole run should stop calling Apollo, not just this record."""
        return self.category in _GLOBAL_FATAL_CATEGORIES

    @property
    def record_level(self) -> bool:
        return not self.global_fatal

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category.value,
            "status": self.status,
            "error_code": self.error_code,
            "message": self.message,
            "retry_after": self.retry_after,
            "endpoint": self.endpoint,
            "global_fatal": self.global_fatal,
            "context": dict(self.context),
        }


def _response_of(exc: BaseException) -> Optional[requests.Response]:
    return getattr(exc, "response", None)


def _status_of(exc: BaseException) -> Optional[int]:
    response = _response_of(exc)
    if response is None:
        return None
    return getattr(response, "status_code", None)


def _endpoint_of(exc: BaseException) -> str:
    response = _response_of(exc)
    if response is None:
        return ""
    return str(getattr(response, "url", "") or "")


def _body_text(exc: BaseException) -> str:
    response = _response_of(exc)
    if response is None:
        return ""
    try:
        return response.text or ""
    except Exception:  # pragma: no cover - defensive
        return ""


def _sanitize_text(text: str) -> str:
    """Truncate and strip anything that looks like a secret key/value pair."""
    if not text:
        return ""
    # Redact ``"api_key": "..."``/``token=...`` style pairs the provider might echo.
    redacted = re.sub(
        r'("?[\w-]*(?:api[_-]?key|authorization|token|password|secret)"?\s*[:=]\s*)'
        r'("?)[^"\s,}]+("?)',
        r"\1\2[REDACTED]\3",
        text,
        flags=re.IGNORECASE,
    )
    return redacted[:_MAX_MESSAGE].strip()


def looks_like_credit_exhaustion(text: str) -> bool:
    body = (text or "").lower()
    return any(marker in body for marker in CREDIT_MARKERS)


def _extract_error_fields(body: str) -> Dict[str, Any]:
    """Pull ``error_code`` and a human message from Apollo's JSON body, sanitized.

    Falls back to the raw (sanitized) text when the body is not JSON.
    """
    fields: Dict[str, Any] = {}
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        data = None

    if isinstance(data, dict):
        # Apollo nests its machine-readable code under ``error_details``:
        #   {"error": "You have insufficient credits! <a href=...>Upgrade</a>...",
        #    "error_details": {"code": "BILLING.LIMIT.CREDITS_EXHAUSTED",
        #                      "message": "Your team has used all of its credits
        #                                  for this billing cycle."}}
        # Reading only the top level dropped the one field that says WHICH billing
        # control fired, so a credit stop reached the operator as our category and
        # nothing of Apollo's. ``error_details.message`` is preferred over
        # ``error`` for the same reason: it is the plain sentence, where ``error``
        # is an HTML upsell blob.
        details = data.get("error_details")
        details = details if isinstance(details, dict) else {}
        code = data.get("error_code") or data.get("code") or details.get("code")
        if code is not None:
            fields["error_code"] = _sanitize_text(str(code))
        message = (
            details.get("message")
            or data.get("error")
            or data.get("message")
            or data.get("error_message")
            or data.get("errors")
        )
        if message is not None:
            fields["message"] = _sanitize_text(
                message if isinstance(message, str) else json.dumps(message)
            )
        # ``context`` is a map of {name: {value, description}}. Keep the values
        # only, and only scalars -- a nested structure here is not evidence we
        # know how to read, and copying it wholesale risks carrying something
        # unsanitized into a log line.
        raw_context = details.get("context")
        if isinstance(raw_context, dict):
            extracted = {}
            for name, entry in raw_context.items():
                value = entry.get("value") if isinstance(entry, dict) else entry
                if isinstance(value, (str, int, float, bool)) or value is None:
                    extracted[_sanitize_text(str(name))] = (
                        _sanitize_text(value) if isinstance(value, str) else value)
            if extracted:
                fields["context"] = extracted
    if "message" not in fields:
        fields["message"] = _sanitize_text(body)
    return fields


def classify_apollo_error(exc: BaseException) -> ApolloErrorClassification:
    """Classify an Apollo failure into exactly one operational category.

    Ordering is deliberate: an *explicit* credit-exhaustion body wins over any
    status code, and an excessively long retry window (``RetryWindowTooLong``) is
    a rate-limit condition -- never credit exhaustion.
    """
    status = _status_of(exc)
    endpoint = _endpoint_of(exc)
    body = _body_text(exc)
    fields = _extract_error_fields(body)
    retry_after = getattr(exc, "retry_after", None)

    def build(category: ApolloErrorCategory) -> ApolloErrorClassification:
        return ApolloErrorClassification(
            category=category,
            status=status,
            error_code=fields.get("error_code"),
            message=fields.get("message", ""),
            retry_after=float(retry_after) if retry_after is not None else None,
            endpoint=endpoint,
            context=dict(fields.get("context") or {}),
        )

    # 1) Explicit credit exhaustion -- the only path to CREDIT_EXHAUSTED.
    if looks_like_credit_exhaustion(body):
        return build(ApolloErrorCategory.CREDIT_EXHAUSTED)

    # 2) An impractically long retry window is a rate-limit stop, not credits.
    #    (http_utils.RetryWindowTooLong carries ``retry_after``.)
    if retry_after is not None or type(exc).__name__ == "RetryWindowTooLong":
        return build(ApolloErrorCategory.RATE_LIMIT)

    # 3) Authorization / scope.
    if status in {401, 403}:
        return build(ApolloErrorCategory.AUTHORIZATION)

    # 4) Rate limit by status.
    if status == 429:
        return build(ApolloErrorCategory.RATE_LIMIT)

    # 5) Record-specific validation / unprocessable.
    if status in {404, 422}:
        return build(ApolloErrorCategory.VALIDATION)

    # 6) Server / network.
    if status is not None and status >= 500:
        return build(ApolloErrorCategory.SERVER)
    if isinstance(exc, requests.RequestException) and _response_of(exc) is None:
        return build(ApolloErrorCategory.SERVER)

    return build(ApolloErrorCategory.UNKNOWN)


def build_error_record(
    classification: ApolloErrorClassification,
    *,
    company_key: str = "",
    domain: str = "",
    retry_decision: str = "",
    final_outcome: str = "",
) -> Dict[str, Any]:
    """A secret-free evidence record for one classified Apollo failure."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "endpoint": classification.endpoint,
        "http_status": classification.status,
        "apollo_error_code": classification.error_code,
        "apollo_message": classification.message,
        "retry_after": classification.retry_after,
        "apollo_context": dict(classification.context),
        "company_key": company_key,
        "domain": domain,
        "classification": classification.category.value,
        "global_fatal": classification.global_fatal,
        "retry_decision": retry_decision,
        "final_outcome": final_outcome,
    }


def write_error_artifact(directory: str | Path, record: Dict[str, Any]) -> Optional[Path]:
    """Persist ``record`` under ``directory``. Best-effort: never raises.

    The record is already sanitized by :func:`build_error_record`; this function
    additionally never writes API keys, headers, or ownership tokens because it is
    only ever handed a built record, not raw response state.
    """
    try:
        base = Path(directory)
        base.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        classification = str(record.get("classification", "error"))
        path = base / f"apollo_error_{classification}_{stamp}.json"
        path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        return path
    except Exception:  # pragma: no cover - evidence writing is never fatal
        logger.debug("Could not persist Apollo error artifact", exc_info=True)
        return None
