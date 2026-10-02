"""R03 -- transient inference failure, missing configuration, insufficient evidence and
commercial exclusion are four different outcomes. A timeout/429 leaves recoverable work
that resumes by itself; an unchanged posting is never re-bought or modified to recover."""

from __future__ import annotations

from datetime import timedelta

from tgtc_core.domain.inference import UNAVAILABLE_TRANSIENT, InferenceResponse, ReplayAdapter
from tgtc_core.services.classification_service import classify_one, reopen_for_inference
from tgtc_core.testing.fakes import make_posting_row
from tgtc_core.testing.scenario import build_nine_route_scenario
from tests_core.helpers import runner, sql1, sqlall
from tests_core.seed import seed_posting

VAGUE = ("This role coordinates a range of internal activities across teams and keeps the wheels turning day to day, "
         "supporting leadership with whatever is needed across the business. Full-time, remote within the United States. ")
ANSWER = {"compatible_functions": ["operations"], "responsibilities": [
    {"phrase": "coordinates internal activities", "excerpt": "coordinates a range of internal activities across teams"}],
    "seniority": "ic", "people_management": False, "incompatible_reasons": [], "confidence": 0.9}


class FlakyAdapter:
    """Fails with a transient error N times, then answers like a replay adapter."""

    model_version = "flaky/1"

    def __init__(self, failures: int, reason: str = "inference_error:APITimeoutError"):
        self.failures = failures
        self.reason = reason
        self.calls = 0

    def classify(self, request):
        self.calls += 1
        if self.calls <= self.failures:
            return InferenceResponse(available=False, unavailable_reason=self.reason, unavailable_kind=UNAVAILABLE_TRANSIENT,
                                     model_version=self.model_version)
        return InferenceResponse.from_dict(dict(ANSWER), model_version=self.model_version)


def _seed_vague(conn, clock, job_id="vague-1"):
    row = make_posting_row(id=job_id, title="Team Member", organization="Acme", domain="acme.com", description=VAGUE,
                           date_created=clock() - timedelta(hours=4))
    pid = seed_posting(conn, clock, row)
    from tgtc_core.services.identity_service import resolve_posting_identity
    resolve_posting_identity(conn, pid, now=clock())
    return pid


def test_transient_failure_waits_and_recovers_without_reacquisition(conn, clock):
    pid = _seed_vague(conn, clock)
    flaky = FlakyAdapter(failures=2)
    out = classify_one(conn, pid, inference=flaky, now=clock())
    assert out.outcome == "wait" and out.reason.startswith("inference_transient:") and out.retry_after > clock()
    assert sql1(conn, "SELECT state FROM postings WHERE id = %s", (pid,)) == "identity_resolved"   # NOT closed
    assert sql1(conn, "SELECT count(*) FROM classifications") == 0
    out2 = classify_one(conn, pid, inference=flaky, now=clock())
    assert out2.outcome == "wait"
    out3 = classify_one(conn, pid, inference=flaky, now=clock())
    assert out3.outcome == "classified" and out3.method == "semantic"
    assert sql1(conn, "SELECT count(*) FROM postings") == 1                # same posting, no re-acquisition
    assert sql1(conn, "SELECT max(version) FROM posting_versions") == 1     # no artificial modification
    assert sql1(conn, "SELECT count(*) FROM opportunities") == 1


def test_runner_puts_transient_failures_in_waiting_and_resumes_them(conn, clock):
    sc = build_nine_route_scenario(clock())
    sc.fantastic.rows = [make_posting_row(id="vague-r", title="Team Member", organization="Acme", domain="acme.com",
                                          description=VAGUE, date_created=clock() - timedelta(hours=4))]
    flaky = FlakyAdapter(failures=1, reason="inference_error:RateLimitError")
    r = runner(conn, sc, clock, inference_enabled=True)
    r.inference = flaky
    r.inference_configured = True
    first = r.cycle()
    assert first.stages["classify"] == {"wait": 1, "technical_failure": 1}
    item = sqlall(conn, "SELECT state, waiting_on, available_at FROM work_items WHERE kind = 'classify'")[0]
    assert item["state"] == "waiting" and item["waiting_on"].startswith("inference_transient:")
    assert sql1(conn, "SELECT state FROM postings") == "identity_resolved"
    # not before the backoff, then it resumes on its own
    second = r.cycle(acquire=False)
    assert second.stages["classify"] == {}
    clock.advance(minutes=16)
    third = r.cycle(acquire=False)
    assert third.stages["classify"] == {"classified": 1}
    assert sql1(conn, "SELECT count(*) FROM postings") == 1 and sql1(conn, "SELECT count(*) FROM opportunities") == 1


def test_missing_configuration_closes_distinctly_and_reopens_when_configured(conn, clock):
    pid = _seed_vague(conn, clock)
    out = classify_one(conn, pid, inference=None, now=clock())
    assert out.outcome == "closed" and out.reason == "inference_unavailable:no_inference_configured"
    from tgtc_core.domain.inference import NullAdapter
    pid2 = _seed_vague(conn, clock, job_id="vague-2")
    out2 = classify_one(conn, pid2, inference=NullAdapter(), now=clock())
    assert out2.outcome == "closed" and out2.reason == "inference_unavailable:no_inference_configured"
    # inference gets configured: both postings are re-entered, same ids, no new rows
    replay = ReplayAdapter({sql1(conn, "SELECT content_hash FROM postings WHERE id = %s", (pid,)): ANSWER,
                            sql1(conn, "SELECT content_hash FROM postings WHERE id = %s", (pid2,)): ANSWER}, model_version="replay/2")
    reopened = reopen_for_inference(conn, model_version=replay.model_version, now=clock())
    assert reopened == 2
    assert sql1(conn, "SELECT count(*) FROM work_items WHERE kind = 'classify' AND state = 'ready'") == 2
    assert classify_one(conn, pid, inference=replay, now=clock()).outcome == "classified"
    assert classify_one(conn, pid2, inference=replay, now=clock()).outcome == "classified"
    assert sql1(conn, "SELECT count(*) FROM postings") == 2


def test_commercial_exclusion_and_insufficient_evidence_stay_closed(conn, clock):
    """These are not technical failures: no waiting, no automatic resumption without new evidence."""
    # Phase 2 audit task 1 (2026-09-19): a leadership title alone ("Director of Finance")
    # no longer deterministically excludes (facts.py role_level is a fact only), so this
    # exercises a different, still-valid deterministic exclusion (employment:part_time)
    # to prove the same "closed, not waiting" behaviour for a commercial exclusion.
    row = make_posting_row(id="excl-1", title="Director of Finance", organization="Acme", domain="acme.com",
                           description="This is a part-time position with a fixed schedule. " + VAGUE,
                           date_created=clock() - timedelta(hours=4))
    pid = seed_posting(conn, clock, row)
    from tgtc_core.services.identity_service import resolve_posting_identity
    resolve_posting_identity(conn, pid, now=clock())
    out = classify_one(conn, pid, inference=FlakyAdapter(failures=0), now=clock())
    assert out.outcome == "closed" and out.reason == "employment:part_time"
    short = make_posting_row(id="short-1", title="Ops", organization="Acme", domain="acme.com", description="Apply now.",
                             date_created=clock() - timedelta(hours=4))
    pid2 = seed_posting(conn, clock, short)
    resolve_posting_identity(conn, pid2, now=clock())
    out2 = classify_one(conn, pid2, inference=FlakyAdapter(failures=0), now=clock())
    assert out2.outcome == "closed" and out2.reason.startswith("insufficient_evidence:description_too_short")
    assert reopen_for_inference(conn, model_version="anything/9", now=clock()) == 0


def test_sdk_configuration_error_waits_without_business_receipt(conn, clock):
    from tgtc_core.domain.inference import UNAVAILABLE_CONFIG

    class BadSchema:
        model_version = "anthropic:claude-opus-5:posting-classification/1"

        def classify(self, request):
            return InferenceResponse(available=False, unavailable_reason="inference_error:BadRequestError",
                                     unavailable_kind=UNAVAILABLE_CONFIG, model_version=self.model_version)

    pid = _seed_vague(conn, clock)
    out = classify_one(conn, pid, inference=BadSchema(), now=clock())
    assert out.outcome == "wait" and out.reason == "inference_config:inference_error:BadRequestError"
    assert sql1(conn, "SELECT state FROM postings WHERE id = %s", (pid,)) == "identity_resolved"
    assert sql1(conn, "SELECT count(*) FROM classifications") == 0


def test_same_model_recovers_four_old_sdk_failures_but_preserves_business_receipt(conn, clock):
    from tgtc_core.domain.inference import UNAVAILABLE_ANSWER

    model = "anthropic:claude-opus-5:posting-classification/1"
    pids = [_seed_vague(conn, clock, job_id=f"old-bad-request-{i}") for i in range(4)]
    with conn.cursor() as cur:
        cur.execute("UPDATE postings SET state = 'closed', close_reason = 'inference_unavailable:inference_error:BadRequestError' "
                    "WHERE id = ANY(%s)", (pids,))
    conn.commit()

    class Refusal:
        model_version = model

        def classify(self, request):
            return InferenceResponse(available=False, unavailable_kind=UNAVAILABLE_ANSWER,
                                     unavailable_reason="refusal", model_version=model)

    business_pid = _seed_vague(conn, clock, job_id="business-refusal")
    assert classify_one(conn, business_pid, inference=Refusal(), now=clock()).outcome == "closed"
    assert reopen_for_inference(conn, model_version=model, now=clock()) == 4
    assert sql1(conn, "SELECT state FROM postings WHERE id = %s", (business_pid,)) == "closed"
    replay = ReplayAdapter({sql1(conn, "SELECT content_hash FROM postings WHERE id = %s", (pid,)): ANSWER for pid in pids},
                           model_version=model)
    for pid in pids:
        assert classify_one(conn, pid, inference=replay, now=clock()).outcome == "classified"
    assert reopen_for_inference(conn, model_version=model, now=clock()) == 0
    assert sql1(conn, "SELECT count(*) FROM postings") == 5
    assert sql1(conn, "SELECT count(*) FROM opportunities") == 1
    assert sql1(conn, "SELECT max(version) FROM posting_versions") == 1
