"""Shared HTTP helpers with bounded retries and rate-limit handling."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable

import requests

import config

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
MAX_SERVER_RETRY_AFTER_SECONDS = 60.0


class RetryWindowTooLong(requests.HTTPError):
    """Raised instead of sleeping for an impractically long server retry window."""

    def __init__(self, message: str, *, response: requests.Response, retry_after: float):
        super().__init__(message, response=response)
        self.retry_after = retry_after


def request_with_retry(
    method: str,
    url: str,
    *,
    headers: Dict[str, str] | None = None,
    params: Any = None,
    json_body: Dict[str, Any] | None = None,
    max_retries: int | None = None,
    timeout: int | None = None,
) -> requests.Response:
    retries = max_retries or config.MAX_HTTP_RETRIES
    timeout_value = timeout or config.REQUEST_TIMEOUT_SECONDS
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=timeout_value,
            )
            if response.status_code not in RETRYABLE_STATUS_CODES:
                response.raise_for_status()
                return response

            retry_after = response.headers.get("Retry-After")
            try:
                server_delay = float(retry_after) if retry_after is not None else None
            except (TypeError, ValueError):
                server_delay = None

            if (
                response.status_code == 429
                and server_delay is not None
                and server_delay > MAX_SERVER_RETRY_AFTER_SECONDS
            ):
                logger.error(
                    "HTTP 429 for %s requested a %.1fs retry window; failing fast "
                    "instead of blocking the pipeline.",
                    url,
                    server_delay,
                )
                raise RetryWindowTooLong(
                    f"HTTP 429 retry window too long ({server_delay:.1f}s): "
                    f"{response.text[:500]}",
                    response=response,
                    retry_after=server_delay,
                )

            delay = server_delay if server_delay is not None else min(2 ** attempt, 20)
            last_error = requests.HTTPError(
                f"Retryable HTTP {response.status_code}: {response.text[:500]}",
                response=response,
            )
            if attempt < retries:
                logger.warning("HTTP %s for %s; retrying in %.1fs", response.status_code, url, delay)
                time.sleep(delay)
        except RetryWindowTooLong:
            raise
        except requests.HTTPError as exc:
            last_error = exc
            status = exc.response.status_code if exc.response is not None else None
            if status not in RETRYABLE_STATUS_CODES:
                raise
            if attempt < retries:
                delay = min(2 ** attempt, 20)
                logger.warning("HTTP %s for %s; retrying in %.1fs", status, url, delay)
                time.sleep(delay)
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                delay = min(2 ** attempt, 20)
                logger.warning("Request failed for %s: %s; retrying in %.1fs", url, exc, delay)
                time.sleep(delay)

    if last_error:
        raise last_error
    raise RuntimeError(f"Request failed without an exception: {method} {url}")


def safe_json(response: requests.Response) -> Dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError(
            f"Expected JSON from {response.request.method} {response.url}; "
            f"got {response.text[:500]!r}"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object from {response.url}, got {type(data).__name__}")
    return data


def debug_dump(name: str, data: Dict[str, Any], redact_keys: Iterable[str] = ()) -> None:
    if not config.DEBUG_API_RESPONSES:
        return

    redact = {key.lower() for key in redact_keys}

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "[REDACTED]" if key.lower() in redact else scrub(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    path = Path(config.LOG_DIR) / f"debug_{name}.json"
    if path.exists():
        return
    path.write_text(json.dumps(scrub(data), indent=2), encoding="utf-8")
