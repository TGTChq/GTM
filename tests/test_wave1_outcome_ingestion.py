"""Wave 1 outcome ingestion: read-only, and honest about what it did not observe.

``measurement.py`` computed a denominator and could never compute a numerator --
its own docstring says outcomes "arrive from outside ... this module joins them;
it never fetches them", and nothing fetched them. These tests pin the half that
was missing, and in particular the two ways a collector like this fabricates a
result:

* writing ``False`` for an outcome it could not classify (``_accumulate`` counts a
  falsy value as "did not happen", so a False deflates the treatment arm);
* letting a failed or truncated campaign contribute a partial count that makes the
  totals disagree with the per-campaign breakdown.
"""

from __future__ import annotations

from typing import Any, Dict, List

from outbound_wave1 import measurement, outcomes


class _Response:
    def __init__(self, payload: Dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> Dict[str, Any]:
        return self._payload


def _lead(email: str, **over: Any) -> Dict[str, Any]:
    """A lead in the shape production actually returns (verified 2026-09-05)."""
    lead = {
        "id": "019f54bc-a242-70a5-81dc-d8d55c4af023",
        "email": email,
        "campaign": "camp-control",
        "timestamp_created": "2026-09-04T00:00:00.000Z",
        "status": outcomes.STATUS_COMPLETED,
        "email_open_count": 0,
        "email_click_count": 0,
        "email_reply_count": 0,
        "status_summary": {"lastStep": {"stepID": "0_2_0",
                                        "timestamp_executed": "2026-09-04T15:34:07.801Z"}},
        "payload": {"email": email},
    }
    lead.update(over)
    return lead


def _requester(pages_by_campaign: Dict[str, List[Dict[str, Any]]]):
    calls: List[Dict[str, Any]] = []

    def request(method, url, *, headers=None, json_body=None, **kwargs):
        calls.append({"method": method, "url": url, "body": dict(json_body or {})})
        campaign = (json_body or {}).get("campaign")
        pages = pages_by_campaign.get(campaign)
        if pages is None:
            raise RuntimeError(f"unexpected campaign {campaign}")
        cursor = (json_body or {}).get("starting_after") or ""
        index = int(cursor or 0)
        page = pages[index]
        if isinstance(page, Exception):
            raise page
        return _Response(page)

    request.calls = calls  # type: ignore[attr-defined]
    return request


# --------------------------------------------------------------------------
# field derivation
# --------------------------------------------------------------------------

def test_a_lead_that_never_replied_reports_delivered_and_nothing_else():
    row = outcomes.outcome_from_lead(_lead("a@x.com"))
    assert row["delivered"] is True
    assert row["replied"] is False
    assert row["bounced"] is False
    assert "reply_step" not in row, "a lead that did not reply has no reply step"
    # lt_interest_status is absent on a lead that never replied, so the interest
    # outcomes must be ABSENT rather than False.
    for key in ("positive_reply", "meeting_booked", "opportunity_created", "fulfilled"):
        assert key not in row


def test_an_interested_reply_is_positive_and_carries_its_step():
    row = outcomes.outcome_from_lead(_lead(
        "b@x.com", email_reply_count=1, email_replied_step=2, lt_interest_status=1))
    assert row["replied"] is True
    assert row["reply_step"] == 2
    assert row["positive_reply"] is True
    assert row["meeting_booked"] is False
    assert row["fulfilled"] is False


def test_a_negative_reply_is_classified_not_dropped():
    row = outcomes.outcome_from_lead(_lead(
        "c@x.com", email_reply_count=1, email_replied_step=1, lt_interest_status=-1))
    assert row["replied"] is True
    assert row["positive_reply"] is False, "an observed 'not interested' IS a False"


def test_an_unknown_interest_value_leaves_the_outcome_absent():
    """The rule that keeps the numerator honest.

    ``_accumulate`` counts a falsy outcome as 'did not happen'. Writing False for
    a value we cannot classify would deflate whichever arm happened to receive it.
    """
    row = outcomes.outcome_from_lead(_lead(
        "d@x.com", email_reply_count=1, lt_interest_status=42))
    assert row["replied"] is True
    assert "positive_reply" not in row
    assert row["lt_interest_status"] == 42, "the raw value is kept for reclassification"


def test_a_reply_implies_delivery_even_with_no_step_record():
    row = outcomes.outcome_from_lead(_lead(
        "e@x.com", email_reply_count=1, status_summary={}))
    assert row["delivered"] is True, "you cannot reply to an email that never arrived"


def test_a_bounced_lead_is_reported_as_bounced():
    row = outcomes.outcome_from_lead(_lead("f@x.com", status=outcomes.STATUS_BOUNCED))
    assert row["bounced"] is True
    assert row["instantly_status"] == outcomes.STATUS_BOUNCED


def test_the_join_key_is_the_normalized_email():
    row = outcomes.outcome_from_lead(_lead("  MiXeD@Example.COM  "))
    assert row["contact_key"] == "mixed@example.com"


# --------------------------------------------------------------------------
# collection
# --------------------------------------------------------------------------

def test_it_reads_both_arms_and_deduplicates_a_shared_campaign():
    """customer_success and customer_support share one campaign in production."""
    pages = {
        "camp-control": [{"items": [_lead("a@x.com")], "next_starting_after": ""}],
        "camp-challenger": [{"items": [_lead("b@x.com")], "next_starting_after": ""}],
    }
    request = _requester(pages)
    result = outcomes.collect_outcomes(
        ["camp-control", "camp-challenger", "camp-control"],
        api_key="k", requester=request)

    assert result.ok
    assert sorted(result.rows) == ["a@x.com", "b@x.com"]
    assert result.campaigns_read == ["camp-control", "camp-challenger"]
    assert len(request.calls) == 2, "the shared campaign is read once, not twice"


def test_a_failed_campaign_contributes_nothing_and_is_named():
    pages = {
        "good": [{"items": [_lead("a@x.com")], "next_starting_after": ""}],
        "bad": [RuntimeError("HTTP 500")],
    }
    result = outcomes.collect_outcomes(["good", "bad"], api_key="k",
                                       requester=_requester(pages))

    assert not result.ok
    assert result.campaigns_failed == ["bad"]
    assert sorted(result.rows) == ["a@x.com"], (
        "a half-read campaign must contribute nothing, or the total disagrees "
        "with the per-campaign breakdown"
    )
    assert any("bad" in e for e in result.errors)


def test_the_watermark_excludes_leads_enrolled_before_the_experiment():
    pages = {"c": [{"items": [
        _lead("old@x.com", timestamp_created="2026-08-01T00:00:00.000Z"),
        _lead("new@x.com", timestamp_created="2026-09-04T00:00:00.000Z"),
    ], "next_starting_after": ""}]}
    result = outcomes.collect_outcomes(
        ["c"], api_key="k", since="2026-09-03T03:29:36Z",
        requester=_requester(pages))
    assert sorted(result.rows) == ["new@x.com"]


def test_pagination_follows_the_cursor_and_stops_cleanly():
    pages = {"c": [
        {"items": [_lead("a@x.com")], "next_starting_after": "1"},
        {"items": [_lead("b@x.com")], "next_starting_after": ""},
    ]}
    result = outcomes.collect_outcomes(["c"], api_key="k", requester=_requester(pages))
    assert sorted(result.rows) == ["a@x.com", "b@x.com"]
    assert result.leads_scanned == 2
    assert result.ok


def test_a_deadline_names_the_campaigns_it_could_not_reach():
    ticks = iter([0.0, 100.0, 100.0, 100.0])
    pages = {"first": [{"items": [_lead("a@x.com")], "next_starting_after": ""}],
             "second": [{"items": [_lead("b@x.com")], "next_starting_after": ""}]}
    result = outcomes.collect_outcomes(
        ["first", "second"], api_key="k", requester=_requester(pages),
        deadline=50.0, clock=lambda: next(ticks))

    assert not result.ok
    assert "second" in result.campaigns_failed
    assert "b@x.com" not in result.rows


def test_no_api_key_is_a_declared_gap_not_a_crash():
    result = outcomes.collect_outcomes(["c"], api_key="")
    assert not result.ok and result.rows == {}
    assert "INSTANTLY_API_KEY" in result.errors[0]


def test_the_collection_declares_the_mapping_it_used():
    result = outcomes.collect_outcomes(["c"], api_key="k", requester=_requester(
        {"c": [{"items": [], "next_starting_after": ""}]}))
    described = result.to_dict()
    assert described["read_only_operations"] == ["POST /leads/list (listing only)"]
    assert described["interest_mapping"]["positive"] == sorted(outcomes.INTEREST_POSITIVE)
    assert "replies_with_unclassified_interest" in described
    assert "not measured" in described["interest_mapping"]["note"]


# --------------------------------------------------------------------------
# the join actually closes
# --------------------------------------------------------------------------

def test_collected_outcomes_feed_the_analyzer_and_produce_a_primary_metric():
    """End to end: the collector's output is the analyzer's input, unmodified."""
    frame = [
        measurement.RandomizationRow(
            record_id="rec1", lead_key="x.com|1", contact_key="a@x.com",
            company_assignment_key="x.com", experiment_arm=measurement.ARM_A,
            experiment_id="e", campaign="camp-control", campaign_key="finance",
            signal_tier="T1", signal_type="multi_opening", proof_type="economics",
            outbound_offer_type="o", offer_class="c", friction_angle="f",
            role_page_match=True, copy_version="v1", randomized_eligible=True),
        measurement.RandomizationRow(
            record_id="rec2", lead_key="y.com|1", contact_key="b@y.com",
            company_assignment_key="y.com", experiment_arm=measurement.ARM_B,
            experiment_id="e", campaign="camp-challenger", campaign_key="finance",
            signal_tier="T1", signal_type="multi_opening", proof_type="economics",
            outbound_offer_type="o", offer_class="c", friction_angle="f",
            role_page_match=True, copy_version="v1", randomized_eligible=True),
    ]
    pages = {
        "camp-control": [{"items": [_lead("a@x.com")], "next_starting_after": ""}],
        "camp-challenger": [{"items": [_lead(
            "b@y.com", campaign="camp-challenger", email_reply_count=1,
            email_replied_step=1, lt_interest_status=1)], "next_starting_after": ""}],
    }
    collected = outcomes.collect_outcomes(
        ["camp-control", "camp-challenger"], api_key="k", requester=_requester(pages))
    report = measurement.analyze(frame, outcomes.as_outcome_map(collected))

    assert report["overall"]["A"]["positive_replies"] == 0
    assert report["overall"]["B"]["positive_replies"] == 1
    assert report["overall"]["B"]["reply_steps"] == {"step_1": 1}
    assert report["overall"]["A"]["delivered"] == 1
    assert report["lift"]["challenger"] == 1.0
    assert report["lift"]["control"] == 0.0


def test_every_field_the_analyzer_reads_is_a_field_the_collector_can_emit():
    """A drift between the two modules must be a test failure, not a silent zero."""
    assert set(outcomes.OUTCOME_FIELDS) == set(measurement.OUTCOME_FIELDS)
    emitted = set(outcomes.outcome_from_lead(_lead(
        "z@x.com", email_reply_count=1, email_replied_step=3, lt_interest_status=4)))
    assert set(measurement.OUTCOME_FIELDS) <= emitted


def test_a_lead_that_has_not_replied_is_not_counted_as_unclassified():
    """"Has not replied yet" and "replied, interest unreadable" are different facts.

    Counting the first as unclassified reports a freshly enrolled batch as 100%
    unclassified -- alarming, and about nothing. Measured on the real 2026-09-05
    batch: 769 contacts, none of which had replied.
    """
    pages = {"c": [{"items": [
        _lead("quiet@x.com"),
        _lead("weird@x.com", email_reply_count=1, lt_interest_status=42),
    ], "next_starting_after": ""}]}
    result = outcomes.collect_outcomes(["c"], api_key="k", requester=_requester(pages))
    assert result.leads_unclassified_interest == 1
    assert result.to_dict()["replies_with_unclassified_interest"] == 1
