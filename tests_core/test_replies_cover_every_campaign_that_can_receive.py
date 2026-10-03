"""replies-poll must read every campaign that can still RECEIVE, not only the routes.

On 2026-10-03 routing moved to the v2 campaigns. Reading only the configured ids would
have silently stopped ingesting replies to the 16,875 messages already sent from the nine
originals: a paused campaign sends nothing, but its old threads still receive. Someone who
answers next week would have been dropped on the floor -- their opt-out unrecorded, their
message never reaching a human.
"""
from __future__ import annotations

from tgtc_core.policy.campaigns import (CHALLENGER_V2_BY_ORIGINAL,
                                        EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID,
                                        KNOWN_CHALLENGER_CAMPAIGN_IDS)


def _polled(campaign_env, followup=""):
    """The selection cmd_replies_poll makes, expressed without the CLI plumbing."""
    configured = {v for v in (campaign_env or {}).values() if v}
    return sorted(configured | set(KNOWN_CHALLENGER_CAMPAIGN_IDS)
                  | {EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID}
                  | ({followup} if followup else set()))


def test_the_poll_covers_the_originals_as_well_as_the_v2_routes():
    v2_routes = {"INSTANTLY_CAMPAIGN_%d" % i: cid
                 for i, cid in enumerate(sorted(CHALLENGER_V2_BY_ORIGINAL.values()))}
    polled = set(_polled(v2_routes))
    assert set(CHALLENGER_V2_BY_ORIGINAL.values()) <= polled, "the live routes"
    assert set(CHALLENGER_V2_BY_ORIGINAL) <= polled, "and the paused originals"
    assert len(polled) == 19   # eighteen Challenger ids plus the recovery campaign


def test_the_ooo_followup_is_polled_when_configured():
    polled = set(_polled({"INSTANTLY_CAMPAIGN_X": "some-v2"}, followup="ooo-id"))
    assert "ooo-id" in polled
    assert "some-v2" in polled


def test_a_campaign_nobody_configured_is_still_not_polled():
    polled = set(_polled({"INSTANTLY_CAMPAIGN_X": "some-v2"}))
    assert "45ac1e03-67e7-4bdd-b372-808042104e4c" not in polled, "a Control campaign"
    assert "unrelated-legacy-campaign" not in polled


def test_the_recovery_campaign_is_polled_although_it_is_not_a_challenger_id():
    """It is the one campaign we send from that nothing else would listen to.

    Kept out of `KNOWN_CHALLENGER_CAMPAIGN_IDS` on purpose -- the daily run must never
    route to it -- so its coverage has to be stated separately or it is not there at all.
    A reply from one of its 5,934 recipients revokes the rest of their authorisation, and
    it cannot do that if nothing reads it.
    """
    polled = set(_polled({"INSTANTLY_CAMPAIGN_X": "some-v2"}))
    assert EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID in polled
    assert EMPTY_EMAIL_RECOVERY_CAMPAIGN_ID not in KNOWN_CHALLENGER_CAMPAIGN_IDS
