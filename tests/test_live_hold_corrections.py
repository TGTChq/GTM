"""Regressions derived from the Sep 7 run; all providers stay offline."""
import json
from pathlib import Path

import pytest

from company_display_resolver import CompanyDisplayCache, resolve_company_display
from run_maintenance import send_safe_forensics
from tests.test_fantastic_send_safe_auto_approval import _fantastic_job


#: The five company holds the 2026-09-07 run produced, as the RECORD carried them.
#: ``other_name`` is the second published name the provider returned for the same
#: posting where it returned one -- Endeavor's rebrand is only legible because the
#: record names the company twice, once per anchor, so dropping it would test a
#: record production never saw.
#: ``website`` is the organization's own declared website, present on the record.
#: Endeavor's rebrand needs it: two names inside one record are a SHAPE, and a
#: record can carry an incorrect association, so the shape alone no longer clears.
REVIEWED = [
    ("EndeavorB2B", "endeavorb2b", "endeavorbusinessmedia.com", "Endeavor Business Media"),
    ("BS&B Safety Systems", "bsbsafetysystems", "bsbsystems.com", "BS&B SAFETY SYSTEMS, LLC.."),
    ("Kai", "kaisecurity", "kai.security", ""),
    ("Nash", "", "usenash.com", ""),
    ("Zwicker & Associates, P.C.", "zwickerassociatespc", "zwickerpc.com", ""),
]


def _no_overrides(tmp_path):
    """A resolver with an EMPTY overrides file: nothing may come from a name list."""
    path = tmp_path / "no_overrides.json"
    path.write_text(json.dumps({"entries": {}, "aliases": {}}))
    return path


@pytest.mark.parametrize("name,slug,domain,other_name", REVIEWED)
def test_reviewed_identity_resolves_with_no_manual_entry_at_all(tmp_path, name, slug, domain, other_name):
    """The five 2026-09-07 holds clear from EVIDENCE, not from being listed.

    They were first cleared by five hand-written equivalences. That fixed those five
    companies and nothing else: a sixth company with the same shape stayed held. This
    asserts the replacement -- an empty overrides file, and they still resolve --
    which is the only version of the fix that reaches a company never seen before.
    """
    cache = CompanyDisplayCache(tmp_path / "cache.json", overrides_path=_no_overrides(tmp_path))
    # The rebrand publishes the CURRENT name on LinkedIn and the former one as the
    # organization, so the record is built the way the provider returned it.
    inputs = (dict(organization=other_name, org_linkedin_name=name) if other_name
              else dict(organization=name))
    inputs.update(org_linkedin_slug=slug, employer_domain=domain)
    if other_name:
        inputs["org_linkedin_website"] = f"https://{domain}"
    result = resolve_company_display(**inputs, cache=cache, persist=False)
    assert result.hold is False
    assert result.name == name
    assert result.evidence["manual_override"] is False
    # Changing either identifier is a DIFFERENT organization and must stay held: the
    # evidence is about this name and these anchors, never about the name alone --
    # and the cached approval must not travel with the name either.
    #
    # The attestation is dropped alongside the move, deliberately. A record that
    # still declares the ORIGINAL website for a moved anchor is a record asserting
    # that pair, and honouring it is correct; what must never happen is the name or
    # the cache carrying an approval to anchors nothing has vouched for.
    bare = {k: v for k, v in inputs.items() if k != "org_linkedin_website"}
    for other in ({"employer_domain": "unrelated.example"},
                  {"org_linkedin_slug": "unrelated"}):
        moved = resolve_company_display(**{**bare, **other}, cache=cache, persist=False)
        assert moved.hold, f"{other} must not inherit the resolved identity"
        assert moved.evidence["cache_hit"] is False


def test_no_manual_override_survives_that_the_general_path_already_reaches(tmp_path):
    """A hand-written equivalence is only legitimate where no rule can reach.

    This is what stops the list growing back. Every entry in the shipped overrides
    file is re-resolved from its OWN recorded evidence with overrides disabled; any
    entry the general resolver already produces the same name for is redundant and
    fails here, so the next person cannot quietly add one instead of extending the
    evidence rules.
    """
    overrides = Path(__file__).resolve().parents[1] / "company_display_overrides.json"
    data = json.loads(overrides.read_text(encoding="utf-8"))
    redundant = []
    for index, (key, entry) in enumerate(data["entries"].items()):
        evidence = entry.get("evidence") or {}
        slug = evidence.get("org_linkedin_slug") or (
            key.split(":", 1)[1] if key.startswith("linkedin:") else "")
        domain = evidence.get("domain") or next(
            (k.split(":", 1)[1] for k in entry.get("identity_keys", []) if k.startswith("domain:")), "")
        general = resolve_company_display(
            organization=evidence.get("organization") or entry["display_name"],
            org_linkedin_name=evidence.get("org_linkedin_name") or "",
            org_linkedin_slug=slug, employer_domain=domain,
            cache=CompanyDisplayCache(tmp_path / f"c{index}.json",
                                      overrides_path=_no_overrides(tmp_path)),
            persist=False)
        if not general.hold and general.name == entry["display_name"]:
            redundant.append(key)
        else:
            assert entry["evidence"].get("retained_because"), (
                f"{key} is not reachable by a general rule; say why it must be manual")
    assert not redundant, (
        f"these overrides are already produced by the evidence rules: {redundant}")


def test_a_parent_domain_still_holds_and_says_what_is_missing(tmp_path):
    """Minth North America on minthgroup.com -- the shape that must NOT resolve.

    A subsidiary posting under its group's domain looks exactly like the cases above
    minus the thing that makes them safe: no ordered subset of "Minth North America"
    spells ``minthgroup``. It stayed held through the whole change, and the hold now
    names the evidence that would settle it instead of being anonymous.
    """
    result = resolve_company_display(
        organization="Minth North America", org_linkedin_name="Minth North America, Inc.",
        org_linkedin_slug="minthnorthamericainc", employer_domain="minthgroup.com",
        cache=CompanyDisplayCache(tmp_path / "c.json", overrides_path=_no_overrides(tmp_path)),
        persist=False)
    assert result.hold is True
    assert "linkedin_slug_domain_disagreement" in result.evidence["reasons"]
    need = " ".join(result.evidence["missing_evidence"]["need"])
    assert "declared website" in need and "BOTH identifiers" in need


def _corpus(tmp_path, jobs):
    path = tmp_path / "run_artifacts/r1/enrichment/batch/jobs_enriched_1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"jobs": jobs}))
    return path


def test_forensics_reaches_holds_after_row_200_and_keeps_more_than_25_details(tmp_path):
    jobs = [_fantastic_job() for _ in range(201)]
    jobs.extend(_fantastic_job(_outbound_company_hold=True, outbound_company_confidence="low")
                for _ in range(27))
    source = _corpus(tmp_path, jobs)
    original = source.read_bytes()
    row = send_safe_forensics(tmp_path, ["r1"])["runs"][0]
    assert row["examined"] == 228
    assert row["send_safe"] == 201
    assert row["reasons"] == {"outbound_company_held_for_review": 27}
    assert len(row["verified_withheld"]) == 27
    assert row["scan_complete"] is True
    assert source.read_bytes() == original


def test_explicit_diagnostic_limit_is_labelled_partial(tmp_path):
    _corpus(tmp_path, [_fantastic_job() for _ in range(3)])
    row = send_safe_forensics(tmp_path, ["r1"], limit=2)["runs"][0]
    assert row["examined"] == 2
    assert row["scan_complete"] is False
    assert row["omitted_by_limit"] == 1


def test_unreadable_file_cannot_be_reported_as_complete(tmp_path):
    source = _corpus(tmp_path, [_fantastic_job()])
    source.with_name("jobs_enriched_broken.json").write_text("{broken")
    row = send_safe_forensics(tmp_path, ["r1"])["runs"][0]
    assert row["scan_complete"] is False
    assert row["unavailable"]


def test_cross_file_populations_are_not_claimed_to_be_distinct_leads(tmp_path):
    source = _corpus(tmp_path, [_fantastic_job()])
    source.with_name("jobs_enriched_copy.json").write_bytes(source.read_bytes())
    row = send_safe_forensics(tmp_path, ["r1"])["runs"][0]
    assert row["examined"] == 2
    assert row["population_unit"] == "retained_row_occurrences"
    assert row["population_identity_reconciled"] is False
    assert len(row["files"]) == 2


def test_original_reason_counts_and_approved_receipts_are_independent_of_replay(tmp_path, capsys):
    from run_maintenance import print_send_safe_forensics
    source = _corpus(tmp_path, [_fantastic_job() for _ in range(3)])
    original = {"delivery": {"created": 28, "send_safe_withheld": 27, "detail": {"airtable": {
        "created_approved_lead_keys": ["key-with-email@example.test"] * 2,
        "created_approval_status_unknown": 1,
        "not_written_send_safe_reasons": {"outbound_company_held_for_review": 26,
                                         "apollo_email_not_verified": 1}}}}}
    result_path = source.parents[2] / "orchestrator_result.json"
    result_path.write_text(json.dumps(original))
    result = send_safe_forensics(tmp_path, ["r1"], limit=1)
    facts = result["runs"][0]["original_delivery"]
    assert facts["confirmed_approved"] == 1
    assert facts["created_approval_status_unknown"] == 1
    assert facts["send_safe_reasons_reconcile"] is True
    assert facts["send_safe_withheld"] == 27
    print_send_safe_forensics(result)
    output = capsys.readouterr().out
    records = [json.loads(line) for line in output.splitlines()]
    assert records[0]["event"] == "send_safe_forensics_summary"
    assert records[0]["run_id"] == "r1"
    assert "@" not in output


def test_absent_original_receipts_stay_unknown():
    from orchestrator.delivery_evidence import delivery_evidence
    facts = delivery_evidence({"created": 28, "send_safe_withheld": 27})
    assert facts["confirmed_approved"] is None
    assert facts["send_safe_reasons"] is None
    assert facts["send_safe_reasons_reconcile"] is None


def test_source_yield_counts_billed_repeats_and_zero_kept_sources(tmp_path):
    from orchestrator.yield_ledger import YieldLedger
    from tests.test_yield_ledger_cache_allocator import _job
    ledger = YieldLedger(str(tmp_path / "ledger.jsonl"), "r1")
    ledger.record_acquired([_job(i, _acquisition_source="fantastic_jobs_ats") for i in range(328)])
    for i in range(22):
        ledger.mark(str(i), airtable_created=True)
    sources = ledger.summary(billed_by_source={"fantastic_jobs_ats": 500,
        "fantastic_jobs_linkedin": 100})["by_source"]
    assert sources["fantastic_jobs_ats"]["recorded_rows"] == 328
    assert sources["fantastic_jobs_ats"]["credits"] == 500
    assert sources["fantastic_jobs_ats"]["airtable_created_per_1k_credits"] == 44.0
    assert sources["fantastic_jobs_linkedin"]["credits"] == 100
    assert sources["fantastic_jobs_linkedin"]["airtable_created_per_1k_credits"] == 0
    assert ledger.summary()["credits"] is None
    disabled = YieldLedger(str(tmp_path / "disabled.jsonl"), "r1", enabled=False)
    absent = disabled.by_source(billed_by_source={"fantastic_jobs_ats": 500})
    assert absent["fantastic_jobs_ats"]["airtable_created_per_1k_credits"] is None


@pytest.mark.parametrize("name,slug,domain,other_name", REVIEWED)
def test_resolved_company_does_not_promote_unverified_email_or_unsafe_contact(tmp_path, name, slug, domain, other_name):
    from airtable_client import _job_to_fields, send_safe_facts
    cache = CompanyDisplayCache(tmp_path / "cache", overrides_path=
        Path(__file__).resolve().parents[1] / "company_display_overrides.json")
    display = resolve_company_display(organization=other_name or name,
        org_linkedin_name=name if other_name else "", org_linkedin_slug=slug,
        employer_domain=domain,
        org_linkedin_website=(f"https://{domain}" if other_name else ""),
        cache=cache, persist=False)
    job = _fantastic_job(canonical_company_name=name, company_domain=domain,
        hiring_manager_email=f"fixture@{domain}", outbound_company_name=display.name,
        outbound_company_confidence=display.confidence,
        outbound_company_identity_key=display.identity_key,
        _outbound_company_hold=display.hold)
    assert send_safe_facts(_job_to_fields(job)) == (True, "send_safe")
    assert send_safe_facts(_job_to_fields({**job, "apollo_email_status": "unverified"}))[0] is False
    assert send_safe_facts(_job_to_fields({**job, "_contact_gate_state": "NEEDS_CHECK"}))[0] is False
    assert send_safe_facts(_job_to_fields({**job, "_outbound_role_hold": True,
        "outbound_role_confidence": "low"}))[0] is False
