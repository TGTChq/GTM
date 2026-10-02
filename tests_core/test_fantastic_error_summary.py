"""Fantastic error evidence is useful, bounded and safe to persist."""

import json

import pytest

from tgtc_core.providers.fantastic import FantasticClient, FantasticRequestError
from tgtc_core.providers.fantastic import _redact_error_text
from tgtc_core.providers.http import Response


class ErrorTransport:
    def request(self, *args, **kwargs):
        return Response(
            status=400,
            headers={
                "x-api-jobs-remaining": "499",
                "x-api-next-billing-date": "live-secret" * 20_000,
            },
            text=json.dumps({
                "detail": [{
                    "loc": ["query", "field"],
                    "msg": 'Authorization: Bearer live-secret {"token": "OTHER-SENSITIVE-TOKEN"}',
                    "input": "omit",
                }],
                "api_key": "live-secret",
                "unrelated": "omit",
            }),
        )


def test_http_error_summary_redacts_credentials_and_ignores_arbitrary_fields():
    client = FantasticClient(ErrorTransport(), base_url="https://data.fantastic.jobs", api_key="live-secret",
                             max_retries=0)
    with pytest.raises(FantasticRequestError) as caught:
        client.fetch_page("/v1/active-jb", {"time_frame": "7d"})

    summary = caught.value.response_summary
    serialized = json.dumps(summary)
    assert summary["status"] == 400
    assert summary["quota"] == {"jobs_remaining": 499}
    assert summary["body"]["detail"][0]["loc"] == ["query", "field"]
    assert "[REDACTED]" in summary["body"]["detail"][0]["msg"]
    assert "live-secret" not in serialized
    assert "OTHER-SENSITIVE-TOKEN" not in serialized
    assert len(serialized.encode("utf-8")) <= 4096
    assert "api_key" not in serialized and "unrelated" not in serialized and "input" not in serialized


def test_short_configured_secret_is_redacted_inside_other_text():
    assert _redact_error_text("simulated_404", ("sim",)) == "[REDACTED]ulated_404"
