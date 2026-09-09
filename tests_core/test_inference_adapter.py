"""The real Anthropic adapter: request shape, response parsing, refusal handling, DB cache.
No network: ``_send`` is replaced. A live call remains UNVERIFIED (no credential)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from tgtc_core.domain.inference import (
    RESPONSE_SCHEMA, AnthropicAdapter, CachedInference, InferenceRequest, ReplayAdapter,
)


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
    assert a.model_version.startswith("anthropic:claude-opus-5:posting-classification/1")


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
