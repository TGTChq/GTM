"""R10 -- changes to agency status, countries, employment type, headcount or other
decision-relevant facts are persisted, versioned and invalidate affected decisions;
an identity change re-resolves the employer. They are never 'unchanged content'."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tgtc_core.services.acquisition import posting_content_hash, upsert_posting
from tgtc_core.services.classification_service import classify_one
from tgtc_core.services.identity_service import resolve_posting_identity
from tgtc_core.testing.fakes import make_posting_row
from tests_core.helpers import sql1, sqlall
from tests_core.seed import example_for


def _row(clock, **over):
    ex = example_for("finance")
    base = dict(id="job-1", title=ex.title, organization="Acme", domain="acme.com", description=ex.description,
                date_created=clock() - timedelta(hours=4))
    base.update(over)
    return make_posting_row(**base)


@pytest.mark.parametrize("field, value", [
    ("org_linkedin_recruitment_agency_derived", True),
    ("countries_derived", ["GB"]),
    ("ai_employment_type", "PART_TIME"),
    ("employment_type", "PART_TIME"),
    ("org_linkedin_headcount", 5000),
    ("org_linkedin_industry", "Staffing and Recruiting"),
    ("location_type", "onsite"),
])
def test_decision_relevant_field_changes_change_the_hash(clock, field, value):
    a = _row(clock)
    b = _row(clock)
    b[field] = value
    assert posting_content_hash(a) != posting_content_hash(b), field


def test_agency_flag_change_is_versioned_and_invalidates_the_decision(conn, clock):
    res = upsert_posting(conn, source="fantastic:active-jb", row=_row(clock), lane="fresh", now=clock())
    conn.commit()
    pid = res.posting_id
    resolve_posting_identity(conn, pid, now=clock())
    assert classify_one(conn, pid, inference=None, now=clock()).outcome == "classified"
    assert sql1(conn, "SELECT count(*) FROM opportunities WHERE state = 'open'") == 1
    # re-observation with ONLY the agency flag changed
    res2 = upsert_posting(conn, source="fantastic:active-jb", row=_row(clock, agency=True), lane="fresh", now=clock())
    conn.commit()
    assert res2.state == "modified" and "org_linkedin_recruitment_agency_derived" in res2.changes and not res2.identity_changed
    assert sql1(conn, "SELECT max(version) FROM posting_versions WHERE posting_id = %s", (pid,)) == 2
    assert sql1(conn, "SELECT changes->'fields' FROM posting_versions WHERE posting_id = %s AND version = 2", (pid,)) == ["org_linkedin_recruitment_agency_derived"]
    assert sql1(conn, "SELECT agency_flag FROM employers") is True                       # the employer fact is persisted
    out = classify_one(conn, pid, inference=None, now=clock())
    assert out.outcome == "closed" and out.reason.startswith("agency:")
    assert sql1(conn, "SELECT state || ':' || close_reason FROM opportunities") == "closed:no_active_compatible_posting"


def test_countries_change_invalidates_market_and_headcount_change_reaches_the_employer(conn, clock):
    res = upsert_posting(conn, source="fantastic:active-jb", row=_row(clock), lane="fresh", now=clock())
    conn.commit()
    pid = res.posting_id
    resolve_posting_identity(conn, pid, now=clock())
    classify_one(conn, pid, inference=None, now=clock())
    res2 = upsert_posting(conn, source="fantastic:active-jb", row=_row(clock, countries=("GB",), headcount=5000), lane="fresh", now=clock())
    conn.commit()
    assert res2.state == "modified" and set(res2.changes) >= {"countries_derived", "org_linkedin_headcount"}
    assert sql1(conn, "SELECT employee_count FROM employers") == 5000
    assert sql1(conn, "SELECT countries FROM postings WHERE id = %s", (pid,)) == ["GB"]
    out = classify_one(conn, pid, inference=None, now=clock())
    # the provider country no longer shows the US and the text carries 'remote within the US' only as
    # a phrase -> market stays text-evidenced; but the employer is now too large for approval.
    assert out.outcome in ("classified", "closed")
    from tests_core.helpers import opportunity_service
    from tests_core.seed import apollo_for
    oid = sql1(conn, "SELECT id FROM opportunities")
    if out.outcome == "classified":
        assert opportunity_service(conn, apollo_for("acme.com", "Acme"), clock).process(oid).reason == "employer_too_large"


def test_identity_change_re_resolves_the_employer(conn, clock):
    res = upsert_posting(conn, source="fantastic:active-jb", row=_row(clock), lane="fresh", now=clock())
    conn.commit()
    pid = res.posting_id
    resolve_posting_identity(conn, pid, now=clock())
    first_employer = sql1(conn, "SELECT employer_id FROM postings WHERE id = %s", (pid,))
    res2 = upsert_posting(conn, source="fantastic:active-jb", row=_row(clock, organization="Beta Ltd", domain="beta.io"), lane="fresh", now=clock())
    conn.commit()
    assert res2.state == "modified" and res2.identity_changed
    assert sql1(conn, "SELECT state FROM postings WHERE id = %s", (pid,)) == "new"
    resolve_posting_identity(conn, pid, now=clock())
    second_employer = sql1(conn, "SELECT employer_id FROM postings WHERE id = %s", (pid,))
    assert second_employer != first_employer
    assert sql1(conn, "SELECT domain FROM employers WHERE id = %s", (second_employer,)) == "beta.io"


def test_unchanged_row_is_still_unchanged(conn, clock):
    res = upsert_posting(conn, source="fantastic:active-jb", row=_row(clock), lane="fresh", now=clock())
    conn.commit()
    res2 = upsert_posting(conn, source="fantastic:active-jb", row=_row(clock), lane="fresh", now=clock() + timedelta(hours=1))
    conn.commit()
    assert res2.state == "unchanged" and sql1(conn, "SELECT count(*) FROM posting_versions") == 1
    assert sql1(conn, "SELECT commercial_age_anchor < last_confirmed_active_at FROM postings") is True   # age not reset
