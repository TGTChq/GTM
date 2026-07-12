"""Conservative role-fit classifier for JSearch results.

It rejects only clear mismatches, accepts strong matches, and sends ambiguous
results to human review instead of pretending a keyword search is perfect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Pattern


@dataclass(frozen=True)
class RoleAssessment:
    status: str  # accept / review / reject
    score: int
    reasons: List[str]


MAX_RELEVANCE_POINTS = 8


def normalize_relevance_score(points: int | float | None) -> int:
    """Convert the internal 0-8 evidence score into an Airtable-friendly 0-100 score.

    The classifier still makes accept/review/reject decisions from the raw points;
    this function only changes presentation so a value such as 7 is not mistaken
    for 7 percent.
    """
    try:
        value = float(points or 0)
    except (TypeError, ValueError):
        value = 0.0
    value = max(0.0, min(float(MAX_RELEVANCE_POINTS), value))
    return int((value * 100 / MAX_RELEVANCE_POINTS) + 0.5)


def _compile(items: Iterable[str]) -> List[Pattern[str]]:
    return [re.compile(item, re.I) for item in items]


RULES: Dict[str, Dict[str, List[Pattern[str]]]] = {
    "GTM Engineer": {
        "strong": _compile([
            r"\bgtm\s+engineer\b", r"\bgo[- ]to[- ]market\s+engineer\b",
            r"\brevops\s+engineer\b", r"\brevenue\s+(systems|operations)\s+engineer\b",
            r"\bgtm\s+(systems|automation)\b", r"\bsales\s+systems\s+engineer\b",
        ]),
        "context": _compile([
            r"\b(go[- ]to[- ]market|gtm|revops|revenue operations)\b",
            r"\b(salesforce|hubspot|clay|outreach|salesloft|gong|apollo)\b",
            r"\b(workflow|automation|systems integration|data orchestration)\b",
        ]),
        "negative": _compile([
            r"\bconstruction\b", r"\bmanufacturing\b", r"\bembedded systems\b",
            r"\bnetwork engineer\b", r"\bcivil engineer\b",
            # Reject roles where GTM is merely the business area, not the
            # engineering function (for example, "Data Engineer, GTM").
            r"\b(data|analytics|backend|frontend|full[- ]stack|platform|software)\s+engineer\b",
        ]),
    },
    "AI Engineer": {
        "strong": _compile([
            r"\bai engineer\b", r"\bartificial intelligence engineer\b",
            r"\bmachine learning engineer\b", r"\bml engineer\b",
            r"\bllm engineer\b", r"\bgenerative ai engineer\b", r"\bapplied ai\b",
        ]),
        "context": _compile([
            r"\b(llm|large language model|rag|retrieval augmented generation)\b",
            r"\b(openai|anthropic|langchain|llamaindex|pytorch|tensorflow)\b",
            r"\bmachine learning|generative ai|artificial intelligence\b",
        ]),
        "negative": _compile([
            r"\bai trainer\b", r"\bdata annotat(or|ion)\b", r"\bcontent evaluator\b",
            r"\bsearch quality rater\b",
        ]),
    },
    "Automation Specialist": {
        "strong": _compile([
            r"\bautomation specialist\b", r"\bworkflow automation\b",
            r"\bbusiness automation\b", r"\bprocess automation specialist\b",
            r"\bno[- ]code automation\b", r"\bai automation\b",
        ]),
        "context": _compile([
            r"\b(n8n|make\.com|make|zapier|workato|tray\.io)\b",
            r"\bworkflow|business process|low[- ]code|no[- ]code\b",
            r"\bapi integration|webhook|automation platform\b",
        ]),
        "negative": _compile([
            r"\bplc\b", r"\bscada\b", r"\bcontrols engineer\b", r"\bindustrial automation\b",
            r"\bmanufacturing automation\b", r"\binstrumentation\b", r"\brobotics\b",
            r"\btest automation\b", r"\bqa automation\b", r"\bselenium\b",
        ]),
    },
    "Graphic Designer": {
        "strong": _compile([
            r"\bgraphic designer\b", r"\bvisual designer\b", r"\bbrand designer\b",
            r"\bmarketing designer\b", r"\bdigital designer\b", r"\bproduction designer\b",
        ]),
        "context": _compile([
            r"\b(adobe creative suite|photoshop|illustrator|indesign|figma)\b",
            r"\bbrand identity|marketing collateral|social media graphics\b",
        ]),
        "negative": _compile([
            r"\barchitectural designer\b", r"\bmechanical designer\b", r"\bcad designer\b",
            r"\binterior designer\b",
        ]),
    },
    "Video Editor": {
        "strong": _compile([
            r"\bvideo editor\b", r"\bvideo producer/editor\b", r"\bmotion graphics editor\b",
            r"\bpost[- ]production editor\b",
        ]),
        "context": _compile([
            r"\b(premiere pro|after effects|davinci resolve|final cut)\b",
            r"\bvideo editing|post[- ]production|motion graphics\b",
        ]),
        "negative": _compile([
            r"\bmedical editor\b", r"\bcopy editor\b", r"\bnews editor\b",
        ]),
    },
    "Performance Marketing Manager": {
        "strong": _compile([
            r"\bperformance marketing\b", r"\bpaid media\b", r"\bpaid social\b",
            r"\bacquisition marketing\b", r"\bgrowth marketing manager\b",
            r"\bppc manager\b", r"\bsearch engine marketing\b",
        ]),
        "context": _compile([
            r"\b(meta ads|facebook ads|google ads|tiktok ads|paid acquisition)\b",
            r"\b(roas|cac|cpa|media buying|conversion rate)\b",
        ]),
        "negative": _compile([
            r"\bmarketing operations\b", r"\bproduct marketing\b", r"\bfield marketing\b",
        ]),
    },
    "Customer Support": {
        "strong": _compile([
            r"\bcustomer support\b", r"\bcustomer service\b", r"\bsupport specialist\b",
            r"\bsupport representative\b", r"\bsupport agent\b", r"\bmember support\b",
        ]),
        "context": _compile([
            r"\b(zendesk|intercom|freshdesk|gorgias)\b",
            r"\b(ticket|customer inquiry|support queue|service level)\b",
        ]),
        "negative": _compile([
            r"\bit support\b", r"\bdesktop support\b", r"\bhelp desk technician\b",
            r"\bfield support\b", r"\bclinical support\b", r"\btechnical support engineer\b",
        ]),
    },
    "Customer Success Manager": {
        "strong": _compile([
            r"\bcustomer success manager\b", r"\bcustomer success specialist\b",
            r"\bclient success manager\b", r"\bcustomer success associate\b",
            r"\bclient success specialist\b",
        ]),
        "context": _compile([
            r"\b(onboarding|retention|renewal|expansion|adoption|customer health)\b",
            r"\b(csm|book of business|qbr|customer lifecycle)\b",
        ]),
        "negative": _compile([
            r"\bcustomer success engineer\b", r"\bimplementation engineer\b",
            r"\btechnical account manager\b",
        ]),
    },
}


def _matches(patterns: List[Pattern[str]], text: str) -> List[str]:
    return [pattern.pattern for pattern in patterns if pattern.search(text)]


def assess_role(job: Dict, target_role: str) -> RoleAssessment:
    rules = RULES.get(target_role)
    if not rules:
        return RoleAssessment("review", 0, ["no_role_rules"])

    title = (job.get("job_title") or "").strip()
    description = (job.get("job_description") or "")[:12000]
    title_text = title.lower()
    full_text = f"{title}\n{description}".lower()

    strong_title = _matches(rules["strong"], title_text)
    strong_anywhere = _matches(rules["strong"], full_text)
    context = _matches(rules["context"], full_text)
    negative_title = _matches(rules["negative"], title_text)
    negative_anywhere = _matches(rules["negative"], full_text)

    score = 0
    reasons: List[str] = []

    if strong_title:
        score += 5
        reasons.append("strong_title_match")
    elif strong_anywhere:
        score += 2
        reasons.append("strong_description_match")

    if context:
        score += min(3, len(context))
        reasons.append(f"context_matches:{min(3, len(context))}")

    if negative_title:
        score -= 7
        reasons.append("negative_title_signal")
    elif negative_anywhere:
        score -= 3
        reasons.append("negative_description_signal")

    if negative_title and not strong_title:
        return RoleAssessment("reject", score, reasons)
    if score >= 5:
        return RoleAssessment("accept", score, reasons)
    if score >= 1:
        return RoleAssessment("review", score, reasons)
    return RoleAssessment("reject", score, reasons or ["insufficient_role_evidence"])
