"""A refusing model provider is asked once an hour, not 1,500 times a day.

Measured in production on 2026-09-24: the Anthropic key had no credit balance, so every
classify call returned 400 "Your credit balance is too low". That is a CONFIG failure,
which makes the work item wait and try again next cycle -- 1,500 attempts in one day,
each one burning a request slot of the day's allowance and classifying nothing. Semantic
classification had been dead all day and the only visible trace was a budget full of
refusals.

The rule, the same one Apollo and Instantly already follow: a provider on record as
refusing is asked by exactly ONE caller per interval, and a served call lifts it.
"""

from __future__ import annotations

import pytest

from tgtc_core.domain.inference import BudgetedInference, InferenceRequest, InferenceResponse
from tgtc_core.services import provider_state
from tgtc_core.services.spend_budget import BudgetLimits, SpendBudget, create_budget
from tests_core.helpers import sql1

BUDGET = "gate-test"


class Adapter:
    """Answers however the test tells it to, and counts how often it was asked."""

    model_version = "gate-test/1"

    def __init__(self, response):
        self.response, self.calls = response, 0

    def classify(self, request):
        self.calls += 1
        return self.response


NO_CREDIT = InferenceResponse(available=False, unavailable_kind="config",
                              unavailable_reason="inference_error:BadRequestError",
                              diagnostics={"http_status": 400})
GOOD = InferenceResponse(available=True, compatible_functions=[], responsibilities=[], seniority="ic",
                         people_management=False, incompatible_reasons=[], confidence=0.9,
                         model_version="gate-test/1")


def budget(conn, requests=50):
    create_budget(conn, BUDGET, BudgetLimits(anthropic_requests=requests, anthropic_input_tokens=10_000_000,
                                             anthropic_output_tokens=100_000))
    return SpendBudget(conn, BUDGET)


def req(h="h1"):
    return InferenceRequest(content_hash=h, description="a description long enough to be real" * 3)


def attempts(conn):
    return int(sql1(conn, "SELECT count(*) FROM request_attempts WHERE provider = 'anthropic'"))


def test_a_provider_with_no_credit_is_asked_once_not_every_cycle(conn):
    inner = Adapter(NO_CREDIT)
    wrapped = BudgetedInference(conn, inner, budget(conn))

    first = wrapped.classify(req("h1"))
    assert first.available is False and inner.calls == 1
    assert provider_state.load(conn, "anthropic")["state"] == "refusing"

    # Everything after it, in the same interval, is told to wait without a call.
    for i in range(20):
        answer = wrapped.classify(req(f"h{i + 2}"))
        assert answer.available is False
        assert answer.unavailable_reason == "provider_refusing"
        assert answer.unavailable_kind == "transient", "waiting, never a posting closed over billing"
    assert inner.calls == 1, "one refusal answers for the whole interval"
    assert attempts(conn) == 1, "and the day's allowance is not burned by the refusals"


def test_the_refusal_lifts_as_soon_as_a_call_is_served(conn):
    inner = Adapter(NO_CREDIT)
    wrapped = BudgetedInference(conn, inner, budget(conn))
    wrapped.classify(req("h1"))
    assert provider_state.load(conn, "anthropic")["state"] == "refusing"

    # The interval elapses (the probe reservation is what a real caller wins).
    conn.execute("UPDATE provider_state SET last_attempt_at = now() - interval '3 hours', "
                 "refusing_since = now() - interval '3 hours' WHERE provider = 'anthropic'")
    conn.commit()
    inner.response = GOOD

    served = wrapped.classify(req("h2"))
    assert served.available is True and inner.calls == 2
    assert provider_state.load(conn, "anthropic")["state"] == "serving"

    # ... and from then on every request goes through again.
    assert wrapped.classify(req("h3")).available is True
    assert inner.calls == 3


def test_a_healthy_provider_is_never_gated(conn):
    inner = Adapter(GOOD)
    wrapped = BudgetedInference(conn, inner, budget(conn))
    for i in range(5):
        assert wrapped.classify(req(f"h{i}")).available is True
    assert inner.calls == 5
    assert provider_state.load(conn, "anthropic")["state"] == "serving"


def test_a_transient_failure_does_not_put_the_provider_on_record_as_refusing(conn):
    """A timeout is a wait, not a verdict about the provider."""
    inner = Adapter(InferenceResponse(available=False, unavailable_kind="transient",
                                      unavailable_reason="inference_error:APITimeoutError"))
    wrapped = BudgetedInference(conn, inner, budget(conn))
    wrapped.classify(req("h1"))
    wrapped.classify(req("h2"))
    assert inner.calls == 2
    assert provider_state.load(conn, "anthropic")["state"] in ("unknown", "serving")
