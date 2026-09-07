"""Company identity resolved from EVIDENCE, on companies nobody wrote down.

The five 2026-09-07 holds were first cleared by five hand-written equivalences.
That cleared those five and nothing else: a sixth company with the same shape was
still held, and would be held today. These tests are about the replacement -- the
rules that derive an identifier from a company's own published name -- and they are
deliberately weighted toward companies that appear in no list anywhere:

* the corpus below is pinned with an EMPTY overrides file, so nothing can pass by
  being named;
* the adversarial half (parent domains, shared applicant-tracking hosts, initialisms,
  government portals, franchises) must still be held, or the rules are merely looser
  rather than better;
* the path is exercised end to end -- resolver, mapper, send-safe gate, Airtable
  suppression -- with providers and Airtable simulated, because a display name that
  resolves but never reaches a row is not a fix;
* caches are exercised across a version bump, an expiry and a restart, because
  evidence gathered once must not decide forever.
"""

import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import airtable_client
import config
from airtable_client import (
    _company_function_keys_from_fields,
    _company_identity_keys_from_fields,
    _job_to_fields,
    push_leads,
    send_safe_facts,
)
from company_anchor_evidence import (
    best_derivation,
    domain_anchor_forms,
    rename_correspondence,
    slug_anchor_forms,
)
from company_display_resolver import (
    CACHE_TTL_DAYS,
    RESOLVER_VERSION,
    CompanyDisplayCache,
    resolve_company_display,
)
from tests.test_fantastic_send_safe_auto_approval import _fantastic_job

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance"))
from company_identity_corpus import CORPUS, FIELDS, resolve_corpus  # noqa: E402


def _empty_overrides(tmp_path) -> Path:
    path = tmp_path / "no_overrides.json"
    path.write_text(json.dumps({"entries": {}, "aliases": {}}), encoding="utf-8")
    return path


def _resolve(tmp_path, *, name, slug="", domain="", other_name="", website="",
             tag="c"):
    return resolve_company_display(
        organization=name, org_linkedin_name=other_name, org_linkedin_slug=slug,
        employer_domain=domain, org_linkedin_website=website,
        cache=CompanyDisplayCache(tmp_path / f"{tag}.json",
                                  overrides_path=_empty_overrides(tmp_path)),
        persist=False)


# ---------------------------------------------------------------------------
# 1. The corpus: every row, with no manual entry available to any of them
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("row", CORPUS, ids=[r[0] for r in CORPUS])
def test_corpus_row_meets_its_expectation(row):
    """Pins the after-state so neither direction can drift silently.

    A later change that re-holds a released row fails here, and so does one that
    releases a row this work holds on purpose.
    """
    record = dict(zip(FIELDS, row))
    resolved = {r["id"]: r for r in resolve_corpus()}[record["id"]]
    assert resolved["hold"] is (record["expectation"] == "hold"), record["note"]
    assert resolved["manual_override"] is False, "no row may pass by being listed"
    if resolved["hold"]:
        assert resolved["missing_evidence"], "a hold must say what would settle it"


def test_the_corpus_actually_contains_unseen_companies():
    """Guards the guard: a corpus of only the five fixed cases proves nothing."""
    reviewed = {"endeavor_attested", "endeavor_bare", "bsb", "kai", "nash", "zwicker"}
    unseen_resolves = [r["id"] for r in resolve_corpus()
                       if not r["hold"] and r["id"] not in reviewed]
    assert len(unseen_resolves) >= 4, unseen_resolves


def test_string_identical_pairs_are_separated_only_by_corroboration():
    """The homonym fix, stated as the property that makes it general.

    ``Apple``/``applebank``/``apple.com`` and ``Clark``/``clarkaudit``/
    ``getclark.com`` stand in the SAME string relation. No rule reading only those
    strings can clear one and hold the other, so any rule that tried was wrong about
    one of them. What separates them here is the organization's own declared
    website, and nothing else -- which is why it works on companies never seen.
    """
    rows = {r["id"]: r for r in resolve_corpus()}
    assert rows["homonym_prefix"]["relation"] == rows["vanity_get_bare"]["relation"]
    assert rows["homonym_prefix"]["hold"] is True
    assert rows["vanity_get_bare"]["hold"] is True          # same shape, same outcome
    assert rows["vanity_get_attested"]["hold"] is False     # corroborated, so it clears
    # And an attestation pointing somewhere else corroborates nothing.
    assert rows["homonym_counter_attested"]["hold"] is True


# ---------------------------------------------------------------------------
# 2. The derivations themselves, and what they refuse
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,domain,rule", [
    ("BS&B Safety Systems", "bsbsystems.com", "token_subsequence"),
    ("Zwicker & Associates, P.C.", "zwickerpc.com", "token_subsequence"),
    ("Northwind Logistics Group", "northwindgroup.com", "token_subsequence"),
    ("Kai", "kai.security", "domain_leading_label"),
    ("Nash", "usenash.com", "vanity_prefix_stripped"),
])
def test_each_derivation_reports_how_it_reached_the_anchor(name, domain, rule):
    derivation = best_derivation(name, domain_anchor_forms(domain))
    assert derivation is not None
    assert derivation.rule == rule
    assert derivation.anchor_key
    # The words consumed are recorded, so the construction can be re-checked by
    # hand rather than trusted.
    assert derivation.words_used


@pytest.mark.parametrize("name,domain", [
    ("Minth North America", "minthgroup.com"),      # subsidiary under a group domain
    ("Instagram", "meta.com"),                       # brand under a parent domain
    ("Acme Corp", "applicantpro.com"),               # shared applicant-tracking host
    ("Resource Management Concepts", "rmcweb.com"),  # initialism, deliberately absent
    ("Hex Technologies", "hexagon.com"),             # shared opening fragment
    ("Diamond Jo Casino & Hotel", "boydgaming.com"),  # operator's corporate domain
    ("Massachusetts Department of Developmental Services", "mass.gov"),
    ("Resa", "theresa.com"),                         # 'the' is not a vanity prefix
])
def test_no_derivation_is_invented_for_a_different_organization(name, domain):
    assert best_derivation(name, domain_anchor_forms(domain)) is None


def test_a_derivation_never_skips_the_first_word_of_the_name():
    """``systems`` alone must not carry a match; the leading word is mandatory.

    Without this, any company whose name ends in a common word could derive a
    domain built from that word -- ``Globex Systems`` would 'derive' ``systems.com``.
    """
    assert best_derivation("Globex Systems", domain_anchor_forms("systems.com")) is None
    assert best_derivation("Globex Systems", domain_anchor_forms("globexsystems.com"))


def test_a_rename_needs_a_long_leading_token_shared_by_both_names():
    assert rename_correspondence(["Endeavor Business Media", "EndeavorB2B"])["related"]
    assert not rename_correspondence(["Micro Focus", "Microsoft"])["related"]
    assert not rename_correspondence(["Acme Corp", "ApplicantPro"])["related"]
    assert not rename_correspondence(["Instagram", "Meta"])["related"]


# ---------------------------------------------------------------------------
# 3. Contradictory evidence
# ---------------------------------------------------------------------------

def test_two_corroborated_names_that_are_unrelated_stay_held(tmp_path):
    """Both names check out against an identifier, and they are not one company.

    This is the shape a rebrand produces MINUS the thing that makes a rebrand
    legible: no leading token of either name opens the other. Two attested names
    that share nothing are evidence of ambiguity, not of a rename.
    """
    result = _resolve(tmp_path, name="Globex Industries", other_name="Acme Holdings",
                      slug="acmeholdings", domain="globexindustries.com")
    assert result.hold is True
    assert "linkedin_slug_domain_disagreement" in result.evidence["reasons"]
    assert result.evidence["missing_evidence"]["need"]


def test_a_name_corroborated_by_neither_identifier_is_held_not_guessed(tmp_path):
    result = _resolve(tmp_path, name="Contoso", slug="northwind", domain="fabrikam.com")
    assert result.hold is True
    assert result.evidence["conflict_resolution"]["resolved"] is False


def test_the_same_name_under_a_different_identifier_does_not_inherit(tmp_path):
    """Evidence is about a name AND its anchors, never about the name alone."""
    cache = CompanyDisplayCache(tmp_path / "shared.json",
                                overrides_path=_empty_overrides(tmp_path))
    first = resolve_company_display(organization="Kai", org_linkedin_slug="kaisecurity",
                                    employer_domain="kai.security", cache=cache)
    assert first.hold is False
    moved = resolve_company_display(organization="Kai", org_linkedin_slug="kaisecurity",
                                    employer_domain="kai-unrelated.example", cache=cache)
    assert moved.hold is True, "a new anchor requires a fresh decision"


# ---------------------------------------------------------------------------
# 4. Cache vigency: version, expiry, restart
# ---------------------------------------------------------------------------

def _seed_cache(path, *, version, resolved_at, manual=False, name="Stale Name"):
    path.write_text(json.dumps({
        "schema": "company-display-cache/1",
        "entries": {"linkedin:acmeco": {
            "display_name": name, "confidence": "high", "identity_safe": True,
            "identity_keys": ["linkedin:acmeco", "domain:acme.com"],
            "evidence": {}, "resolver_version": version,
            "resolved_at": resolved_at, "manual_override": manual}},
        "aliases": {},
    }), encoding="utf-8")


def test_an_entry_from_an_older_resolver_is_not_reused(tmp_path):
    """The defect that made every improvement invisible to companies already seen.

    A cached decision carried no version check on read, so the first answer a
    company ever got was the answer it kept. Every correction after that reached
    only companies never resolved before.
    """
    cache_path = tmp_path / "cache.json"
    _seed_cache(cache_path, version="company-display/1", resolved_at="2026-09-07T00:00:00Z")
    result = resolve_company_display(
        organization="Acme", org_linkedin_slug="acmeco", employer_domain="acme.com",
        cache=CompanyDisplayCache(cache_path, overrides_path=_empty_overrides(tmp_path)),
        persist=False)
    assert result.name != "Stale Name"
    assert result.evidence["cache_hit"] is False


def test_an_entry_older_than_the_ttl_is_not_reused(tmp_path):
    """Anchors move under one company; evidence gathered once must not decide forever."""
    cache_path = tmp_path / "cache.json"
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(time.time() - (CACHE_TTL_DAYS + 2) * 86400))
    _seed_cache(cache_path, version=RESOLVER_VERSION, resolved_at=old)
    result = resolve_company_display(
        organization="Acme", org_linkedin_slug="acmeco", employer_domain="acme.com",
        cache=CompanyDisplayCache(cache_path, overrides_path=_empty_overrides(tmp_path)),
        persist=False)
    assert result.evidence["cache_hit"] is False


def test_an_entry_with_no_recorded_age_is_treated_as_stale(tmp_path):
    cache_path = tmp_path / "cache.json"
    _seed_cache(cache_path, version=RESOLVER_VERSION, resolved_at="")
    result = resolve_company_display(
        organization="Acme", org_linkedin_slug="acmeco", employer_domain="acme.com",
        cache=CompanyDisplayCache(cache_path, overrides_path=_empty_overrides(tmp_path)),
        persist=False)
    assert result.evidence["cache_hit"] is False


def test_a_current_entry_is_reused_and_a_reviewed_override_never_expires(tmp_path):
    """A human decision is not a cached computation and does not age out."""
    fresh = tmp_path / "fresh.json"
    _seed_cache(fresh, version=RESOLVER_VERSION,
                resolved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                name="Acme Reused")
    hit = resolve_company_display(
        organization="Acme", org_linkedin_slug="acmeco", employer_domain="acme.com",
        cache=CompanyDisplayCache(fresh, overrides_path=_empty_overrides(tmp_path)),
        persist=False)
    assert hit.evidence["cache_hit"] is True and hit.name == "Acme Reused"

    ancient = tmp_path / "manual.json"
    _seed_cache(ancient, version="company-display/0", resolved_at="2020-01-01T00:00:00Z",
                manual=True, name="Reviewed By Hand")
    kept = resolve_company_display(
        organization="Acme", org_linkedin_slug="acmeco", employer_domain="acme.com",
        cache=CompanyDisplayCache(ancient, overrides_path=_empty_overrides(tmp_path)),
        persist=False)
    assert kept.evidence["cache_hit"] is True and kept.name == "Reviewed By Hand"


def test_a_restart_reloads_the_same_decision_from_disk(tmp_path):
    """A resolved company survives the process that resolved it."""
    path = tmp_path / "persisted.json"
    overrides = _empty_overrides(tmp_path)
    first = resolve_company_display(
        organization="Northwind Logistics Group",
        org_linkedin_slug="northwindlogisticsgroup",
        employer_domain="northwindgroup.com",
        cache=CompanyDisplayCache(path, overrides_path=overrides), persist=True)
    assert first.hold is False and path.is_file()
    # A brand-new cache object over the same file is what the next run does.
    second = resolve_company_display(
        organization="Northwind Logistics Group",
        org_linkedin_slug="northwindlogisticsgroup",
        employer_domain="northwindgroup.com",
        cache=CompanyDisplayCache(path, overrides_path=overrides), persist=False)
    assert second.name == first.name
    assert second.evidence["cache_hit"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["entries"][
        first.identity_key]["resolver_version"] == RESOLVER_VERSION


# ---------------------------------------------------------------------------
# 5. End to end: resolver -> mapper -> send-safe gate -> Airtable, all simulated
# ---------------------------------------------------------------------------

def _job_for(display, *, bucket="gtm_revenue", email=None, domain=None):
    domain = domain or (display.identity_key.split(":", 1)[1]
                        if display.identity_key.startswith("domain:") else "example.test")
    return _fantastic_job(
        lead_key=f"{domain}|{email or 'hm@' + domain}|{bucket}",
        canonical_company_name=display.name or "Unresolved",
        employer_name=display.name or "Unresolved",
        company_domain=domain, _role_bucket=bucket,
        hiring_manager_email=email or f"hm@{domain}",
        outbound_company_name=display.name,
        outbound_company_confidence=display.confidence,
        outbound_company_identity_key=display.identity_key,
        _outbound_company_hold=display.hold)


def _push(jobs, existing):
    last = {"n": 0}

    def fake_req(method, url, **kw):
        body = kw.get("json_body") or {}
        if isinstance(body, dict) and "records" in body:
            last["n"] = len(body["records"])
        return Mock()

    with (
        patch.object(airtable_client, "validate_preflight", return_value=None),
        patch.object(airtable_client, "_get_existing_leads", return_value=existing),
        patch.object(config, "AIRTABLE_RATE_LIMIT_DELAY", 0),
        patch.object(config, "AIRTABLE_SUPPRESS_EXISTING_COMPANY_FUNCTION", True),
        patch.object(airtable_client, "request_with_retry", side_effect=fake_req),
        patch.object(airtable_client, "safe_json",
                     side_effect=lambda r: {"records": [{"id": f"r{i}"}
                                                        for i in range(last["n"])]}),
    ):
        return push_leads(jobs)


def test_a_generally_resolved_company_reaches_a_row_and_a_held_one_does_not(tmp_path):
    """The whole point: resolving the name has to produce a deliverable row.

    Providers and Airtable are simulated; every other gate is the production one.
    """
    resolved = _resolve(tmp_path, name="BS&B Safety Systems",
                        slug="bsbsafetysystems", domain="bsbsystems.com", tag="ok")
    held = _resolve(tmp_path, name="Minth North America",
                    other_name="Minth North America, Inc.",
                    slug="minthnorthamericainc", domain="minthgroup.com", tag="held")
    assert resolved.hold is False and held.hold is True

    ok, reason = send_safe_facts(_job_to_fields(_job_for(resolved)))
    assert (ok, reason) == (True, "send_safe")
    blocked, blocked_reason = send_safe_facts(_job_to_fields(_job_for(held)))
    assert blocked is False
    assert blocked_reason == "outbound_company_held_for_review"

    pushed = _push([_job_for(resolved)], {})
    assert pushed["created"] == 1


def test_resolving_the_name_does_not_relax_any_other_gate(tmp_path):
    """Identity is one gate. Email, contact and role must still each be able to stop it."""
    display = _resolve(tmp_path, name="Kai", slug="kaisecurity", domain="kai.security")
    base = _job_for(display)
    assert send_safe_facts(_job_to_fields(base))[0] is True
    for override in ({"apollo_email_status": "unverified"},
                     {"_contact_gate_state": "NEEDS_CHECK"},
                     {"_email_gate_state": "FAIL"},
                     {"_outbound_role_hold": True, "outbound_role_confidence": "low"}):
        assert send_safe_facts(_job_to_fields({**base, **override}))[0] is False, override


# ---------------------------------------------------------------------------
# 6. Suppression: the stable identifier the resolver already decided on
# ---------------------------------------------------------------------------

def test_one_organization_under_two_domains_is_one_company_for_suppression():
    """Two domains, one LinkedIn organization -- previously two rows for one company."""
    left = _company_identity_keys_from_fields({
        "Website": "https://acme.com", "Company": "Acme",
        "Outbound Company Identity": "linkedin:acmeco",
        "Outbound Company Confidence": "high"})
    right = _company_identity_keys_from_fields({
        "Website": "https://acme-careers.example", "Company": "Acme Global",
        "Outbound Company Identity": "linkedin:acmeco",
        "Outbound Company Confidence": "medium"})
    assert left & right == {"linkedin:acmeco"}


def test_two_employers_on_one_applicant_tracking_host_still_do_not_match():
    """The identity key must not reintroduce what the domain rule already refuses."""
    left = _company_identity_keys_from_fields({
        "Website": "https://applicantpro.com", "Company": "Acme",
        "Outbound Company Identity": "domain:applicantpro.com"})
    right = _company_identity_keys_from_fields({
        "Website": "https://applicantpro.com", "Company": "Beta",
        "Outbound Company Identity": "domain:applicantpro.com"})
    assert not (left & right)


def test_a_held_row_carries_no_identity_and_suppresses_nothing_extra():
    keys = _company_identity_keys_from_fields({
        "Website": "https://gamma.com", "Company": "Gamma",
        "Outbound Company Identity": ""})
    assert keys == {"domain:gamma.com", "name:gamma"}


def test_the_identity_key_stays_function_aware():
    """Acme+Marketing and Acme+Sales remain distinct opportunities."""
    fields = {"Website": "https://acme.com", "Company": "Acme", "Role Bucket": "marketing",
              "Outbound Company Identity": "linkedin:acmeco",
              "Outbound Company Confidence": "high"}
    marketing = _company_function_keys_from_fields(fields)
    sales = _company_function_keys_from_fields({**fields, "Role Bucket": "gtm_revenue"})
    assert "linkedin:acmeco|bucket:marketing" in marketing
    assert not (marketing & sales)


def test_a_second_batch_for_the_same_organization_is_suppressed_by_identity(tmp_path):
    """Two BATCHES, one organization -- and NOTHING links them but the identity key.

    Batch one writes the row. Batch two is the same LinkedIn organization posting
    under a different domain AND a different published label, which is what a
    regional entity or a domain moved mid-rebrand looks like. The domain differs and
    the name differs, so the old key set matched on neither and created a second
    active row for one company x function.

    The differing name is the whole point of the fixture: with a shared label the
    old `name:` key would have caught it anyway and this would prove nothing.
    """
    display = _resolve(tmp_path, name="Kai", slug="kaisecurity", domain="kai.security")
    first = _job_for(display, email="a@kai.security", domain="kai.security")
    assert _push([first], {})["created"] == 1

    existing = {first["lead_key"]: {"id": "rec1", "fields": {
        "Lead Key": first["lead_key"], "Company": "Kai",
        "Website": "https://kai.security", "Role Bucket": "gtm_revenue",
        "Outbound Company Identity": display.identity_key,
        "Outbound Company Confidence": display.confidence, "Status": "Pending"}}}
    second = _job_for(display, email="b@kai-eu.example", domain="kai-eu.example")
    second["employer_name"] = second["canonical_company_name"] = "Kai Security Europe"
    # Neither legacy key can bridge these two rows.
    assert not (_company_identity_keys_from_fields(existing[first["lead_key"]]["fields"])
                - {display.identity_key}) & {"domain:kai-eu.example",
                                             "name:kai security europe"}
    result = _push([second], existing)
    assert result["created"] == 0
    assert second["lead_key"] in result["suppressed_company_lead_keys"]


# ---------------------------------------------------------------------------
# 7. Cost: a free lane is not a missing bill
# ---------------------------------------------------------------------------

def test_a_free_lane_is_known_zero_and_does_not_blank_the_run_total():
    from orchestrator import source_cost

    costs = source_cost.costs_by_source({
        "fantastic_jobs_ats": {"returned_billed": 500},
        "ats_greenhouse": {"postings_in": 40},
        "himalayas": {"postings_in": 12},
    })
    assert costs["ats_greenhouse"] == {"credits": 0, "basis": source_cost.KNOWN_ZERO}
    assert costs["himalayas"]["credits"] == 0
    total = source_cost.run_total(costs)
    assert total["credits"] == 500
    assert total["unknown_cost_sources"] == []
    assert set(total["known_zero_cost_sources"]) == {"ats_greenhouse", "himalayas"}


def test_a_paid_source_with_no_billing_total_is_unknown_and_is_named():
    from orchestrator import source_cost

    costs = source_cost.costs_by_source({
        "fantastic_jobs_linkedin": {},              # paid, answered nothing
        "ats_greenhouse": {"postings_in": 40},      # free, nothing to answer
    })
    assert costs["fantastic_jobs_linkedin"] == {
        "credits": None, "basis": source_cost.PAID_BILLING_ABSENT}
    total = source_cost.run_total(costs)
    assert total["credits"] is None
    assert total["unknown_cost_sources"] == ["fantastic_jobs_linkedin"]
    assert total["known_zero_cost_sources"] == ["ats_greenhouse"]


def test_an_unclassified_source_is_never_assumed_free():
    from orchestrator import source_cost

    cost = source_cost.source_cost("some_new_provider", {"postings_in": 10})
    assert cost == {"credits": None, "basis": source_cost.UNCLASSIFIED}


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# ---------------------------------------------------------------------------
# 8. What the conflict resolution will and will not accept
# ---------------------------------------------------------------------------

def test_two_anchors_derived_from_two_DIFFERENT_names_do_not_resolve(tmp_path):
    """One name must source both identifiers, not one each.

    Each identifier being derivable from SOME published name is not evidence that
    they name one organization -- that is precisely the shape of a posting whose
    employer and whose domain owner are different companies. Only a single name
    that both are built from settles it.
    """
    result = _resolve(tmp_path, name="Northwind Logistics Group",
                      other_name="Southgate Freight Systems",
                      slug="southgatesystems", domain="northwindgroup.com")
    assert result.hold is True
    assert result.evidence["conflict_resolution"]["resolved"] is False


def test_one_name_sourcing_both_identifiers_resolves_at_medium(tmp_path):
    result = _resolve(tmp_path, name="Northwind Logistics Group",
                      slug="northwindlogisticsgroup", domain="northwindgroup.com")
    assert result.hold is False
    assert result.confidence == "medium", "a resolved conflict is never promoted to high"
    resolution = result.evidence["conflict_resolution"]
    assert resolution["basis"] == "both_identifiers_accounted_for_by_one_published_name"
    assert resolution["derivations"]["domain"]["rule"] == "token_subsequence"


def test_the_organizations_own_profile_can_attest_the_correspondence(tmp_path):
    """LinkedIn stating this slug's website IS this domain settles the disagreement.

    Not us inferring it from spelling -- the organization's own page asserting it.
    Anything less (a different declared website, or none) leaves the row held.
    """
    attested = resolve_company_display(
        organization="Globex Industries", org_linkedin_slug="acmeholdings",
        employer_domain="globexindustries.com",
        org_linkedin_website="https://globexindustries.com",
        cache=CompanyDisplayCache(tmp_path / "a.json",
                                  overrides_path=_empty_overrides(tmp_path)),
        persist=False)
    assert attested.hold is False
    assert attested.evidence["conflict_resolution"]["basis"] == (
        "linkedin_organization_profile_website")

    elsewhere = resolve_company_display(
        organization="Globex Industries", org_linkedin_slug="acmeholdings",
        employer_domain="globexindustries.com",
        org_linkedin_website="https://somewhere-else.example",
        cache=CompanyDisplayCache(tmp_path / "b.json",
                                  overrides_path=_empty_overrides(tmp_path)),
        persist=False)
    assert elsewhere.hold is True


# ---------------------------------------------------------------------------
# 9. A company hold is retryable, and a rule change reaches it
# ---------------------------------------------------------------------------

def test_a_company_hold_is_not_a_terminal_state(tmp_path):
    """The property that makes correcting the rules worth anything.

    A held row is withheld at delivery, not rejected. If that made its posting
    terminal it would enter cross-run suppression and no later run -- however much
    better its rules -- would ever look at it again, so every identity correction
    would apply only to inventory bought afterwards.

    ``terminal_posting_ids`` admits FINAL_PASS only when delivery evidenced the key,
    and REJECT. A withheld FINAL_PASS and a NEEDS_CHECK are both excluded, so the
    posting stays in custody for a later run.
    """
    from orchestrator.enrichment import Disposition, EnrichmentReport, Lead
    from orchestrator.reasons import ReasonCode

    withheld = Lead(posting_id="p-withheld", company={"name": "Held Co"},
                    contact={"email": "hm@held.example"}, contact_key="k-withheld",
                    disposition=Disposition.FINAL_PASS, primary_reason=ReasonCode.OK)
    needs_check = Lead(posting_id="p-needs", company={"name": "Held Co"},
                       contact={"email": "hm2@held.example"}, contact_key="k-needs",
                       disposition=Disposition.NEEDS_CHECK,
                       primary_reason=ReasonCode.OK)
    delivered = Lead(posting_id="p-ok", company={"name": "Fine Co"},
                     contact={"email": "hm@fine.example"}, contact_key="k-ok",
                     disposition=Disposition.FINAL_PASS, primary_reason=ReasonCode.OK)
    report = EnrichmentReport(stages=[], leads=[withheld, needs_check, delivered],
                              funnel={})
    terminal = report.terminal_posting_ids(delivered_lead_keys=["k-ok"])
    assert "p-ok" in terminal
    assert "p-withheld" not in terminal, "a withheld row must stay retryable"
    assert "p-needs" not in terminal


def test_new_evidence_clears_a_row_that_was_held_under_the_old_record(tmp_path):
    """Same company, same anchors; the record gains its declared website.

    This is what a retry buys. The first pass has nothing to corroborate a prefix
    relation and holds. The second has the organization's own website and clears --
    without any entry naming the company, and without the cache pinning the first
    answer.
    """
    cache = CompanyDisplayCache(tmp_path / "shared.json",
                               overrides_path=_empty_overrides(tmp_path))
    first = resolve_company_display(
        organization="Clark", org_linkedin_slug="clarkaudit",
        employer_domain="getclark.com", cache=cache)
    assert first.hold is True

    second = resolve_company_display(
        organization="Clark", org_linkedin_slug="clarkaudit",
        employer_domain="getclark.com", org_linkedin_website="https://getclark.com",
        cache=cache)
    assert second.hold is False
    assert second.evidence["cache_hit"] is False, "the hold must not have been cached"
    assert second.evidence["conflict_resolution"]["basis"] == (
        "linkedin_organization_profile_website")


def test_an_updated_rule_reaches_a_company_the_old_rule_already_answered(tmp_path):
    """A resolver upgrade must not stop at companies nobody has seen yet.

    An entry written by an earlier resolver is retired on version alone, so the row
    is decided again under the current rules instead of inheriting the old verdict.
    """
    from company_display_resolver import RESOLVER_VERSION

    path = tmp_path / "cache.json"
    path.write_text(json.dumps({
        "schema": "company-display-cache/1",
        "entries": {"linkedin:clarkaudit": {
            "display_name": "Stale Answer", "confidence": "high",
            "identity_safe": True,
            "identity_keys": ["linkedin:clarkaudit", "domain:getclark.com"],
            "evidence": {}, "resolver_version": "company-display/1",
            "resolved_at": "2026-09-07T00:00:00Z", "manual_override": False}},
        "aliases": {},
    }), encoding="utf-8")
    result = resolve_company_display(
        organization="Clark", org_linkedin_slug="clarkaudit",
        employer_domain="getclark.com",
        cache=CompanyDisplayCache(path, overrides_path=_empty_overrides(tmp_path)),
        persist=False)
    assert result.name != "Stale Answer"
    assert result.evidence["resolver_version"] == RESOLVER_VERSION
    assert result.hold is True, "re-decided under the current rules, not inherited"
