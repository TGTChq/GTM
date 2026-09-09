"""R11 -- known expiry and closure are enforced at approval and before a delayed delivery;
re-observation never resets commercial age."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tgtc_core.services.acquisition import upsert_posting
from tgtc_core.services.classification_service import classify_one
from tgtc_core.services.identity_service import resolve_posting_identity
from tgtc_core.services.lifecycle import expire_postings
from tgtc_core.testing.fakes import FakeAirtable, FakeInstantly, make_posting_row
from tgtc_core.testing.scenario import CONTROL_ID_BY_CAMPAIGN_KEY
from tests_core.helpers import delivery_service, opportunity_service, sql1, sqlall
from tests_core.seed import apollo_for, example_for

CS = CONTROL_ID_BY_CAMPAIGN_KEY["customer_experience"]


def _seed(conn, clock, *, valid_through, job_id="job-1"):
    ex = example_for("customer_success")
    row = make_posting_row(id=job_id, title=ex.title, organization="Acme", domain="acme.com", description=ex.description,
                           date_created=clock() - timedelta(hours=4), date_valid_through=valid_through)
    res = upsert_posting(conn, source="fantastic:active-jb", row=row, lane="fresh", now=clock())
    conn.commit()
    return res


def test_recently_posted_but_already_expired_vacancy_is_never_approved(conn, clock):
    res = _seed(conn, clock, valid_through=clock() - timedelta(hours=1))
    assert res.state == "expired" and res.expired
    assert sql1(conn, "SELECT state FROM postings") == "expired"
    assert sql1(conn, "SELECT count(*) FROM work_items") == 0
    assert sql1(conn, "SELECT count(*) FROM opportunities") == 0


def test_expiry_at_approval_time(conn, clock):
    res = _seed(conn, clock, valid_through=clock() + timedelta(minutes=30))
    resolve_posting_identity(conn, res.posting_id, now=clock())
    classify_one(conn, res.posting_id, inference=None, now=clock())
    oid = sql1(conn, "SELECT id FROM opportunities")
    clock.advance(hours=1)                                    # the vacancy expired while it waited
    out = opportunity_service(conn, apollo_for("acme.com", "Acme"), clock).process(oid)
    assert out.outcome == "closed" and out.reason in ("no_active_compatible_posting", "approval_refused:posting_expired")
    assert sql1(conn, "SELECT count(*) FROM approvals") == 0
    assert sql1(conn, "SELECT count(*) FROM request_attempts WHERE operation = 'person_match'") == 0   # nothing bought for it


@pytest.mark.parametrize("first_channel", ["airtable", "instantly"])
def test_approval_awaiting_delivery_is_revoked_when_its_vacancy_expires(conn, clock, first_channel):
    res = _seed(conn, clock, valid_through=clock() + timedelta(hours=2))
    resolve_posting_identity(conn, res.posting_id, now=clock())
    classify_one(conn, res.posting_id, inference=None, now=clock())
    oid = sql1(conn, "SELECT id FROM opportunities")
    assert opportunity_service(conn, apollo_for("acme.com", "Acme"), clock).process(oid).outcome == "approved"
    clock.advance(hours=3)                                    # delivery capacity arrives after expiry
    assert expire_postings(conn, now=clock())["postings_expired"] == 1
    at, ins = FakeAirtable(), FakeInstantly(campaign_status={CS: 1}, clock=clock)
    svc = delivery_service(conn, at, ins, clock)
    first = svc.drain(first_channel)
    second_channel = "instantly" if first_channel == "airtable" else "airtable"
    second = svc.drain(second_channel)
    assert [x.outcome for x in first] == ["blocked"] and first[0].reason.startswith("posting_no_longer_active")
    # Revocation already blocked BOTH channels atomically. The second drain must
    # claim nothing; an empty return is not evidence that delivery is still pending.
    assert second == []
    outbox = sqlall(conn, "SELECT channel, state, blocked_reason FROM delivery_outbox ORDER BY channel")
    assert [(r["channel"], r["state"]) for r in outbox] == [("airtable", "blocked"), ("instantly", "blocked")]
    assert all(r["blocked_reason"].startswith("posting_no_longer_active") for r in outbox)
    assert at.requests == [] and ins.requests == []
    assert len(at.records) == 0 and len(ins.leads) == 0
    assert sql1(conn, "SELECT state || ':' || revoke_reason FROM approvals").startswith("revoked:posting_no_longer_active")


def test_approval_awaiting_delivery_is_revoked_when_its_vacancy_changes(conn, clock):
    res = _seed(conn, clock, valid_through=None)
    resolve_posting_identity(conn, res.posting_id, now=clock())
    classify_one(conn, res.posting_id, inference=None, now=clock())
    oid = sql1(conn, "SELECT id FROM opportunities")
    assert opportunity_service(conn, apollo_for("acme.com", "Acme"), clock).process(oid).outcome == "approved"
    # the provider modifies the posting into a contract role before delivery
    ex = example_for("customer_success")
    changed = make_posting_row(id="job-1", title=ex.title, organization="Acme", domain="acme.com",
                               description="This is a 6-month contract role. " + ex.description,
                               date_created=clock() - timedelta(hours=4), employment_type="CONTRACT")
    res2 = upsert_posting(conn, source="fantastic:active-jb", row=changed, lane="fresh", now=clock())
    conn.commit()
    assert res2.state == "modified"
    at = FakeAirtable()
    out = delivery_service(conn, at, None, clock).drain("airtable")
    assert [x.outcome for x in out] == ["blocked"] and out[0].reason == "posting_evidence_changed_since_approval"
    assert len(at.records) == 0
    assert sql1(conn, "SELECT state FROM approvals") == "revoked"


def test_reobservation_never_resets_commercial_age(conn, clock):
    res = _seed(conn, clock, valid_through=None)
    anchor = sql1(conn, "SELECT commercial_age_anchor FROM postings")
    clock.advance(days=10)
    res2 = _seed(conn, clock, valid_through=None)
    assert res2.state == "unchanged"
    assert sql1(conn, "SELECT commercial_age_anchor FROM postings") == anchor
    assert sql1(conn, "SELECT last_confirmed_active_at FROM postings") == clock()
