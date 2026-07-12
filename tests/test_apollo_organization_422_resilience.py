"""Regression tests for company-level Apollo enrichment failures."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

import apollo_client


def _http_error(status_code: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://api.apollo.io/api/v1/organizations/enrich"
    return requests.HTTPError(f"HTTP {status_code}", response=response)


def _json_response(payload: dict) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.url = "https://api.apollo.io/api/v1/organizations/enrich"
    response._content = __import__("json").dumps(payload).encode("utf-8")
    response.headers["Content-Type"] = "application/json"
    response.request = requests.Request(
        "GET", response.url
    ).prepare()
    return response


class ApolloOrganizationEnrichmentResilienceTests(unittest.TestCase):
    @patch("apollo_client.request_with_retry")
    def test_422_retries_once_with_domain_only(self, mocked_request):
        mocked_request.side_effect = [
            _http_error(422),
            _json_response(
                {
                    "organization": {
                        "id": "org_123",
                        "name": "AvePoint",
                        "primary_domain": "avepoint.com",
                        "estimated_num_employees": 2000,
                    }
                }
            ),
        ]

        result = apollo_client.enrich_organization(
            domain="avepoint.com",
            name="AvePoint",
            website="https://avepoint.com",
        )

        self.assertTrue(result.found)
        self.assertEqual(result.domain, "avepoint.com")
        self.assertEqual(mocked_request.call_count, 2)
        self.assertEqual(
            mocked_request.call_args_list[1].kwargs["params"],
            {"domain": "avepoint.com"},
        )

    @patch("apollo_client.request_with_retry")
    def test_two_record_level_misses_continue_with_input_domain(self, mocked_request):
        mocked_request.side_effect = [_http_error(422), _http_error(422)]

        result = apollo_client.enrich_organization(
            domain="avepoint.com",
            name="AvePoint",
            website="https://avepoint.com",
        )

        self.assertFalse(result.found)
        self.assertEqual(result.domain, "avepoint.com")
        self.assertEqual(result.name, "AvePoint")
        self.assertEqual(mocked_request.call_count, 2)

    @patch("apollo_client.request_with_retry")
    def test_domain_only_422_does_not_loop(self, mocked_request):
        mocked_request.side_effect = _http_error(422)

        result = apollo_client.enrich_organization(domain="avepoint.com")

        self.assertFalse(result.found)
        self.assertEqual(result.domain, "avepoint.com")
        self.assertEqual(mocked_request.call_count, 1)

    @patch("apollo_client.request_with_retry")
    def test_authentication_error_still_fails_the_run(self, mocked_request):
        mocked_request.side_effect = _http_error(401)

        with self.assertRaises(requests.HTTPError):
            apollo_client.enrich_organization(
                domain="avepoint.com",
                name="AvePoint",
                website="https://avepoint.com",
            )

    @patch("apollo_client.request_with_retry")
    def test_rate_limit_error_still_fails_after_shared_retry_logic(self, mocked_request):
        mocked_request.side_effect = _http_error(429)

        with self.assertRaises(requests.HTTPError):
            apollo_client.enrich_organization(
                domain="avepoint.com",
                name="AvePoint",
                website="https://avepoint.com",
            )


if __name__ == "__main__":
    unittest.main()
