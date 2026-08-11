"""Regression tests: the signed Step-2 decisions must reach the orchestrator path.

Gap: ``job_filter.run_filter`` is the only place that wrote
``_work_arrangement`` / ``_remote_scope`` / ``_employment_quality`` (and their
reason fields). The production orchestrator path is
``RealEnrichmentStage -> run_precontact_qualification -> JobGate``, which never
calls ``run_filter``. Those fields were therefore absent on every record, and
``job_source_resolver._prefilter_full_time`` / ``_prefilter_supported_us_work``
-- which gate ``_provider_structured_review_fallback`` -- could never be
satisfied. The fallback was dead code on that path FOR EVERY SOURCE, and every
record fell through to SOURCE_UNRESOLVED -> UNVERIFIED_OFFICIAL_SOURCE.

These tests pin the fix:
  * orchestrator-path jobs receive the annotations;
  * full-time evidence reaches the fallback;
  * supported-US-work evidence reaches the fallback;
  * genuinely part-time / non-US records still fail (no rule weakened);
  * no new network call is introduced;
  * records that already carry Step-2 decisions are not re-derived;
  * behaviour is source-agnostic (no Fantastic/LinkedIn special case).
"""
from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import config
import job_filter
import job_source_resolver as jsr
from qualification_pipeline import run_precontact_qualification


def _job(**overrides) -> dict:
    posted = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    job = {
        "job_id": "j1",
        "job_title": "Account Executive",
        "employer_name": "Acme Analytics",
        "employer_website": "acmeanalytics.com",
        "job_description": (
            "Acme Analytics is hiring an Account Executive to own new business. "
            "You will build pipeline, run discovery calls and close deals. " * 12
        ),
        "job_apply_link": "https://www.linkedin.com/jobs/view/account-executive-at-acme-1",
        "canonical_source_url": "https://www.linkedin.com/jobs/view/account-executive-at-acme-1",
        "job_location": "Austin, Texas, United States",
        "job_country": "United States",
        "job_employment_type": "FULLTIME",
        "job_posted_at_datetime_utc": posted,
        "_acquisition_source": "ats_greenhouse",     # deliberately NOT Fantastic
        "_provider_record_structured": True,
        "_matched_role": "Account Executive",
    }
    job.update(overrides)
    return job


SIGNED = ("_work_arrangement", "_work_arrangement_reason", "_remote_scope",
          "_us_eligibility_reason", "_employment_quality", "_employment_quality_reason")


# --------------------------------------------------------------------------
# annotation itself
# --------------------------------------------------------------------------
def test_annotation_populates_every_signed_step2_field():
    job = _job()
    assert not job_filter.has_pre_enrichment_annotations(job)
    job_filter.annotate_pre_enrichment_assessment(job)
    for field in SIGNED:
        assert str(job.get(field) or ""), f"{field} not populated"
    assert job_filter.has_pre_enrichment_annotations(job)


def test_annotation_does_not_filter_anything():
    """A record Step 2 would REJECT must still be annotated, not dropped or
    marked -- this function forwards evidence, it does not gate."""
    job = _job(job_title="Part-Time Account Executive")
    result = job_filter.annotate_pre_enrichment_assessment(job)
    assert result is job
    assert job["_employment_quality"] == "non_full_time"
    assert "_filter_reason" not in job


def test_annotation_is_idempotent_and_does_not_re_derive():
    job = _job()
    job_filter.annotate_pre_enrichment_assessment(job)
    job["_employment_quality"] = "sentinel_preserved"
    job_filter.annotate_pre_enrichment_assessment(job)
    assert job["_employment_quality"] == "sentinel_preserved", (
        "an already-signed record must not be re-derived")


def test_run_filter_still_uses_the_same_annotation_shape():
    """Step 2 and the orchestrator path must not diverge."""
    from job_filter import assess_pre_enrichment_viability, pre_enrichment_annotations
    job = _job()
    assessment = assess_pre_enrichment_viability(job)
    keys = set(pre_enrichment_annotations(assessment))
    assert set(SIGNED) <= keys
    assert "_employer_domain_input" in keys and "_normalized_location" in keys


# --------------------------------------------------------------------------
# the evidence actually reaches the resolver prefilters
# --------------------------------------------------------------------------
def test_full_time_evidence_reaches_the_provider_structured_prefilter():
    job = _job()
    assert jsr._prefilter_full_time(job) is False, "precondition: unannotated fails"
    job_filter.annotate_pre_enrichment_assessment(job)
    assert jsr._prefilter_full_time(job) is True


def test_supported_us_work_evidence_reaches_the_prefilter():
    job = _job()
    assert jsr._prefilter_supported_us_work(job) is False
    job_filter.annotate_pre_enrichment_assessment(job)
    assert jsr._prefilter_supported_us_work(job) is True


def test_part_time_record_still_fails_the_prefilter():
    job = _job(job_title="Account Executive (Part-Time)")
    job_filter.annotate_pre_enrichment_assessment(job)
    assert jsr._prefilter_full_time(job) is False, "a rule was weakened"


def test_non_us_record_still_fails_the_prefilter():
    job = _job(job_location="Berlin, Germany", job_country="Germany",
               job_title="Account Executive - Germany")
    job_filter.annotate_pre_enrichment_assessment(job)
    assert jsr._prefilter_supported_us_work(job) is False, "a rule was weakened"


# --------------------------------------------------------------------------
# end to end through the real orchestrator entry point
# --------------------------------------------------------------------------
def _run_qualification(tmp_path, jobs):
    src = tmp_path / "postings.json"
    src.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    return run_precontact_qualification(str(src), output_dir=str(out), fetch_sources=False)


def test_orchestrator_path_annotates_every_job(tmp_path):
    result = _run_qualification(tmp_path, [_job()])
    payload = json.loads(Path(result.output_path).read_text(encoding="utf-8"))
    rows = payload["jobs"] + json.loads(
        Path(result.nonpass_path).read_text(encoding="utf-8"))["jobs"]
    assert rows, "expected at least one qualified or nonpass row"
    for row in rows:
        for field in SIGNED:
            assert str(row.get(field) or ""), f"{field} missing on the orchestrator path"


def test_orchestrator_path_makes_no_network_call(tmp_path):
    """The annotation must not introduce any request."""
    attempts = []
    real_conn, real_gai = socket.create_connection, socket.getaddrinfo

    def blocked(*a, **k):
        attempts.append(a[:1])
        raise AssertionError(f"unexpected outbound network call: {a[:1]}")

    socket.create_connection = blocked
    socket.getaddrinfo = blocked
    try:
        _run_qualification(tmp_path, [_job()])
    finally:
        socket.create_connection = real_conn
        socket.getaddrinfo = real_gai
    assert attempts == []


def test_provider_structured_fallback_now_reachable_offline(tmp_path):
    """The point of the fix: a fresh, full-time, US, substantial provider record
    reaches ACTIVE_PROVIDER_STRUCTURED instead of SOURCE_UNRESOLVED."""
    class _NoFetchResolver(jsr.JobSourceResolver):
        def _fetch(self, url, *a, **k):
            raise AssertionError("must not fetch when fetch=False")

    job = _job()
    resolver = _NoFetchResolver()
    before = resolver.resolve(dict(job), fetch=False)
    assert before.state == "SOURCE_UNRESOLVED"

    job_filter.annotate_pre_enrichment_assessment(job)
    after = resolver.resolve(dict(job), fetch=False)
    assert after.state == "ACTIVE_PROVIDER_STRUCTURED"
    assert after.corroborated is True
    assert after.official is False, "must NOT be promoted to an official source"
    assert "approved_revalidation_required" in after.notes, (
        "review-only policy must be preserved")


def test_fallback_still_refuses_a_part_time_record(tmp_path):
    job = _job(job_title="Account Executive (Part-Time)")
    job_filter.annotate_pre_enrichment_assessment(job)
    resolved = jsr.JobSourceResolver().resolve(dict(job), fetch=False)
    assert resolved.state != "ACTIVE_PROVIDER_STRUCTURED"


def test_fallback_still_refuses_a_thin_description():
    job = _job(job_description="Short posting.")
    job_filter.annotate_pre_enrichment_assessment(job)
    resolved = jsr.JobSourceResolver().resolve(dict(job), fetch=False)
    assert resolved.state != "ACTIVE_PROVIDER_STRUCTURED"


def test_fallback_still_refuses_a_stale_record():
    old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    job = _job(job_posted_at_datetime_utc=old)
    job_filter.annotate_pre_enrichment_assessment(job)
    resolved = jsr.JobSourceResolver().resolve(dict(job), fetch=False)
    assert resolved.state != "ACTIVE_PROVIDER_STRUCTURED"


@pytest.mark.parametrize("source", ["ats_greenhouse", "jsearch", "himalayas",
                                    "adzuna", "fantastic_jobs_linkedin"])
def test_behaviour_is_source_agnostic(source):
    """No Fantastic/LinkedIn-specific trust exception was introduced."""
    job = _job(_acquisition_source=source)
    job_filter.annotate_pre_enrichment_assessment(job)
    assert jsr._prefilter_full_time(job) is True
    assert jsr._prefilter_supported_us_work(job) is True


def test_no_trust_promotion_for_linkedin_urls():
    """A LinkedIn URL must not become an OFFICIAL source."""
    job = _job(_acquisition_source="fantastic_jobs_linkedin")
    job_filter.annotate_pre_enrichment_assessment(job)
    resolved = jsr.JobSourceResolver().resolve(dict(job), fetch=False)
    assert resolved.official is False
    assert resolved.source_type == "provider_structured"
