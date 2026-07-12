"""Regression tests for exhausted Apollo credits and long Retry-After windows."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

import apollo_client
import http_utils


def _response(status: int, body: str = "", retry_after: str | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = "https://api.apollo.io/api/v1/organizations/enrich"
    response._content = body.encode("utf-8")
    response.request = requests.Request("GET", response.url).prepare()
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    return response


class HttpRetryAfterGuardTests(unittest.TestCase):
    @patch("http_utils.time.sleep")
    @patch("http_utils.requests.request")
    def test_long_retry_after_fails_fast_without_sleeping(self, mocked_request, mocked_sleep):
        mocked_request.return_value = _response(429, "rate limited", "1900")

        with self.assertRaises(http_utils.RetryWindowTooLong) as ctx:
            http_utils.request_with_retry(
                "GET",
                "https://api.apollo.io/api/v1/organizations/enrich",
                max_retries=3,
            )

        self.assertEqual(ctx.exception.retry_after, 1900.0)
        mocked_sleep.assert_not_called()
        self.assertEqual(mocked_request.call_count, 1)


class ApolloCreditGuardTests(unittest.TestCase):
    @patch("apollo_client.request_with_retry")
    def test_credit_message_is_not_treated_as_record_level_422(self, mocked_request):
        error = requests.HTTPError(
            "HTTP 422",
            response=_response(
                422,
                "Your team's shared credits are used up. Ask your admin to buy more credits.",
            ),
        )
        mocked_request.side_effect = error

        with self.assertRaises(apollo_client.ApolloCreditsExhaustedError):
            apollo_client.enrich_organization(domain="avepoint.com")

        self.assertEqual(mocked_request.call_count, 1)

    @patch("apollo_client.request_with_retry")
    def test_long_retry_window_becomes_clear_apollo_error(self, mocked_request):
        response = _response(429, "too many requests", "1900")
        mocked_request.side_effect = http_utils.RetryWindowTooLong(
            "retry window too long", response=response, retry_after=1900.0
        )

        with self.assertRaises(apollo_client.ApolloCreditsExhaustedError):
            apollo_client.enrich_organization(domain="databricks.com")


if __name__ == "__main__":
    unittest.main()
