"""Credentials never reach the database, receipts or logs."""

from __future__ import annotations

import json

from tgtc_core.testing.scenario import build_nine_route_scenario
from tests_core.helpers import runner, sqlall

SECRET = "SECRET-KEY-VALUE-XYZ-987"


def test_no_credential_is_persisted_anywhere(conn, clock, caplog):
    sc = build_nine_route_scenario(clock())
    r = runner(conn, sc, clock, fantastic_api_key=SECRET, apollo_api_key=SECRET, airtable_token=SECRET, instantly_api_key=SECRET,
               signing_key="another-secret-value")
    r.cycle()
    for table, cols in (("request_attempts", "params_json::text || coalesce(response_summary::text,'')"),
                        ("run_log", "details::text"), ("delivery_receipts", "response_summary::text"),
                        ("approvals", "lead_json::text"), ("delivery_outbox", "payload_json::text"), ("evidence", "coalesce(value::text,'') || coalesce(excerpt,'')")):
        for row in sqlall(conn, f"SELECT {cols} AS t FROM {table}"):
            assert SECRET not in row["t"], table
            assert "another-secret-value" not in row["t"], table
    assert SECRET not in caplog.text
    # the fakes saw the credential only in headers, never in params
    assert all(req["auth_header_present"] for req in sc.fantastic.requests)
    assert all(SECRET not in json.dumps(req["params"]) for req in sc.fantastic.requests)
    assert all(req["key_present"] for req in sc.apollo.requests)
