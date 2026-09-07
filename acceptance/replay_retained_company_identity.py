"""Read-only local replay; prints no contact details and opens no network socket.

Run with PYTHONPATH=. and a retained jobs_enriched JSON path. This reuses the
recorded non-display decisions, not live validation, billing or Airtable state.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile


def _offline(event, args):
    if event.startswith("socket."):
        raise RuntimeError("This artifact replay cannot use the network")


def replay(path: Path) -> dict:
    # Install before importing pipeline modules. No credential is used or needed.
    sys.addaudithook(_offline)
    import config
    from airtable_client import _job_to_fields, send_safe_facts
    from company_display_resolver import CompanyDisplayCache, resolve_company_display
    from decision_engine import annotate_final_decision
    from decision_types import gate_decision_from_dict
    from hiring_manager import _outbound_display_gate, company_key_for_job

    raw = path.read_bytes()
    data = json.loads(raw)
    jobs = data["jobs"]
    result = {
        "source_sha256": hashlib.sha256(raw).hexdigest(), "source_bytes": len(raw),
        "rows": len(jobs), "production_writes": 0, "network_requests": 0,
        "basis": "retained non-display gates plus current display resolver and mapper",
        "historical_signature_verified": False, "live_approval_proven": False,
        "reviewed": [], "shared_ats_rows": [],
    }
    # Explicit local test settings: current mapper creates new LOCAL signatures;
    # this is not evidence about the production signing key or effective flags.
    config.VALIDATION_SIGNING_KEY = "offline-retained-replay-not-production"
    config.FANTASTIC_AUTO_APPROVE_SEND_SAFE = True
    config.VALIDATION_VERSION = data["validation_version"]
    with tempfile.TemporaryDirectory() as tmp:
        cache = CompanyDisplayCache(Path(tmp) / "cache.json",
                                    overrides_path=Path(__file__).resolve().parents[1] / "company_display_overrides.json")
        for index, original in enumerate(jobs):
            if original.get("company_domain") in {"isolvedhire.com", "applicantpro.com"}:
                result["shared_ats_rows"].append({
                    "job_index": index, "employer": original.get("employer_name"),
                    "retained_company": original.get("canonical_company_name"),
                    "retained_domain": original.get("company_domain"),
                    "current_group_key": company_key_for_job(original),
                })
            if original.get("apollo_email_status") != "verified":
                continue
            before = send_safe_facts(_job_to_fields(original))
            lead = copy.deepcopy(original)
            decisions = {key: gate_decision_from_dict(value, gate=key)
                         for key, value in lead.get("_gate_decisions", {}).items()}
            if set(decisions) != {"job", "role", "account", "display", "contact", "email"}:
                raise ValueError("Missing original gates; refusing inferred replay")
            account = decisions["account"]
            if not account.metadata.get("canonical_domain"):
                raise ValueError("Original account domain absent; refusing to infer the org fallback input")
            display = resolve_company_display(
                organization=lead.get("organization") or lead.get("employer_name"),
                org_linkedin_name=lead.get("org_linkedin_name"),
                canonical_company_name=account.metadata.get("canonical_company_name"),
                org_linkedin_slug=lead.get("org_linkedin_slug"),
                org_linkedin_website=lead.get("org_linkedin_website"),
                employer_domain=account.metadata.get("canonical_domain"),
                canonical_identity_verified=account.state_value == "PASS",
                cache=cache, persist=False)
            lead.update(outbound_company_name=display.name, outbound_company_confidence=display.confidence,
                        outbound_company_identity_key=display.identity_key,
                        _outbound_company_hold=display.hold, _outbound_company_identity_safe=display.identity_safe,
                        _outbound_company_evidence=display.evidence,
                        _outbound_display_resolver_version=display.resolver_version)
            decisions["display"] = _outbound_display_gate(lead)
            lead = annotate_final_decision(lead, decisions)
            fields = _job_to_fields(lead)
            result["reviewed"].append({
                "job_index": index, "company": lead.get("organization"), "bucket": lead.get("_role_bucket"),
                "retained_company_evidence": original.get("_outbound_company_evidence"),
                "original_gate_states": {k: v.get("state") for k, v in original["_gate_decisions"].items()},
                "before_send_safe": list(before), "after_send_safe": list(send_safe_facts(fields)),
                "after_final_state": lead["_final_state"], "after_mapped_status": fields["Status"],
            })
    result["distinct_verified_contacts"] = len({
        str(j.get("hiring_manager_email") or "").strip().lower()
        for j in jobs if j.get("apollo_email_status") == "verified" and j.get("hiring_manager_email")})
    result["ats_distinct_retained_domains"] = len({j["retained_domain"] for j in result["shared_ats_rows"]})
    result["ats_distinct_current_group_keys"] = len({j["current_group_key"] for j in result["shared_ats_rows"]})
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    print(json.dumps(replay(args.source), ensure_ascii=False, indent=2))
