"""R01 -- Apollo People Search returns limited data (no LinkedIn, no email, no history).
A valid search id must reach enrichment; the complete checks run on the enriched record;
search-only gaps never blacklist a candidate. Real client + real service, SIMULATED Apollo
in its documented shape, real PostgreSQL."""

from __future__ import annotations

import pytest

from tgtc_core.policy.campaigns import FUNCTION_KEYS
from tgtc_core.testing.fakes import make_person, search_projection
from tgtc_core.testing.scenario import BUYER_TITLE_BY_FUNCTION
from tests_core.helpers import opportunity_service, sql1, sqlall
from tests_core.seed import apollo_for, good_buyer, seed_opportunity


def test_documented_search_shape_has_no_linkedin_email_or_history():
    p = make_person(id="p1", first="A", last="B", title="VP Product", org_name="Acme", org_domain="acme.com",
                    email="a@acme.com", email_status="verified")
    s = search_projection(p)
    assert "linkedin_url" not in s and "email" not in s and "employment_history" not in s
    assert s["id"] == "p1" and s["title"] == "VP Product" and s["organization"]["name"] == "Acme"


@pytest.mark.parametrize("function_key", FUNCTION_KEYS)
def test_sparse_search_result_is_enriched_and_then_fully_gated(conn, clock, function_key):
    domain, org = "acme.com", "Acme"
    pid, eid, oid = seed_opportunity(conn, clock, function_key=function_key, domain=domain, org_name=org)
    fake = apollo_for(domain, org, function_key=function_key)          # default fake: sparse search
    assert fake.rich_search is False
    out = opportunity_service(conn, fake, clock).process(oid)
    assert out.outcome == "approved", out
    searches = [r for r in fake.requests if r["path"].endswith("/mixed_people/api_search")]
    matches = [r for r in fake.requests if r["path"].endswith("/people/match")]
    assert len(searches) >= 1 and len(matches) == 1
    # the approved lead carries LinkedIn and the verified email -- both came from enrichment
    lead = sqlall(conn, "SELECT lead_json FROM approvals")[0]["lead_json"]
    assert lead["linkedin_url"].startswith("https://www.linkedin.com/in/") and lead["email_status"] == "verified"
    assert sql1(conn, "SELECT count(*) FROM candidate_attempts WHERE reason = 'contact:no_linkedin_identity_anchor'") == 0


def test_enrichment_still_rejects_a_candidate_without_linkedin(conn, clock):
    """The LinkedIn requirement is enforced AFTER enrichment, not dropped."""
    pid, eid, oid = seed_opportunity(conn, clock)
    nolinkedin = good_buyer("acme.com", "Acme", id="p-nolinkedin", email="nolink@acme.com")
    nolinkedin["linkedin_url"] = ""
    good = good_buyer("acme.com", "Acme", id="p-good")
    fake = apollo_for("acme.com", "Acme", people=[nolinkedin, good])
    out = opportunity_service(conn, fake, clock).process(oid)
    assert out.outcome == "approved"
    assert sql1(conn, "SELECT lead_json->>'email' FROM approvals") == "good.buyer@acme.com"
    assert sql1(conn, "SELECT reason FROM candidate_attempts WHERE candidate_ref = 'pid:p-nolinkedin' AND attempt_kind = 'gate'") == "contact:no_linkedin_identity_anchor"
    assert sql1(conn, "SELECT count(*) FROM request_attempts WHERE operation = 'person_match'") == 2   # both were enriched


def test_pre_enrichment_skip_is_not_a_permanent_blacklist(conn, clock):
    """A title outside the hierarchy skips enrichment at zero cost; when the person's title
    later matches (new evidence), the same id is enriched and can be approved."""
    pid, eid, oid = seed_opportunity(conn, clock)
    person = good_buyer("acme.com", "Acme", id="p-x", email="x@acme.com")
    person["title"] = "Marketing Coordinator"
    fake = apollo_for("acme.com", "Acme", people=[person])
    out = opportunity_service(conn, fake, clock).process(oid)
    assert out.outcome == "closed" and out.reason == "no_verified_buyer_in_candidates"
    assert sql1(conn, "SELECT outcome FROM candidate_attempts WHERE candidate_ref = 'pid:p-x'") == "skipped_pre_enrichment"
    assert fake.served_paid == 0
    # new evidence reopens; the candidate now holds a buyer title
    from tests_core.seed import seed_opportunity as seed2
    person["title"] = BUYER_TITLE_BY_FUNCTION["customer_success"]
    seed2(conn, clock, job_id="job-2")
    out2 = opportunity_service(conn, fake, clock).process(oid)
    assert out2.outcome == "approved" and fake.served_paid == 1


def test_search_without_organization_block_still_reaches_enrichment(conn, clock):
    """Some search rows carry no organization details at all; that is not a rejection."""
    pid, eid, oid = seed_opportunity(conn, clock)
    person = good_buyer("acme.com", "Acme", id="p-noorg", email="noorg@acme.com")
    fake = apollo_for("acme.com", "Acme", people=[person])
    orig = fake.request

    def strip_org(method, url, **kw):
        resp = orig(method, url, **kw)
        if url.endswith("/mixed_people/api_search"):
            import json
            body = json.loads(resp.text)
            for p in body["people"]:
                p.pop("organization", None)
            resp.text = json.dumps(body)
        return resp

    fake.request = strip_org  # type: ignore[assignment]
    out = opportunity_service(conn, fake, clock).process(oid)
    assert out.outcome == "approved", out
