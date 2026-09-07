"""The calibration merged unrelated tenants before contact discovery."""
import pytest

import config
from company_identity import safe_company_domain
from hiring_manager import _best_input_domain, company_key_for_job
from job_filter import get_safe_employer_domain
from job_signal import ATS_DOMAINS


@pytest.mark.parametrize("host", ["isolvedhire.com", "applicantpro.com"])
def test_distinct_employers_on_shared_ats_do_not_collapse(host):
    jobs = [dict(employer_name=name, organization=name,
                 employer_website=f"{tenant}.{host}",
                 _employer_domain_input=host,
                 job_apply_link=f"https://{tenant}.{host}/jobs/123",
                 job_apply_is_direct=True)
            for name, tenant in [("Acme Manufacturing", "acme"), ("Globex Labs", "globex")]]
    assert len({company_key_for_job(j) for j in jobs}) == 2
    for j in jobs:
        assert _best_input_domain(j) == ""
        assert get_safe_employer_domain(j)[0] == ""


def test_every_recognized_ats_is_excluded_from_employer_identity():
    for host in ATS_DOMAINS:
        assert safe_company_domain(f"tenant.{host}", config.INTERMEDIARY_JOB_DOMAINS) == "", host


def test_provider_domain_cannot_be_reintroduced_by_an_empty_local_denylist():
    assert safe_company_domain("isolvedhire.com", ()) == ""
    assert safe_company_domain("applicantpro.com", ()) == ""


def test_real_employer_domain_preserves_same_company_grouping():
    jobs = [dict(employer_name="Acme", employer_website="careers.acme.com"),
            dict(employer_name="Acme Inc.", employer_website="acme.com")]
    assert {company_key_for_job(j) for j in jobs} == {"acme.com"}
