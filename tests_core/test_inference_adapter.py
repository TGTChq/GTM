"""The real Anthropic adapter: request shape, response parsing, refusal handling, DB cache.
No network: ``_send`` is replaced. A live call remains UNVERIFIED (no credential)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tgtc_core.domain.inference import (
    RESPONSE_SCHEMA, AnthropicAdapter, CachedInference, InferenceRequest, ReplayAdapter,
)
from tgtc_core.db.connection import jsonb


def _req(desc="Own accounts payable and receivable and the month-end close. Full-time, remote US.") -> InferenceRequest:
    return InferenceRequest(content_hash="abc", description=desc, title="", structured={"countries": ["US"]})


def test_request_uses_structured_output_and_treats_the_posting_as_data():
    a = AnthropicAdapter(api_key="k", model="claude-opus-5", effort="medium")
    params = a.build_params(_req())
    assert params["model"] == "claude-opus-5"
    assert params["output_config"]["format"] == {"type": "json_schema", "schema": RESPONSE_SCHEMA}
    assert params["output_config"]["effort"] == "medium"
    assert "tools" not in params and "tool_choice" not in params
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    user = params["messages"][0]["content"]
    assert user.startswith("<posting>") and "Own accounts payable" in user
    assert a.model_version.startswith("anthropic:claude-opus-5:posting-classification/2")


def test_wire_schema_omits_unsupported_array_limits_but_local_caps_remain():
    schema = AnthropicAdapter(api_key="k").build_params(_req())["output_config"]["format"]["schema"]
    assert "maxItems" not in json.dumps(schema)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["responsibilities"]["items"]["additionalProperties"] is False
    response = AnthropicAdapter(api_key="k").parse_message(_message(json.dumps({
        "compatible_functions": ["finance", "operations", "engineering"],
        "responsibilities": [{"phrase": "accounts payable", "excerpt": "Own accounts payable"}] * 4,
        "seniority": "ic", "people_management": False,
        "incompatible_reasons": [], "confidence": 0.9,
    })))
    assert response.available
    assert response.compatible_functions == ["finance", "operations"]
    assert len(response.responsibilities) == 3


def test_real_sdk_serializes_compatible_schema_without_network():
    import anthropic
    import httpx2 as httpx

    captured = []

    def handle(request):
        payload = json.loads(request.content)
        captured.append(payload)
        assert "maxItems" not in json.dumps(payload["output_config"]["format"]["schema"])
        return httpx.Response(200, json={
            "id": "msg_offline", "type": "message", "role": "assistant", "model": "claude-opus-5",
            "content": [{"type": "text", "text": json.dumps({
                "compatible_functions": ["finance"], "responsibilities": [], "seniority": "ic",
                "people_management": False, "incompatible_reasons": [], "confidence": 0.9,
            })}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 40},
        })

    adapter = AnthropicAdapter(api_key="offline-key")
    with anthropic.Anthropic(api_key="offline-key", max_retries=0,
                             http_client=httpx.Client(transport=httpx.MockTransport(handle))) as client:
        adapter._client = client
        result = adapter.classify(_req())
    assert result.available and len(captured) == 1
    assert captured[0]["model"] == "claude-opus-5"
    assert captured[0]["output_config"]["effort"] == "medium"
    assert captured[0]["max_tokens"] == 1024


def test_sdk_bad_request_diagnostic_is_bounded_and_contains_no_echoed_secrets():
    import anthropic
    import httpx2 as httpx

    secret = "sk-ant-do-not-log"
    body = {"type": "error", "error": {"type": "invalid_request_error",
            "message": "output_config.format.schema: maxItems unsupported " + secret + " private posting " + "x" * 10000}}
    calls = []

    def handle(request):
        calls.append(True)
        return httpx.Response(400, json=body, headers={"request-id": "req_offline123"})

    adapter = AnthropicAdapter(api_key=secret)
    with anthropic.Anthropic(api_key=secret, max_retries=0,
                             http_client=httpx.Client(transport=httpx.MockTransport(handle))) as client:
        adapter._client = client
        result = adapter.classify(_req())
    assert len(calls) == 1 and not result.available
    assert result.unavailable_kind == "config"
    assert result.unavailable_reason == "inference_error:BadRequestError"
    assert result.diagnostics == {"http_status": 400, "request_id": "req_offline123",
                                  "error_type": "invalid_request_error",
                                  "mentioned_keywords": ["maxItems", "output_config"]}
    serialized = json.dumps(result.to_dict())
    assert secret not in serialized and "private posting" not in serialized
    assert len(json.dumps(result.diagnostics)) < 300


@pytest.mark.parametrize("body", [None, "secret", {"error": "secret"}, {"error": {"type": "secret", "message": "secret"}}])
def test_unrecognized_error_details_are_not_persisted(body):
    from tgtc_core.domain.inference import safe_exception_diagnostics

    exc = RuntimeError("secret")
    exc.body = body
    exc.request_id = "req_secret-with-unexpected-characters"
    assert safe_exception_diagnostics(exc) == {}


def test_budget_ledger_preserves_safe_inference_diagnostics(conn):
    from tgtc_core.domain.inference import BudgetedInference, InferenceResponse
    from tgtc_core.services.spend_budget import BudgetLimits, SpendBudget, create_budget

    create_budget(conn, "offline-diagnostic", BudgetLimits(
        anthropic_requests=1, anthropic_input_tokens=100000, anthropic_output_tokens=1024))

    class Refused:
        model_version = "offline-refused/1"

        def classify(self, request):
            return InferenceResponse(available=False, unavailable_kind="config",
                                     unavailable_reason="inference_error:BadRequestError",
                                     diagnostics={"http_status": 400, "mentioned_keywords": ["maxItems"]})

    wrapped = BudgetedInference(conn, Refused(), SpendBudget(conn, "offline-diagnostic"))
    assert not wrapped.classify(_req()).available
    with conn.cursor() as cur:
        cur.execute("SELECT status, response_summary FROM request_attempts WHERE provider = 'anthropic'")
        row = cur.fetchone()
        assert row["status"] == "refused"
        assert row["response_summary"]["diagnostics"] == {"http_status": 400, "mentioned_keywords": ["maxItems"]}
        cur.execute("SELECT count(*) AS n FROM spend_reservations WHERE budget_id = 'offline-diagnostic'")
        assert cur.fetchone()["n"] == 1


def _message(text, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=100, output_tokens=40, cache_read_input_tokens=90))


def test_parse_valid_json_refusal_and_garbage():
    a = AnthropicAdapter(api_key="k")
    good = a.parse_message(_message(json.dumps({"compatible_functions": ["finance"], "responsibilities": [
        {"phrase": "accounts payable", "excerpt": "Own accounts payable and receivable"}], "seniority": "ic",
        "people_management": False, "incompatible_reasons": [], "confidence": 0.93})))
    assert good.available and good.compatible_functions == ["finance"] and good.usage["cache_read_input_tokens"] == 90
    refusal = a.parse_message(_message("", stop="refusal"))
    assert not refusal.available and refusal.unavailable_reason == "model_refusal"
    garbage = a.parse_message(_message("not json"))
    assert not garbage.available and garbage.unavailable_reason == "invalid_json"
    truncated = a.parse_message(_message("{", stop="max_tokens"))
    assert not truncated.available and truncated.unavailable_reason == "max_tokens"


def test_transport_failure_is_unavailable_never_a_guess():
    a = AnthropicAdapter(api_key="k")
    a._send = lambda params: (_ for _ in ()).throw(ConnectionError("down"))  # type: ignore[assignment]
    out = a.classify(_req())
    assert not out.available and out.unavailable_reason == "inference_error:ConnectionError"


def test_sdk_internal_retries_are_disabled_so_each_physical_call_needs_a_reservation(monkeypatch):
    captured = {}

    class FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import sys
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=FakeAnthropic))
    AnthropicAdapter(api_key="k")._get_client()
    assert captured["max_retries"] == 0


def test_db_cache_avoids_a_second_call_for_the_same_content(conn):
    inner = ReplayAdapter({"abc": {"compatible_functions": ["finance"], "responsibilities": [
        {"phrase": "accounts payable", "excerpt": "Own accounts payable and receivable"}], "seniority": "ic",
        "people_management": False, "incompatible_reasons": [], "confidence": 0.9}})
    cached = CachedInference(conn, inner)
    first = cached.classify(_req())
    second = cached.classify(_req())
    assert first.available and second.available and inner.calls == 1
    assert (cached.hits, cached.misses) == (1, 1)
    # unavailable answers are NOT cached (a transient failure must be retried)
    missing = cached.classify(InferenceRequest(content_hash="zzz", description="x" * 200))
    assert not missing.available and inner.calls == 2
    cached.classify(InferenceRequest(content_hash="zzz", description="x" * 200))
    assert inner.calls == 3


def test_v2_reuses_the_v1_raw_answer_without_another_model_call(conn):
    payload = {
        "available": True, "compatible_functions": ["finance"], "responsibilities": [
            {"phrase": "accounts payable", "excerpt": "Own accounts payable and receivable"}
        ], "seniority": "ic", "people_management": False, "incompatible_reasons": [],
        "exclusion_evidence": [], "confidence": 0.9,
    }
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inference_cache (content_hash, policy_version, model_version, response_json) VALUES (%s, %s, %s, %s)",
            ("abc", "tgtc-core/1", "replay/1", jsonb(payload)),
        )
    conn.commit()
    inner = ReplayAdapter({})
    cached = CachedInference(conn, inner, policy_version="tgtc-core/2")
    out = cached.classify(_req())
    assert out.available and out.compatible_functions == ["finance"]
    assert inner.calls == 0 and (cached.hits, cached.misses) == (1, 0)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM inference_cache WHERE content_hash = 'abc' AND policy_version = 'tgtc-core/2'")
        assert cur.fetchone()["n"] == 1
    conn.commit()
