"""Regression tests for record-level Apollo person-enrichment failures."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

import apollo_client


def _http_error(status_code: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://api.apollo.io/api/v1/people/match"
    return requests.HTTPError(f"HTTP {status_code}", response=response)


CANDIDATE = {
    "id": "620e0ffaeda4380001c8d76e",
    "first_name": "Ada",
    "last_name": "Lovelace",
    "title": "VP Marketing",
    "linkedin_url": "https://www.linkedin.com/in/example",
    "organization": {
        "name": "Example Co",
        "primary_domain": "example.com",
    },
}


class ApolloPersonMatchResilienceTests(unittest.TestCase):
    @patch("apollo_client.request_with_retry")
    def test_422_preserves_candidate_for_hunter_fallback(self, mocked_request):
        mocked_request.side_effect = _http_error(422)

        result = apollo_client.match_person(CANDIDATE)

        self.assertTrue(result.person_found)
        self.assertFalse(result.email_found)
        self.assertEqual(result.person_id, CANDIDATE["id"])
        self.assertEqual(result.first_name, "Ada")
        self.assertEqual(result.last_name, "Lovelace")
        self.assertEqual(result.organization_domain, "example.com")
        self.assertIsNone(result.email)

    @patch("apollo_client.request_with_retry")
    def test_404_is_also_treated_as_record_level_miss(self, mocked_request):
        mocked_request.side_effect = _http_error(404)

        result = apollo_client.match_person(CANDIDATE)

        self.assertEqual(result.person_id, CANDIDATE["id"])
        self.assertEqual(result.first_name, "Ada")
        self.assertIsNone(result.email)

    @patch("apollo_client.request_with_retry")
    def test_authentication_error_still_fails_the_run(self, mocked_request):
        mocked_request.side_effect = _http_error(401)

        with self.assertRaises(requests.HTTPError):
            apollo_client.match_person(CANDIDATE)

    @patch("apollo_client.request_with_retry")
    def test_rate_limit_error_still_fails_after_shared_retry_logic(self, mocked_request):
        mocked_request.side_effect = _http_error(429)

        with self.assertRaises(requests.HTTPError):
            apollo_client.match_person(CANDIDATE)


if __name__ == "__main__":
    unittest.main()
