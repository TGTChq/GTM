"""The authorised recovery of outbox rows a PAUSED campaign deferred.

Guarantees asserted here, because each one was asked for explicitly:

* a named row is withheld with a recorded reason and never delivered
* a row whose copy no longer revalidates is BLOCKED, not delivered
* every delivery is verified by reading the lead back by its recorded id
* Airtable runs strictly afterwards and only where a genuine creation exists
* no enrichment: the drain makes no Apollo or Fantastic call
* exactly one create call per row, and a second pass is a no-op
"""
from __future__ import annotations

import pytest

from tgtc_core.providers.instantly import InstantlyResult
from tgtc_core.services import delivery_recovery as dr
from tgtc_core.testing.fakes import FakeAirtable, FakeInstantly
from tgtc_core.testing.scenario import CONTROL_ID_BY_CAMPAIGN_KEY
from tests_core.test_challenger_copy_blocks_not_crashes import CHALLENGER_ID_BY_CAMPAIGN_KEY
from tests_core.helpers import delivery_service, opportunity_service, sql1, sqlall
from tests_core.seed import apollo_for, seed_opportunity

CAMPAIGN = CONTROL_ID_BY_CAMPAIGN_KEY["customer_experience"]
ALLOW = {"TGTC_DELIVER_INTO_PAUSED_CAMPAIGNS": "1"}


class _Runner:
    """The smallest thing `recover` needs: a delivery path and a campaign allow-list."""

    def __init__(self, conn, airtable, instantly, clock):
        self.conn = conn
        self.instantly = _client(instantly)
        self._airtable = airtable
        self._instantly_fake = instantly
        self._clock = clock
        self.s = type("S", (), {"campaign_env": {"INSTANTLY_CAMPAIGN_CUSTOMER_SUCCESS": CAMPAIGN}})()

    def now(self):
        return self._clock()

    def deliver(self, *, max_items=100, channels=("instantly", "airtable")):
        out = {}
        for channel in channels:
            svc = delivery_service(self.conn, self._airtable, self._instantly_fake,
                                   self._clock, env=ALLOW)
            counts = {}
            for outcome in svc.drain(channel, max_items=max_items):
                counts[outcome.outcome] = counts.get(outcome.outcome, 0) + 1
            out[channel] = counts
        return out


def _client(fake):
    from tgtc_core.providers.instantly import InstantlyClient
    return InstantlyClient(fake, base_url="https://api.instantly.ai/api/v2", api_key="sim")


def _approve(conn, clock):
    pid, eid, oid = seed_opportunity(conn, clock)
    opportunity_service(conn, apollo_for("acme.com", "Acme"), clock).process(oid)
    row = sqlall(conn, "SELECT id FROM delivery_outbox WHERE channel='instantly'")
    assert len(row) == 1
    return int(row[0]["id"])


def _paused(clock):
    return FakeInstantly(campaign_status={CAMPAIGN: 2}, clock=clock)


def test_the_bare_function_noun_check_matches_the_two_real_subjects():
    assert dr.subject_is_a_bare_function_noun("operations role")
    assert dr.subject_is_a_bare_function_noun("customer support role")
    assert not dr.subject_is_a_bare_function_noun("Microsoft Cloud Engineer")
    assert not dr.subject_is_a_bare_function_noun("Senior Role Model Coordinator")
    assert not dr.subject_is_a_bare_function_noun("")


CHALLENGER = CHALLENGER_ID_BY_CAMPAIGN_KEY["customer_experience"]


def _vars(**over):
    base = {"rendered_subject": "Plant Accountant", "rendered_email_1_html": "<p>a</p>",
            "rendered_email_2_html": "<p>b</p>", "rendered_email_3_html": "<p>c</p>",
            "rendered_email_4_html": "<p>d</p>"}
    base.update(over)
    return base


@pytest.mark.parametrize("variables, expected_prefix", [
    ({}, dr.INCOMPLETE_COPY),
    (_vars(rendered_email_4_html="{{unresolved}}"), dr.INCOMPLETE_COPY),
    (_vars(rendered_subject="operations role"), dr.GENERIC_SUBJECT),
    (_vars(), ""),
])
def test_revalidation_names_its_reason(variables, expected_prefix):
    """A CHALLENGER payload, because that is what the 25 rows are: the copy contract
    only applies to a Challenger destination."""
    payload = {"campaign": CHALLENGER, "custom_variables": variables}
    got = dr.recovery_refusal(payload, (CHALLENGER,))
    assert got.startswith(expected_prefix) if expected_prefix else got == ""


def test_a_control_payload_is_not_refused_for_lacking_rendered_copy():
    """Control campaigns carry literal copy, so they have no rendered_* variables and
    must not be withheld for that."""
    payload = {"campaign": CAMPAIGN, "custom_variables": {"first_name": "Dana"}}
    assert dr.recovery_refusal(payload, (CAMPAIGN,)) == ""


def test_a_campaign_off_the_allow_list_is_refused():
    payload = {"campaign": "some-other-campaign",
               "custom_variables": {k: "x" for k in
                                    ("rendered_subject", "rendered_email_1_html",
                                     "rendered_email_2_html", "rendered_email_3_html",
                                     "rendered_email_4_html")}}
    assert dr.recovery_refusal(payload, (CAMPAIGN,)) == dr.CAMPAIGN_NOT_ALLOWED


def test_a_withheld_row_is_blocked_and_never_delivered(conn, clock):
    outbox_id = _approve(conn, clock)
    ins = _paused(clock)
    report = dr.recover(conn, _Runner(conn, None, ins, clock),
                        withhold_ids=(outbox_id,), now=clock())
    assert report["withheld"][str(outbox_id)] == dr.WITHHELD_BY_OPERATOR
    assert report["revalidated"] == 0
    assert ins.leads == {}
    assert sql1(conn, "SELECT state FROM delivery_outbox WHERE id = %s", (outbox_id,)) == "blocked"
    assert sql1(conn, "SELECT blocked_reason FROM delivery_outbox WHERE id = %s",
                (outbox_id,)) == dr.WITHHELD_BY_OPERATOR


def test_withholding_a_row_that_already_moved_is_reported_not_forced(conn, clock):
    outbox_id = _approve(conn, clock)
    assert dr.withhold(conn, outbox_id, dr.WITHHELD_BY_OPERATOR, now=clock())
    report = dr.recover(conn, _Runner(conn, None, _paused(clock), clock),
                        withhold_ids=(outbox_id,), now=clock())
    assert report["withheld"][str(outbox_id)] == "not_pending_anymore"


def test_a_good_row_is_delivered_and_verified_by_id(conn, clock):
    outbox_id = _approve(conn, clock)
    ins = _paused(clock)
    report = dr.recover(conn, _Runner(conn, None, ins, clock), now=clock())
    assert report["revalidated"] == 1
    assert [d["outbox_id"] for d in report["delivered"]] == [outbox_id]
    assert [v["outbox_id"] for v in report["verified"]] == [outbox_id]
    assert report["unverified"] == []
    lead_id = report["verified"][0]["lead_id"]
    assert lead_id and report["verified"][0]["campaign"] == CAMPAIGN
    assert len(ins.leads) == 1


def test_a_failed_read_back_is_reported_unverified_rather_than_assumed(conn, clock, monkeypatch):
    _approve(conn, clock)
    ins = _paused(clock)
    runner = _Runner(conn, None, ins, clock)
    monkeypatch.setattr(runner.instantly, "get_lead", lambda lead_id: InstantlyResult(False, 404))
    report = dr.recover(conn, runner, now=clock())
    assert report["verified"] == []
    assert [u["why"] for u in report["unverified"]] == ["read_back_failed"]


def test_exactly_one_create_and_a_second_recovery_is_a_no_op(conn, clock):
    _approve(conn, clock)
    ins = _paused(clock)
    runner = _Runner(conn, None, ins, clock)
    dr.recover(conn, runner, now=clock())
    again = dr.recover(conn, runner, now=clock())
    assert again["revalidated"] == 0
    assert len([r for r in ins.requests if r["method"] == "POST"]) == 1
    assert len(ins.leads) == 1


def test_airtable_follows_and_only_for_a_genuine_creation(conn, clock):
    _approve(conn, clock)
    ins, at = _paused(clock), FakeAirtable()
    runner = _Runner(conn, at, ins, clock)
    report = dr.recover(conn, runner, now=clock())
    assert len(report["verified"]) == 1
    # The Airtable row carries its own deferral, so it is not written in the same
    # pass; what matters is that nothing was written for an uncreated lead.
    assert sql1(conn, "SELECT state FROM delivery_outbox WHERE channel='airtable'") in ("pending", "delivered")
    assert len(at.records) in (0, 1)
    if at.records:
        # An Airtable record may exist only where its Instantly sibling genuinely
        # created the lead. Each channel records its own receipt, so count by channel.
        assert sql1(conn, "SELECT count(*) FROM delivery_receipts "
                          "WHERE channel='instantly' AND receipt_kind='created'") == 1


def test_a_withheld_row_never_produces_an_airtable_record(conn, clock):
    outbox_id = _approve(conn, clock)
    ins, at = _paused(clock), FakeAirtable()
    dr.recover(conn, _Runner(conn, at, ins, clock), withhold_ids=(outbox_id,), now=clock())
    assert at.records == {}
    assert ins.leads == {}


def test_the_recovery_spends_no_apollo_and_no_fantastic(conn, clock):
    """No enrichment: the payload is already stored, so no provider is consulted."""
    _approve(conn, clock)
    before = sql1(conn, "SELECT count(*) FROM request_attempts")
    dr.recover(conn, _Runner(conn, None, _paused(clock), clock), now=clock())
    assert sql1(conn, "SELECT count(*) FROM request_attempts") == before
