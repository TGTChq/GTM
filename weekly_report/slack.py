"""Slack delivery of the weekly report, via a single Incoming Webhook.

Scope is deliberately tiny: one HTTP POST of the *already generated* human
summary. No OAuth, no Socket Mode, no event subscriptions, no bot user. The
webhook URL is the only credential and it is treated as a secret throughout --
never printed, never written to an artifact, never placed in a receipt, and
scrubbed out of any error text before that text reaches a log.

Three properties matter more than the feature itself:

1. **It can never break the pipeline.** Every failure mode -- missing webhook,
   timeout, 4xx, 5xx, malformed response, an exception nobody predicted --
   resolves to a returned :class:`SlackDelivery` describing the failure. This
   module raises nothing at all to its caller.

2. **It never double-sends.** Success is recorded as a receipt file written
   atomically *after* Slack confirms, so a restart that finds a report without a
   receipt retries delivery, and one that finds both skips it. The receipt is the
   only source of truth for "delivered"; a 2xx that we failed to record is
   re-sent, which is the safe direction to err in for a weekly digest.

3. **It is bounded.** Its own small time budget, separate from the reporter's, so
   a hanging Slack cannot eat the reporting window.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

#: The only credential this feature has. Secret; see module docstring.
ENV_WEBHOOK = "SLACK_WEEKLY_REPORT_WEBHOOK_URL"

#: Receipt written beside the report once, and only once, Slack has accepted it.
RECEIPT_SUFFIX = ".slack_sent.json"
RECEIPT_SCHEMA = "tgtc-weekly-report-slack-receipt/1"

STATUS_SENT = "sent"
STATUS_ALREADY_SENT = "already_sent"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_FAILED = "failed"
STATUS_DISABLED = "disabled"

#: Bounded and small on purpose: Slack gets its own budget so it can never spend
#: the reporter's (--max-seconds). Worst case here is roughly
#: attempts * timeout + backoffs, about 35s.
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 2.0

#: Retry only what can plausibly succeed on a second try. A 403/404 means the
#: webhook is wrong or revoked; retrying that is just noise.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Any Slack webhook, not just the configured one, must never reach a log.
_WEBHOOK_PATTERN = re.compile(r"https://hooks\.slack\.com/\S*", re.I)

#: Slack's own body for an accepted webhook post. Anything else on a 2xx is
#: surfaced (truncated) rather than assumed to mean success.
_SLACK_OK_BODY = "ok"

#: Slack silently truncates very long messages; keep well inside the block limit.
MAX_MESSAGE_CHARS = 38000


def redact(text: Any, webhook: str = "") -> str:
    """Scrub the webhook out of free text, then bound the length.

    A requests exception embeds the request URL, so the credential is one
    unhandled f-string away from a log line. Scrubbing is unconditional rather
    than applied where it "looks risky".
    """
    cleaned = str(text)
    if webhook:
        cleaned = cleaned.replace(webhook, "<redacted>")
    cleaned = _WEBHOOK_PATTERN.sub("<redacted>", cleaned)
    if len(cleaned) > 400:
        cleaned = cleaned[:400] + "... (truncated)"
    return cleaned


def webhook_from_env(env: Optional[Mapping[str, str]] = None) -> str:
    """The configured webhook, or ``""``. Never logged by this module."""
    source = os.environ if env is None else env
    return str(source.get(ENV_WEBHOOK, "") or "").strip()


def receipt_path_for(report_path: Path) -> Path:
    """``weekly_report_2026-W36.json`` -> ``weekly_report_2026-W36.slack_sent.json``."""
    return report_path.with_name(report_path.stem + RECEIPT_SUFFIX)


@dataclass
class SlackDelivery:
    """The outcome of one delivery attempt. Safe to print and to serialise."""

    status: str
    attempted: bool = False
    http_status: Optional[int] = None
    attempts: int = 0
    error: str = ""
    receipt_path: Optional[Path] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def delivered(self) -> bool:
        return self.status in (STATUS_SENT, STATUS_ALREADY_SENT)

    def describe(self) -> str:
        """One redacted line for an operator reading Railway logs."""
        if self.status == STATUS_SENT:
            return f"slack: delivered (HTTP {self.http_status}) after {self.attempts} attempt(s)"
        if self.status == STATUS_ALREADY_SENT:
            return "slack: already delivered for this window; not re-sending"
        if self.status == STATUS_NOT_CONFIGURED:
            return (
                f"slack: NOT delivered -- {ENV_WEBHOOK} is not set. The report files were "
                "written; set the webhook to enable delivery."
            )
        if self.status == STATUS_DISABLED:
            return "slack: delivery not requested"
        return (
            f"slack: NOT delivered after {self.attempts} attempt(s)"
            + (f" (HTTP {self.http_status})" if self.http_status is not None else "")
            + (f": {self.error}" if self.error else "")
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "status": self.status,
            "attempted": self.attempted,
            "http_status": self.http_status,
            "attempts": self.attempts,
        }
        if self.error:
            payload["error"] = self.error
        if self.receipt_path is not None:
            payload["receipt"] = str(self.receipt_path)
        if self.detail:
            payload["detail"] = dict(self.detail)
        return payload


def _write_receipt_atomically(path: Path, payload: Dict[str, Any]) -> None:
    """Temp file + ``os.replace``: a receipt is either absent or complete.

    A half-written receipt would read as "delivered" on the next run and silently
    suppress a report that never reached anyone.
    """
    from retrieval_measurement.artifacts import atomic_write_json  # noqa: PLC0415

    atomic_write_json(path, payload)


def build_payload(summary: str) -> Dict[str, str]:
    """The Slack request body. The already-rendered summary, nothing recomputed."""
    text = summary if len(summary) <= MAX_MESSAGE_CHARS else summary[:MAX_MESSAGE_CHARS] + "\n... (truncated)"
    return {"text": text}


def _default_poster(url: str, payload: Dict[str, str], timeout: float) -> Any:
    import requests  # noqa: PLC0415

    return requests.post(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )


def _response_parts(response: Any) -> tuple[Optional[int], str]:
    """``(status_code, body)`` from a response object or a plain mapping."""
    if isinstance(response, Mapping):
        status = response.get("status_code")
        body = response.get("text", "")
    else:
        status = getattr(response, "status_code", None)
        body = getattr(response, "text", "")
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    return status, str(body or "")


def deliver(
    summary: str,
    *,
    report_id: str,
    report_path: Path,
    window: Optional[Mapping[str, Any]] = None,
    webhook: Optional[str] = None,
    poster: Optional[Callable[..., Any]] = None,
    timeout: Optional[float] = None,
    max_attempts: Optional[int] = None,
    backoff: Optional[float] = None,
    sleeper: Optional[Callable[[float], None]] = None,
    now: Optional[datetime] = None,
    env: Optional[Mapping[str, str]] = None,
) -> SlackDelivery:
    """POST ``summary`` to the webhook, once per window. Never raises.

    ``report_path`` is the JSON report; the receipt is written beside it and is
    what makes a repeat invocation a no-op.
    """
    # Resolved at call time, not bound as default arguments, so the module
    # constants stay the single place the policy is tuned (and overridable).
    timeout = DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout
    max_attempts = DEFAULT_MAX_ATTEMPTS if max_attempts is None else max_attempts
    backoff = DEFAULT_BACKOFF_SECONDS if backoff is None else backoff

    receipt = receipt_path_for(Path(report_path))

    try:
        if receipt.exists():
            return SlackDelivery(
                status=STATUS_ALREADY_SENT, attempted=False, receipt_path=receipt
            )
    except OSError as exc:  # noqa: BLE001 - an unreadable directory is a gap, not a crash
        return SlackDelivery(
            status=STATUS_FAILED, attempted=False, error=redact(exc), receipt_path=receipt
        )

    url = webhook if webhook is not None else webhook_from_env(env)
    if not url:
        return SlackDelivery(status=STATUS_NOT_CONFIGURED, attempted=False)

    post = poster or _default_poster
    if sleeper is None:
        import time  # noqa: PLC0415

        sleeper = time.sleep

    payload = build_payload(summary)
    attempts = 0
    last_status: Optional[int] = None
    last_error = ""

    while attempts < max_attempts:
        attempts += 1
        try:
            response = post(url, payload, timeout)
            status, body = _response_parts(response)
            last_status = status
            if status is not None and 200 <= status < 300:
                # Slack answers "ok" on success. A 2xx with a different body is
                # reported rather than assumed good, but is still a delivery.
                if body.strip().lower() != _SLACK_OK_BODY:
                    last_error = f"unexpected 2xx body: {redact(body, url)}"
                sent_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
                receipt_payload = {
                    "schema": RECEIPT_SCHEMA,
                    "report_id": report_id,
                    "report_file": Path(report_path).name,
                    "sent_at": sent_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "http_status": status,
                    "attempts": attempts,
                    "delivery_status": STATUS_SENT,
                    "channel": "slack_incoming_webhook",
                }
                if window:
                    for key in ("reporting_window_start", "reporting_window_end", "reporting_window_label"):
                        if window.get(key):
                            receipt_payload[key] = window[key]
                try:
                    _write_receipt_atomically(receipt, receipt_payload)
                except Exception as exc:  # noqa: BLE001 - delivered, but unrecorded
                    return SlackDelivery(
                        status=STATUS_SENT,
                        attempted=True,
                        http_status=status,
                        attempts=attempts,
                        error=(
                            "delivered, but the receipt could not be written, so the next run "
                            f"will send again: {redact(exc, url)}"
                        ),
                    )
                return SlackDelivery(
                    status=STATUS_SENT,
                    attempted=True,
                    http_status=status,
                    attempts=attempts,
                    error=last_error,
                    receipt_path=receipt,
                )

            last_error = f"HTTP {status}: {redact(body, url)}" if status else "no HTTP status in response"
            if status is None or status not in _RETRYABLE_STATUS:
                break
        except Exception as exc:  # noqa: BLE001 - transport failures are a gap, not a crash
            last_error = f"{type(exc).__name__}: {redact(exc, url)}"

        if attempts < max_attempts:
            sleeper(backoff * attempts)

    return SlackDelivery(
        status=STATUS_FAILED,
        attempted=True,
        http_status=last_status,
        attempts=attempts,
        error=redact(last_error, url),
    )
