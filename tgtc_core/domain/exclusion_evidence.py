"""Conservative corroboration for model-proposed hard exclusions.

A model's reason string is never a business fact. Quotes must be grounded AND
describe a supported requirement. Unsupported exclusions remain insufficient
evidence, never silently compatible. No provider calls occur here.
"""
from __future__ import annotations

import re

from .facts import CLEARANCE, FACILITY_LIFT_INCIDENTAL, FIELD, LICENSE, TRAVEL_HARD, extract_job_facts

PATTERNS = {
    "physical_work": [
        r"\b(?:lift|lifting)\s+(?:up to\s+)?\d{2,3}\s*(?:lbs|pounds)\b",
        r"\b(?:clean|cleaning|sanitize|sanitizing)\b.{0,60}\b(?:rooms|floors|restrooms|facilities)\b",
        r"\b(?:patrol|patrolling)\b.{0,60}\b(?:site|premises|grounds|property)\b",
        r"\b(?:operate|operating|repair|repairing|assemble|assembling)\b.{0,50}\b(?:machinery|forklift|equipment)\b",
        r"\b(?:bedside|in-person)\b.{0,60}\b(?:registration|reception|check-in|patient care)\b",
    ],
    "clinical_care": [
        r"\b(?:provide|providing|deliver|delivering|perform|performing)\b.{0,60}\b(?:bedside|direct patient care|phlebotomy|injections|surgery)\b",
        r"\b(?:take|taking|measure|measuring)\b.{0,30}\bvital signs\b",
        r"\b(?:administer|administering|give|giving)\b.{0,30}\b(?:injections|medication|medications|vaccines)\b",
        r"\b(?:prepare|preparing|dispense|dispensing)\b.{0,30}\b(?:medications|prescriptions)\b",
    ],
    "field_work": FIELD,
    "security_clearance": CLEARANCE,
    "professional_license": LICENSE,
    "substantial_travel": TRAVEL_HARD + [r"\b(?:up to|at least|minimum)\s+(?:2[0-9]|[3-9]\d|100)%\s+travel\b"],
}


def corroborates(code: str, excerpt: str, description: str = "") -> bool:
    # Recover surrounding context: quoting just a substring of a negated duty or
    # a customer's work must not turn it into a requirement of the advertised job.
    normalized = " ".join(excerpt.lower().split())
    contexts = [s for s in re.split(r"(?<=[.!?;])\s+", description)
                if normalized in " ".join(s.lower().split())] or [excerpt]
    context = " ".join(contexts)
    if code in {"physical_work", "clinical_care"} and re.search(
        r"\b(?:our|the) (?:software|platform|product|customers?|clients?)\b", context, re.I
    ):
        return False
    # A quoted negation/preference is not proof of a mandatory restriction.
    if re.search(r"\b(?:not required|no .{0,65}required|without|optional|preferred|desirable|not responsible|no direct reports)\b", context, re.I):
        return False
    # Phase 2 audit fix round 1 (2026-09-19, CRITICAL): "people_management" used to
    # call has_people_authority(excerpt) directly, bypassing facts.exclusions
    # entirely -- so removing the standalone exclusion in facts.py (task 2) did not
    # stop the semantic/LLM fallback from still rejecting on it. Routing it through
    # the same extract_job_facts().exclusions check as employment/seniority/
    # inactive_posting shares the one predicate that matters (an exclusion no
    # longer exists to find) instead of duplicating a second copy of the rule that
    # can drift again.
    if code in {"employment", "seniority", "inactive_posting", "people_management"}:
        facts = extract_job_facts(title=None, description=excerpt)
        prefix = {"employment": "employment:", "seniority": "seniority:", "inactive_posting": "active:",
                  "people_management": "people_management"}[code]
        return any(e.reason.startswith(prefix) for e in facts.exclusions)
    if code == "physical_work":
        # Same task-3 narrowing as facts.FACILITY: a lift/lifting clause qualified
        # by ADA-boilerplate language (FACILITY_LIFT_INCIDENTAL, imported from
        # facts.py rather than copied) is not evidence of physical work unless
        # another physical_work span is present in the same excerpt.
        lift_pattern = PATTERNS["physical_work"][0]
        lift_hit = re.search(lift_pattern, excerpt, re.I)
        other_hit = any(re.search(p, excerpt, re.I) for p in PATTERNS["physical_work"][1:])
        if lift_hit and not other_hit and FACILITY_LIFT_INCIDENTAL.search(excerpt):
            return False
        return bool(lift_hit or other_hit)
    return any(re.search(p, excerpt, re.I) for p in PATTERNS.get(code, ()))
