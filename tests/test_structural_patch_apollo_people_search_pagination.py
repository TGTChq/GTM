"""Phase 7 of FINAL_30_PLUS_SYSTEM_SPEC.md: apollo_client.search_people_at_company()
previously fetched only page 1 (25 results) with no pagination at all -- any
candidate beyond the first 25 for a given title set was silently unavailable
for ranking, regardless of relevance. People search itself is free (only
match_person/enrichment consumes credits, per config.py's own comment), so
pagination costs nothing extra.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

import config
import apollo_client


def _response(people, total_pages=None):
    payload = {"people": people}
    if total_pages is not None:
        payload["pagination"] = {"total_pages": total_pages}
    response = MagicMock()
    response.json.return_value = payload
    response.text = json.dumps(payload)
    return response


class ApolloPeopleSearchPaginationTests(unittest.TestCase):
    def test_stops_after_a_short_page_without_requesting_further_pages(self):
        short_page = [{"id": f"p{i}", "organization": {"primary_domain": "acme.com"}} for i in range(10)]
        with patch.object(apollo_client, "request_with_retry", return_value=_response(short_page)) as mocked:
            people = apollo_client.search_people_at_company("acme.com", ["CEO"])
        self.assertEqual(len(people), 10)
        self.assertEqual(mocked.call_count, 1)

    def test_paginates_across_full_pages_up_to_the_configured_max(self):
        full_page = [{"id": f"p{i}", "organization": {"primary_domain": "acme.com"}} for i in range(25)]
        short_final_page = [{"id": "last", "organization": {"primary_domain": "acme.com"}}]
        responses = [_response(full_page), _response(full_page), _response(short_final_page)]

        with patch.object(config, "APOLLO_PEOPLE_SEARCH_MAX_PAGES", 4), patch.object(
            apollo_client, "request_with_retry", side_effect=responses
        ) as mocked:
            people = apollo_client.search_people_at_company("acme.com", ["CEO"])
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(len(people), 25 + 25 + 1)

    def test_respects_total_pages_from_pagination_metadata(self):
        full_page = [{"id": f"p{i}", "organization": {"primary_domain": "acme.com"}} for i in range(25)]
        with patch.object(config, "APOLLO_PEOPLE_SEARCH_MAX_PAGES", 4), patch.object(
            apollo_client, "request_with_retry", return_value=_response(full_page, total_pages=1)
        ) as mocked:
            apollo_client.search_people_at_company("acme.com", ["CEO"])
        self.assertEqual(mocked.call_count, 1)

    def test_never_exceeds_configured_max_pages_even_with_all_full_pages(self):
        full_page = [{"id": f"p{i}", "organization": {"primary_domain": "acme.com"}} for i in range(25)]
        with patch.object(config, "APOLLO_PEOPLE_SEARCH_MAX_PAGES", 2), patch.object(
            apollo_client, "request_with_retry", return_value=_response(full_page)
        ) as mocked:
            people = apollo_client.search_people_at_company("acme.com", ["CEO"])
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(len(people), 50)

    def test_a_later_page_failure_keeps_earlier_results_instead_of_discarding_them(self):
        full_page = [{"id": f"p{i}", "organization": {"primary_domain": "acme.com"}} for i in range(25)]

        def side_effect(*args, **kwargs):
            if side_effect.calls == 0:
                side_effect.calls += 1
                return _response(full_page)
            raise RuntimeError("transient provider error")
        side_effect.calls = 0

        with patch.object(config, "APOLLO_PEOPLE_SEARCH_MAX_PAGES", 3), patch.object(
            apollo_client, "request_with_retry", side_effect=side_effect
        ):
            people = apollo_client.search_people_at_company("acme.com", ["CEO"])
        self.assertEqual(len(people), 25)

    def test_first_page_failure_still_raises(self):
        with patch.object(
            apollo_client, "request_with_retry", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                apollo_client.search_people_at_company("acme.com", ["CEO"])


if __name__ == "__main__":
    unittest.main()
